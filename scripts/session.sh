#!/usr/bin/env bash
# Small wrapper around an agent-wow session running inside the Colima VM.
#
#   session.sh start [character]   start a gameplay session (default: Reachbot)
#   session.sh stop                logout, then kill that session's client
#   session.sh rpc <method> [json] call a module RPC through the VM
#   session.sh status              show client processes and module containers
#   session.sh logs [lines]        tail the session log
#
# Multi-account: set ACCOUNT=<name> (see scripts/account.sh ls). Every account
# gets its own VM directory (aw-<name>), its own worldrpc port (from the
# account config) and its own session log, so several characters can play side
# by side. Without ACCOUNT the legacy single-account layout ($HOME/aw, port
# 8086) is used.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY="$ROOT/scripts/agent-policy.py"
# Manual override for the human operator's own accounts. The console sets it
# only for accounts outside agent-policy.json; the keeper and other automated
# callers never set it, so autonomous access stays policy-bound.
MANUAL="${AGENT_WOW_MANUAL:-0}"
ACCOUNT="${ACCOUNT:-}"
ACCOUNT="$(printf '%s' "$ACCOUNT" | tr '[:upper:]' '[:lower:]')"
if [ -n "$ACCOUNT" ] && [ -z "${AGENT_WOW_VM_DIR:-}" ]; then
	VM_DIR="aw-$ACCOUNT"
else
	VM_DIR="${AGENT_WOW_VM_DIR:-aw}"
fi
CMD="${1:-}"

conf_file() {
	if [ -n "$ACCOUNT" ]; then
		echo "$ROOT/accounts/$ACCOUNT/agent-wow.yaml"
	else
		echo "$ROOT/config.yaml"
	fi
}

conf_port() {
	awk '/^worldrpc:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&$1=="port:"{print $2;exit}' "$1" 2>/dev/null || true
}

PORT="${AGENT_WOW_WORLDRPC_PORT:-$(conf_port "$(conf_file)")}"
PORT="${PORT:-8086}"

rpc_raw() {
	local method="$1" params="${2-}"
	[ -n "$params" ] || params='{}'
	local payload b64
	payload="$(python3 -c '
import json, sys
print(json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": sys.argv[1],
    "params": json.loads(sys.argv[2]),
}, separators=(",", ":")))
' "$method" "$params")"
	b64="$(printf '%s' "$payload" | base64)"
	colima ssh -- sh -c "curl -s --max-time 40 http://127.0.0.1:$PORT/rpc \
		-H 'Content-Type: application/json' \
		-d \"\$(printf %s '$b64' | base64 -d)\""
}

case "$CMD" in
start)
	CHAR="${2:-Reachbot}"
	# The legacy (no ACCOUNT) layout belongs to the doumoman account.
	if [ "$MANUAL" != "1" ]; then
		python3 -I "$POLICY" char "${ACCOUNT:-doumoman}" "$CHAR" || exit $?
	fi
	ACCOUNT="$ACCOUNT" AGENT_WOW_VM_DIR="$VM_DIR" "$ROOT/scripts/sync-to-vm.sh"
	colima ssh -- env CHAR="$CHAR" ACCOUNT="$ACCOUNT" VM_DIR="$VM_DIR" sh -c '
		cd $HOME/$VM_DIR || exit 1
		mkdir -p logs
		CONF=""
		if [ -n "$ACCOUNT" ]; then
			CONF="accounts/$ACCOUNT/agent-wow.yaml"
			[ -f "$CONF" ] || { echo "unknown account: $ACCOUNT (see scripts/account.sh ls)" >&2; exit 2; }
			CONF="--config $CONF"
		fi
		nohup $HOME/agent-wow $CONF char play "$CHAR" --module-timeout 5m \
			> logs/session.log 2>&1 &
		echo $! > logs/session.pid
		echo "started pid $(cat logs/session.pid)"
	'
	echo "vm=$VM_DIR account=${ACCOUNT:-default} port=$PORT"
	;;
stop)
	rpc_raw session.logout '{}' || true
	colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		dir="$HOME/$VM_DIR"
		pids=""
		for pid in $(pgrep -f "[a]gent-wow.*char play" 2>/dev/null); do
			cwd=$(readlink /proc/$pid/cwd 2>/dev/null || true)
			[ "$cwd" = "$dir" ] || [ "$cwd" = "$dir (deleted)" ] && pids="$pids $pid"
		done
		for p in $pids; do kill "$p" 2>/dev/null || true; done
		[ -n "$pids" ] && echo "killed: $pids" || echo "no session in $dir"
	'
	# Only `auth login` writes account session data. Gameplay is read-only, so
	# never copy a VM replica back over the host's per-account credentials.
	;;
rpc)
	[ -n "${2:-}" ] || { echo "usage: session.sh rpc <method> [json]" >&2; exit 2; }
	# stop/logout runs through rpc_raw directly so a session on an off-limits
	# account can still be removed.
	if [ "$MANUAL" != "1" ]; then
		python3 -I "$POLICY" account "${ACCOUNT:-doumoman}" || exit $?
	fi
	rpc_raw "$2" "${3-}"
	echo
	;;
status)
	colima ssh -- sh -c '
		echo "-- clients --"
		for pid in $(pgrep -f "[a]gent-wow.*char play" 2>/dev/null); do
			cwd=$(readlink /proc/$pid/cwd 2>/dev/null || true)
			echo "pid $pid cwd $cwd"
		done
		pgrep -f "[a]gent-wow.*char play" >/dev/null 2>&1 || echo "not running"
		echo "-- modules --"
		docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null || true
	'
	;;
logs)
	LINES="${2:-50}"
	colima ssh -- sh -c "tail -n $LINES \$HOME/$VM_DIR/logs/session.log 2>/dev/null || echo 'no session log'"
	;;
*)
	sed -n '2,17p' "$0"
	exit 2
	;;
esac
