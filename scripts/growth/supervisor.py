"""Minimal growth supervisor.

The supervisor keeps session-keeper.py and the roster workers alive. It never
starts or stops a game session: that remains session-keeper.py's sole job. It
honours the operator STOP file before any restart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .roster import CharacterSpec, Roster
from .stop import StopFlag


@dataclass(frozen=True)
class SupervisorPlan:
    blocked: bool = False
    reason: str = ""
    start_keeper: bool = False
    start_workers: list[CharacterSpec] = field(default_factory=list)


class GrowthSupervisor:
    def __init__(
        self,
        roster: Roster,
        *,
        stop_flag: StopFlag | None = None,
        is_keeper_alive: Callable[[], bool],
        is_worker_alive: Callable[[CharacterSpec], bool],
        spawn: Callable[..., bool],
        logger: Callable[[str], None] | None = None,
    ):
        self.roster = roster
        self.stop_flag = stop_flag or StopFlag()
        self.is_keeper_alive = is_keeper_alive
        self.is_worker_alive = is_worker_alive
        self.spawn = spawn
        self.log = logger or (lambda message: None)

    def plan(self) -> SupervisorPlan:
        if self.stop_flag.is_set():
            return SupervisorPlan(
                blocked=True,
                reason="operator STOP file present; no restarts",
            )
        start_workers = [
            spec for spec in self.roster.characters
            if not self.is_worker_alive(spec)
        ]
        return SupervisorPlan(
            blocked=False,
            start_keeper=not self.is_keeper_alive(),
            start_workers=start_workers,
        )

    def run_once(self) -> SupervisorPlan:
        plan = self.plan()
        if plan.blocked:
            self.log(plan.reason)
            return plan
        if plan.start_keeper:
            self.spawn("keeper")
        for spec in plan.start_workers:
            self.spawn("worker", spec)
        return plan
