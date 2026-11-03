"""Consistent SQLite backup/restore for growth runtime state.

Uses the SQLite backup API, so an open WAL database is copied consistently and
raw ``-wal``/``-shm`` files are never shipped. The operator-owned ``control/``
directory (including STOP) is not part of a backup.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .permissions import ensure_private_dir, secure_write

INCLUDE_DIRS = ("state", "coordinator")
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _database_files(source: Path) -> list[Path]:
    found: list[Path] = []
    if not source.exists():
        return found
    for name in INCLUDE_DIRS:
        base = source / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in DB_SUFFIXES:
                found.append(path)
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix in DB_SUFFIXES:
            found.append(path)
    return found


def _backup_database(source: Path, target: Path) -> None:
    ensure_private_dir(target.parent)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def create_backup(
    source: str | os.PathLike[str],
    dest: str | os.PathLike[str],
) -> dict:
    source_path = Path(source)
    dest_path = Path(dest)
    ensure_private_dir(dest_path)
    databases = []
    for db in _database_files(source_path):
        relative = db.relative_to(source_path)
        target = dest_path / relative
        _backup_database(db, target)
        databases.append({
            "relative": relative.as_posix(),
            "bytes": target.stat().st_size,
        })
    manifest = {
        "version": 1,
        "source": str(source_path),
        "databases": databases,
    }
    secure_write(
        dest_path / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def restore_backup(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
) -> dict:
    source_path = Path(source)
    target_path = Path(target)
    ensure_private_dir(target_path)
    manifest_file = source_path / "manifest.json"
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        relatives = [entry["relative"] for entry in manifest.get("databases", [])]
    else:
        relatives = [
            path.relative_to(source_path).as_posix()
            for path in _database_files(source_path)
        ]
        manifest = {"version": 1, "databases": [
            {"relative": relative} for relative in relatives]}

    restored = []
    for relative in relatives:
        src = source_path / relative
        dst = target_path / relative
        if not src.is_file():
            continue
        if dst.exists():
            # Backup into the existing database connection so WAL state is
            # replaced consistently instead of deleting files underneath a
            # process that may still hold them.
            src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(str(dst))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        else:
            _backup_database(src, dst)
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
        restored.append(relative)
    return {"version": 1, "restored": restored}
