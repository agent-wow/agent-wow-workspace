"""Non-destructive event consumption with epoch/dropped reconciliation.

Module ``poll_events`` returns ordered events plus ``next_cursor`` and
per-domain dropped counters. Polling never advances module state. The consumer
commits events, applied-event markers, cursors, and intent transitions in one
checkpoint transaction; the committed cursor is the consumption acknowledgement.

A crash between poll and commit leaves the old cursor in place, so the module
returns the same events again and the applied-event marker suppresses duplicate
application. Epoch changes and dropped counters request a full re-read of the
affected domain; the actual snapshot re-read is the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .store import CheckpointStore

Poller = Callable[[int | str, int, int], dict]


@dataclass(frozen=True)
class ReconcilePlan:
    epoch: int | str
    domain: str
    events: tuple[dict, ...]
    next_cursor: int
    dropped: int
    full_reread: frozenset[str] = field(default_factory=frozenset)
    epoch_changed: bool = False
    intents: tuple[tuple[str, str, str | None], ...] = field(default_factory=tuple)


class EventReconciler:
    def __init__(self, store: CheckpointStore):
        self.store = store

    def reconcile(
        self,
        domain: str,
        epoch: int | str,
        poller: Poller,
        *,
        max_events: int = 64,
        intents: Iterable[tuple[str, str, str | None]] = (),
        commit: bool = True,
    ) -> tuple[ReconcilePlan, int]:
        state = self.store.cursor(domain)
        epoch_changed = str(state["epoch"]) != str(epoch)
        after = 0 if epoch_changed else int(state["cursor"])

        batch = poller(epoch, after, max_events) or {}
        batch_epoch = batch.get("epoch", epoch)

        full_reread: set[str] = set()
        if epoch_changed or str(batch_epoch) != str(epoch):
            epoch_changed = True
            full_reread.update(self.store.cursors())
            full_reread.add(domain)

        events = tuple(batch.get("events") or ())
        next_cursor = int(batch.get("next_cursor", after))
        dropped_map = batch.get("dropped") or {}
        dropped = int(dropped_map.get(domain) or 0)
        if dropped > int(state.get("dropped") or 0):
            full_reread.add(domain)

        plan = ReconcilePlan(
            epoch=batch_epoch,
            domain=domain,
            events=events,
            next_cursor=next_cursor,
            dropped=dropped,
            full_reread=frozenset(full_reread),
            epoch_changed=epoch_changed,
            intents=tuple(intents),
        )
        applied = self.commit(plan) if commit else 0
        return plan, applied

    def commit(
        self,
        plan: ReconcilePlan,
        *,
        intents: Iterable[tuple[str, str, str | None]] | None = None,
    ) -> int:
        chosen = plan.intents if intents is None else tuple(intents)
        return self.store.commit_poll(
            epoch=plan.epoch,
            events=plan.events,
            cursors={plan.domain: plan.next_cursor},
            dropped={plan.domain: plan.dropped},
            intents=chosen,
        )
