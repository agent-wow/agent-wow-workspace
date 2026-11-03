#!/usr/bin/env python3
"""Local web console for agent-wow: account/character overview and session control.

Zero dependencies (Python standard library only). The backend shells out to the
existing scripts (account.sh / session.sh / agent-wow) so this stays a thin UI
over the workspace tooling rather than a second implementation.

    python3 web/server.py            # http://127.0.0.1:8765
    WOW_WEB_PORT=9000 python3 web/server.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = ROOT / "accounts"
STATIC = Path(__file__).resolve().parent / "static"
SESSION_SH = ROOT / "scripts" / "session.sh"
POLICY = ROOT / "scripts" / "agent-policy.py"
KEEPER = ROOT / "scripts" / "session-keeper.py"
KEEPER_LOG = ROOT / "logs" / "session-keeper.log"
ACTIONS_LOG = ROOT / "logs" / "web-actions.log"
HOST = "127.0.0.1"
PORT = int(os.environ.get("WOW_WEB_PORT", "8765"))

CHARS_TTL = 300  # character lists rarely change; avoid hammering the authserver
_chars_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Stock 3.3.5a race/class combinations (WotLK: blood elf and draenei unlock
# every basic class). agent-wow rejects invalid pairs server-side; this table
# only keeps the UI from offering them in the first place.
RACES = [
    ("human", "人类", ["warrior", "paladin", "hunter", "rogue", "priest",
                       "death-knight", "mage", "warlock"]),
    ("orc", "兽人", ["warrior", "hunter", "death-knight", "shaman", "warlock"]),
    ("dwarf", "矮人", ["warrior", "hunter", "rogue", "priest", "death-knight"]),
    ("night-elf", "暗夜精灵", ["warrior", "hunter", "rogue", "priest",
                               "death-knight", "druid"]),
    ("undead", "亡灵", ["warrior", "hunter", "priest", "death-knight",
                         "mage", "warlock"]),
    ("tauren", "牛头人", ["warrior", "hunter", "shaman", "death-knight", "druid"]),
    ("gnome", "侏儒", ["warrior", "rogue", "mage", "warlock", "death-knight"]),
    ("troll", "巨魔", ["warrior", "hunter", "rogue", "priest", "shaman",
                       "mage", "warlock", "death-knight"]),
    ("blood-elf", "血精灵", ["warrior", "paladin", "hunter", "rogue", "priest",
                             "death-knight", "mage", "warlock"]),
    ("draenei", "德莱尼", ["warrior", "paladin", "hunter", "rogue", "priest",
                           "shaman", "death-knight", "druid"]),
]

CLASS_NAMES = {
    "warrior": "战士", "paladin": "圣骑士", "hunter": "猎人", "rogue": "盗贼",
    "priest": "牧师", "death-knight": "死亡骑士", "shaman": "萨满",
    "mage": "法师", "warlock": "术士", "druid": "德鲁伊",
}

GENDERS = [("male", "男"), ("female", "女")]


def run(cmd: list[str], timeout: int = 30, env: dict | None = None,
        cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd or ROOT), env=env, timeout=timeout,
            capture_output=True, text=True,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


# ---------------------------------------------------------------- policy

def policy_denied(*args: str) -> str | None:
    """Return the policy error for these args, or None when access is allowed."""
    rc, out, err = run(["python3", "-I", str(POLICY), *args], timeout=10)
    if rc == 0:
        return None
    return (err or out).strip() or "agent policy denied"


def policy_accounts() -> dict[str, dict]:
    rc, out, err = run(["python3", "-I", str(POLICY), "show"], timeout=10)
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    return {name.lower(): (entry or {}) for name, entry in accounts.items()}


def account_is_agent(account: str) -> bool:
    """True when the account is in agent-policy.json (agent-managed)."""
    return account.lower() in policy_accounts()


def log_action(message: str) -> None:
    try:
        ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIONS_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- session intent

def read_intent(account: str) -> dict:
    try:
        data = json.loads((ACCOUNTS / account / "session.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_intent(account: str, desired: str, character: str = "") -> None:
    """Record the console's desired session; session-keeper.py enforces it."""
    path = ACCOUNTS / account / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    data = {"desired": desired, "character": character, "updated": time.time()}
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False, indent=2).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def keeper_running() -> bool:
    rc, out, err = run(["pgrep", "-f", "[s]ession-keeper.py"], timeout=5)
    return rc == 0 and bool(out)


