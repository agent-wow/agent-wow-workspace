#!/usr/bin/env python3
"""Prepare agent-specific state and the guest-only worldserver bridge."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import tomllib


def prepare_codex_private_dirs(destination):
    # Helper symlinks and daemon/updater sockets belong to the guest. Keeping
    # them in the shared home makes the host workspace guard reject the next
    # launch (including after `codex update`). Bind only these runtime dirs;
    # configuration, credentials and session history remain in the workspace.
    for name in ("tmp", "app-server-daemon", "app-server-control"):
        private = Path.home() / ".cache/codex" / name
        target = destination / name
        if private.is_symlink() or target.is_symlink():
            raise ValueError(f"Codex {name} directory must not be a symlink")
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.mkdir(exist_ok=True, mode=0o700)
        if subprocess.run(["mountpoint", "-q", str(target)]).returncode == 0:
            if not os.path.samefile(private, target):
                raise ValueError(f"Unexpected mount at the Codex {name} directory")
            continue
        subprocess.run(["sudo", "mount", "--bind", str(private), str(target)], check=True)


def without_gateway(config):
    lines = []
    skip = False
    for line in config.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skip = stripped.startswith("[mcp_servers.")
        if not skip:
            lines.append(line)
    # Keep a valid transport shape even though the host gateway is disabled.
    gateway = tomllib.loads(config).get("mcp_servers", {}).get("mcp-gateway", {})
    url = gateway.get("url", "http://mcp-gateway.docker.internal/mcp")
    return ("".join(lines).rstrip() + "\n\n[mcp_servers.mcp-gateway]\nenabled = false\n"
            + f"url = {json.dumps(url)}\n")


def migrate_codex_home(workspace):
    source = workspace / "data/codex"
    destination = workspace / ".sandbox/codex"
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Codex home directories must not be symlinks")
    if not source.exists():
        return destination
    for process in Path("/proc").glob("[0-9]*/comm"):
        try:
            active = process.read_text().strip() == "codex"
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if active:
            raise ValueError("Stop running Codex sessions before moving data/codex to .sandbox/codex")
    if destination.exists():
        if any(destination.iterdir()):
            raise ValueError("Both data/codex and .sandbox/codex contain state; refusing to overwrite Codex sessions")
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Move the entire home together, including the SQLite catalog, WAL/SHM,
    # archived sessions and name index, so session metadata stays consistent.
    source.rename(destination)
    return destination


def prepare_codex(workspace):
    destination = migrate_codex_home(workspace)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    source = Path.home() / ".codex"
    source_config = (source / "config.toml").read_text()
    template = tomllib.loads(source_config)
    provider_name = template.get("model_provider")
    provider = template.get("model_providers", {}).get(provider_name, {})
    if provider.get("experimental_bearer_token") != "oai-oat01-proxy-managed":
        raise ValueError("The Codex template has no supported proxy-managed provider; refusing to copy credentials")
    config_path = destination / "config.toml"
    if not config_path.exists():
        config_path.write_text(without_gateway(source_config))
        config_path.chmod(0o600)
    configured = tomllib.loads(config_path.read_text())
    if configured.get("model_provider") != provider_name or configured.get("model_providers", {}).get(provider_name) != provider:
        raise ValueError("Workspace Codex configuration must preserve the template's sandbox proxy provider")
    if configured.get("mcp_servers", {}).get("mcp-gateway", {}).get("enabled") is not False:
        raise ValueError("Host MCP gateway must remain disabled in .sandbox/codex/config.toml")
    auth = json.loads((source / "auth.json").read_text())
    key = auth.get("OPENAI_API_KEY")
    if key != "proxy-managed":
        raise ValueError("Unsupported template authentication: only a proxy-managed sentinel may be shared")
    # Only copy the non-secret sentinel; the host's OAuth credentials stay in sbx.
    auth_path = destination / "auth.json"
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": key}) + "\n")
    auth_path.chmod(0o600)
    (destination / "sessions").mkdir(exist_ok=True, mode=0o700)
    prepare_codex_private_dirs(destination)


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def prepare_forwarder(workspace):
    runtime = Path.home() / ".local/state/wow-sandbox"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid_path = runtime / "world-forwarder.pid"
    command = ["socat", "TCP4-LISTEN:8085,bind=127.0.0.1,reuseaddr,fork", "TCP:host.docker.internal:8085"]
    for candidate in (pid_path, workspace / "data/sandbox/world-forwarder.pid"):
        try:
            pid = int(candidate.read_text())
            actual = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if actual[:len(command)] == [item.encode() for item in command] and listening(8085):
                if candidate != pid_path:
                    pid_path.write_text(str(pid) + "\n")
                return
        except (OSError, ValueError):
            pass
    if listening(8085):
        raise ValueError("Sandbox loopback port 8085 is occupied by another process; release it before starting the worldserver forwarder")
    with (workspace / ".sandbox/logs/world-forwarder.log").open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, close_fds=True)
    pid_path.write_text(str(process.pid) + "\n")
    for _ in range(30):
        if process.poll() is not None:
            raise ValueError("Worldserver forwarder exited; inspect .sandbox/logs/world-forwarder.log")
        if listening(8085):
            return
        time.sleep(0.1)
    raise ValueError("Worldserver forwarder did not become ready; inspect .sandbox/logs/world-forwarder.log")


def main():
    command, directory = sys.argv[1:]
    workspace = Path(directory).resolve(strict=True)
    if not Path("/etc/sandbox-persistent.sh").exists() or os.environ.get("WOW_SANDBOX_WORKSPACE") != str(workspace):
        raise ValueError("This helper must run inside the sandbox through ./.sandbox/sbx.sh")
    agent = os.environ.get("WOW_SANDBOX_AGENT")
    if not agent:
        raise ValueError("Sandbox agent is required; run this helper through ./.sandbox/sbx.sh")
    if agent == "codex" and os.environ.get("CODEX_HOME") != str(workspace / ".sandbox/codex"):
        raise ValueError("Codex home must be the shared workspace .sandbox/codex directory")
    if command != "prepare":
        raise ValueError("Unknown runtime command")
    (workspace / ".sandbox/logs").mkdir(parents=True, exist_ok=True)
    if agent == "codex":
        prepare_codex(workspace)
    prepare_forwarder(workspace)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"sandbox: {error}", file=sys.stderr)
        sys.exit(1)
