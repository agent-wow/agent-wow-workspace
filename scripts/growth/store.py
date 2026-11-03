"""Per-character checkpoint SQLite store (single writer, WAL, short transactions).

Recovery contract:

- ``sending`` is committed *before* an RPC is dispatched.
- On startup every ``sending``/``sent`` intent becomes ``unknown``. It may only
  move to a final state after a server read-back.
- Events, applied-event markers, cursors, and intent transitions are committed
  in one ``BEGIN IMMEDIATE`` transaction. A crash before ``COMMIT`` rolls all of
  them back, so the old cursor is re-read without duplicate application.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .lock import ExclusiveLock, LockError
from .permissions import ensure_private_dir

METRICS_KEY = "growth_metrics"

INTENT_STATES = (
    "planned", "sending", "sent", "confirmed", "failed", "unknown", "abandoned",
)
TRANSITIONS = {
    "planned": {"sending", "abandoned"},
    "sending": {"sent", "failed", "unknown", "abandoned"},
    "sent": {"confirmed", "failed", "unknown", "abandoned"},
    "unknown": {"confirmed", "failed", "abandoned"},
    "confirmed": set(),
    "failed": set(),
    "abandoned": set(),
}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    intent_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{{}}',
    state TEXT NOT NULL CHECK (state IN ({",".join(repr(s) for s in INTENT_STATES)})),
    attempts INTEGER NOT NULL DEFAULT 0,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    epoch TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (epoch, domain, event_id)
);
CREATE TABLE IF NOT EXISTS applied_events (
    event_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    epoch TEXT NOT NULL,
    applied_at REAL NOT NULL,
    PRIMARY KEY (epoch, domain, event_id)
);
CREATE TABLE IF NOT EXISTS cursor (
    domain TEXT PRIMARY KEY,
    epoch TEXT NOT NULL DEFAULT '',
    cursor INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS local_strategy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    candidate TEXT,
    applied TEXT,
    updated REAL
);
CREATE TABLE IF NOT EXISTS fencing (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token INTEGER NOT NULL DEFAULT 0,
    holder TEXT,
    updated REAL NOT NULL
);
"""


class StoreError(RuntimeError):
    """A checkpoint store operation failed."""


class StoreLocked(StoreError):
    """Another writer already owns this character checkpoint."""


class InvalidTransition(StoreError):
    """The requested intent state transition is not allowed."""


class CheckpointStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lock_path: str | os.PathLike[str] | None = None,
        timeout: float = 5.0,
    ):
        self.path = Path(path)
        self.lock_path = (
            Path(lock_path) if lock_path is not None
            else self.path.with_name(self.path.name + ".writer.lock")
        )
        self.timeout = timeout
        self._conn: sqlite3.Connection | None = None
        self._lock = ExclusiveLock(self.lock_path)

    # ------------------------------------------------------------- lifecycle

    def open(self) -> "CheckpointStore":
        if self._conn is not None:
            return self
        ensure_private_dir(self.path.parent)
        try:
            self._lock.acquire({"kind": "checkpoint-store", "path": str(self.path)})
        except LockError as exc:
            raise StoreLocked(str(exc)) from exc
        conn = sqlite3.connect(
            str(self.path), isolation_level=None, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
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

    @property
    def connection(self) -> sqlite3.Connection:
        return self._require_open()

    def __enter__(self) -> "CheckpointStore":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("checkpoint store is closed")
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._require_open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------ meta/fencing

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._require_open().execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    def bump_fencing_token(self, holder: str) -> int:
        with self._tx() as conn:
            self._ensure_fencing_row(conn)
            conn.execute(
                "UPDATE fencing SET token=token+1, holder=?, updated=? WHERE id=1",
                (holder, time.time()))
            token = conn.execute(
                "SELECT token FROM fencing WHERE id=1").fetchone()["token"]
        return int(token)

    def fencing_token(self) -> int:
        row = self._require_open().execute(
            "SELECT token FROM fencing WHERE id=1").fetchone()
        return int(row["token"]) if row else 0

    def require_fencing(self, token: int) -> None:
        current = self.fencing_token()
        if int(token) != current:
            raise StoreError(
                f"stale fencing token {token}; current token is {current}")

    @staticmethod
    def _ensure_fencing_row(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO fencing(id,token,holder,updated) "
            "VALUES(1,0,NULL,?)", (time.time(),))

    # --------------------------------------------------------------- intents

    def begin_intent(
        self, intent_key: str, kind: str, payload: dict | None = None) -> str:
        """Insert a planned intent with a stable unique key; idempotent."""
        now = time.time()
        encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT kind,payload,state FROM intents WHERE intent_key=?",
                (intent_key,)).fetchone()
            if existing:
                if existing["kind"] != kind or existing["payload"] != encoded:
                    raise StoreError(
                        f"intent key {intent_key!r} already exists with "
                        "different kind or payload")
                return str(existing["state"])
            conn.execute(
                "INSERT INTO intents(intent_key,kind,payload,state,attempts,"
                "fencing_token,created,updated,error) "
                "VALUES(?,?,?,'planned',0,?,?,?,NULL)",
                (intent_key, kind, encoded, self.fencing_token(), now, now))
        return "planned"

    def begin_send(self, intent_key: str) -> None:
        """Durably move planned -> sending before the caller dispatches."""
        with self._tx() as conn:
            self._transition_locked(conn, intent_key, "sending", None)
            conn.execute(
                "UPDATE intents SET attempts=attempts+1 WHERE intent_key=?",
                (intent_key,))
            conn.execute(
                "UPDATE intents SET fencing_token=? WHERE intent_key=?",
                (self.fencing_token(), intent_key))

    def mark_sent(self, intent_key: str) -> None:
        self.transition(intent_key, "sent")

    def mark_confirmed(self, intent_key: str) -> None:
        self.transition(intent_key, "confirmed")

    def mark_failed(self, intent_key: str, error: str | None = None) -> None:
        self.transition(intent_key, "failed", error)

    def mark_unknown(self, intent_key: str, error: str | None = None) -> None:
        self.transition(intent_key, "unknown", error)

    def mark_abandoned(self, intent_key: str, error: str | None = None) -> None:
        self.transition(intent_key, "abandoned", error)

    def resolve_unknown(self, intent_key: str, final_state: str) -> None:
        if final_state not in ("confirmed", "failed", "abandoned"):
            raise InvalidTransition(
                f"unknown can only resolve to confirmed/failed/abandoned, "
                f"not {final_state!r}")
        self.transition(intent_key, final_state)

    def transition(
        self, intent_key: str, state: str, error: str | None = None) -> None:
        with self._tx() as conn:
            self._transition_locked(conn, intent_key, state, error)

    def _transition_locked(
        self, conn: sqlite3.Connection, intent_key: str, state: str,
        error: str | None,
    ) -> None:
        if state not in INTENT_STATES:
            raise InvalidTransition(f"unknown intent state {state!r}")
        row = conn.execute(
            "SELECT state FROM intents WHERE intent_key=?",
            (intent_key,)).fetchone()
        if row is None:
            raise StoreError(f"intent not found: {intent_key}")
        current = str(row["state"])
        if state not in TRANSITIONS[current]:
            raise InvalidTransition(
                f"intent {intent_key}: {current} -> {state} is not allowed")
        conn.execute(
            "UPDATE intents SET state=?, error=?, updated=?, fencing_token=? "
            "WHERE intent_key=?",
            (state, error, time.time(), self.fencing_token(), intent_key))

    def recover_inflight(self) -> int:
        """Startup recovery: sending/sent -> unknown (needs server read-back)."""
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE intents SET state='unknown', error=?, updated=? "
                "WHERE state IN ('sending','sent')",
                ("recovered after restart", time.time()))
            return int(cursor.rowcount or 0)

    def intent(self, intent_key: str) -> dict | None:
        row = self._require_open().execute(
            "SELECT * FROM intents WHERE intent_key=?", (intent_key,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(data["payload"])
        except (TypeError, json.JSONDecodeError):
            pass
        return data

    def intents(self, state: str | None = None) -> list[dict]:
        if state is None:
            rows = self._require_open().execute(
                "SELECT * FROM intents ORDER BY created, intent_key").fetchall()
        else:
            rows = self._require_open().execute(
                "SELECT * FROM intents WHERE state=? ORDER BY created, intent_key",
                (state,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            try:
                data["payload"] = json.loads(data["payload"])
            except (TypeError, json.JSONDecodeError):
                pass
            out.append(data)
        return out

    # ---------------------------------------------------------------- events

    def commit_poll(
        self,
        *,
        epoch: int | str,
        events: Iterable[dict] = (),
        cursors: dict[str, int] | None = None,
        intents: Iterable[tuple[str, str, str | None]] = (),
        dropped: dict[str, int] | None = None,
    ) -> int:
        """Atomically apply events + applied markers + cursors + intents."""
        cursors = cursors or {}
        dropped = dropped or {}
        applied = 0
        with self._tx() as conn:
            stored_epoch = self._meta_locked(conn, "epoch", "")
            if str(epoch) != stored_epoch:
                conn.execute("UPDATE cursor SET epoch=?, cursor=0", (str(epoch),))
                conn.execute(
                    "UPDATE intents SET state='unknown', error=?, updated=? "
                    "WHERE state IN ('sending','sent')",
                    ("session epoch changed", time.time()))
                self._set_meta_locked(conn, "epoch", str(epoch))

            for raw in events:
                event_id = str(raw["id"])
                domain = str(raw.get("domain") or "")
                seq = int(raw.get("seq") or 0)
                payload = json.dumps(
                    raw.get("payload"), ensure_ascii=False, sort_keys=True)
                conn.execute(
                    "INSERT OR IGNORE INTO events(event_id,domain,epoch,seq,payload,created) "
                    "VALUES(?,?,?,?,?,?)",
                    (event_id, domain, str(epoch), seq, payload, time.time()))
                marker = conn.execute(
                    "INSERT OR IGNORE INTO applied_events(event_id,domain,epoch,applied_at) "
                    "VALUES(?,?,?,?)",
                    (event_id, domain, str(epoch), time.time()))
                if marker.rowcount:
                    applied += 1

            for domain, cursor in cursors.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cursor(domain,epoch,cursor,dropped) "
                    "VALUES(?,?,0,0)", (domain, str(epoch)))
                conn.execute(
                    "UPDATE cursor SET epoch=?, cursor=? WHERE domain=?",
                    (str(epoch), int(cursor), domain))

            for domain, count in dropped.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cursor(domain,epoch,cursor,dropped) "
                    "VALUES(?,?,0,0)", (domain, str(epoch)))
                conn.execute(
                    "UPDATE cursor SET dropped=? WHERE domain=?",
                    (int(count), domain))

            for intent_key, state, error in intents:
                self._transition_locked(conn, intent_key, state, error)
        return applied

    def cursor(self, domain: str) -> dict:
        row = self._require_open().execute(
            "SELECT epoch,cursor,dropped FROM cursor WHERE domain=?",
            (domain,)).fetchone()
        if row is None:
            return {
                "epoch": self.get_meta("epoch", "") or "",
                "cursor": 0,
                "dropped": 0,
            }
        return {
            "epoch": str(row["epoch"]),
            "cursor": int(row["cursor"]),
            "dropped": int(row["dropped"]),
        }

    def cursors(self) -> dict[str, dict]:
        rows = self._require_open().execute(
            "SELECT domain,epoch,cursor,dropped FROM cursor").fetchall()
        return {
            str(row["domain"]): {
                "epoch": str(row["epoch"]),
                "cursor": int(row["cursor"]),
                "dropped": int(row["dropped"]),
            }
            for row in rows
        }

    def events(self, domain: str) -> list[dict]:
        """Replay one event domain in deterministic order after a restart."""
        rows = self._require_open().execute(
            "SELECT event_id,epoch,seq,payload FROM events WHERE domain=? "
            "ORDER BY created,seq,event_id", (domain,)).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            out.append({
                "id": str(row["event_id"]), "epoch": str(row["epoch"]),
                "seq": int(row["seq"]), "payload": payload,
            })
        return out

    def event_count(self) -> int:
        return int(self._require_open().execute(
            "SELECT COUNT(*) AS n FROM events").fetchone()["n"])

    def applied_event_ids(self) -> set[str]:
        rows = self._require_open().execute(
            "SELECT event_id FROM applied_events").fetchall()
        return {str(row["event_id"]) for row in rows}

    @staticmethod
    def _meta_locked(conn: sqlite3.Connection, key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @staticmethod
    def _set_meta_locked(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -------------------------------------------------------- local strategy

    def set_local_strategy(
        self, *, candidate: str | None = None, applied: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO local_strategy(id,candidate,applied,updated) "
                "VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "candidate=excluded.candidate, applied=excluded.applied, "
                "updated=excluded.updated",
                (candidate, applied, time.time()))

    def local_strategy(self) -> dict:
        row = self._require_open().execute(
            "SELECT candidate,applied,updated FROM local_strategy WHERE id=1"
        ).fetchone()
        if row is None:
            return {"candidate": None, "applied": None, "updated": None}
        return dict(row)

    # --------------------------------------------------------------- metrics

    def save_metrics(self, document: dict) -> None:
        """Persist the sampled growth metrics inside the checkpoint store."""
        if not isinstance(document, dict):
            raise StoreError("metrics document must be a mapping")
        self.set_meta(
            METRICS_KEY,
            json.dumps(document, ensure_ascii=False, sort_keys=True))

    def load_metrics(self) -> dict | None:
        raw = self.get_meta(METRICS_KEY)
        if raw is None:
            return None
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    # ---------------------------------------------------------------- backup

    def backup_to(self, dest_path: str | os.PathLike[str]) -> Path:
        """Consistent SQLite backup (never a raw copy of WAL files)."""
        dest = Path(dest_path)
        ensure_private_dir(dest.parent)
        target = sqlite3.connect(str(dest))
        try:
            self._require_open().backup(target)
        finally:
            target.close()
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        return dest
