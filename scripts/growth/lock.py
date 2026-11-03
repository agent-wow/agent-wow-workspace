"""Small exclusive OS file lock used by workers and single-writer stores."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path


class LockError(RuntimeError):
    """Another process (or another open description) holds the lock."""


class ExclusiveLock:
    """An advisory ``flock`` on ``path`` with JSON metadata in the file.

    The lock file is created 0600. It is intentionally left on disk after
    release: removing a lock file while another process is about to open it
    reintroduces the classic unlink/lock race.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._handle = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, metadata: dict | None = None) -> "ExclusiveLock":
        if self._held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockError(f"lock already held: {self.path}") from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._handle = handle
        self._held = True
        self.update_metadata(metadata or {})
        return self

    def update_metadata(self, metadata: dict) -> None:
        if not self._held or self._handle is None:
            raise LockError("lock is not held")
        self._handle.seek(0)
        self._handle.truncate()
        json.dump(metadata, self._handle, ensure_ascii=False, sort_keys=True)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def release(self) -> None:
        if not self._held or self._handle is None:
            return
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._held = False

    def __enter__(self) -> "ExclusiveLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
