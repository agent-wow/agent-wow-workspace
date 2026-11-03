package wow

import "github.com/doumoman/agent-wow-mods/wow/dbc"

// Skill decoding.
//
// 3.3.5a has no SMSG_INITIAL_SKILLS: professions and the other skill lines
// arrive only as the player's own update fields, in the
// PLAYER_SKILL_INFO_1_1 block that starts at PlayerSkillInfo.
//
// The block holds one record per skill line, three fields wide, with two
// uint16 in every field:
//
//	field 0: low  skill line id   high skill step
//	field 1: low  current rank    high max rank
//	field 2: low  temporary bonus high permanent bonus
//
// The record of slot i starts at PlayerSkillInfo + i*3, which is how
// TrinityCore reads it (src/server/game/Entities/Player/Player.h, the
// GetSkillLineIdByPos family of accessors).

// skillRecordFields is the number of update fields one skill record uses.
const skillRecordFields = 3

// maxSkillRecords is how many skill records the block can hold. The block is
// 384 fields wide, which is 128 records.
const maxSkillRecords = 128

// Skill is one skill line of the character.
type Skill struct {
	// ID is the skill line id, the key into the skill line table.
	ID uint16
	// Step is the skill step the rank has reached, 0 for the first step.
	Step uint16
	// Value is the current rank of the skill.
	Value uint16
	// Max is the maximum rank the skill can reach at the character's level.
	Max uint16
	// TempBonus is the temporary bonus applied to the skill.
	TempBonus uint16
	// PermBonus is the permanent bonus applied to the skill.
	PermBonus uint16
}

// Name returns the display name of the skill line.
func (s Skill) Name() string { return dbc.SkillLineName(s.ID) }

// IsProfession reports whether the skill line is one of the professions.
func (s Skill) IsProfession() bool { return dbc.IsProfession(s.ID) }

// Category returns the skill line category.
func (s Skill) Category() dbc.SkillLineCategory {
	line, ok := dbc.SkillLineByID(s.ID)
	if !ok {
		return dbc.SkillLineCategory(0)
	}
	return line.Category
}

// DecodeSkills decodes the character's skill lines from accumulated update
// fields.
//
// Update blocks are incremental, so the fields have to have been accumulated
// first; see the sense module, which keeps a running snapshot of the
// player's own fields. A record is only reported when the server sent both
// its identity field and its rank field.
func DecodeSkills(fields UpdateFields) []Skill {
	skills := make([]Skill, 0, 32)
	for pos := 0; pos < maxSkillRecords; pos++ {
		base := PlayerSkillInfo + uint32(pos*skillRecordFields)
		head, ok := fields[base]
		if !ok {
			continue
		}
		rank, ok := fields[base+1]
		if !ok {
			continue
		}
		id := uint16(head & 0xFFFF)
		if id == 0 {
			continue // the slot is empty
		}
		bonus, _ := fields[base+2]
		skills = append(skills, Skill{
			ID:        id,
			Step:      uint16(head >> 16),
			Value:     uint16(rank & 0xFFFF),
			Max:       uint16(rank >> 16),
			TempBonus: uint16(bonus & 0xFFFF),
			PermBonus: uint16(bonus >> 16),
		})
	}
	return skills
}

// Professions returns the professions among the given skills.
func Professions(skills []Skill) []Skill {
	out := make([]Skill, 0, 2)
	for _, s := range skills {
		if s.IsProfession() {
			out = append(out, s)
		}
	}
	return out
}
