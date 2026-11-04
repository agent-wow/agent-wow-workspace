package wow

import "testing"

// skillRecord builds the three update fields of one skill record.
func skillRecord(pos int, id, step, value, max, temp, perm uint16) map[uint32]uint32 {
	base := PlayerSkillInfo + uint32(pos*skillRecordFields)
	return map[uint32]uint32{
		base:     uint32(id) | uint32(step)<<16,
		base + 1: uint32(value) | uint32(max)<<16,
		base + 2: uint32(temp) | uint32(perm)<<16,
	}
}

func TestDecodeSkills(t *testing.T) {
	fields := UpdateFields{}
	for k, v := range skillRecord(0, 171, 4, 300, 375, 0, 5) { // Alchemy
		fields[k] = v
	}
	for k, v := range skillRecord(1, 182, 3, 225, 300, 0, 0) { // Herbalism
		fields[k] = v
	}
	// An empty slot in between must not produce a skill.
	fields[PlayerSkillInfo+6] = 0
	fields[PlayerSkillInfo+7] = 0

	skills := DecodeSkills(fields)
	if len(skills) != 2 {
		t.Fatalf("got %d skills, want 2: %+v", len(skills), skills)
	}
	first := skills[0]
	if first.ID != 171 || first.Step != 4 || first.Value != 300 || first.Max != 375 || first.PermBonus != 5 {
		t.Fatalf("alchemy: %+v", first)
	}
	if !first.IsProfession() {
		t.Fatalf("alchemy should be a profession: %+v", first)
	}
	if first.Name() != "Alchemy" {
		t.Fatalf("alchemy name: %q", first.Name())
	}
	if second := skills[1]; second.ID != 182 || second.Step != 3 || second.Value != 225 || second.Max != 300 {
		t.Fatalf("herbalism: %+v", second)
	}
	if got := Professions(skills); len(got) != 2 {
		t.Fatalf("professions: %+v", got)
	}
}

func TestDecodeSkillsIgnoresIncompleteRecords(t *testing.T) {
	fields := UpdateFields{}
	fields[PlayerSkillInfo] = 171 // identity without a rank field
	if skills := DecodeSkills(fields); len(skills) != 0 {
		t.Fatalf("got %+v, want no skills", skills)
	}
}
