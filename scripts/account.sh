#!/usr/bin/env bash
# Multi-account wrapper for agent-wow.
#
# agent-wow keeps exactly one auth.json (config_dir) and one saved session key
# (data_dir). To hold several logins at once, every account gets its own pair,
# selected with the global --config flag.
#
#   account.sh ls                  list accounts and their usernames
#   account.sh new <name> [--port N]  create an account (then run init)
#   account.sh init <name>         interactive username/password setup
#
# Account directories are named after the auth username (lowercased, since the
# filesystem is case-insensitive). `init` renames the directory automatically
# once the username is known, and every command lowercases its argument first.
#   account.sh login <name>        authenticate with the saved credentials
#   account.sh status <name>       check the saved session
#   account.sh chars <name>        list characters on the selected realm
#   account.sh realm <name> [args] realm list/set/status
#   account.sh play <name> <char>  write session intent (keeper starts it)
#   account.sh conf <name>         print the config file path
#
# Human accounts (not listed in agent-policy.json) are manual-only. Prefix a
# login/init/play command with AGENT_WOW_MANUAL=1 to act on one yourself; the
# agent and the session keeper never set that flag.
#
# The root config.yaml and accounts/doumoman/agent-wow.yaml point at the same
# account directory, so the legacy single-account flow still works.
#
# Every account has its own config_dir (auth.json, realm.json) and data_dir
# (session key), its own worldrpc port and its own VM directory (aw-<name>),
# so several accounts can be logged in and played at the same time.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCOUNTS="$ROOT/accounts"
POLICY="$ROOT/scripts/agent-policy.py"
KEEPER="$ROOT/scripts/session-keeper.py"
PORT_OVERRIDE="${AGENT_WOW_WORLDRPC_PORT:-}"

conf() { echo "$ACCOUNTS/$1/agent-wow.yaml"; }

die() { echo "$*" >&2; exit 2; }

norm() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Credentials and session keys are account-private: 0700 directories, 0600
# files. This only runs after account-owned setup/login commands.
secure_account() {
	local name="$1"
	mkdir -p "$ACCOUNTS/$name/config" "$ACCOUNTS/$name/data"
	chmod 700 "$ACCOUNTS/$name" "$ACCOUNTS/$name/config" "$ACCOUNTS/$name/data"
	find "$ACCOUNTS/$name" -type f -exec chmod 600 {} + 2>/dev/null || true
}

# session-keeper.py is the only automated session-action owner; callers write
# an intent and let it reconcile.
ensure_keeper() {
	mkdir -p "$ROOT/logs"
	if pgrep -f "[s]ession-keeper.py" >/dev/null 2>&1; then
		return 0
	fi
	nohup python3 -I "$KEEPER" >>"$ROOT/logs/session-keeper.log" 2>&1 &
}

# Keep the directory name equal to the auth username. Called after `auth init`
# has written auth.json, so the directory can be renamed to match.
align_dir() {
	local name="$1" user slug
	user="$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1])).get("username",""))' \
		"$ACCOUNTS/$name/config/auth.json" 2>/dev/null || true)"
	[ -n "$user" ] || return 0
	slug="$(norm "$user")"
	printf '%s' "$slug" | grep -Eq '^[a-z][a-z0-9_-]*$' || return 0
	[ "$slug" = "$name" ] && return 0
	if [ -e "$ACCOUNTS/$slug" ]; then
		echo "warning: $ACCOUNTS/$slug already exists; keeping '$name'" >&2
		return 0
	fi
	mv "$ACCOUNTS/$name" "$ACCOUNTS/$slug"
	python3 -I - "$ACCOUNTS/$slug/agent-wow.yaml" "$name" "$slug" <<'PY'
import sys
path, old, new = sys.argv[1:4]
text = open(path).read().replace(f"./accounts/{old}/", f"./accounts/{new}/")
open(path, "w").write(text)
PY
	echo "renamed account directory '$name' -> '$slug' (matches auth username)"
}

