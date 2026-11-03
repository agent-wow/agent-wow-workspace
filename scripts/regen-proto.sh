#!/usr/bin/env bash
# Regenerate src/api/*.go and module.pb for one module.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE="${1:-}"

if [[ -z "$MODULE" ]]; then
	echo "usage: scripts/regen-proto.sh <module>" >&2
	exit 2
fi
if ! command -v protoc >/dev/null 2>&1; then
	echo "protoc is missing; install it with: brew install protobuf" >&2
	exit 1
fi

cd "$ROOT/modules/$MODULE"
make generate
echo "regenerated modules/$MODULE"
