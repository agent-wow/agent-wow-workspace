"""Minimal per-character worker lifecycle.

This unit deliberately contains no gameplay strategy. It establishes the
boundaries U2 extends: OS single-writer lock, fencing token, STOP handling,
startup intent recovery, and the epoch identity gate.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .coordinator import CoordinatorClient, CoordinatorError
from .identity import EpochIdentityGate
from .lock import ExclusiveLock, LockError
from .roster import CharacterSpec
from .stop import StopFlag, StopRequested
from .store import CheckpointStore

__all__ = ["GrowthWorker", "StopRequested", "TickResult", "WorkerLocked"]


class WorkerLocked(RuntimeError):
    """Another worker already owns this character's OS lock."""


@dataclass(frozen=True)
class TickResult:
    epoch: int | str
    coordinator_error: str | None = None


class GrowthWorker:
    def __init__(
        self,
        spec: CharacterSpec,
        store: CheckpointStore,
        *,
        lock_path: str | os.PathLike[str],
        stop_flag: StopFlag | None = None,
        gate: EpochIdentityGate | None = None,
        coordinator: CoordinatorClient | None = None,
        epoch: int | str = 0,
        clock: Callable[[], float] = time.time,
        logger: Callable[[str], None] | None = None,
    ):
        self.spec = spec
        self.store = store
        self.lock_path = Path(lock_path)
        self.stop_flag = stop_flag or StopFlag()
        self.gate = gate
        self.coordinator = coordinator
        self.epoch = epoch
        self.clock = clock
        self.log = logger or (lambda message: None)
        self._lock = ExclusiveLock(self.lock_path)
        self._started = False
        self._fencing_token = 0

    @property
    def started(self) -> bool:
        return self._started

    @property
    def fencing_token(self) -> int:
        return self._fencing_token

    def start(self) -> int:
        self.stop_flag.ensure_clear("start a worker")
        try:
            self._lock.acquire({
                "kind": "growth-worker",
                "account": self.spec.account,
                "character": self.spec.name,
                "pid": os.getpid(),
                "started": self.clock(),
            })
        except LockError as exc:
            raise WorkerLocked(str(exc)) from exc
        try:
            self._fencing_token = self.store.bump_fencing_token(
                f"{self.spec.account}/{self.spec.name}/{os.getpid()}")
            self._lock.update_metadata({
                "kind": "growth-worker",
                "account": self.spec.account,
                "character": self.spec.name,
                "pid": os.getpid(),
                "started": self.clock(),
                "fencing_token": self._fencing_token,
            })
            recovered = self.store.recover_inflight()
            if recovered:
                self.log(
                    f"{self.spec.account}/{self.spec.name}: "
                    f"{recovered} in-flight intent(s) moved to unknown")
        except BaseException:
            self._lock.release()
            raise
        self._started = True
        return self._fencing_token

    def set_epoch(self, epoch: int | str) -> None:
        self.epoch = epoch

    def tick(self) -> TickResult:
        self.stop_flag.ensure_clear("continue a worker tick")
        if not self._started:
            raise RuntimeError("worker is not started")
        if self.gate is not None and self.epoch:
            self.gate.ensure(self.epoch)

        # U1 only records liveness. U2 will attach event consumption and the
        # single-character growth loop here, after the identity gate.
        self.store.set_meta("last_tick", str(self.clock()))

        coordinator_error = None
        if self.coordinator is not None:
            try:
                self.coordinator.submit({
                    "op": "heartbeat",
                    "account": self.spec.account,
                    "character": self.spec.name,
                    "epoch": self.epoch,
                    "fencing_token": self._fencing_token,
                })
            except CoordinatorError as exc:
                coordinator_error = str(exc)
                self.log(
                    f"{self.spec.account}/{self.spec.name}: "
                    f"coordinator unavailable, continuing solo: {exc}")
        return TickResult(epoch=self.epoch, coordinator_error=coordinator_error)

    def stop(self) -> None:
        self._started = False
        self._lock.release()

    def run(
        self,
        *,
        max_ticks: int | None = None,
        interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            self.tick()
            ticks += 1
            if max_ticks is None or ticks < max_ticks:
                sleep(interval)
        return ticks

    def __enter__(self) -> "GrowthWorker":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
