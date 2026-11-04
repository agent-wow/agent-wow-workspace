# 2026-11-03 — Authoritative data sources

## Decision

Before building the next feature on top of "professions", work out what the
authoritative source of each kind of truth is, and make it verifiable. The
map is `docs/data-sources.md`; this entry records the reasoning and the traps.

Three layers, three owners:

| Layer | Owns | Source |
|---|---|---|
| Live protocol | the character's current state | update fields on the wire |
| Client data | names, icons, categories, thresholds | DBC tables |
| World data | item names/quality, creatures, loot, quests, trainers | AzerothCore world DB |

The trap that costs the most time when it is missed: `Item.dbc` has **no item
name and no item quality**, and the client data has no creature names, no loot
and no quest titles. Those live in the server's `db_world`. Conversely the
trainer list is not in the world DB alone — the live server also sends it over
the gossip packets.

## The skill block

3.3.5a has **no `SMSG_INITIAL_SKILLS`**. Skills and professions arrive only as
the player's own update field `PLAYER_SKILL_INFO_1_1` (`0x027C`), one record
per skill line, three fields wide:

```
field 0: low  skill line id   high skill step
field 1: low  current rank    high max rank
field 2: low  temporary bonus high permanent bonus
```

Two things that break a naive decoder:

- Update blocks are **incremental**, so a snapshot must accumulate fields
  across packets rather than replace them.
- The third field is **omitted when the bonus is zero**, so a record can be
  two fields long. Requiring the third field silently drops every skill that
  has no bonus.

## Provenance, not trust

`wowgaming/node-dbc-reader` mirrors the upstream DBC files, one per file,
which makes it possible to pull a single table without downloading the 1.2 GB
`Data.zip`. A mirror is only useful if it can be checked, so
`lib/wow/dbc/cmd/gendbc` compares the mirror's `SkillLine.dbc` against
`skillline_dbc.sql` from wowgaming's own SQL release, field by field, for
every row. It matches. That check re-runs on every regeneration, so a
substituted or corrupted mirror cannot enter the generated tables.

Both downloads are pinned by sha256 in `scripts/fetch-client-data.sh`.

The SQL dump uses the wide modern schema (32 locale columns); the mapping
back to the 56-field 3.3.5a DBC layout is not the identity — it is
`0..3 -> 0..3`, `19..34 -> none`, `35 -> 19`, `36..51 -> 20..35`,
`52..67 -> none`, `68 -> 36`, `69 -> 37`, `70..85 -> 38..53`,
`86..101 -> none`, `102 -> 54`, `103 -> 55`. Getting this wrong produced 150
false mismatches on the first attempt.

## Locale

The client data reachable here is **enUS only**: across all 150 skill line
rows and all 49,839 spell rows, only locale slot 0 carries data. Item names
are fine (`item_template_locale` in the AC database has zhCN); skill and spell
names have no authoritative Chinese source. The route that would work is
extracting the DBCs from a Chinese 3.3.5a client archive, and the language
slot must be detected by content — the physical slot order inside a DBC is
*not* the column order of the SQL dump.

Until then: English from the DBC, Chinese from a hand-maintained overlay in
`web/static/game-data.js`, the same way the race, class and map tables already
work.

## Licensing

CC BY-SA 3.0 on the repository does not grant rights to Blizzard's content.
`data/` stays gitignored, and the generated tables are derived data whose
redistribution is a legal question, not a technical one.

## Next

The profession display works and the decode is live-verified
(`sense.get_skills` returns 33+ skill lines for Reachbot). No agent character
has a profession yet, so the 专业 row has not rendered live. Teaching one
needs trainer discovery (`npc_trainer` in the world DB, plus the gossip
packets), `CMSG_TRAINER_LIST` / `CMSG_LEARN_SPELL` support, and pathing to a
trainer — the first milestone of
`docs/plans/2026-11-03-0011-feat-autonomous-growth-mvp-plan.md`.