def ensure_keeper() -> bool:
    """Spawn the session keeper if it is not already running."""
    if keeper_running():
        return True
    try:
        KEEPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        logf = open(KEEPER_LOG, "ab", buffering=0)
        subprocess.Popen(
            ["python3", "-I", str(KEEPER)],
            cwd=str(ROOT), stdout=logf, stderr=logf,
            start_new_session=True, close_fds=True,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- accounts

def read_yaml_value(text: str, section: str, key: str) -> str:
    in_section = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_section = line.strip() == f"{section}:"
        elif in_section:
            m = re.match(rf"^\s+{re.escape(key)}:\s*(\S+)", line)
            if m:
                return m.group(1)
    return ""


def read_accounts() -> list[dict]:
    accounts = []
    for conf in sorted(ACCOUNTS.glob("*/agent-wow.yaml")):
        name = conf.parent.name
        text = conf.read_text()
        config_dir = ""
        for line in text.splitlines():
            m = re.match(r"^config_dir:\s*(\S+)", line)
            if m:
                config_dir = m.group(1)
        username = ""
        auth = ROOT / config_dir / "auth.json" if config_dir else None
        if auth and auth.exists():
            try:
                username = json.loads(auth.read_text()).get("username", "")
            except (json.JSONDecodeError, OSError):
                pass
        accounts.append({
            "name": name,
            "username": username,
            "port": int(read_yaml_value(text, "worldrpc", "port") or 8086),
            "configDir": config_dir,
        })
    return accounts


# ---------------------------------------------------------------- VM state

def vm_processes() -> tuple[dict[str, dict], bool]:
    """Map account name -> {pid, vmDir} for clients running inside Colima."""
    script = (
        'for pid in $(pgrep -f "[a]gent-wow.*char play" 2>/dev/null); do '
        'cwd=$(readlink /proc/$pid/cwd 2>/dev/null); '
        '[ -n "$cwd" ] && echo "$pid $cwd"; done'
    )
    rc, out, err = run(["colima", "ssh", "--", "sh", "-c", script], timeout=20)
    if rc != 0:
        return {}, False
    procs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, cwd = parts
        base = os.path.basename(cwd)
        if base == "aw":
            name = "doumoman"  # legacy single-account directory
        elif base.startswith("aw-"):
            name = base[3:]
        else:
            continue
        procs[name] = {"pid": int(pid), "vmDir": base}
    return procs, True


def session_env(account: str, vm_dir: str | None = None) -> dict:
    env = dict(os.environ)
    if account_is_agent(account):
        # Automated path: never let an ambient manual flag widen access.
        env.pop("AGENT_WOW_MANUAL", None)
    else:
        # Human account: the console acts on the operator's behalf. The keeper
        # and other automated callers do not use this env.
        env["AGENT_WOW_MANUAL"] = "1"
    if vm_dir == "aw":
        # Legacy directory: default config.yaml, no ACCOUNT.
        env.pop("ACCOUNT", None)
        env["AGENT_WOW_VM_DIR"] = "aw"
    else:
        env["ACCOUNT"] = account
        env.pop("AGENT_WOW_VM_DIR", None)
    return env


def rpc(account: str, method: str, params: dict | None = None) -> dict:
    # worldrpc is port-scoped, not directory-scoped: the account's configured
    # port identifies the session regardless of which VM dir it runs from.
    env = session_env(account)
    payload = json.dumps(params or {}, ensure_ascii=False)
    rc, out, err = run([str(SESSION_SH), "rpc", method, payload], timeout=45, env=env)
    if not out:
        return {"error": err or f"rpc failed (rc={rc})"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": out[:500]}
    if "error" in data:
        return {"error": str(data["error"])}
    return data.get("result", {})


# ---------------------------------------------------------------- actions

def list_chars(account: str, refresh: bool = False) -> dict:
    with _cache_lock:
        if not refresh and account in _chars_cache:
            ts, data = _chars_cache[account]
            if time.time() - ts < CHARS_TTL:
                return data
    conf = ACCOUNTS / account / "agent-wow.yaml"
    if not conf.exists():
        return {"error": f"unknown account: {account}"}
    rc, out, err = run(["agent-wow", "--config", str(conf), "char", "list"], timeout=40)
    combined = f"{out}\n{err}".strip()
    if rc != 0 or "GUID" not in out:
        return {"error": combined or "char list failed", "needLogin": "session" in combined}
    chars = []
    for line in out.splitlines():
        if not re.match(r"^\d+\s", line):
            continue
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) >= 8:
            chars.append({
                "guid": cols[0], "name": cols[1], "race": cols[2],
                "class": cols[3], "gender": cols[4], "level": int(cols[5]),
                "zone": cols[6], "map": cols[7],
            })
    data = {"chars": chars}
    with _cache_lock:
        _chars_cache[account] = (time.time(), data)
    return data


def login(account: str) -> dict:
    conf = ACCOUNTS / account / "agent-wow.yaml"
    if not conf.exists():
        return {"error": f"unknown account: {account}"}
    # A new login invalidates the saved session key and kicks the account's
    # active client, so this is gameplay-affecting. Agent accounts stay
    # policy-bound; human accounts are the operator's own and are allowed.
    if account_is_agent(account):
        denied = policy_denied("account", account)
        if denied:
            return {"error": denied}
    rc, out, err = run(["agent-wow", "--config", str(conf), "auth", "login"], timeout=40)
    ok = rc == 0 and "Authentication success" in out
    with _cache_lock:
        _chars_cache.pop(account, None)
    return {"ok": ok, "output": f"{out}\n{err}".strip()}


def start_session(account: str, character: str) -> dict:
    if not (ACCOUNTS / account / "agent-wow.yaml").exists():
        return {"error": f"unknown account: {account}"}
    if not character:
        return {"error": "character is required"}
    if account_is_agent(account):
        denied = policy_denied("char", account, character)
        if denied:
            return {"error": denied}
        # The console only records intent. session-keeper.py is the sole owner
        # of session actions, so start/restart policy, STOP, cooldown and
        # health probing all live in one place.
        write_intent(account, "online", character)
        if not ensure_keeper():
            return {"error": "could not start session-keeper"}
        return {
            "ok": True,
            "output": f"session intent queued for {character}; keeper will start it",
        }
    # Human account: manual start only. No intent is written, so the keeper
    # never adopts or reconnects this session.
    rc, out, err = run([str(SESSION_SH), "start", character], timeout=180,
                       env=session_env(account))
    combined = f"{out}\n{err}".strip()
    ok = rc == 0 and "started pid" in out
    log_action(f"manual start account={account!r} character={character!r} ok={ok}")
    return {"ok": ok, "output": combined}


def stop_session(account: str) -> dict:
    if account_is_agent(account):
        # Clear the intent first so the keeper does not bring the character back.
        previous = read_intent(account).get("character", "")
        write_intent(account, "offline", previous)
        ensure_keeper()
        return {"ok": True, "output": "session intent queued; keeper will stop it"}
    # Human account: direct, manual stop. No keeper involvement.
    procs, _ = vm_processes()
    vm_dir = procs.get(account, {}).get("vmDir")
    env = session_env(account, vm_dir)
    rc, out, err = run([str(SESSION_SH), "stop"], timeout=60, env=env)
    log_action(f"manual stop account={account!r} ok={rc == 0}")
    return {"ok": rc == 0, "output": f"{out}\n{err}".strip()}


def get_logs(account: str, lines: int = 50) -> dict:
    procs, _ = vm_processes()
    vm_dir = procs.get(account, {}).get("vmDir")
    env = session_env(account, vm_dir)
    rc, out, err = run([str(SESSION_SH), "logs", str(lines)], timeout=30, env=env)
    return {"logs": out or err}


def create_character(account: str, name: str, race: str, cls: str,
                     gender: str) -> dict:
    conf = ACCOUNTS / account / "agent-wow.yaml"
    if not conf.exists():
        return {"error": f"unknown account: {account}"}
    # Agent accounts stay policy-bound; human accounts are manual-only but the
    # operator may create a character on their own account.
    if account_is_agent(account):
        denied = policy_denied("account", account)
        if denied:
            return {"error": denied}
    cmd = ["agent-wow", "--config", str(conf), "char", "create"]
    if name:
        cmd += ["--name", name]
    if race:
        cmd += ["--race", race]
    if cls:
        cmd += ["--class", cls]
    if gender:
        cmd += ["--gender", gender]
    rc, out, err = run(cmd, timeout=90)
    combined = f"{out}\n{err}".strip()
    ok = rc == 0 and "error" not in combined.lower() and bool(out)
    with _cache_lock:
        _chars_cache.pop(account, None)
    return {"ok": ok, "output": combined}


def creation_options() -> dict:
    return {
        "races": [{"id": rid, "label": label, "classes": list(cls)}
                  for rid, label, cls in RACES],
        "classes": [{"id": k, "label": v} for k, v in CLASS_NAMES.items()],
        "genders": [{"id": gid, "label": glabel} for gid, glabel in GENDERS],
    }


# The literal the console requires before it will delete a character. It is
# checked here as well as in the browser: the browser check is convenience,
# this one is the guard.
DELETE_CONFIRM = "DELETE"


def _delete_character(account: str, name: str, confirm: str) -> dict:
    conf = ACCOUNTS / account / "agent-wow.yaml"
    if not conf.exists():
        return {"error": f"unknown account: {account}"}
    if not name:
        return {"error": "character name is required"}
    if confirm != DELETE_CONFIRM:
        return {"error": f"confirmation must be exactly {DELETE_CONFIRM}"}

    if account_is_agent(account):
        denied = policy_denied("char", account, name)
        if denied:
            return {"error": denied}

    # Deleting a character while its account is in the world would pull the
    # character out from under the running client.
    procs, _ = vm_processes()
    if account in procs:
        return {"error": "该账号正在游戏中，请先下线再删除角色"}

    listed = list_chars(account, refresh=True)
    if "error" in listed:
        return {"error": listed["error"]}
    if not any(c["name"] == name for c in listed.get("chars", [])):
        return {"error": f"character not found: {name}"}

    cmd = ["agent-wow", "--config", str(conf), "char", "delete", name, "--yes"]
    rc, out, err = run(cmd, timeout=90)
    combined = f"{out}\n{err}".strip()
    ok = rc == 0 and "Deleted character" in out
    with _cache_lock:
        _chars_cache.pop(account, None)
    return {"ok": ok, "output": combined}


def delete_character(account: str, name: str, confirm: str) -> dict:
    result = _delete_character(account, name, confirm)
    log_action(
        f"delete account={account!r} character={name!r} "
        f"ok={bool(result.get('ok'))} "
        f"error={(result.get('error') or '').strip()[:300]!r} "
        f"output={(result.get('output') or '').strip()[:300]!r}"
    )
    return result


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "WowConsole/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local dev console: always serve the current asset after an edit.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/state":
                accounts = read_accounts()
                procs, colima_ok = vm_processes()
                keeper_ok = keeper_running()
                allowed = policy_accounts()
                for a in accounts:
                    p = procs.get(a["name"])
                    intent = read_intent(a["name"])
                    a["online"] = bool(p)
                    a["pid"] = p["pid"] if p else None
                    a["vmDir"] = p["vmDir"] if p else None
                    a["desired"] = intent.get("desired")
                    a["desiredCharacter"] = intent.get("character")
                    a["agentAllowed"] = a["name"].lower() in allowed
                self.send_json({
                    "accounts": accounts,
                    "colima": colima_ok,
                    "keeper": keeper_ok,
                })
            elif path == "/api/chars":
                account = qs.get("account", [""])[0]
                refresh = qs.get("refresh", ["0"])[0] == "1"
                self.send_json(list_chars(account, refresh))
            elif path == "/api/self":
                account = qs.get("account", [""])[0]
                result = rpc(account, "sense.get_self")
                self.send_json(result if "error" in result else {"self": result})
            elif path == "/api/logs":
                account = qs.get("account", [""])[0]
                lines = int(qs.get("lines", ["50"])[0])
                self.send_json(get_logs(account, lines))
            elif path == "/api/creation-options":
                self.send_json(creation_options())
            elif path in ("/", "/index.html"):
                self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                target = (STATIC / path[len("/static/"):]).resolve()
                if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
                    self.send_json({"error": "not found"}, 404)
                    return
                ctype = {"js": "text/javascript", "css": "text/css"}.get(
                    target.suffix.lstrip("."), "application/octet-stream")
                self.send_file(target, ctype)
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:  # keep the console alive on unexpected errors
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self.read_body()
        try:
            if path == "/api/login":
                self.send_json(login(body.get("account", "")))
            elif path == "/api/start":
                self.send_json(start_session(body.get("account", ""), body.get("character", "")))
            elif path == "/api/stop":
                self.send_json(stop_session(body.get("account", "")))
            elif path == "/api/create":
                self.send_json(create_character(
                    body.get("account", ""), body.get("name", "").strip(),
                    body.get("race", ""), body.get("class", ""),
                    body.get("gender", ""),
                ))
            elif path == "/api/delete":
                self.send_json(delete_character(
                    body.get("account", ""), body.get("name", "").strip(),
                    body.get("confirm", "").strip(),
                ))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    ensure_keeper()
    print(f"WoW console: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
