"""Checkpoint-backed execution for actions whose RPC reply can be lost.

A stable intent is dispatched at most once. ``sent``/``unknown`` intents are
resolved only by a caller-supplied server read-back: True means applied, False
means definitively not applied, and None keeps the action ambiguous. Ambiguous
actions expire as abandoned; the executor never turns a timeout into a retry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .store import CheckpointStore


@dataclass(frozen=True)
class ActionResult:
    state: str
    dispatched: bool = False


class DurableActionExecutor:
    def __init__(
        self, store: CheckpointStore, *, clock: Callable[[], float] = time.time,
        unknown_timeout: float = 120.0,
    ):
        self.store = store
        self.clock = clock
        self.unknown_timeout = float(unknown_timeout)

    def run(
        self,
        intent_key: str,
        kind: str,
        payload: dict,
        dispatch: Callable[[], object],
        reconcile: Callable[[], bool | None],
    ) -> ActionResult:
        state = self.store.begin_intent(intent_key, kind, payload)
        if state in ("confirmed", "failed", "abandoned"):
            return ActionResult(state)

        dispatched = False
        if state == "planned":
            self.store.begin_send(intent_key)
            dispatched = True
            try:
                dispatch()
            except Exception as exc:
                self.store.mark_unknown(intent_key, str(exc))
                return ActionResult("unknown", dispatched=True)
            self.store.mark_sent(intent_key)
            state = "sent"

        try:
            outcome = reconcile()
        except Exception:
            outcome = None
        if outcome is True:
            if state == "unknown":
                self.store.resolve_unknown(intent_key, "confirmed")
            elif state == "sending":
                self.store.mark_unknown(intent_key, "confirmed by read-back")
                self.store.resolve_unknown(intent_key, "confirmed")
            else:
                self.store.mark_confirmed(intent_key)
            return ActionResult("confirmed", dispatched)
        if outcome is False:
            if state == "unknown":
                self.store.resolve_unknown(intent_key, "failed")
            else:
                self.store.mark_failed(intent_key, "server read-back shows no effect")
            return ActionResult("failed", dispatched)

        if state != "unknown":
            self.store.mark_unknown(intent_key, "awaiting server read-back")
        intent = self.store.intent(intent_key) or {}
        if (self.clock() - float(intent.get("created") or self.clock())
                > self.unknown_timeout):
            self.store.mark_abandoned(intent_key, "read-back deadline exceeded")
            return ActionResult("abandoned", dispatched)
        return ActionResult("unknown", dispatched)
