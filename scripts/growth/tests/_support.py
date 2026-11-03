"""Shared fixtures for the growth unit tests.

These tests exercise behaviour (locks, SQLite transactions, identity gates)
with temp directories and small fakes. Nothing here touches the real accounts
tree, the Colima VM, or a live game session.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ACCOUNT_PORTS = {
    "doumoboy1": 8093,
    "doumoboy2": 8092,
    "doumoboy3": 8091,
    "doumoboy4": 8090,
    "doumoboy5": 8089,
}
ACCOUNTS = tuple(sorted(ACCOUNT_PORTS))
ROLES = ("tank", "healer", "dps", "dps", "dps")


def roster_data(**overrides) -> dict:
    chars = []
    for index, account in enumerate(ACCOUNTS):
        chars.append({
            "account": account,
            "name": f"Growth{index + 1}",
            "guid": 101 + index,
            "port": ACCOUNT_PORTS[account],
            "role": ROLES[index],
            "class": "warrior" if index == 0 else "priest" if index == 1 else "mage",
            "race": "human",
            "faction": "alliance",
            "start_zone": "elwynn-forest",
        })
    data = {
        "version": 1,
        "configured": True,
        "faction": "alliance",
        "start_zone": "elwynn-forest",
        "characters": chars,
    }
    data.update(overrides)
    return data


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def load_session_keeper():
    path = ROOT / "scripts" / "session-keeper.py"
    spec = importlib.util.spec_from_file_location("aw_session_keeper", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeStopFlag:
    def __init__(self, value: bool = False):
        self.value = value

    def is_set(self) -> bool:
        return self.value


class FakeClients:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def __call__(self):
        return self.mapping
