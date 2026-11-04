"""Operator-owned STOP handling.

The STOP file is created and removed only by the human operator. Growth code
has no API that clears it: ``clear_stop`` requires the explicit ``operator``
actor and every worker/supervisor path simply observes the file.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class StopRequested(RuntimeError):
    """The operator-owned STOP file is present."""


class StopClearDenied(PermissionError):
    """Only the operator may clear STOP."""


def default_stop_path(root: str | os.PathLike[str] | None = None) -> Path:
    env = os.environ.get("GROWTH_STOP_FILE")
    if env:
        return Path(env)
    base = Path(root) if root is not None else ROOT / "data" / "growth"
    return base / "control" / "STOP"


class StopFlag:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else default_stop_path()

    def is_set(self) -> bool:
        return self.path.exists()

    def ensure_clear(self, action: str = "continue") -> None:
        if self.is_set():
            raise StopRequested(
                f"operator STOP file present ({self.path}); refusing to {action}")


def clear_stop(path: str | os.PathLike[str], *, actor: str) -> None:
    """Remove STOP. Only the operator identity may do this."""
    if actor != "operator":
        raise StopClearDenied(
            f"actor {actor!r} may not clear the operator STOP file")
    target = Path(path)
    if target.exists():
        target.unlink()
