# party

Joins groups. Listens for `SMSG_GROUP_INVITE` and answers `CMSG_GROUP_ACCEPT`
so an unattended character accepts invitations without a human watching the
screen. Acceptance is queued to a worker because `OnPacket` runs on the session
owner loop and must never call `SendPacket` synchronously.

## Wire formats (3.3.5a)

- `SMSG_GROUP_INVITE`: `uint8 flag`, NUL-terminated inviter name, `uint32 unk`,
  `uint8 count`, `uint32 unk` (AzerothCore `GroupHandler.cpp`).
- `CMSG_GROUP_ACCEPT`: 4 zero bytes; `HandleGroupAcceptOpcode` skips one
  `uint32` before using the pending invite.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `party.get_state` | — | `PartyState` | `invites`, `accepts`, `auto_accept`, `last_inviter` |
| `party.set_auto_accept` | `enabled` | `PartyState` | Turn the automatic reply off or on |
| `party.accept_invite` | — | `PartyState` | Send an accept now, for a lost automatic reply |

## Example

```bash
scripts/session.sh rpc party.get_state
scripts/session.sh rpc party.set_auto_accept '{"enabled":false}'
scripts/session.sh rpc party.accept_invite
```
