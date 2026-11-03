# U1 durable runtime verification — 2026-11-03

## Local evidence

- `python3 -m unittest discover -s scripts/growth/tests -v`: 78 tests passed.
- `go -C lib/events test -race ./...`: passed.
- `go -C lib/wow test -race ./...`: passed.
- `./scripts/lint-modules.sh`: `modules OK`.
- Python compilation, shell syntax checks and `gofmt -l lib`: passed.
- `python3 -I scripts/growth/cli.py permissions audit-accounts --accounts-root accounts`: passed after changing account directories to 0700 and files to 0600. No account login or session action was performed.

Fault-injection coverage includes duplicate worker exclusion, policy/port/GUID gates, durable pre-dispatch `sending`, restart and epoch-change recovery to `unknown`, atomic event/cursor/intent commits, replay across identical event IDs in different epochs, dropped-event reconciliation, STOP behavior, account-scoped required-module health checks, and checkpoint/coordinator failure isolation.

## Sync, permissions and recovery

- Account sync tests prove the archive contains only `accounts/<selected-account>` and preserves that path layout.
- Growth sync tests prove no `accounts/` tree or credentials are shipped. The only derived account data is the policy-filtered account/worldrpc-port map.
- Growth workers use direct localhost worldrpc in the VM; they do not invoke nested `colima ssh` or read account configuration.
- VM backup uses the SQLite backup API, copies only sealed database snapshots to the host, and excludes WAL/SHM and STOP control files.
- A delete-and-rebuild test restored the character checkpoint, intent state, epoch and cursor successfully.
- Host-owned VM backup is attempted every 60 seconds once the roster is configured. The declared checkpoint RPO is 60 seconds plus the duration of the current backup; failed attempts wait for the next interval.
- Production supervisor setup supports separate growth/coordinator/operator ownership and re-executes workers under the growth UID with read-only code. `scripts/growth-control.sh` is the operator-only host+VM STOP/resume control; automated paths never resume it.

## Deliberately not claimed

- `config/roster.yaml` remains intentionally unconfigured, so no real five-character worker fleet was started.
- Four-session and five-session live capacity/soak gates remain unrun. No game session, module build, login, VM sync, or live gameplay verification was performed for U1.
- Final capacity evidence still requires the user-created five-character roster, ready modules, and human repair of `doumoboy1` credentials.
