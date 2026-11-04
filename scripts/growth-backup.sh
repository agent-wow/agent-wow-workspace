#!/usr/bin/env bash
# Consistent host/VM growth SQLite backup and restore.
#
#   scripts/growth-backup.sh                       # host state snapshot
#   scripts/growth-backup.sh --vm                  # aw-growth VM -> host snapshot
#   scripts/growth-backup.sh --vm --dest DIR       # VM snapshot to DIR
#   scripts/growth-backup.sh --restore DIR         # restore host state
#   scripts/growth-backup.sh --vm --restore DIR    # restore VM after a rebuild
#
# SQLite's backup API produces a consistent database without copying WAL/SHM.
# The operator control/ tree and STOP are never included. VM restore refuses to
# run while growth workers hold their code/state locks.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${GROWTH_DATA_DIR:-$ROOT/data/growth}"
VM_DIR="${GROWTH_VM_DIR:-aw-growth}"
VM_MODE=0
DEST=""
TARGET=""
RESTORE=0

while [ $# -gt 0 ]; do
	case "$1" in
		--vm) VM_MODE=1; shift ;;
		--dest) DEST="${2:?--dest needs a directory}"; shift 2 ;;
		--restore) RESTORE=1; DEST="${2:?--restore needs a directory}"; shift 2 ;;
		--target) TARGET="${2:?--target needs a directory}"; shift 2 ;;
		--source) SOURCE="${2:?--source needs a directory}"; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

CLI="$ROOT/scripts/growth/cli.py"

if [ "$VM_MODE" = 1 ] && [ "$RESTORE" = 1 ]; then
	[ -n "$DEST" ] || { echo "--vm --restore needs a host backup directory" >&2; exit 2; }
	[ -f "$DEST/manifest.json" ] || { echo "backup manifest not found: $DEST" >&2; exit 1; }
	# Restoring code/state underneath a live worker can duplicate non-idempotent
	# actions. Reuse the same process/lock guard as growth sync.
	if colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		if pgrep -f "[p]ython[0-9.]* .*scripts/growth/cli[.]py worker" >/dev/null 2>&1; then
			exit 0
		fi
		dir="$HOME/$VM_DIR/data/growth"
		for f in "$dir"/state/*/*.worker.lock "$dir"/state/*.lock; do
			[ -e "$f" ] || continue
			flock -n "$f" true 2>/dev/null || exit 0
		done
		exit 1
	' 2>/dev/null; then
		echo "growth workers are running; stop them before VM restore" >&2
		exit 1
	fi
	colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		set -e
		rm -rf "$HOME/.$VM_DIR.restore"
		mkdir -p "$HOME/.$VM_DIR.restore"
	'
	COPYFILE_DISABLE=1 tar --no-xattrs -C "$DEST" -czf - . |
		colima ssh -- env VM_DIR="$VM_DIR" sh -c '
			set -e
			tar xzf - -C "$HOME/.$VM_DIR.restore"
			cd "$HOME/$VM_DIR"
			python3 -I scripts/growth/cli.py backup restore \
				--source "$HOME/.$VM_DIR.restore" --target data/growth
			rm -rf "$HOME/.$VM_DIR.restore"
		'
	exit 0
fi

if [ "$VM_MODE" = 1 ]; then
	backup_id="$(date +%Y%m%d-%H%M%S)-$$"
	if [ -z "$DEST" ]; then
		DEST="$ROOT/data/growth-backups/$backup_id"
	fi
	[ ! -e "$DEST" ] || { echo "backup destination already exists: $DEST" >&2; exit 1; }
	# Create the SQLite-consistent snapshot inside the VM first, then copy only
	# the sealed backup (never live DB/WAL files) to the host.
	colima ssh -- env VM_DIR="$VM_DIR" BACKUP_ID="$backup_id" sh -c '
		set -e
		cd "$HOME/$VM_DIR"
		python3 -I scripts/growth/cli.py backup create \
			--source data/growth --dest "data/growth-backups/$BACKUP_ID"
	'
	STAGE="$DEST.tmp.$$"
	trap 'rm -rf "$STAGE"' EXIT
	mkdir -p "$STAGE"
	colima ssh -- env VM_DIR="$VM_DIR" BACKUP_ID="$backup_id" sh -c '
		tar -C "$HOME/$VM_DIR/data/growth-backups/$BACKUP_ID" -czf - .
	' | tar -C "$STAGE" -xzf -
	mv "$STAGE" "$DEST"
	trap - EXIT
	echo "VM growth backup stored at $DEST"
	exit 0
fi

if [ "$RESTORE" = 1 ]; then
	exec python3 -I "$CLI" backup restore \
		--source "$DEST" --target "${TARGET:-$SOURCE}"
fi

if [ -z "$DEST" ]; then
	DEST="$SOURCE-backups/$(date +%Y%m%d-%H%M%S)"
fi
exec python3 -I "$CLI" backup create --source "$SOURCE" --dest "$DEST"
