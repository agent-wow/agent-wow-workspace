# party

Exposes server-backed contacts, exact-name who results, pending invitations,
the group roster and party chat. The module never accepts an invitation on its
own: `party.respond_invite` must name the current invite ID and inviter before
its 30-second local expiry, and each invitation can be answered only once.
Roster membership and consent policy belong to the growth worker.

`SMSG_MESSAGECHAT` sender GUIDs are retained exactly as supplied by the server;
a message body cannot override them. Chat events use a random per-process epoch
plus a sequence, so a module restart does not reuse old checkpoint cursors.
Packet callbacks only update state and never call `SendPacket`.

## RPCs

| RPC | Purpose |
|---|---|
| `party.get_state` | Contacts, who rows, pending invite, roster, loot settings and recent chat |
| `party.request_contacts` | Request the friend list |
| `party.add_friend` | Add a named friend with a bounded note |
| `party.who` | Exact-name online/level/class query |
| `party.invite` | Invite a named character |
| `party.respond_invite` | Explicit accept or decline of one current invite |
| `party.set_loot_method` | Send method/master/threshold; confirm through the next group list |
| `party.leave_party` | Leave the current group |
| `party.send_party_chat` | Send bounded party addon-language text |

```bash
scripts/session.sh rpc party.request_contacts
scripts/session.sh rpc party.respond_invite '{"invite_id":1,"inviter":"Alpha","accept":true}'
scripts/session.sh rpc party.send_party_chat '{"message":"AGW1|PING"}'
```
