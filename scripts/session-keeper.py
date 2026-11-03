#!/usr/bin/env python3
"""Keep manually-started console sessions online.

The web console records "上线" as a persistent intent in
accounts/<account>/session.json:

    {"desired": "online", "character": "Name", "updated": <epoch>}

"下线" writes desired=offline. This daemon is the only automated owner of
session actions. It reconciles that intent with the actual clients every
--interval seconds:

- online, no client for the account  -> start the selected character
- online, wrong character            -> stop the account, start the selected one
- offline, client still running      -> stop it
- client died (worldserver EOF, crash) -> start the selected character again
- client alive but RPC/modules fail  -> restart after consecutive failures
- saved session key invalid          -> degraded, wait for a human (never login)

Only accounts listed in agent-policy.json are touched. An operator-owned STOP
file blocks starts and restarts; it never blocks an explicit offline stop, and
growth code has no way to clear it.

Usage:
  session-keeper.py                       run the daemon
  session-keeper.py --once                reconcile once and exit
  session-keeper.py --dry-run --once      log intended actions, execute nothing
  session-keeper.py status                show intents and live clients
  session-keeper.py desired <account> online <character>
  session-keeper.py desired <account> offline

The web server spawns this daemon on startup; it also exits early if another
instance holds logs/session-keeper.lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = ROOT / "accounts"
SESSION_SH = ROOT / "scripts" / "session.sh"
ACCOUNT_SH = ROOT / "scripts" / "account.sh"
POLICY = ROOT / "scripts" / "agent-policy.py"
LOCK_FILE = ROOT / "logs" / "session-keeper.lock"
BACKUP_SH = ROOT / "scripts" / "growth-backup.sh"
ROSTER_FILE = ROOT / "config" / "roster.yaml"

# Isolated mode (-I) may omit the script directory from sys.path, so make the
# sibling `growth` package importable however the keeper was launched.
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:  # executed as scripts/session-keeper.py (script dir on sys.path)
    from growth.stop import StopFlag, default_stop_path
except ImportError:  # imported from the workspace root by tests
    from scripts.growth.stop import StopFlag, default_stop_path

INTERVAL = 15          # seconds between reconciliations
START_VERIFY = 8       # seconds to wait for the client process after start
BACKOFF = 120          # retry delay after a failed (non-auth) start
HEALTH_FAILURES = 3    # consecutive health failures before a restart
DEGRADED_RECHECK = 300 # how often a degraded account re-checks session validity
BACKUP_INTERVAL = 60   # host-owned VM checkpoint RPO, plus backup runtime
AUTH_ERRORS = (
    "saved session is invalid",
    "reconnect proof",
    "no saved session",
)
MODULE_NAMES = re.compile(
    r"(?:^|[-_])(sense|move|combat|loot|quest|party|inventory|character)(?:[-_]|$)")
REQUIRED_MODULES = frozenset(("sense", "move", "combat", "loot", "quest", "party"))

_stop = False


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(cmd: list[str], timeout: int = 30,
        env: dict | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=timeout,
                           capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def account_env(account: str) -> dict:
    env = dict(os.environ)
    env["ACCOUNT"] = account
    env["AGENT_WOW_VM_DIR"] = f"aw-{account}"
    env.pop("AGENT_WOW_WORLDRPC_PORT", None)
    return env


def intent_path(account: str, accounts_dir: Path = ACCOUNTS) -> Path:
    return accounts_dir / account / "session.json"


def read_intent(account: str, accounts_dir: Path = ACCOUNTS) -> dict:
    try:
        data = json.loads(intent_path(account, accounts_dir).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_intent(account: str, desired: str, character: str = "",
                 accounts_dir: Path = ACCOUNTS) -> None:
    path = intent_path(account, accounts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    data = {
        "desired": desired,
        "character": character,
        "updated": time.time(),
    }
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False, indent=2).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def all_intents(accounts_dir: Path = ACCOUNTS) -> dict[str, dict]:
    out = {}
    if not accounts_dir.is_dir():
        return out
    for path in sorted(accounts_dir.glob("*/session.json")):
        intent = read_intent(path.parent.name, accounts_dir)
        if intent:
            out[path.parent.name] = intent
    return out


def policy_allows(account: str) -> bool:
    rc, _, _ = run(["python3", "-I", str(POLICY), "account", account], timeout=10)
    return rc == 0


def character_policy_allows(account: str, character: str) -> bool:
    rc, _, _ = run(
        ["python3", "-I", str(POLICY), "char", account, character], timeout=10)
    return rc == 0


_CLIENT_SCRIPT = r'''
for pid in $(pgrep -f "[a]gent-wow.*char play" 2>/dev/null); do
    cwd=$(readlink /proc/$pid/cwd 2>/dev/null || true)
    [ -n "$cwd" ] || continue
    cmd=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null || true)
    printf "%s\t%s\t%s\n" "$pid" "$cwd" "$cmd"
done
'''


def list_clients() -> dict[str, list[dict]]:
    """account -> [{pid, cwd, char}] from the Colima VM."""
    rc, out, err = run(["colima", "ssh", "--", "sh", "-c", _CLIENT_SCRIPT],
                       timeout=25)
    if rc != 0 and not out:
        log(f"client scan failed: {err or rc}")
        return {}
    clients: dict[str, list[dict]] = {}
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        pid, cwd, cmdline = parts
        base = os.path.basename(cwd)
        if base == "aw":
            account = "doumoman"          # legacy single-account directory
        elif base.startswith("aw-"):
            account = base[3:]
        else:
            continue
        m = re.search(r"\bchar\s+play\s+(\S+)", cmdline)
        clients.setdefault(account, []).append({
            "pid": int(pid),
            "cwd": cwd,
            "char": m.group(1) if m else "",
        })
    return clients


def matching(procs: list[dict], character: str) -> bool:
    want = character.casefold()
    return any(p["char"].casefold() == want for p in procs)


def start_account(account: str, character: str) -> bool:
    log(f"{account}: starting {character}")
    rc, out, err = run([str(SESSION_SH), "start", character], timeout=300,
                       env=account_env(account))
    if rc != 0:
        log(f"{account}: start failed (rc={rc}): {(err or out)[:300]}")
        return False
    time.sleep(START_VERIFY)
    if matching(list_clients().get(account, []), character):
        log(f"{account}: {character} is online")
        return True
    log(f"{account}: {character} did not come online")
    return False


def stop_account(account: str) -> bool:
    log(f"{account}: stopping")
    rc, out, err = run([str(SESSION_SH), "stop"], timeout=90,
                       env=account_env(account))
    if rc != 0:
        log(f"{account}: stop failed (rc={rc}): {(err or out)[:300]}")
        return False
    return True


def tail_session_log(account: str) -> str:
    rc, out, _ = run(
        ["colima", "ssh", "--", "sh", "-c",
         f'tail -n 40 "$HOME/aw-{account}/logs/session.log" 2>/dev/null'],
        timeout=20)
    return out if rc == 0 else ""


def probe_rpc(account: str) -> tuple[bool, str]:
    """Bounded read-only RPC probe for an otherwise alive client."""
    rc, out, err = run(
        [str(SESSION_SH), "rpc", "sense.get_self"], timeout=10,
        env=account_env(account))
    if rc != 0:
        return False, (err or out or f"rc={rc}")[:200]
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, "invalid RPC response"
    if isinstance(data, dict) and data.get("error"):
        return False, str(data["error"])[:200]
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not result:
        return False, "empty sense snapshot"
    return True, "ok"


def probe_modules(account: str) -> tuple[bool, str]:
    """Bounded probe requiring every current gameplay module for this account."""
    rc, out, err = run(
        ["colima", "ssh", "--", "docker", "ps", "--format", "{{.Names}}"],
        timeout=10)
    if rc != 0:
        return False, (err or f"rc={rc}")[:200]
    names = [
        line.strip() for line in out.splitlines()
        if line.strip() and account in line
    ]
    present = {
        match.group(1) for name in names
        if (match := MODULE_NAMES.search(name)) is not None
    }
    missing = sorted(REQUIRED_MODULES - present)
    if not missing:
        return True, "ok"
    return False, "missing module containers: " + ",".join(missing)


def session_status_valid(account: str) -> bool:
    """Read-only auth status check used only to leave the degraded state."""
    rc, _, _ = run([str(ACCOUNT_SH), "status", account], timeout=20,
                   env=account_env(account))
    return rc == 0


def growth_configured() -> bool:
    try:
        return json.loads(ROSTER_FILE.read_text()).get("configured") is True
    except (OSError, json.JSONDecodeError):
        return False


def run_growth_backup() -> bool:
    rc, out, err = run([str(BACKUP_SH), "--vm"], timeout=180)
    if rc != 0:
        log(f"growth backup failed (rc={rc}): {(err or out)[:300]}")
    return rc == 0


class Keeper:
    """Intent reconciliation with injectable probes for fault-injection tests."""

    def __init__(
        self,
        *,
        accounts_dir: Path = ACCOUNTS,
        policy_allows=policy_allows,
        character_policy_allows=character_policy_allows,
        list_clients_fn=list_clients,
        start_fn=start_account,
        stop_fn=stop_account,
        rpc_probe=probe_rpc,
        module_probe=probe_modules,
        log_tail_fn=tail_session_log,
        session_valid_fn=session_status_valid,
        stop_flag=None,
        clock=time.time,
        logger=log,
        sleep_fn=time.sleep,
        health_threshold: int = HEALTH_FAILURES,
        backoff: int = BACKOFF,
        degraded_recheck: int = DEGRADED_RECHECK,
        backup_interval: int = BACKUP_INTERVAL,
        backup_fn=run_growth_backup,
        backup_enabled=growth_configured,
    ):
        self.accounts_dir = Path(accounts_dir)
        self.policy_allows = policy_allows
        self.character_policy_allows = character_policy_allows
        self.list_clients_fn = list_clients_fn
        self.start_fn = start_fn
        self.stop_fn = stop_fn
        self.rpc_probe = rpc_probe
        self.module_probe = module_probe
        self.log_tail_fn = log_tail_fn
        self.session_valid_fn = session_valid_fn
        self.stop_flag = stop_flag or StopFlag(default_stop_path(ROOT / "data" / "growth"))
        self.clock = clock
        self.logger = logger
        self.sleep_fn = sleep_fn
        self.health_threshold = max(1, int(health_threshold))
        self.backoff = backoff
        self.degraded_recheck = degraded_recheck
        self.backup_interval = backup_interval
        self.backup_fn = backup_fn
        self.backup_enabled = backup_enabled
        self.last_backup_attempt = 0.0
        self.degraded: dict[str, float] = {}
        self.health_failures: dict[str, int] = {}
        self.backoff_until: dict[str, float] = {}

    def maybe_backup(self) -> bool:
        """Attempt the host-owned VM backup once per declared RPO interval."""
        now = self.clock()
        if now - self.last_backup_attempt < self.backup_interval:
            return False
        if not self.backup_enabled():
            return False
        self.last_backup_attempt = now
        return bool(self.backup_fn())

    def _intents(self) -> dict[str, dict]:
        return all_intents(self.accounts_dir)

    def reconcile_once(self, dry_run: bool = False) -> None:
        intents = self._intents()
        if not intents:
            return
        clients = self.list_clients_fn()
        now = self.clock()
        for account, intent in sorted(intents.items()):
            desired = str(intent.get("desired") or "").lower()
            character = str(intent.get("character") or "")
            if not self.policy_allows(account):
                # Human account: never touch it, even if an intent file exists.
                continue

            procs = clients.get(account, [])
            if desired == "online" and character:
                if not self.character_policy_allows(account, character):
                    self.logger(
                        f"{account}: character {character} denied by policy; "
                        "leaving the current session untouched")
                    continue
                if matching(procs, character):
                    self._check_health(account, character, dry_run)
                    continue
                if procs:
                    found = ", ".join(p["char"] or "?" for p in procs)
                    self.logger(
                        f"{account}: want {character}, found {found}; correcting")
                    if dry_run:
                        continue
                    if not self.stop_fn(account):
                        self.logger(
                            f"{account}: could not stop the previous character; "
                            "not starting a second client")
                        continue
                    clients = self.list_clients_fn()
                    procs = clients.get(account, [])
                    if procs:
                        self.logger(
                            f"{account}: previous client is still alive; "
                            "deferring the character switch")
                        continue
                if self._start_blocked(account, now, dry_run):
                    continue
                if dry_run:
                    self.logger(f"{account}: would start {character}")
                    continue
                self._try_start(account, character, now)

            elif desired == "offline":
                if procs:
                    self.logger(f"{account}: offline requested, client still running")
                    if not dry_run:
                        self.stop_fn(account)
                        clients = self.list_clients_fn()

    def _start_blocked(self, account: str, now: float, dry_run: bool) -> bool:
        if self.stop_flag.is_set():
            self.logger(
                f"{account}: operator STOP present; not starting or restarting")
            return True
        if account in self.degraded:
            if now - self.degraded[account] >= self.degraded_recheck:
                if self.session_valid_fn(account):
                    self.degraded.pop(account, None)
                    self.logger(f"{account}: session key valid again; resuming")
                else:
                    self.degraded[account] = now
            if account in self.degraded:
                if not dry_run:
                    self.logger(
                        f"{account}: degraded (invalid session key), waiting for a human")
                return True
        if self.backoff_until.get(account, 0) > now:
            if not dry_run:
                self.logger(f"{account}: backing off after a failed start")
            return True
        return False

    def _try_start(self, account: str, character: str, now: float) -> bool:
        ok = self.start_fn(account, character)
        if ok:
            self.health_failures.pop(account, None)
            self.backoff_until.pop(account, None)
            self.degraded.pop(account, None)
            return True
        tail = (self.log_tail_fn(account) or "").lower()
        if any(message in tail for message in AUTH_ERRORS):
            self.degraded[account] = now
            self.logger(
                f"{account}: session key invalid; degraded and waiting for a human")
        else:
            self.backoff_until[account] = now + self.backoff
            self.logger(f"{account}: start failed; backing off")
        return False

    def _check_health(self, account: str, character: str, dry_run: bool) -> None:
        if dry_run:
            return
        rpc_ok, rpc_reason = self.rpc_probe(account)
        module_ok, module_reason = (
            self.module_probe(account) if rpc_ok else (False, "rpc unavailable"))
        if rpc_ok and module_ok:
            self.health_failures.pop(account, None)
            return

        failures = self.health_failures.get(account, 0) + 1
        self.health_failures[account] = failures
        reason = rpc_reason or module_reason or "health probe failed"
        self.logger(
            f"{account}: health failure {failures}/{self.health_threshold} "
            f"({reason})")
        if failures < self.health_threshold:
            return
        if self.stop_flag.is_set():
            self.logger(f"{account}: operator STOP present; not restarting")
            return
        if account in self.degraded:
            return
        now = self.clock()
        if self.backoff_until.get(account, 0) > now:
            return

        self.logger(
            f"{account}: restarting {character} after consecutive health failures")
        if not self.stop_fn(account):
            self.backoff_until[account] = now + self.backoff
            self.logger(
                f"{account}: stop failed; not starting a duplicate client")
            return
        self.sleep_fn(2)
        self._try_start(account, character, now)


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def show_status() -> None:
    clients = list_clients()
    intents = all_intents()
    accounts = sorted(set(intents) | set(clients))
    if not accounts:
        print("no intents, no clients")
        return
    for account in accounts:
        intent = intents.get(account, {})
        procs = clients.get(account, [])
        chars = ", ".join(p["char"] or "?" for p in procs) or "-"
        print(f"{account:<11} desired={intent.get('desired', '-'):<8} "
              f"want={intent.get('character') or '-':<12} clients={chars}")


def main(argv: list[str]) -> int:
    global _stop
    args = argv[1:]
    dry_run = "--dry-run" in args
    once = "--once" in args
    interval = INTERVAL

    if args and args[0] == "status":
        show_status()
        return 0
    if args and args[0] == "desired":
        if len(args) < 3:
            print("usage: session-keeper.py desired <account> online|offline [character]",
                  file=sys.stderr)
            return 2
        account, desired = args[1], args[2].lower()
        if desired not in ("online", "offline"):
            print("desired must be online or offline", file=sys.stderr)
            return 2
        character = args[3] if len(args) > 3 else read_intent(account).get("character", "")
        if desired == "online" and not character:
            print("online needs a character (or an existing remembered one)",
                  file=sys.stderr)
            return 2
        if desired == "online" and not policy_allows(account):
            print(f"agent policy: account '{account}' is off-limits", file=sys.stderr)
            return 3
        if desired == "online" and not character_policy_allows(account, character):
            print(f"agent policy: character '{character}' on '{account}' is off-limits",
                  file=sys.stderr)
            return 3
        write_intent(account, desired, character)
        print(f"{account}: desired={desired} character={character or '-'}")
        return 0

    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except (IndexError, ValueError):
            print("--interval needs a number", file=sys.stderr)
            return 2

    def on_signal(signum, frame):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    keeper = Keeper()
    if once:
        if dry_run:
            log("dry-run: reconciling once")
        keeper.reconcile_once(dry_run=dry_run)
        return 0

    lock = acquire_lock()
    if lock is None:
        log("another session-keeper is already running")
        return 0
    log(f"session-keeper started (interval={interval}s)")
    while not _stop:
        try:
            keeper.reconcile_once(dry_run=dry_run)
            if not dry_run:
                keeper.maybe_backup()
        except Exception as exc:  # keep the daemon alive
            log(f"reconcile error: {type(exc).__name__}: {exc}")
        for _ in range(interval):
            if _stop:
                break
            time.sleep(1)
    log("session-keeper stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
