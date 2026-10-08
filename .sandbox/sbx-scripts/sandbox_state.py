#!/usr/bin/env python3
"""Host-side validation of a Docker Sandboxes v0.46+ direct workspace."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def sandbox_name(workspace):
    root = Path(workspace).resolve(strict=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")[:30] or "workspace"
    return f"wow-{slug}-{hashlib.sha256(os.fsencode(root)).hexdigest()[:12]}"


def response(text):
    # The CLI can print a daemon-start notice before its JSON response.
    offsets = [text.find(char) for char in "[{" if char in text]
    if not offsets:
        raise ValueError("Docker Sandboxes did not return JSON")
    value, _ = json.JSONDecoder().raw_decode(text[min(offsets):])
    return value


def supported_agents(help_text):
    match = re.search(r"^Available agents: (.+)$", help_text, re.M)
    if not match:
        raise ValueError("Cannot read supported agents from sbx run --help; unsupported sbx output")
    agents = [item.strip() for item in match[1].split(",")]
    if not agents or any(not re.fullmatch(r"[a-z][a-z0-9-]*", agent) for agent in agents):
        raise ValueError("Invalid supported-agent list from sbx run --help")
    return agents


def guard_workspace(workspace):
    root = Path(workspace).resolve(strict=True)
    def walk_error(error):
        raise ValueError(f"Cannot inspect workspace: {error}")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    path.resolve().relative_to(root)
                except (ValueError, RuntimeError):
                    raise ValueError(f"Symlink escapes workspace: {path.relative_to(root)}") from None
            elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError(f"Multiply linked file: {path.relative_to(root)}; use an independent GitHub clone")
            elif stat.S_ISSOCK(info.st_mode) or stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode):
                raise ValueError(f"Host socket/device cannot be shared: {path.relative_to(root)}")
    # These paths must be private to this checkout, not aliases to another tree.
    for relative in ("data", "data/codex", ".sandbox", ".sandbox/codex", ".sandbox/logs", "config", "logs"):
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"Managed directory must not be a symlink: {relative}")


def validate_spec(metadata, workspace, name, agent):
    spec = metadata.get("Spec", {})
    expected = {
        "WorkspaceDir": str(Path(workspace).resolve(strict=True)),
        "RuntimeName": name,
        "AgentName": agent,
        "Skills": "off",
        "SkillsResolved": True,
        "EnableVirtiofsCache": False,
        "Nested": False,
        "Template": "",
    }
    for key, value in expected.items():
        if spec.get(key) != value:
            raise ValueError(f"Unsafe/incompatible sandbox: {key} must be {value!r}. Stop and recreate this sandbox")
    for key in ("AdditionalWorkspaces", "SourceRepoDir", "SSHAgentSocketPath", "Display", "GPU", "USBDevices", "RuntimeKits", "KitRegistry"):
        if key not in spec or spec[key]:
            raise ValueError(f"Unsafe/incompatible sandbox: unexpected {key}. Stop and recreate this sandbox")
    if spec.get("KitUsage") != "[]":
        raise ValueError("Unexpected sandbox kits; only built-in agent templates are supported")
    environment = spec.get("Environment", {})
    if environment.get("WOW_SANDBOX_AGENT", agent) != agent:
        raise ValueError("Sandbox environment does not match its agent template")
    if agent == "codex" and environment.get("CODEX_HOME") not in {
            str(Path(workspace) / ".sandbox/codex"), str(Path(workspace) / "data/codex")}:
        raise ValueError("Sandbox Codex home must be .sandbox/codex or the legacy data/codex directory")


def validate_settings(settings):
    values = {item["key"]: item["value"] for item in settings}
    if values.get("clipboard.imagePaste") is not False:
        raise ValueError("Disable host clipboard image access: sbx settings set clipboard.imagePaste false")
    if values.get("ssh.agentForwardingEnabled") is not False and values.get("ssh.agentSocketPath") != "":
        raise ValueError("A fixed host SSH agent is configured. Disable forwarding with sbx settings set ssh.agentForwardingEnabled false and restart the daemon")


def main():
    command, *args = sys.argv[1:]
    if command == "name":
        print(sandbox_name(args[0]))
    elif command == "supported-agent":
        agents = supported_agents(sys.stdin.read())
        if args[0] not in agents:
            raise ValueError(f"Unsupported agent {args[0]!r}. Available agents: {', '.join(agents)}")
    elif command == "agent":
        agent = response(sys.stdin.read())["agent"]
        if not isinstance(agent, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", agent):
            raise ValueError("Invalid sandbox agent in inventory")
        print(agent)
    elif command == "version":
        match = re.search(r"v(\d+)\.(\d+)\.(\d+)", sys.stdin.read())
        if not match or tuple(map(int, match.groups())) < (0, 46, 0):
            raise ValueError("Install Docker Sandboxes (sbx) v0.46.0 or newer")
    elif command == "guard":
        guard_workspace(args[0])
    elif command == "settings":
        validate_settings(response(sys.stdin.read()))
    elif command == "mcp":
        data = response(sys.stdin.read())
        if "servers" not in data or data["servers"]:
            raise ValueError("Host MCP servers are registered. This isolated launcher requires an empty host MCP registry; inspect with sbx mcp ls")
    elif command == "missing-policy":
        data = response(sys.stdin.read())
        allowed = set()
        for rule in data["rules"]:
            if (rule.get("scope") == f"sandbox:{args[0]}" and rule.get("decision") == "allow"
                    and rule.get("status") == "active" and "net:connect:tcp" in rule.get("actions", [])):
                allowed.update(rule.get("resources", []))
        for resource in ("**", "localhost:3724", "localhost:8085"):
            if resource not in allowed:
                print(resource)
    elif command == "entry":
        data = response(sys.stdin.read())
        matches = [item for item in data["sandboxes"] if item["name"] == args[0]]
        if not matches:
            return 3
        if len(matches) != 1:
            raise ValueError("Ambiguous sandbox inventory")
        print(json.dumps(matches[0], indent=2))
    elif command == "metadata-path":
        match = re.search(r"^Socket: (.+/sandboxd\.sock)(?: .*)?$", sys.stdin.read(), re.M)
        if not match:
            raise ValueError("Cannot locate native sandbox metadata; unsupported sbx daemon status output")
        print(Path(match[1]).parent / "runtimes" / f"{args[0]}.json")
    elif command == "validate":
        validate_spec(json.loads(Path(args[0]).read_text()), args[1], args[2], args[3])
    elif command == "sessions":
        root = Path(args[0]).resolve(strict=True)
        home = root / ".sandbox/codex"
        print(f"\nWorkspace: {root}\nCodex home: {home}\nSessions: {home / 'sessions'}")
    else:
        raise ValueError(f"Unknown helper command: {command}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"sandbox: {error}", file=sys.stderr)
        sys.exit(1)
