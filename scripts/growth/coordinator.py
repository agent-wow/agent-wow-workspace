"""Independent coordinator/validator store boundary.

Only the coordinator or validator identity may open the shared store. Growth
workers submit bounded requests through ``CoordinatorClient`` and keep running
solo when the coordinator is unavailable. Local candidates and applied strategy
pointers stay in each character's checkpoint store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .lock import ExclusiveLock, LockError
from .permissions import ensure_private_dir

COORDINATOR_ROLES = ("coordinator", "validator")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    participants TEXT NOT NULL,
    state TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempt_id TEXT,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    account TEXT NOT NULL,
    character TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (account, character)
);
CREATE TABLE IF NOT EXISTS leases (
    resource TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS promoted_strategies (
    strategy_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    promoted_at REAL NOT NULL
);
"""


class CoordinatorError(RuntimeError):
    pass


class CoordinatorUnavailable(CoordinatorError):
    """The independent coordinator is not reachable; solo runtime continues."""


class CoordinatorStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        role: str,
        timeout: float = 5.0,
    ):
        if role not in COORDINATOR_ROLES:
            raise PermissionError(
                f"role {role!r} may not open the coordinator store; "
                f"allowed roles: {COORDINATOR_ROLES}")
        self.path = Path(path)
        self.role = role
        self.timeout = timeout
        self._lock = ExclusiveLock(
            self.path.with_name(self.path.name + ".coordinator.lock"))
        self._conn: sqlite3.Connection | None = None

    def open(self) -> "CoordinatorStore":
        if self._conn is not None:
            return self
        ensure_private_dir(self.path.parent)
        try:
            self._lock.acquire({
                "kind": "coordinator-store", "role": self.role,
                "path": str(self.path)})
        except LockError as exc:
            raise CoordinatorUnavailable(str(exc)) from exc
        conn = sqlite3.connect(
            str(self.path), isolation_level=None, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        conn.executescript(SCHEMA)
        self._conn = conn
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        self._lock.release()

    def __enter__(self) -> "CoordinatorStore":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise CoordinatorUnavailable("coordinator store is closed")
        return self._conn

    @contextmanager
    def _tx(self):
        conn = self._require_open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ----------------------------------------------------------- registries

    def register_worker(
        self, account: str, character: str, worker_id: str, *,
        epoch: int, status: str = "alive",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO workers(account,character,worker_id,epoch,status,updated) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(account,character) DO UPDATE SET "
                "worker_id=excluded.worker_id, epoch=excluded.epoch, "
                "status=excluded.status, updated=excluded.updated",
                (account, character, worker_id, int(epoch), status, time.time()))

    def workers(self) -> list[dict]:
        rows = self._require_open().execute(
            "SELECT * FROM workers ORDER BY account, character").fetchall()
        return [dict(row) for row in rows]

    def upsert_agreement(
        self, agreement_id: str, *, scope: str, participants: list[str],
        state: str, nonce: str, expires_at: float, attempt_id: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO agreements(agreement_id,scope,participants,state,"
                "nonce,expires_at,attempt_id,updated) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(agreement_id) DO UPDATE SET scope=excluded.scope, "
                "participants=excluded.participants, state=excluded.state, "
                "nonce=excluded.nonce, expires_at=excluded.expires_at, "
                "attempt_id=excluded.attempt_id, updated=excluded.updated",
                (agreement_id, scope, json.dumps(participants), state, nonce,
                 float(expires_at), attempt_id, time.time()))

    def agreement(self, agreement_id: str) -> dict | None:
        row = self._require_open().execute(
            "SELECT * FROM agreements WHERE agreement_id=?",
            (agreement_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["participants"] = json.loads(data["participants"])
        return data

    # ---------------------------------------------------------------- leases

    def acquire_lease(
        self, resource: str, owner: str, *, ttl: float, fencing_token: int,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        with self._tx() as conn:
            row = conn.execute(
                "SELECT owner,fencing_token,expires_at FROM leases WHERE resource=?",
                (resource,)).fetchone()
            if row is not None and float(row["expires_at"]) > now:
                if row["owner"] != owner:
                    return False
                if int(fencing_token) < int(row["fencing_token"]):
                    return False
            conn.execute(
                "INSERT INTO leases(resource,owner,fencing_token,expires_at,updated) "
                "VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET "
                "owner=excluded.owner, fencing_token=excluded.fencing_token, "
                "expires_at=excluded.expires_at, updated=excluded.updated",
                (resource, owner, int(fencing_token), now + float(ttl), now))
            return True

    def active_lease(self, resource: str, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        row = self._require_open().execute(
            "SELECT * FROM leases WHERE resource=? AND expires_at > ?",
            (resource, now)).fetchone()
        return dict(row) if row else None

    def release_lease(self, resource: str, owner: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM leases WHERE resource=? AND owner=?",
                (resource, owner))

    # -------------------------------------------------------------- strategy

    def promote_strategy(
        self, strategy_id: str, scope: str, version: int, payload: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO promoted_strategies(strategy_id,scope,version,"
                "payload,promoted_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET scope=excluded.scope, "
                "version=excluded.version, payload=excluded.payload, "
                "promoted_at=excluded.promoted_at",
                (strategy_id, scope, int(version),
                 json.dumps(payload, sort_keys=True), time.time()))

    def promoted(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._require_open().execute(
                "SELECT * FROM promoted_strategies ORDER BY scope, version"
            ).fetchall()
        else:
            rows = self._require_open().execute(
                "SELECT * FROM promoted_strategies WHERE scope=? "
                "ORDER BY version", (scope,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            try:
                data["payload"] = json.loads(data["payload"])
            except (TypeError, json.JSONDecodeError):
                pass
            out.append(data)
        return out


class CoordinatorClient:
    """Worker-side handle. No worker ever opens the coordinator DB directly."""

    def __init__(self, handler=None, *, queue_size: int = 32):
        self._handler = handler
        self.queue_size = queue_size

    def submit(self, request: dict):
        if self._handler is None:
            raise CoordinatorUnavailable("no coordinator configured")
        try:
            return self._handler(request)
        except CoordinatorError:
            raise
        except Exception as exc:  # coordination must never kill solo runtime
            raise CoordinatorUnavailable(
                f"coordinator request failed: {type(exc).__name__}: {exc}") from exc
