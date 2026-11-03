#!/usr/bin/env python3
"""Command-line entry point for the growth runtime.

The worker command attaches the stateful U2 solo growth loop after the U1
identity gate; supervisor/backup/permission commands are unchanged.

Usage (the shell wrappers in scripts/ call this file):

  cli.py roster validate [--path config/roster.yaml]
  cli.py worker --account doumoboy2 --character Name [--once]
  cli.py supervisor [--once]
  cli.py backup create --source data/growth --dest data/growth-backups/<ts>
  cli.py backup restore --source <backup> --target data/growth
  cli.py permissions audit-accounts [--accounts-root accounts]
  cli.py permissions audit-growth [--root data/growth]
  cli.py permissions enforce-growth [--root data/growth]
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.growth.adapter import RPCCallTransport, WorldAdapter  # noqa: E402
from scripts.growth.backup import create_backup, restore_backup  # noqa: E402
from scripts.growth.identity import (  # noqa: E402
    load_account_ports,
    load_exported_account_ports,
    verify_roster,
)
from scripts.growth.machine import SoloGrowthMachine  # noqa: E402
from scripts.growth.cooperation import CooperationRuntime  # noqa: E402
from scripts.growth.permissions import (  # noqa: E402
    audit_accounts,
    audit_tree,
    enforce_tree,
    ensure_private_dir,
)
from scripts.growth.profiles import ProfileError, load_profile  # noqa: E402
from scripts.growth.roster import load_roster  # noqa: E402
from scripts.growth.routes import RouteError, load_route  # noqa: E402
from scripts.growth.stop import StopFlag  # noqa: E402
from scripts.growth.store import CheckpointStore  # noqa: E402
from scripts.growth.supervisor import GrowthSupervisor  # noqa: E402
from scripts.growth.worker import GrowthWorker  # noqa: E402

POLICY = ROOT / "scripts" / "agent-policy.py"
SESSION_SH = ROOT / "scripts" / "session.sh"
KEEPER = ROOT / "scripts" / "session-keeper.py"
CLI_PATH = Path(__file__).resolve()


def data_root() -> Path:
    env = os.environ.get("GROWTH_DATA_DIR")
    return Path(env) if env else ROOT / "data" / "growth"


def state_root() -> Path:
    return data_root() / "state"


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


# ------------------------------------------------------------------ policy

def policy_account(account: str) -> bool:
    return subprocess.run(
        [sys.executable, "-I", str(POLICY), "account", account],
        cwd=str(ROOT), capture_output=True).returncode == 0


def policy_character(account: str, character: str) -> bool:
    return subprocess.run(
        [sys.executable, "-I", str(POLICY), "char", account, character],
        cwd=str(ROOT), capture_output=True).returncode == 0


def verified_roster(path: Path | None = None):
    roster = load_roster(path)
    accounts = ROOT / "accounts"
    account_ports = (
        load_account_ports(roster, accounts) if accounts.is_dir()
        else load_exported_account_ports(ROOT / "config" / "account-ports.json")
    )
    verify_roster(
        roster,
        policy_account=policy_account,
        policy_character=policy_character,
        account_ports=account_ports,
    )
    return roster


# ----------------------------------------------------------------- process

def process_alive(pattern: str) -> bool:
    return subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _spawn(command: list[str], log_path: Path) -> bool:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "ab", buffering=0) as handle:
            subprocess.Popen(
                command, cwd=str(ROOT), stdout=handle, stderr=handle,
                start_new_session=True, close_fds=True)
        return True
    except OSError as exc:
        log(f"spawn failed: {exc}")
        return False


def spawn_keeper() -> bool:
    log_dir = ROOT / "logs"
    log(f"starting session-keeper")
    return _spawn(
        [sys.executable, "-I", str(KEEPER)], log_dir / "session-keeper.log")


def worker_process_pattern(spec) -> str:
    return (
        f"[g]rowth.*worker.*--account {spec.account}.*--character {spec.name}")


def spawn_worker(spec) -> bool:
    state = state_root() / spec.account
    ensure_private_dir(state)
    log(f"starting worker {spec.account}/{spec.name}")
    return _spawn(
        [sys.executable, "-I", str(CLI_PATH), "worker",
         "--account", spec.account, "--character", spec.name],
        state / f"{spec.name}.log")


def build_supervisor(roster) -> GrowthSupervisor:
    with_keeper = os.environ.get("GROWTH_WITH_KEEPER", "1") != "0"
    return GrowthSupervisor(
        roster,
        stop_flag=StopFlag(),
        is_keeper_alive=lambda: (
            True if not with_keeper else process_alive("[s]ession-keeper.py")),
        is_worker_alive=lambda spec: process_alive(
            worker_process_pattern(spec)),
        spawn=lambda kind, spec=None: (
            spawn_keeper() if kind == "keeper" else spawn_worker(spec)),
        logger=log,
    )


# ------------------------------------------------------------------- worker

def run_worker(args) -> int:
    roster = verified_roster(Path(args.roster) if args.roster else None)
    spec = roster.by_character().get(args.character)
    if spec is None or spec.account != args.account:
        log(f"roster has no {args.account}/{args.character}")
        return 2
    try:
        profile = load_profile(spec.class_name)
        route = load_route(profile.route.zone)
    except (ProfileError, RouteError) as exc:
        log(f"growth content unavailable for {spec.class_name}: {exc}")
        return 2
    if not profile.supports_role(spec.role):
        log(f"profile {profile.class_name} does not support roster role "
            f"{spec.role}")
        return 2

    state = state_root() / spec.account
    ensure_private_dir(state)
    store = CheckpointStore(state / f"{spec.name}.db")
    store.open()
    from scripts.growth.identity import EpochIdentityGate
    adapter = WorldAdapter(RPCCallTransport(spec.port))
    cooperation = CooperationRuntime(
        roster, spec, adapter, store, profile.inventory)
    growth = SoloGrowthMachine(
        profile, route, adapter=adapter, store=store, logger=log,
        epoch_hint=args.epoch, cooperation=cooperation)
    worker = GrowthWorker(
        spec, store,
        lock_path=state / f"{spec.name}.worker.lock",
        stop_flag=StopFlag(),
        gate=EpochIdentityGate(spec, adapter.get_self),
        growth=growth,
        epoch=args.epoch,
        logger=log,
    )
    try:
        worker.start()
        if args.once:
            worker.tick()
        else:
            stopping = {"now": False}

            def on_signal(signum, frame):
                stopping["now"] = True

            signal.signal(signal.SIGTERM, on_signal)
            signal.signal(signal.SIGINT, on_signal)
            while not stopping["now"]:
                try:
                    worker.tick()
                except Exception as exc:
                    log(f"tick failed: {type(exc).__name__}: {exc}")
                for _ in range(max(1, args.interval)):
                    if stopping["now"]:
                        break
                    time.sleep(1)
    finally:
        worker.stop()
        store.close()
    return 0


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    roster_parser = sub.add_parser("roster")
    roster_sub = roster_parser.add_subparsers(dest="roster_command", required=True)
    validate = roster_sub.add_parser("validate")
    validate.add_argument("--path", default=None)

    worker = sub.add_parser("worker")
    worker.add_argument("--account", required=True)
    worker.add_argument("--character", required=True)
    worker.add_argument("--epoch", type=int, default=0)
    worker.add_argument("--interval", type=int, default=5)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--roster", default=None)

    supervisor = sub.add_parser("supervisor")
    supervisor.add_argument("--once", action="store_true")
    supervisor.add_argument("--interval", type=int, default=15)
    supervisor.add_argument("--roster", default=None)

    backup = sub.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_sub.add_parser("create")
    backup_create.add_argument("--source", default=str(data_root()))
    backup_create.add_argument("--dest", required=True)
    backup_restore = backup_sub.add_parser("restore")
    backup_restore.add_argument("--source", required=True)
    backup_restore.add_argument("--target", default=str(data_root()))

    perms = sub.add_parser("permissions")
    perms_sub = perms.add_subparsers(dest="permissions_command", required=True)
    perms_accounts = perms_sub.add_parser("audit-accounts")
    perms_accounts.add_argument("--accounts-root", default=str(ROOT / "accounts"))
    perms_growth = perms_sub.add_parser("audit-growth")
    perms_growth.add_argument("--root", default=str(data_root()))
    perms_enforce = perms_sub.add_parser("enforce-growth")
    perms_enforce.add_argument("--root", default=str(data_root()))

    args = parser.parse_args(argv)

    if args.command == "roster":
        roster = verified_roster(Path(args.path) if args.path else None)
        print(f"roster OK: {len(roster.characters)} characters, "
              f"faction={roster.faction}, zone={roster.start_zone}")
        return 0

    if args.command == "worker":
        return run_worker(args)

    if args.command == "supervisor":
        roster = verified_roster(Path(args.roster) if args.roster else None)
        ensure_private_dir(data_root())
        ensure_private_dir(state_root())
        supervisor_ctl = build_supervisor(roster)
        if args.once:
            supervisor_ctl.run_once()
            return 0
        while True:
            try:
                supervisor_ctl.run_once()
            except Exception as exc:
                log(f"supervisor error: {type(exc).__name__}: {exc}")
            time.sleep(max(1, args.interval))

    if args.command == "backup":
        if args.backup_command == "create":
            manifest = create_backup(args.source, args.dest)
            print(f"backed up {len(manifest['databases'])} database(s) "
                  f"to {args.dest}")
        else:
            result = restore_backup(args.source, args.target)
            print(f"restored {len(result['restored'])} database(s) "
                  f"to {args.target}")
        return 0

    if args.command == "permissions":
        if args.permissions_command == "audit-accounts":
            issues = audit_accounts(args.accounts_root)
        else:
            if args.permissions_command == "enforce-growth":
                enforce_tree(args.root)
            issues = audit_tree(args.root)
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1 if issues else 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