# First free worldrpc port after whatever is already configured.
next_port() {
	local max=8085 f p
	for f in "$ROOT/config.yaml" "$ACCOUNTS"/*/agent-wow.yaml; do
		[ -e "$f" ] || continue
		p="$(awk '/^worldrpc:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&$1=="port:"{print $2;exit}' "$f")"
		[ -n "$p" ] && [ "$p" -gt "$max" ] 2>/dev/null && max="$p"
	done
	echo $((max + 1))
}

ensure_account() {
	local name="$1" port="${2:-${PORT_OVERRIDE:-$(next_port)}}"
	[[ "$name" =~ ^[a-z][a-z0-9_-]*$ ]] || die "account name must match ^[a-z][a-z0-9_-]*$: $name"
	if [ -f "$(conf "$name")" ]; then return; fi

	mkdir -p "$ACCOUNTS/$name/config" "$ACCOUNTS/$name/data"
	chmod 700 "$ACCOUNTS/$name" "$ACCOUNTS/$name/config" "$ACCOUNTS/$name/data"

	# Inherit authserver settings from the root config so they never drift.
	local auth_host auth_port
	auth_host="$(awk '/^authserver:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&$1=="host:"{print $2;exit}' "$ROOT/config.yaml")"
	auth_port="$(awk '/^authserver:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&$1=="port:"{print $2;exit}' "$ROOT/config.yaml")"
	auth_host="${auth_host:-login.ymwow.com}"
	auth_port="${auth_port:-3720}"

	cat > "$(conf "$name")" <<YAML
config_dir: ./accounts/$name/config
data_dir: ./accounts/$name/data
module_dir: ./modules

authserver:
  host: $auth_host
  port: $auth_port

worldrpc:
  host: localhost
  port: $port

log_level: info
YAML
	echo "created account '$name' -> $(conf "$name")"
	echo "next: $0 init $name"
}

aw() {
	local name="$1"; shift
	[ -f "$(conf "$name")" ] || die "unknown account: $name (see: $0 ls)"
	( cd "$ROOT" && agent-wow --config "$(conf "$name")" "$@" )
}

cmd="${1:-ls}"; name="$(norm "${2:-}")"

case "$cmd" in
ls)
	printf '%-12s %-16s %-6s %s\n' ACCOUNT USERNAME PORT CONFIG_DIR
	if [ -d "$ACCOUNTS" ]; then
		for f in "$ACCOUNTS"/*/agent-wow.yaml; do
			[ -e "$f" ] || continue
			a="$(basename "$(dirname "$f")")"
			d="$(sed -n 's/^config_dir:[[:space:]]*//p' "$f" | head -1)"
			p="$(awk '/^worldrpc:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&$1=="port:"{print $2;exit}' "$f")"
			u="$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1])).get("username","-"))' \
				"$ROOT/${d:-./accounts/$a/config}/auth.json" 2>/dev/null || echo '-')"
			printf '%-12s %-16s %-6s %s\n' "$a" "$u" "${p:-8086}" "${d:-?}"
		done
	fi
	;;
new)
	[ -n "$name" ] || die "usage: $0 new <name> [--port N]"
	shift 2
	port=""
	if [ "${1:-}" = "--port" ]; then
		port="${2:-}"
		[ -n "$port" ] || die "--port needs a port number"
	fi
	ensure_account "$name" "$port"
	;;
init)
	[ -n "$name" ] || die "usage: $0 init <name>"
	ensure_account "$name"
	# A brand-new account may be set up freely; re-initializing an existing
	# account would overwrite credentials and can kick its active client.
	if [ -f "$ACCOUNTS/$name/config/auth.json" ] && [ "${AGENT_WOW_MANUAL:-0}" != "1" ]; then
		python3 -I "$POLICY" account "$name" || exit $?
	fi
	aw "$name" auth init
	secure_account "$name"
	align_dir "$name"
	;;
status)
	[ -n "$name" ] || die "usage: $0 status <name>"
	aw "$name" auth status
	;;
login)
	[ -n "$name" ] || die "usage: $0 login <name>"
	# A new login invalidates the saved session key and kicks the account's
	# active client, so it is gameplay-affecting. Human accounts require the
	# explicit manual override; automated callers never set it.
	if [ "${AGENT_WOW_MANUAL:-0}" != "1" ]; then
		python3 -I "$POLICY" account "$name" || exit $?
	fi
	aw "$name" auth login
	secure_account "$name"
	;;
chars)
	[ -n "$name" ] || die "usage: $0 chars <name>"
	aw "$name" char list
	;;
realm)
	[ -n "$name" ] || die "usage: $0 realm <name> [list|set ...|status]"
	shift 2
	aw "$name" realm "$@"
	;;
play)
	[ -n "${3:-}" ] || die "usage: $0 play <name> <character>"
	# Route through the durable session intent; session-keeper.py owns the
	# actual start so there is never a second session-action owner.
	if [ "${AGENT_WOW_MANUAL:-0}" = "1" ]; then
		# Manual mode: the operator starts a session directly and it is not
		# kept alive. Used for human accounts, which the keeper never touches.
		ACCOUNT="$name" AGENT_WOW_VM_DIR="aw-$name" AGENT_WOW_MANUAL=1 \
			"$ROOT/scripts/session.sh" start "$3"
		exit $?
	fi
	python3 -I "$POLICY" account "$name" || exit $?
	python3 -I "$POLICY" char "$name" "$3" || exit $?
	python3 -I "$KEEPER" desired "$name" online "$3"
	ensure_keeper
	;;
conf)
	[ -n "$name" ] || die "usage: $0 conf <name>"
	ensure_account "$name"
	conf "$name"
	;;
*)
	sed -n '2,20p' "$0"
	exit 2
	;;
esac
