#!/usr/bin/env bash
# Validate the modules/ layout and the shared library before a session.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:$HOME/go/bin:$PATH"
fail=0

# 1. Every directory under modules/ must be a valid module. Discovery errors out
#    on anything else, so catch it here with a clearer message.
for dir in "$ROOT"/modules/*/; do
	name="$(basename "$dir")"
	if [[ ! "$name" =~ ^[a-z][a-z0-9_-]*$ || "$name" == "session" ]]; then
		echo "invalid module directory name: $name" >&2
		fail=1
	fi
	if [[ ! -f "$dir/module.yaml" ]]; then
		echo "missing module.yaml: modules/$name" >&2
		fail=1
	fi
	if [[ ! -f "$dir/module.pb" ]]; then
		echo "missing module.pb: modules/$name" >&2
		fail=1
	fi
done

# 2. Formatting.
if command -v gofmt >/dev/null 2>&1; then
	unformatted="$(gofmt -l "$ROOT/lib" "$ROOT/modules" 2>/dev/null || true)"
	if [[ -n "$unformatted" ]]; then
		echo "gofmt needed:" >&2
		echo "$unformatted" >&2
		fail=1
	fi
else
	echo "gofmt not found; skipping formatting check" >&2
fi

# 3. Shared library tests (each library is its own Go module).
for lib in wow events; do
	if [[ -f "$ROOT/lib/$lib/go.mod" ]]; then
		(cd "$ROOT/lib/$lib" && GOPROXY=https://goproxy.cn,direct go test ./... >/dev/null) || {
			echo "lib/$lib tests failed" >&2
			fail=1
		}
	fi
done

# 4. Let agent-wow validate the manifests itself.
if command -v agent-wow >/dev/null 2>&1; then
	(cd "$ROOT" && agent-wow module list >/dev/null) || {
		echo "agent-wow module list failed" >&2
		fail=1
	}
else
	echo "agent-wow not on PATH; skipping manifest validation" >&2
fi

if [[ "$fail" -ne 0 ]]; then
	exit 1
fi
echo "modules OK"
