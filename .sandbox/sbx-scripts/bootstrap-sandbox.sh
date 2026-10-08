#!/usr/bin/env bash
set -Eeuo pipefail
WORKSPACE=${1:?workspace argument required}
[[ -f /etc/sandbox-persistent.sh && ${WOW_SANDBOX_WORKSPACE:-} == "$WORKSPACE" ]] || {
  printf 'Run this installer through ./.sandbox/sbx.sh init, inside Docker Sandboxes.\n' >&2; exit 1;
}
cd -- "$WORKSPACE"
bootstrap_dir=$(mktemp -d /tmp/wow-bootstrap.XXXXXX)
trap 'rm -rf -- "$bootstrap_dir"' EXIT

missing_tools=false
for tool in curl python3 unzip socat ss nc gcc g++ make git sha256sum tar mount mountpoint; do
  command -v "$tool" >/dev/null || missing_tools=true
done
if "$missing_tools"; then
  # The template may still be provisioning packages just after its first boot.
  # apt's DPkg timeout does not cover the package-list lock used by update.
  updated=false
  for attempt in {1..24}; do
    if sudo apt-get -o DPkg::Lock::Timeout=120 update >"$bootstrap_dir/apt-update.log" 2>&1; then
      updated=true; break
    fi
    if ! grep -qE 'Could not get lock|Unable to lock directory' "$bootstrap_dir/apt-update.log"; then break; fi
    sleep 5
  done
  "$updated" || { cat "$bootstrap_dir/apt-update.log" >&2; exit 1; }
  sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends ca-certificates curl python3 unzip socat iproute2 netcat-openbsd build-essential git util-linux
fi

case $(uname -m) in
  x86_64)
    GO_ARCH=amd64; PROTOC_ARCH=x86_64
    GO_SHA=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
    PROTOC_SHA=121f6c7afe1d4d0e3ea6aab9432038599250134cbf4474cb1167d2c7decd4278
    ;;
  aarch64|arm64)
    GO_ARCH=arm64; PROTOC_ARCH=aarch_64
    GO_SHA=3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec
    PROTOC_SHA=8b8f18bd2b30346efbc698dd5a73dd7c805f3ef8380f6dfc95c768f3f1852f6a
    ;;
  *) printf 'Unsupported architecture: %s\n' "$(uname -m)" >&2; exit 1 ;;
esac

fetch_verified() {
  local url=$1 path=$2 digest=$3
  if [[ ! -f "$path" ]] || ! printf '%s  %s\n' "$digest" "$path" | sha256sum --check --status; then
    curl --fail --location --retry 3 --connect-timeout 15 --max-time 600 "$url" -o "$path.part"
    printf '%s  %s\n' "$digest" "$path.part" | sha256sum --check --status || {
      rm -f -- "$path.part"; printf 'Download checksum mismatch: %s\n' "$url" >&2; return 1;
    }
    mv -- "$path.part" "$path"
  fi
}

if [[ ! -x /usr/local/go/bin/go ]] || [[ $(/usr/local/go/bin/go version) != 'go version go1.27.1 linux/'"$GO_ARCH" ]]; then
  printf 'Installing Go 1.27.1\n'
  fetch_verified "https://go.dev/dl/go1.27.1.linux-$GO_ARCH.tar.gz" "$bootstrap_dir/go.tar.gz" "$GO_SHA"
  tar -xzf "$bootstrap_dir/go.tar.gz" -C "$bootstrap_dir"
  sudo rm -rf -- /usr/local/go
  sudo mv -- "$bootstrap_dir/go" /usr/local/go
fi
if [[ ! -x /usr/local/bin/protoc ]] || [[ $(/usr/local/bin/protoc --version) != 'libprotoc 36.2' ]]; then
  printf 'Installing protoc 36.2\n'
  fetch_verified "https://github.com/protocolbuffers/protobuf/releases/download/v36.2/protoc-36.2-linux-$PROTOC_ARCH.zip" "$bootstrap_dir/protoc.zip" "$PROTOC_SHA"
  unzip -oq "$bootstrap_dir/protoc.zip" -d "$bootstrap_dir/protoc"
  sudo install -m 0755 "$bootstrap_dir/protoc/bin/protoc" /usr/local/bin/protoc
  sudo mkdir -p /usr/local/include
  sudo cp -R -- "$bootstrap_dir/protoc/include/." /usr/local/include/
fi
if [[ ! -x /usr/local/bin/agent-wow ]] || ! go version -m /usr/local/bin/agent-wow | awk '$1 == "mod" && $2 == "github.com/agent-wow/agent-wow" && $3 == "v0.1.0" {found=1} END {exit !found}'; then
  printf 'Installing agent-wow v0.1.0\n'
  GOBIN="$bootstrap_dir/bin" GOTOOLCHAIN=local go install github.com/agent-wow/agent-wow@v0.1.0
  sudo install -m 0755 "$bootstrap_dir/bin/agent-wow" /usr/local/bin/agent-wow
fi
python3 "$WORKSPACE/.sandbox/sbx-scripts/sandbox_runtime.py" prepare "$WORKSPACE"
if [[ ${WOW_SANDBOX_AGENT:-} == codex ]]; then
  # Prepare guest-private runtime directories before the updated CLI can
  # create daemon sockets or helper symlinks in the shared Codex home.
  printf 'Updating Codex\n'
  codex update
  codex --version
fi
go version
protoc --version
docker compose version
printf 'Bootstrap complete. Gameplay credentials are initialized separately.\n'
