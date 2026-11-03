#!/usr/bin/env bash
# Push the workspace (single source of truth) into the Colima VM.
# The VM copy under $HOME/aw is a runtime replica; never edit it directly.
#
# AGENT_WOW_VM_DIR selects the VM directory (default: aw). Each account uses
# its own directory (aw-<account>), so the sessions of several accounts never
# overwrite each other.
#
# Credential scope: an account sync ships only accounts/<that-account>/, never
# the whole accounts tree. Nested .git directories are excluded.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_DIR="${AGENT_WOW_VM_DIR:-aw}"
FORCE="${1:-}"

SYNC_ACCOUNT="${ACCOUNT:-}"
if [ -z "$SYNC_ACCOUNT" ]; then
	case "$VM_DIR" in
		aw) SYNC_ACCOUNT=doumoman ;;
		aw-*) SYNC_ACCOUNT="${VM_DIR#aw-}" ;;
	esac
fi

# A session that is playing inside this VM directory keeps its data/ and logs/
# directory; refuse to swap the directory out from under it.
if [ "$FORCE" != "--force" ]; then
	if colima ssh -- env VM_DIR="$VM_DIR" sh -c '
		dir="$HOME/$VM_DIR"
		for pid in $(pgrep -f "[a]gent-wow.*char play" 2>/dev/null); do
			cwd=$(readlink /proc/$pid/cwd 2>/dev/null || true)
			[ "$cwd" = "$dir" ] && exit 0
		done
		exit 1
	' 2>/dev/null; then
		echo "a session is running in the VM directory $VM_DIR; run scripts/session.sh stop first, or pass --force" >&2
		exit 1
	fi
fi

items=(config.yaml agent-policy.json config lib modules scripts AGENTS.md README.md)
if [ -n "$SYNC_ACCOUNT" ] && [ -d "$ROOT/accounts/$SYNC_ACCOUNT" ]; then
	items+=("accounts/$SYNC_ACCOUNT")
fi

# Stage a copy first: the workspace may be edited while the archive is being
# written, and a file that disappears mid-archive truncates the gzip stream.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/aw-sync.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
for item in "${items[@]}"; do
	[ -e "$ROOT/$item" ] || continue
	mkdir -p "$(dirname "$STAGE/$item")"
	cp -R "$ROOT/$item" "$STAGE/$item"
done

# Never ship VCS metadata or Python bytecode into the runtime replica.
find "$STAGE" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# data/ holds the live auth session and logs/ the current session log. Both are
# runtime state that lives only in the VM, so keep them across a sync. Account
# session dirs (accounts/*/data) are copied from the host: the host owns the
# credentials and the session key, the VM only replays them.
COPYFILE_DISABLE=1 tar --no-xattrs -C "$STAGE" -czf - . 2>/dev/null |
	colima ssh -- env SYNC_ACCOUNT="$SYNC_ACCOUNT" sh -c '
		set -e
		cd $HOME
		rm -rf .'"$VM_DIR"'.stage
		mkdir .'"$VM_DIR"'.stage
		tar xzf - -C .'"$VM_DIR"'.stage
		for keep in data logs; do
			if [ -d '"$VM_DIR"'/$keep ]; then
				rm -rf .'"$VM_DIR"'.stage/$keep
				mv '"$VM_DIR"'/$keep .'"$VM_DIR"'.stage/$keep
			fi
		done
		if [ -n "$SYNC_ACCOUNT" ] && [ -d .'"$VM_DIR"'.stage/accounts/$SYNC_ACCOUNT ]; then
			chmod 700 .'"$VM_DIR"'.stage/accounts/$SYNC_ACCOUNT \
				.'"$VM_DIR"'.stage/accounts/$SYNC_ACCOUNT/config \
				.'"$VM_DIR"'.stage/accounts/$SYNC_ACCOUNT/data 2>/dev/null || true
			find .'"$VM_DIR"'.stage/accounts/$SYNC_ACCOUNT -type f \
				-exec chmod 600 {} + 2>/dev/null || true
		fi
		rm -rf '"$VM_DIR"'
		mv .'"$VM_DIR"'.stage '"$VM_DIR"'
	'

echo "synced $ROOT -> colima:\$HOME/$VM_DIR"
