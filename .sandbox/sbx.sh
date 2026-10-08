#!/usr/bin/env bash
set -Eeuo pipefail

SANDBOX_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)
WORKSPACE=$(cd -- "$SANDBOX_DIR/.." && pwd -P)
SANDBOX_SCRIPTS_DIR="$SANDBOX_DIR/sbx-scripts"
STATE_HELPER="$SANDBOX_SCRIPTS_DIR/sandbox_state.py"

die() { printf 'sandbox: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: ./.sandbox/sbx.sh COMMAND

  init AGENT [--cpus N] [--memory SIZE]  Create/bootstrap (defaults: 4 CPUs, 8g)
  run [-- AGENT_ARGS...]                 Run the agent selected during init
  shell                                  Open an interactive sandbox shell
  status                                 Show state and Codex session paths
  check                                  Check tools, network, and gameplay auth
  rm                                     Remove sandbox; keep workspace files

Choose an agent with init; run reuses that selection.
See sbx run --help for available agents.
To change agents, remove the sandbox and initialize it again.
EOF
}

[[ $# -gt 0 ]] || { usage; exit 2; }
COMMAND=$1; shift
CPUS=4
MEMORY=8g
REQUESTED_AGENT=
AGENT=
case "$COMMAND" in
  init)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --cpus) [[ $# -ge 2 ]] || die '--cpus needs a value'; CPUS=$2; shift 2 ;;
        --memory) [[ $# -ge 2 ]] || die '--memory needs a value'; MEMORY=$2; shift 2 ;;
        -*) die "Unknown init option: $1" ;;
        *) [[ -n $1 && -z $REQUESTED_AGENT ]] || die 'init accepts one non-empty agent name'; REQUESTED_AGENT=$1; shift ;;
      esac
    done
    [[ -n $REQUESTED_AGENT ]] || die 'Use: ./.sandbox/sbx.sh init AGENT [--cpus N] [--memory SIZE]'
    [[ "$CPUS" =~ ^[1-9][0-9]*$ ]] || die '--cpus must be a positive integer'
    [[ "$MEMORY" =~ ^[1-9][0-9]*[mMgG]$ ]] || die '--memory must be a size such as 512m or 4g'
    ;;
  run)
    [[ $# -eq 0 || $1 == -- ]] || die 'Put agent arguments after --'
    if [[ ${1:-} == -- ]]; then shift; fi
    ;;
  shell|status|check|rm) [[ $# -eq 0 ]] || die "$COMMAND takes no arguments" ;;
  help|-h|--help) usage; exit 0 ;;
  *) usage >&2; die "Unknown command: $COMMAND" ;;
esac

[[ $(uname -s) == Linux ]] || die 'Local Docker Sandboxes requires Linux for this launcher'
for tool in python3 sbx readlink; do command -v "$tool" >/dev/null || die "Install $tool on the host"; done
SANDBOX_NAME=$(python3 "$STATE_HELPER" name "$WORKSPACE")
SANDBOX_CODEX_HOME="$WORKSPACE/.sandbox/codex"

# SSH forwarding is selected by the client that creates/joins a sandbox. Never
# pass the host agent socket. Check fixed daemon-side sockets separately below.
sbx_safe() { env -u SSH_AUTH_SOCK DOCKER_SANDBOXES_ENABLE_VIRTIOFS_CACHE=0 sbx "$@"; }
state() { python3 "$STATE_HELPER" "$@"; }
validate_agent() { sbx_safe run --help | state supported-agent "$1"; }
[[ -z $REQUESTED_AGENT ]] || validate_agent "$REQUESTED_AGENT"

list_entry() { sbx_safe ls --json | state entry "$SANDBOX_NAME"; }
validate_existing() {
  local runtime_path
  runtime_path=$(sbx_safe daemon status | state metadata-path "$SANDBOX_NAME")
  state validate "$runtime_path" "$WORKSPACE" "$SANDBOX_NAME" "$AGENT"
}
use_existing() {
  AGENT=$(printf '%s\n' "$1" | state agent)
  validate_agent "$AGENT"
  configure_agent_env
  validate_existing
  if [[ -n $REQUESTED_AGENT && $REQUESTED_AGENT != "$AGENT" ]]; then
    die "Sandbox uses $AGENT; requested $REQUESTED_AGENT. Run ./.sandbox/sbx.sh rm, then ./.sandbox/sbx.sh init $REQUESTED_AGENT to change agents (workspace files are kept)"
  fi
}
host_preflight() {
  [[ -r /dev/kvm && -w /dev/kvm ]] || die 'KVM is unavailable. Enable virtualization and add your user to the kvm group, then sign in again'
  sbx_safe version | state version
  state guard "$WORKSPACE"
  sbx_safe settings list --json | state settings
  sbx_safe mcp ls --json | state mcp
}
configure_agent_env() {
  agent_env=(
    --env "WOW_SANDBOX_AGENT=$AGENT"
    --env "WOW_SANDBOX_WORKSPACE=$WORKSPACE"
    --env "WOW_SANDBOX_NAME=$SANDBOX_NAME"
    --env "PATH=/usr/local/go/bin:/home/agent/go/bin:/home/agent/.local/bin:/usr/local/share/npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    --env "GOPATH=/home/agent/go"
    --env "GOCACHE=/home/agent/.cache/go-build"
    --env "GOBIN=/home/agent/go/bin"
    --env "TMPDIR=/tmp"
    --env "DOCKER_HOST=unix:///var/run/docker.sock"
    --env "DOCKER_CONTEXT=default"
    --env "AGENT_WOW_CONFIG_DIR=$WORKSPACE/config"
    --env "AGENT_WOW_DATA_DIR=$WORKSPACE/data"
    --env "AGENT_WOW_MODULE_DIR=$WORKSPACE/modules"
    --env "AGENT_WOW_AUTHSERVER_HOST=host.docker.internal"
    --env "AGENT_WOW_AUTHSERVER_PORT=3724"
    --env "AGENT_WOW_WORLDRPC_HOST=127.0.0.1"
    --env "AGENT_WOW_WORLDRPC_PORT=8086"
  )
  if [[ $AGENT == codex ]]; then
    agent_env+=(--env "CODEX_HOME=$SANDBOX_CODEX_HOME")
  fi
}
sandbox_exec() {
  sbx_safe exec "${agent_env[@]}" --workdir "$WORKSPACE" "$SANDBOX_NAME" "$@"
}
require_existing() {
  local entry result
  if entry=$(list_entry); then
    use_existing "$entry"
  else
    result=$?
    [[ $result -eq 3 ]] || die 'Cannot read sandbox inventory'
    die 'Sandbox not found. Run ./.sandbox/sbx.sh init AGENT first'
  fi
}
prepare_runtime() { sandbox_exec python3 "$SANDBOX_SCRIPTS_DIR/sandbox_runtime.py" prepare "$WORKSPACE"; }

case "$COMMAND" in
  init)
    host_preflight
    if entry=$(list_entry); then
      use_existing "$entry"
      printf 'Reusing %s (resource flags apply only to new sandboxes).\n' "$SANDBOX_NAME"
    else
      result=$?
      [[ $result -eq 3 ]] || die 'Cannot read sandbox inventory; creation aborted'
      AGENT=$REQUESTED_AGENT
      validate_agent "$AGENT"
      configure_agent_env
      sbx_safe create --name "$SANDBOX_NAME" --skills off --cpus "$CPUS" --memory "$MEMORY" \
        "${agent_env[@]}" "$AGENT" "$WORKSPACE"
      validate_existing
    fi
    # Scope every rule to this sandbox. Never initialize/reset global policy.
    missing_rules=$(sbx_safe policy ls "$SANDBOX_NAME" --json --type network --source local --decision allow | state missing-policy "$SANDBOX_NAME")
    while IFS= read -r resource; do
      [[ -z $resource ]] || sbx_safe policy allow network --sandbox "$SANDBOX_NAME" "$resource"
    done <<< "$missing_rules"
    mkdir -p "$SANDBOX_DIR/logs"
    sandbox_exec bash "$SANDBOX_SCRIPTS_DIR/bootstrap-sandbox.sh" "$WORKSPACE" \
      2>&1 | tee "$SANDBOX_DIR/logs/sandbox-init.log"
    prepare_runtime
    printf '\nReady: ./.sandbox/sbx.sh run\nGameplay login: ./.sandbox/sbx.sh shell, then agent-wow auth init && agent-wow auth login\n'
    ;;
  run|shell|check)
    host_preflight
    require_existing
    prepare_runtime
    case "$COMMAND" in
      run)
        # Let sbx supply each agent's native sandbox flags and authentication.
        sbx_safe run "$AGENT" --name "$SANDBOX_NAME" "${agent_env[@]}" -- "$@"
        ;;
      shell) sbx_safe exec -it "${agent_env[@]}" --workdir "$WORKSPACE" "$SANDBOX_NAME" bash --noprofile --norc ;;
      check) sandbox_exec bash "$SANDBOX_SCRIPTS_DIR/check-sandbox.sh" "$WORKSPACE" ;;
    esac
    ;;
  status)
    if entry=$(list_entry); then
      use_existing "$entry"
      printf '%s\n' "$entry"
      if [[ $AGENT == codex ]]; then
        state sessions "$WORKSPACE"
      else
        printf '\nWorkspace: %s\nAgent: %s\n' "$WORKSPACE" "$AGENT"
      fi
    else
      result=$?
      [[ $result -eq 3 ]] || die 'Cannot read sandbox inventory'
      printf 'Sandbox: %s (not initialized)\nWorkspace: %s\nInitialize: ./.sandbox/sbx.sh init AGENT\n' "$SANDBOX_NAME" "$WORKSPACE"
    fi
    ;;
  rm)
    if entry=$(list_entry); then
      use_existing "$entry"
      sbx_safe rm --force "$SANDBOX_NAME"
      printf 'Workspace files preserved: %s\n' "$WORKSPACE"
    else
      result=$?
      [[ $result -eq 3 ]] || die 'Cannot read sandbox inventory; removal aborted'
      printf 'Sandbox %s is not initialized; nothing to remove.\n' "$SANDBOX_NAME"
    fi
    ;;
esac
