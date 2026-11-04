#!/usr/bin/env bash
# Keep the leveling run alive on the host.
#
# The private worldserver has been closing the connection (EOF) every 15-30
# minutes, and the agent stack exits when that happens. This loop restarts
# whichever half is missing and stops once the target level is in the log.
#
# Session actions go through session-keeper.py's durable intent; this script
# never starts or stops a session directly. An operator STOP file blocks both
# session and agent restarts.
#
# Usage:
#   scripts/leveling-supervisor.sh &          # or nohup ... > logs/supervisor.log 2>&1 &
#   TARGET_LEVEL=6 scripts/leveling-supervisor.sh
set -uo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHARACTER="${CHARACTER:-Reachbot}"
TARGET="${TARGET_LEVEL:-10}"
POLL="${POLL_SECONDS:-20}"
VM_DIR="${AGENT_WOW_VM_DIR:-aw}"
ACCOUNT="${ACCOUNT:-}"
if [ -z "$ACCOUNT" ]; then
	case "$VM_DIR" in
		aw) ACCOUNT=doumoman ;;
		aw-*) ACCOUNT="${VM_DIR#aw-}" ;;
	esac
fi
KEEPER="$ROOT/scripts/session-keeper.py"
STOP_FILE="${GROWTH_STOP_FILE:-$ROOT/data/growth/STOP}"
mkdir -p "$ROOT/logs"

log() { echo "$(date +%H:%M:%S) $*"; }

stop_set() { [ -e "$STOP_FILE" ]; }

ensure_keeper() {
	if ! pgrep -f "[s]ession-keeper.py" >/dev/null 2>&1; then
		nohup python3 -I "$KEEPER" >>"$ROOT/logs/session-keeper.log" 2>&1 &
	fi
}

request_session() {
	python3 -I "$KEEPER" desired "$ACCOUNT" online "$CHARACTER"
	ensure_keeper
}

agent_running() {
	colima ssh -- pgrep -f "scripts/leveling[-]agent[.]py" >/dev/null 2>&1
}

session_running() {
	colima ssh -- pgrep -f "[a]gent-wow char play" >/dev/null 2>&1
}

target_reached() {
	local offset
	offset=$(<"$ROOT/logs/leveling.offset")
	colima ssh -- sh -c '
		tail -c "+$(($1 + 1))" "$2" 2>/dev/null | grep -q "target level reached"
	' sh "$offset" '$HOME/'"$VM_DIR"'/logs/leveling.log'
}

colima ssh -- sh -c 'wc -c < "$1" 2>/dev/null || printf 0' sh '$HOME/'"$VM_DIR"'/logs/leveling.log' >"$ROOT/logs/leveling.offset"

log "supervisor start: character=$CHARACTER account=$ACCOUNT target=$TARGET"
while true; do
	if target_reached; then
		log "target level reached; supervisor stopping"
		exit 0
	fi

	if stop_set; then
		log "operator STOP present; not restarting session or agent"
		sleep "$POLL"
		continue
	fi

	if ! session_running; then
		log "session down; writing online intent"
		request_session >>"$ROOT/logs/supervisor.log" 2>&1
		sleep 20
		continue
	fi

	if ! agent_running; then
		log "agent down; starting"
		colima ssh -- env TARGET="$TARGET" VM_DIR="$VM_DIR" sh -c '
			cd "$HOME/$VM_DIR" || exit 1
			echo "=== supervisor restart $(date +%H:%M:%S) ===" >> logs/leveling.log
			nohup python3 scripts/leveling-agent.py "$TARGET" >> logs/leveling.log 2>&1 &
		' >>"$ROOT/logs/supervisor.log" 2>&1
	fi

	sleep "$POLL"
done
