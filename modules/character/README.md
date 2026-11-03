# character

Trainer, talent and game-object protocol actions with packet-derived state.
The growth worker owns progression policy. This module stores no parallel skill
snapshot: `sense.get_skills` remains the only skill and profession source.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `character.open_trainer` | `trainer_guid` | `CharacterState` | Clears the old list and sends `CMSG_TRAINER_LIST` |
| `character.buy_spell` | `trainer_guid`, `spell_id` | `CharacterState` | Requires that spell in the current server list for the same trainer |
| `character.learn_talent` | `talent_id`, zero-based `requested_rank` | `CharacterState` | Sends `CMSG_LEARN_TALENT`; confirms only from a later talent snapshot |
| `character.use_game_object` | `guid` | `CharacterState` | Sends `CMSG_GAMEOBJ_USE`; does not claim gathering or loot success |
| `character.get_state` | — | `CharacterState` | Current trainer, buy-result and active talent-group facts |
| `character.status` | — | `StatusResponse` | Packet counters and parse errors |

`CharacterState.last_buy_result_known` distinguishes a server refusal with
reason zero from no received result. Talent ranks are zero-based, so rank zero
is a learned first point. Pet talent snapshots do not replace player talents.

## Preconditions and read-back

- Actions send requests only; the caller must reconcile them with server facts.
- Trainer purchases use `combat.get_spells`, inventory money and the matching
  server buy result. A missing reply never authorizes an automatic repurchase.
- Herb gathering starts a spell. Its completion opens the loot window consumed
  by the loot module's auto-loot worker; do not send an immediate `CMSG_LOOT`.
- `SMSG_LOGIN_VERIFY_WORLD` and `SMSG_NEW_WORLD` clear trainer and talent state.

## Subscribed opcodes

`SMSG_TRAINER_LIST`, `SMSG_TRAINER_BUY_SUCCEEDED`,
`SMSG_TRAINER_BUY_FAILED`, `SMSG_TALENTS_INFO`,
`SMSG_LOGIN_VERIFY_WORLD`, `SMSG_NEW_WORLD`.

## Verified

Go unit and race tests cover exact trainer/talent packet fixtures, malformed
packets, buy-result disambiguation, trainer-row gating and game-object request
encoding. Protocol layouts were checked against AzerothCore commit
`d80ce1d87720e6b6a0b9adc952f21a658b1c245e`. No live-server verification has
been run for this revision.

Run `make generate` after changing `module.proto`.
