"""Roster, policy, port, and live GUID/name identity gates.

Every gate runs before the first write of a new session epoch. The live
``sense.get_self`` snapshot must be ready and match the roster GUID/name.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from .roster import CharacterSpec, Roster


class IdentityError(ValueError):
    """The observed identity does not match the configured roster."""


class PolicyError(IdentityError):
    """agent-policy.json denies the account or character."""


def read_account_port(config_path: str | Path) -> int:
    """Read ``worldrpc.port`` from an account's agent-wow.yaml."""
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"cannot read account config {path}: {exc}") from exc
    in_worldrpc = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_worldrpc = line.strip() == "worldrpc:"
            continue
        if not in_worldrpc:
            continue
        match = re.match(r"^\s+port:\s*(\d+)\s*$", line)
        if match:
            return int(match.group(1))
    raise IdentityError(f"account config {path} has no worldrpc.port")


def load_account_ports(
    roster: Roster, accounts_root: str | Path
) -> dict[str, int]:
    base = Path(accounts_root)
    ports: dict[str, int] = {}
    for spec in roster.characters:
        ports[spec.account] = read_account_port(
            base / spec.account / "agent-wow.yaml")
    return ports


def export_account_ports(
    accounts_root: str | Path, destination: str | Path, *,
    policy_path: str | Path | None = None,
) -> dict[str, int]:
    """Export only allowed account names and worldrpc ports, never secrets."""
    base = Path(accounts_root)
    allowed = None
    if policy_path is not None:
        try:
            policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
            allowed = set(policy.get("accounts") or {})
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityError(f"cannot read agent policy for port export: {exc}") from exc
    ports = {
        path.parent.name: read_account_port(path)
        for path in sorted(base.glob("*/agent-wow.yaml"))
        if allowed is None or path.parent.name in allowed
    }
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(ports, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    return ports


def load_exported_account_ports(path: str | Path) -> dict[str, int]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityError(f"cannot read account port map {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise IdentityError("account port map must be an object")
    ports = {}
    for account, port in raw.items():
        if not isinstance(account, str) or isinstance(port, bool) or not isinstance(port, int):
            raise IdentityError("account port map contains an invalid entry")
        if not 1 <= port <= 65535:
            raise IdentityError(f"account {account} has an invalid port")
        ports[account] = port
    return ports


def verify_roster(
    roster: Roster,
    *,
    policy_account: Callable[[str], bool],
    policy_character: Callable[[str, str], bool],
    account_ports: dict[str, int],
) -> bool:
    """Check every roster entry against policy and its account config port."""
    for spec in roster.characters:
        if not policy_account(spec.account):
            raise PolicyError(
                f"account {spec.account!r} is not allowed by agent policy")
        if not policy_character(spec.account, spec.name):
            raise PolicyError(
                f"character {spec.name!r} on {spec.account!r} "
                "is not allowed by agent policy")
        configured = account_ports.get(spec.account)
        if configured is None:
            raise IdentityError(
                f"account {spec.account!r} has no configured worldrpc port")
        if int(configured) != spec.port:
            raise IdentityError(
                f"roster port {spec.port} for {spec.account!r} does not match "
                f"the account config port {configured}")
    return True


def verify_live_identity(spec: CharacterSpec, snapshot: dict) -> bool:
    """Require a ready sense snapshot with the expected GUID and name."""
    if not isinstance(snapshot, dict):
        raise IdentityError("live identity snapshot is not a mapping")
    if snapshot.get("ready") is not True:
        raise IdentityError("live identity snapshot is not ready")
    try:
        guid = int(snapshot.get("guid", -1))
    except (TypeError, ValueError) as exc:
        raise IdentityError("live identity snapshot has no integer GUID") from exc
    if guid != spec.guid:
        raise IdentityError(
            f"GUID mismatch for {spec.account}/{spec.name}: "
            f"roster={spec.guid} live={guid}")
    name = str(snapshot.get("name") or "")
    if name != spec.name:
        raise IdentityError(
            f"name mismatch for {spec.account}: roster={spec.name!r} "
            f"live={name!r}")
    return True


class EpochIdentityGate:
    """Verify identity once per session epoch, before any write."""

    def __init__(
        self,
        spec: CharacterSpec,
        fetch_snapshot: Callable[[], dict],
    ):
        self.spec = spec
        self._fetch_snapshot = fetch_snapshot
        self._verified_epoch: int | str | None = None

    @property
    def verified_epoch(self) -> int | str | None:
        return self._verified_epoch

    def ensure(self, epoch: int | str) -> bool:
        if self._verified_epoch == epoch:
            return True
        snapshot = self._fetch_snapshot()
        verify_live_identity(self.spec, snapshot)
        self._verified_epoch = epoch
        return True
