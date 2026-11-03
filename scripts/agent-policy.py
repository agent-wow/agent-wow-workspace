#!/usr/bin/env python3
"""Agent access policy: which accounts and characters the agent may touch.

    agent-policy.py account <account>            exit 0 if the account is allowed
    agent-policy.py char <account> <character>   exit 0 if the character is allowed
    agent-policy.py accounts                     print allowed account names
    agent-policy.py show                         print the whole policy

The policy lives in agent-policy.json at the workspace root. An account that is
not listed is off-limits: the agent must not log in, start a session, create or
delete characters on it. A listed account can narrow access with "characters";
"*" means every character. Missing "characters" means none.

Exit codes: 0 allowed, 3 denied, 2 policy file missing/invalid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent.parent / "agent-policy.json"


def norm(value: str) -> str:
    return (value or "").strip().lower()


def load() -> dict:
    try:
        data = json.loads(POLICY_FILE.read_text())
    except FileNotFoundError:
        print(f"agent policy: {POLICY_FILE} is missing; denying all access", file=sys.stderr)
        raise SystemExit(2)
    except json.JSONDecodeError as exc:
        print(f"agent policy: {POLICY_FILE} is invalid: {exc}", file=sys.stderr)
        raise SystemExit(2)
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        print(f"agent policy: {POLICY_FILE} has no 'accounts' map", file=sys.stderr)
        raise SystemExit(2)
    return data


def allowed_accounts() -> dict[str, dict]:
    return {norm(name): (entry or {}) for name, entry in load()["accounts"].items()}


def account_allowed(account: str) -> bool:
    return norm(account) in allowed_accounts()


def character_allowed(account: str, character: str) -> bool:
    entry = allowed_accounts().get(norm(account))
    if entry is None:
        return False
    chars = entry.get("characters")
    if not isinstance(chars, list):
        return False
    wanted = norm(character)
    return any(norm(c) == "*" or norm(c) == wanted for c in chars)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "account":
        if len(argv) != 3:
            print("usage: agent-policy.py account <account>", file=sys.stderr)
            return 2
        if account_allowed(argv[2]):
            return 0
        print(
            f"agent policy: account '{argv[2]}' is off-limits "
            f"({POLICY_FILE.name}); it belongs to the user",
            file=sys.stderr,
        )
        return 3
    if cmd == "char":
        if len(argv) != 4:
            print("usage: agent-policy.py char <account> <character>", file=sys.stderr)
            return 2
        if character_allowed(argv[2], argv[3]):
            return 0
        print(
            f"agent policy: character '{argv[3]}' on account '{argv[2]}' is off-limits "
            f"({POLICY_FILE.name})",
            file=sys.stderr,
        )
        return 3
    if cmd == "accounts":
        for name in sorted(allowed_accounts()):
            print(name)
        return 0
    if cmd == "show":
        print(json.dumps(load(), indent=2, ensure_ascii=False))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
