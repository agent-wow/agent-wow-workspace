#!/usr/bin/env bash
# Push the growth runtime into the Colima VM as $HOME/aw-growth.
#
# Safety boundaries:
# - No account credentials are shipped. The growth tree has no accounts/ dir.
# - Nested .git directories and Python bytecode are excluded.
# - Code is not replaced while any growth worker is running; pass --force only
#   when you have intentionally stopped them.
# - VM data/ (including data/growth SQLite state) and logs/ are preserved.
#
# The host copy remains the single source of truth. Host-side growth SQLite
# state is backed up independently with scripts/growth-backup.sh.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_DIR=aw-growth
FORCE="${1:-}"

# Refuse to swap code out from under running workers. Process detection is the
# primary guard; live worker locks catch a process that is just starting.
if [ "$FORCE" != "--force" ]; then
	if colima ssh -- sh -c '
		if pgrep -f "[g]rowth.*worker|[s]cripts.growth.*worker" >/dev/null 2>&1; then
			exit 0
		fi
		dir="$HOME/aw-growth/data/growth"
		for f in "$dir"/state/*/*.worker.lock "$dir"/state/*.lock; do
			[ -e "$f" ] || continue
			flock -n "$f" true 2>/dev/null || exit 0
		done
		exit 1
	' 2>/dev/null; then
		echo "growth workers are running in $VM_DIR; stop them or pass --force" >&2
		exit 1
	fi
fi

# Deliberately excludes accounts/, config.yaml (which points at an account),
# data/ and logs/. config/ holds only versioned roster/content data.
items=(agent-policy.json config lib modules scripts AGENTS.md README.md)

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/aw-growth-sync.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
for item in "${items[@]}"; do
	[ -e "$ROOT/$item" ] || continue
	cp -R "$ROOT/$item" "$STAGE/" 2>/dev/null || true
done
find "$STAGE" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# The worker needs account ownership of each worldrpc port, but never needs
# auth.json, realm.json, session keys, or the account config/data directories.
python3 -I - "$ROOT" "$STAGE/config/account-ports.json" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from scripts.growth.identity import export_account_ports
export_account_ports(
    sys.argv[1] + "/accounts", sys.argv[2],
    policy_path=sys.argv[1] + "/agent-policy.json")
PY

COPYFILE_DISABLE=1 tar --no-xattrs -C "$STAGE" -czf - . 2>/dev/null |
	colima ssh -- sh -c '
		set -e
		cd $HOME
		rm -rf .aw-growth.stage
		mkdir .aw-growth.stage
		tar xzf - -C .aw-growth.stage
		for keep in data logs; do
			if [ -d aw-growth/$keep ]; then
				rm -rf .aw-growth.stage/$keep
				mv aw-growth/$keep .aw-growth.stage/$keep
			fi
		done
		rm -rf aw-growth
		mv .aw-growth.stage aw-growth
		chmod 700 aw-growth/data 2>/dev/null || true
		if [ -d aw-growth/data/growth ]; then
			chmod 700 aw-growth/data/growth 2>/dev/null || true
		fi
	'

echo "synced growth code -> colima:\$HOME/$VM_DIR (no account credentials)"
