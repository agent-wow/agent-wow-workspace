package dbc

import "testing"

func TestProfessions(t *testing.T) {
	want := []uint16{164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773}
	got := ProfessionIDs()
	if len(got) != len(want) {
		t.Fatalf("got %d professions %v, want %d", len(got), got, len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("profession %d: got %d, want %d", i, got[i], want[i])
		}
	}
	for _, id := range want {
		if !IsProfession(id) {
			t.Fatalf("%d should be a profession", id)
		}
	}
	if IsProfession(185) { // Cooking is a life skill, not a profession
		t.Fatal("cooking should not be a profession")
	}
}

func TestSkillLineNames(t *testing.T) {
	line, ok := SkillLineByID(171)
	if !ok {
		t.Fatal("alchemy is missing")
	}
	if line.Name != "Alchemy" {
		t.Fatalf("alchemy name: %q", line.Name)
	}
	if line.Category != SkillCategoryProfession {
		t.Fatalf("alchemy category: %d", line.Category)
	}
	if SkillLineName(65535) != "Unknown skill" {
		t.Fatalf("unknown skill name: %q", SkillLineName(65535))
	}
}

func TestSkillTiersForSkillLine(t *testing.T) {
	thresholds := SkillTiersForSkillLine(171)
	want := []uint32{75, 150, 225, 300, 375, 450}
	if len(thresholds) != len(want) {
		t.Fatalf("got %v, want %v", thresholds, want)
	}
	for i := range want {
		if thresholds[i] != want[i] {
			t.Fatalf("threshold %d: got %d, want %d", i, thresholds[i], want[i])
		}
	}
	if step := SkillStepForValue(171, 225); step != 3 {
		t.Fatalf("step at 225: got %d, want 3", step)
	}
	if step := SkillStepForValue(171, 10); step != 0 {
		t.Fatalf("step at 10: got %d, want 0", step)
	}
}

func TestRaceCanHaveSkill(t *testing.T) {
	const (
		human   = 1
		warrior = 1
		mage    = 8
	)
	if !RaceCanHaveSkill(human, warrior, 171) { // Alchemy
		t.Fatal("a human warrior should be able to learn alchemy")
	}
	if RaceCanHaveSkill(human, mage, 26) { // Arms is a warrior skill
		t.Fatal("a mage should not be able to learn arms")
	}
	if !RaceCanHaveSkill(human, warrior, 26) {
		t.Fatal("a warrior should be able to learn arms")
	}
}
