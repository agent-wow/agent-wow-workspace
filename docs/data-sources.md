# Authoritative data sources (3.3.5a)

What this workspace treats as truth, where each kind of truth comes from, and
how to extend the set. Written for the next feature, not for the profession
display alone.

The rule that keeps the three layers apart:

| Layer | Answers | Source |
|---|---|---|
| Live protocol | What *this* character is doing right now | update fields on the wire |
| Client data | What the *client* knows: skill line names, spell names, map names, icons | DBC tables |
| World data | What the *server* knows: item names and quality, creatures, loot, quests, trainers | AzerothCore world DB |

The most common mistake in a project like this is looking for item names in
the client data (they are not there) or for the server's trainer list in the
protocol (it is not there either).

---

## 1. Live protocol: update fields

The only trustworthy source for the current state of the character. Field
offsets come from TrinityCore 3.3.5 `UpdateFields.h`, cross-checked against
AzerothCore; the values are read live from the running server.

| Field | Offset | Meaning |
|---|---|---|
| `PLAYER_FIELD_COINAGE` | `0x0492` | copper carried |
| `PLAYER_XP` | `0x027A` | experience |
| `PLAYER_NEXT_LEVEL_XP` | `0x027B` | experience needed for the next level |
| `PLAYER_SKILL_INFO_1_1` | `0x027C` | the character's skill lines |

### The skill block

3.3.5a has **no** `SMSG_INITIAL_SKILLS`. Skills and professions arrive only as
the player's own update fields. The block is 384 fields wide and holds one
record per skill line, three fields wide, two uint16 per field:

```
field 0: low  skill line id   high skill step
field 1: low  current rank    high max rank
field 2: low  temporary bonus high permanent bonus
```

The record of slot *i* starts at `0x027C + i*3`, which is how TrinityCore
reads it (`Player.h`, the `GetSkillLineIdByPos` family). The shared decoder
lives in `lib/wow/skills.go`.

Two traps worth writing down:

