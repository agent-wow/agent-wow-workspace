#!/usr/bin/env bash
set -uo pipefail
WORKSPACE=${1:?workspace argument required}
[[ -f /etc/sandbox-persistent.sh && ${WOW_SANDBOX_WORKSPACE:-} == "$WORKSPACE" ]] || exit 1
cd -- "$WORKSPACE"
failures=0
check() {
  printf '\nChecking %s...\n' "$1"; shift
  if ! "$@"; then failures=$((failures + 1)); fi
}
check Go bash -c '[[ $(go version) == "go version go1.27.1 linux/"* ]] && go version'
check protoc bash -c '[[ $(protoc --version) == "libprotoc 36.2" ]] && protoc --version'
check agent-wow bash -c 'go version -m "$(command -v agent-wow)" | awk '\''$1=="mod" && $2=="github.com/agent-wow/agent-wow" && $3=="v0.1.0" {found=1} END {exit !found}'\'' && agent-wow --help >/dev/null'
check 'private Docker Engine' docker info --format '{{.ID}}'
check 'Docker Compose' docker compose version
check internet curl --fail --silent --show-error --max-time 20 --output /dev/null https://go.dev/
check 'AzerothCore host ports' python3 - <<'PY'
import socket
for port in (3724, 8085):
    with socket.create_connection(("host.docker.internal", port), timeout=5):
        print(f"host.docker.internal:{port} reachable")
with socket.create_connection(("127.0.0.1", 8085), timeout=5):
    print("sandbox loopback worldserver forwarder reachable")
PY
printf '\nChecking gameplay authentication...\n'
if agent-wow auth status; then
  check realm agent-wow realm status
  check characters agent-wow char list
else
  printf 'Initialize an account with agent-wow auth init, then agent-wow auth login, in ./.sandbox/sbx.sh shell.\n' >&2
  failures=$((failures + 1))
fi
if [[ $failures -ne 0 ]]; then printf '%s check(s) failed; do not start gameplay.\n' "$failures" >&2; exit 1; fi
printf 'All checks passed.\n'
