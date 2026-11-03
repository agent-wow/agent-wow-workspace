// Package dbc holds the authoritative client-data tables this workspace
// decodes protocol values against.
//
// The tables are generated from the 3.3.5a client data published by
// wowgaming, which is the same upstream agent-wow itself pins (see its
// tools/internal/clientdata package). Every generated file records the source
// URL and the sha256 of the file it was produced from, so a table can always
// be re-derived and checked.
//
// Fetch the source files with:
//
//	scripts/fetch-client-data.sh
//
// Regenerate the tables with:
//
//	cd lib/wow && go run ./dbc/cmd/gendbc -data-dir ../../data/client-data
//
// The generator cross-checks SkillLine.dbc against the SQL dump of the same
// table published by wowgaming, so a corrupted or substituted mirror cannot
// slip into the generated code.
//
// The client data only ships enUS strings. Where a table carries the sixteen
// locale slots of a 3.3.5a DBC, the locale is picked by content rather than
// by a hard coded slot index, so a future release carrying zhCN fills in
// without a code change.
package dbc

import "github.com/doumoman/agent-wow-mods/wow/dbcfile"

// File is a parsed WDBC table. The reader lives in its own package so the
// generator can parse the source files without depending on the tables it
// generates.
type File = dbcfile.File

// SkillLineCategory says what a skill line is used for. The values come from
// the CategoryID column of skillline_dbc.
type SkillLineCategory int32

const (
	// SkillCategoryWeapon is the category for weapon skills (Swords, Bows...).
	SkillCategoryWeapon SkillLineCategory = 6
	// SkillCategoryClassSpec is the category for class specs and pets.
	SkillCategoryClassSpec SkillLineCategory = 7
	// SkillCategoryArmor is the category for armor proficiencies.
	SkillCategoryArmor SkillLineCategory = 8
	// SkillCategoryMisc holds the gathering and life skills that are not
	// professions: First Aid, Cooking, Fishing, Riding, Survival, Swimming
	// and the racial skills.
	SkillCategoryMisc SkillLineCategory = 9
	// SkillCategoryLanguage holds the languages.
	SkillCategoryLanguage SkillLineCategory = 10
	// SkillCategoryProfession holds the eleven professions.
	SkillCategoryProfession SkillLineCategory = 11
)

// SkillLine is one row of SkillLine.dbc.
type SkillLine struct {
	// ID is the skill line id the server puts in the player's skill info.
	ID uint16
	// Name is the display name, in the best locale the client data provides.
	Name string
	// Category is the skill line category.
	Category SkillLineCategory
	// IconID is the spell icon the skill line shows in the client.
	IconID uint32
}

// SkillRaceClassInfo is one row of SkillRaceClassInfo.dbc: which races and
// classes may hold a skill line.
type SkillRaceClassInfo struct {
	// ID is the row id.
	ID uint16
	// SkillID is the skill line this row describes.
	SkillID uint16
	// RaceMask has one bit per race, bit 0 is Human (race id 1).
	RaceMask uint32
	// ClassMask has one bit per class, bit 0 is Warrior (class id 1).
	ClassMask uint32
	// Flags is the row's flag bits.
	Flags uint32
	// SkillTierID points at the SkillTiers row holding the value thresholds
	// for this skill line.
	SkillTierID uint16
}

// SkillTier is one row of SkillTiers.dbc: the value thresholds a skill line
// has to reach for each of its steps.
type SkillTier struct {
	// ID is the row id.
	ID uint16
	// Cost is the per step cost, indexed by step.
	Cost [16]uint32
	// Value is the per step value threshold, indexed by step.
	Value [16]uint32
}

// SkillLineByID returns the skill line with the given id.
func SkillLineByID(id uint16) (SkillLine, bool) {
	line, ok := skillLines[id]
	return line, ok
}

// SkillLineName returns the display name of a skill line, or a placeholder
// when the id is unknown to the table.
func SkillLineName(id uint16) string {
	if line, ok := skillLines[id]; ok && line.Name != "" {
		return line.Name
	}
	return "Unknown skill"
}

// IsProfession reports whether a skill line id is one of the professions.
func IsProfession(id uint16) bool {
	line, ok := skillLines[id]
	return ok && line.Category == SkillCategoryProfession
}

// ProfessionIDs lists the ids of every profession skill line, sorted.
func ProfessionIDs() []uint16 {
	ids := make([]uint16, 0, 16)
	for id, line := range skillLines {
		if line.Category == SkillCategoryProfession {
			ids = append(ids, id)
		}
	}
	sortUint16(ids)
	return ids
}

// SkillRaceClassInfoForSkill returns every row describing who may hold the
// given skill line.
func SkillRaceClassInfoForSkill(skillID uint16) []SkillRaceClassInfo {
	rows := make([]SkillRaceClassInfo, 0, 4)
	for _, row := range skillRaceClassInfo {
		if row.SkillID == skillID {
			rows = append(rows, row)
		}
	}
	return rows
}

// RaceCanHaveSkill reports whether a race and class pair may hold the skill
// line. Race and class ids are the ones the server sends in the player
// fields; unknown ids are treated as "cannot".
func RaceCanHaveSkill(raceID, classID uint32, skillID uint16) bool {
	if raceID == 0 || classID == 0 {
		return false
	}
	raceBit, classBit := RaceBit(raceID), ClassBit(classID)
	if raceBit == 0 || classBit == 0 {
		return false
	}
	for _, row := range SkillRaceClassInfoForSkill(skillID) {
		if row.RaceMask&raceBit != 0 && row.ClassMask&classBit != 0 {
			return true
		}
	}
	return false
}

// RaceBit returns the mask bit of a race id.
func RaceBit(raceID uint32) uint32 {
	if raceID == 0 || raceID > 32 {
		return 0
	}
	return 1 << (raceID - 1)
}

// ClassBit returns the mask bit of a class id.
func ClassBit(classID uint32) uint32 {
	if classID == 0 || classID > 32 {
		return 0
	}
	return 1 << (classID - 1)
}

// SkillTiersForSkillLine returns the value thresholds of a skill line, in
// step order. The thresholds come from the SkillTiers row the skill line's
// race/class rows point at.
func SkillTiersForSkillLine(skillID uint16) []uint32 {
	for _, row := range SkillRaceClassInfoForSkill(skillID) {
		tier, ok := skillTiers[row.SkillTierID]
		if !ok {
			continue
		}
		values := make([]uint32, 0, len(tier.Value))
		for _, v := range tier.Value {
			if v == 0 {
				break
			}
			values = append(values, v)
		}
		if len(values) > 0 {
			return values
		}
	}
	return nil
}

// SkillStepForValue returns the step a skill has reached at the given value,
// using the skill line's thresholds. A value below the first threshold is
// step 0.
func SkillStepForValue(skillID uint16, value uint16) uint16 {
	thresholds := SkillTiersForSkillLine(skillID)
	step := uint16(0)
	for i, t := range thresholds {
		if uint32(value) >= t {
			step = uint16(i + 1)
		}
	}
	return step
}

// sortUint16 sorts ids in place. The generated tables are small, so a simple
// insertion sort keeps this package free of imports.
func sortUint16(ids []uint16) {
	for i := 1; i < len(ids); i++ {
		v := ids[i]
		j := i - 1
		for j >= 0 && ids[j] > v {
			ids[j+1] = ids[j]
			j--
		}
		ids[j+1] = v
	}
}
