#!/usr/bin/env bash
# Operator-only global STOP control for the host keeper and VM growth workers.
# The agent never runs `resume`; only a human may remove either STOP file.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_DIR="${GROWTH_VM_DIR:-aw-growth}"
STOP_FILE="${GROWTH_STOP_FILE:-$ROOT/data/growth/control/STOP}"
CMD="${1:-status}"

case "$CMD" in
stop)
	mkdir -p "$(dirname "$STOP_FILE")"
	chmod 711 "$(dirname "$STOP_FILE")"
	: >"$STOP_FILE"
	chmod 600 "$STOP_FILE"
	colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		dir="$HOME/$VM_DIR/data/growth/control"
		mkdir -p "$dir"
		chmod 711 "$dir"
		umask 077
		: >"$dir/STOP"
		chmod 600 "$dir/STOP"
	'
	echo "STOP set for host keeper and $VM_DIR workers"
	;;
resume)
	# Deliberate human action. Automated growth paths never call this command.
	rm -f "$STOP_FILE"
	colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		rm -f "$HOME/$VM_DIR/data/growth/control/STOP"
	'
	echo "STOP cleared for host keeper and $VM_DIR workers"
	;;
status)
	[ -e "$STOP_FILE" ] && host=STOP || host=clear
	colima ssh -- env VM_DIR="$VM_DIR" HOST_STATE="$host" sh -c '
		[ -e "$HOME/$VM_DIR/data/growth/control/STOP" ] && vm=STOP || vm=clear
		echo "host=$HOST_STATE vm=$vm"
	'
	;;
*)
	echo "usage: $0 stop|resume|status" >&2
	exit 2
	;;
esac