- **Update blocks are incremental.** A block carries only the fields that
  changed, so a snapshot has to accumulate (`sense` keeps a running copy of
  the character's own fields).
- **A record may be incomplete.** The server omits the bonus field when the
  bonus is zero, so the decoder requires the identity and rank fields only.

## 2. Client data: the DBC tables

Published by [wowgaming/client-data](https://github.com/wowgaming/client-data):

| Release | Asset | sha256 | Size |
|---|---|---|---|
| `dbc_sql_v1` | `AzerothcoreDBCToSQL.zip` | `e524aba0bc05aa06e2b6e5421eb80a2358fec1ae27f3ff352c15b2b75081737e` | 8.9 MB, 101 SQL tables |
| `v20.0` | `Data.zip` | `a3d4df635ae6c2c8f08052c32a79e0f806955150ad36b014a823dd08a32a4610` | 1.2 GB, 246 DBC + maps |

`Data.zip` is the same upstream agent-wow itself pins (`tools/internal/clientdata`),
but it is large. `wowgaming/node-dbc-reader` mirrors the same DBC files one by
one, which is how single tables are pulled here without the 1.2 GB archive.

### Pinned tables in this workspace

`scripts/fetch-client-data.sh` downloads and verifies these into
`data/client-data/` (gitignored):

| File | sha256 | What it gives |
|---|---|---|
| `SkillLine.dbc` | `276824940b7a38e1639e974571e5cb1ad0a1ecbaf3d7d14ebf02ab0294edcadb` | skill line id, name, category, icon |
| `SkillTiers.dbc` | `7bf8c5f87fc7917fa3bb42b459246fd8c5870e5223d24d66ae16e27e730c7e49` | the value thresholds of each step |
| `SkillRaceClassInfo.dbc` | `341a42678faee5f208e0874ab154ee174b99ba469306df7458bdaf0ffcf76967` | which races and classes may hold a skill line |

They are turned into Go tables in `lib/wow/dbc/*_gen.go` (committed, each file
records its source and checksum).

### The cross-check

`SkillLine.dbc` comes from a mirror, so the generator does not trust it
blindly: it also reads `skillline_dbc.sql` from the upstream SQL release and
compares **every field of every row** (150 rows x 56 fields). The two agree
exactly, which is what makes the mirror usable as a stand-in for the upstream
archive.

The dump uses the wide modern schema: 32 locale columns per string block, of
which the first sixteen carry the 3.3.5a slots. The mapping between dump
column and DBC field is in `lib/wow/dbc/cmd/gendbc/main.go`.

### The tables a feature will reach for

| Need | Table | Note |
|---|---|---|
| skill line name / category / icon | `SkillLine.dbc` | pinned here |
| which spells a skill line trains | `SkillLineAbility.dbc` | fields 1 (skill line) and 2 (spell) — **not** `Spell.dbc` |
| who may hold a skill line | `SkillRaceClassInfo.dbc` | pinned here |
| the value thresholds of a step | `SkillTiers.dbc` | pinned here |
| spell name / rank / icon / effects | `Spell.dbc` | fields 0 / 136 / 153 / 133; the SQL dump is 276 MB, so parse the DBC on demand |
| map name | `Map.dbc` | field 0 / 5 |
| zone name | `AreaTable.dbc` | field 0 / 11 |
| item class, subclass, display, inventory type | `Item.dbc` | 8 fields |
| talents | `Talent.dbc`, `TalentTab.dbc` | |

`Item.dbc` has **no item name and no item quality**. Neither have creature
names, loot tables or quest names: those are server side.

## 3. World data: what only the server knows

Not client data, so not in `client-data`. The upstream source is the
AzerothCore world database, `data/sql/base/db_world/*.sql` in
`azerothcore/azerothcore-wotlk`:

| Need | Table | Fields |
|---|---|---|
| item name and quality | `item_template` | `name`, `Quality` (0 poor ... 7 heirloom) |
| item class / subclass / display | `item_template` | `class`, `subclass`, `displayid` |
| which profession trains an item | `item_template` | `RequiredSkill`, `RequiredSkillRank`, `spellid_1` |
| creature name and level | `creature_template` | `name`, `minlevel`, `maxlevel` |
| loot | `creature_loot_template` | `Item`, `Chance`, `MinCount`, `MaxCount` |
| quest title | `quest_template` | `LogTitle` |
| profession trainers | `npc_trainer` | `spell`, `spellcost` |

A live server also exposes these at runtime: the trainer list comes over the
gossip packets, and the loot list over `SMSG_LOOT_RESPONSE`.

## 4. Chinese names: the open gap

The client data this workspace can reach ships **enUS only**. All sixteen
locale slots of every string block hold data in slot 0 alone, which was
verified across the whole `Spell.dbc` and `SkillLine.dbc`.

What is available today:

- **Item names** — `item_template_locale` in the AzerothCore database (and
  the `gswxy/AC_db_chinese` mirror of it) carries zhCN.
- **Skill and spell names** — no authoritative source found. The AC
  `spell_dbc.sql` zhCN columns are empty, and the Chinese community database
  has no skill or spell locale table.

The route that would work: extract `Spell.dbc` / `SkillLine.dbc` from a
Chinese 3.3.5a client archive and read the zhCN slot. One warning: the
**physical locale slot order inside a DBC is not the column order of the SQL
dump** (the dump renames them), so the slot has to be detected by content,
never hard coded by index.

Until then, the English name comes from the DBC and the Chinese name is
maintained by hand in `web/static/game-data.js`, the same way the race, class
and map tables already are.

## 5. The pipeline in this workspace

```
scripts/fetch-client-data.sh      pinned download + checksum, into data/ (gitignored)
        |
lib/wow/dbcfile                   WDBC reader (header, records, string block)
        |
lib/wow/dbc/cmd/gendbc            verifies checksums, cross-checks against upstream,
        |                         writes lib/wow/dbc/*_gen.go with source + checksum
lib/wow/dbc                       query API: names, categories, tiers, race/class masks
lib/wow/skills.go                 decodes the live skill block into those tables
modules/sense                     exposes skills over RPC (get_self, get_skills)
web/                              renders them in the console
```

Adding a table is four steps: add the file and its checksum to
`scripts/fetch-client-data.sh`, add a writer to the generator, commit the
generated file, and use it from `lib/wow/dbc`.

## 6. Licensing and redistribution

`wowgaming/client-data` is published under CC BY-SA 3.0, but the content
itself is Blizzard's: the repository states that World of Warcraft content is
trademarks and copyrights of Blizzard. CC BY-SA cannot override that.

So:

- `data/` stays gitignored; never commit the downloaded client data.
- The generated Go tables are derived data. Keeping them in a private
  workspace is the low-risk choice; publishing them is a legal question, not
  a technical one.
- AzerothCore is GPLv2, `node-dbc-reader` is AGPL-3.0 (its *code*; the data
  files are the mirror above).

## 7. Provenance of the claims above

| Claim | Checked against |
|---|---|
| field offsets and the skill layout | TrinityCore 3.3.5 `UpdateFields.h`, `Player.h`; live server |
| skill line categories and names | `SkillLine.dbc`, cross-checked field by field against the upstream SQL dump |
| profession set (11 skills, category 11) | `SkillLine.dbc` |
| locale coverage is enUS only | all 16 slots of all 150 skill line rows and 49,839 spell rows |
| "no `SMSG_INITIAL_SKILLS` in 3.3.5a" | TrinityCore 3.3.5 `Opcodes.h` |
| spell id to name mapping | `Spell.dbc` field 136, 49,839 of 49,839 rows named |
