#!/usr/bin/env bash
# Build one module (or all enabled modules) inside the Colima VM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-}"
VM_DIR="${AGENT_WOW_VM_DIR:-aw}"

AGENT_WOW_VM_DIR="$VM_DIR" "$ROOT/scripts/sync-to-vm.sh"

if [ -n "$TARGET" ]; then
	names=("$TARGET")
else
	names=()
	for dir in "$ROOT"/modules/*/; do
		[ -f "$dir/module.yaml" ] && names+=("$(basename "$dir")")
	done
fi

for name in "${names[@]}"; do
	echo "== building $name"
	colima ssh -- sh -c "cd \$HOME/$VM_DIR/modules/$name && docker compose build"
done
