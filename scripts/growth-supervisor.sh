#!/usr/bin/env bash
# Keep session-keeper.py and the five growth workers alive.
#
# This supervisor never starts or stops a game session. session-keeper.py is
# the sole session-action owner; the supervisor only respawns the keeper and
# worker processes, and it honours the operator STOP file before any restart.
#
# Structural ownership (when launched as root with UIDs supplied):
#
#   data/growth/               operator UID, 0700
#   data/growth/state/         growth UID,   0700  (per-character checkpoints)
#   data/growth/coordinator/   coordinator UID, 0700 (shared coordination DB)
#   data/growth/control/       operator UID, 0711
#   data/growth/control/STOP   operator UID, 0600  (growth can stat, not write)
#
# Without root, the tree stays owned by the current user with 0700 modes; the
# same structural separation is available for a dedicated growth/coordinator
# UID deployment.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${GROWTH_DATA_DIR:-$ROOT/data/growth}"
STATE_DIR="$DATA_ROOT/state"
COORD_DIR="$DATA_ROOT/coordinator"
CONTROL_DIR="$DATA_ROOT/control"
STOP_FILE="${GROWTH_STOP_FILE:-$CONTROL_DIR/STOP}"

mkdir -p "$DATA_ROOT" "$STATE_DIR" "$COORD_DIR" "$CONTROL_DIR"
chmod 700 "$DATA_ROOT" "$STATE_DIR" "$COORD_DIR"

if [ "$(id -u)" = 0 ]; then
	if [ -n "${GROWTH_UID:-}" ]; then
		chown -R "$GROWTH_UID:${GROWTH_GID:-$GROWTH_UID}" "$STATE_DIR"
	fi
	if [ -n "${COORDINATOR_UID:-}" ]; then
		chown -R "$COORDINATOR_UID:${COORDINATOR_GID:-$COORDINATOR_UID}" "$COORD_DIR"
	fi
	if [ -n "${OPERATOR_UID:-}" ] || [ -n "${GROWTH_UID:-}" ]; then
		chown "${OPERATOR_UID:-0}:${OPERATOR_GID:-0}" "$DATA_ROOT" "$CONTROL_DIR"
		# Traversable but not writable by growth, so it can reach state/ and
		# stat control/STOP without being able to create or remove entries.
		chmod 711 "$DATA_ROOT" "$CONTROL_DIR"
	fi
fi

# Growth code is read-only at runtime. Refuse a group/world-writable tree.
if find "$ROOT/scripts/growth" "$ROOT/lib/events" -perm -022 -print -quit 2>/dev/null |
	grep -q .; then
	echo "growth code is group/world writable; runtime publication is not allowed" >&2
	exit 1
fi

export GROWTH_DATA_DIR="$DATA_ROOT"
export GROWTH_STOP_FILE="$STOP_FILE"

# In the production VM, root prepares ownership and then drops to the
# credential-free growth identity. The code tree must not be writable by that
# UID; only data/growth/state remains writable.
if [ "$(id -u)" = 0 ] && [ -n "${GROWTH_UID:-}" ]; then
	if find "$ROOT/scripts/growth" "$ROOT/lib/events" \
		-uid "$GROWTH_UID" -perm -u=w -print -quit 2>/dev/null | grep -q .; then
		echo "growth UID owns writable code; runtime publication is not allowed" >&2
		exit 1
	fi
	if ! command -v setpriv >/dev/null 2>&1; then
		echo "setpriv is required to enter the independent growth UID" >&2
		exit 1
	fi
	exec setpriv --reuid "$GROWTH_UID" --regid "${GROWTH_GID:-$GROWTH_UID}" \
		--clear-groups env GROWTH_ROLE=growth GROWTH_DATA_DIR="$DATA_ROOT" \
		GROWTH_STOP_FILE="$STOP_FILE" GROWTH_WITH_KEEPER="${GROWTH_WITH_KEEPER:-0}" \
		python3 -I "$ROOT/scripts/growth/cli.py" supervisor "$@"
fi

exec python3 -I "$ROOT/scripts/growth/cli.py" supervisor "$@"
