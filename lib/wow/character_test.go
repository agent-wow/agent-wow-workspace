package wow

import (
	"encoding/binary"
	"testing"
)

func TestTrainerListFixture(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 0x1234)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 2)
	for _, spell := range []uint32{585, 2050} {
		body = binary.LittleEndian.AppendUint32(body, spell)
		body = append(body, 0)
		body = binary.LittleEndian.AppendUint32(body, 10)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = append(body, 1)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = binary.LittleEndian.AppendUint32(body, 0)
		for range 3 {
			body = binary.LittleEndian.AppendUint32(body, 0)
		}
	}
	body = append(body, "Train well\x00"...)
	list, err := ParseTrainerList(body)
	if err != nil {
		t.Fatal(err)
	}
	if list.GUID != 0x1234 || len(list.Spells) != 2 || list.Greeting != "Train well" {
		t.Fatalf("trainer list = %+v", list)
	}
	if list.Spells[0].SpellID != 585 || list.Spells[0].Usable != 0 || list.Spells[0].MoneyCost != 10 || list.Spells[0].ReqLevel != 1 {
		t.Fatalf("trainer spell = %+v", list.Spells[0])
	}
}

func TestTrainerPacketsAndMalformedList(t *testing.T) {
	if got := EncodeTrainerList(9); len(got) != 8 || binary.LittleEndian.Uint64(got) != 9 {
		t.Fatalf("trainer list request = %x", got)
	}
	if got := EncodeTrainerBuySpell(9, 585); len(got) != 12 || binary.LittleEndian.Uint32(got[8:]) != 585 {
		t.Fatalf("trainer buy = %x", got)
	}
	failure := binary.LittleEndian.AppendUint64(nil, 1)
	failure = binary.LittleEndian.AppendUint32(failure, 0)
	failure = binary.LittleEndian.AppendUint32(failure, 2)
	result, err := ParseTrainerBuyResult(failure)
	if err != nil || result.Succeeded || result.Result != 2 {
		t.Fatalf("trainer failure = %+v, %v", result, err)
	}
	success := binary.LittleEndian.AppendUint64(nil, 1)
	success = binary.LittleEndian.AppendUint32(success, 585)
	result, err = ParseTrainerBuyResult(success)
	if err != nil || !result.Succeeded || result.SpellID != 585 {
		t.Fatalf("trainer success = %+v, %v", result, err)
	}
	refused := binary.LittleEndian.AppendUint64(nil, 1)
	refused = binary.LittleEndian.AppendUint32(refused, 999)
	refused = binary.LittleEndian.AppendUint32(refused, 0)
	result, err = ParseTrainerBuyResult(refused)
	if err != nil || result.Succeeded || result.Result != 0 {
		t.Fatalf("trainer unavailable = %+v, %v", result, err)
	}
	body := make([]byte, 16)
	binary.LittleEndian.PutUint32(body[12:], 1)
	if _, err := ParseTrainerList(body); err == nil {
		t.Fatal("truncated trainer list accepted")
	}
}

func TestTalentSnapshotFixture(t *testing.T) {
	body := []byte{0, 3, 0, 0, 0, 1, 0, 2}
	body = binary.LittleEndian.AppendUint32(body, 1001)
	body = append(body, 2)
	body = binary.LittleEndian.AppendUint32(body, 2001)
	body = append(body, 1)
	body = append(body, 6)                   // glyph count
	body = append(body, make([]byte, 12)...) // glyphs
	talents, err := ParseTalentsInfo(body)
	if err != nil {
		t.Fatal(err)
	}
	if talents.Pet || talents.FreePoints != 3 || talents.ActiveGroup != 0 || len(talents.Groups) != 1 {
		t.Fatalf("talents = %+v", talents)
	}
	group := talents.Groups[0]
	if !group.Active || len(group.Talents) != 2 || group.Talents[0].ID != 1001 || group.Talents[0].Rank != 2 {
		t.Fatalf("talent group = %+v", group)
	}
	if got := EncodeLearnTalent(1001, 2); len(got) != 8 || binary.LittleEndian.Uint32(got) != 1001 || binary.LittleEndian.Uint32(got[4:]) != 2 {
		t.Fatalf("learn talent = %x", got)
	}
	if _, err := ParseTalentsInfo(body[:len(body)-1]); err == nil {
		t.Fatal("truncated talent snapshot accepted")
	}
}

func TestPetTalentSnapshotFixture(t *testing.T) {
	body := []byte{1, 2, 0, 0, 0, 1}
	body = binary.LittleEndian.AppendUint32(body, 1001)
	body = append(body, 0)
	talents, err := ParseTalentsInfo(body)
	if err != nil {
		t.Fatal(err)
	}
	if !talents.Pet || talents.FreePoints != 2 || len(talents.Groups) != 1 || talents.Groups[0].Talents[0].Rank != 0 {
		t.Fatalf("pet talents = %+v", talents)
	}
}

func TestSpellbookFixture(t *testing.T) {
	body := []byte{0, 2, 0}
	body = binary.LittleEndian.AppendUint32(body, 585)
	body = binary.LittleEndian.AppendUint16(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 78)
	body = binary.LittleEndian.AppendUint16(body, 1)
	body = binary.LittleEndian.AppendUint16(body, 0)
	spells, err := ParseInitialSpells(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(spells) != 2 || spells[0].ID != 585 || spells[0].Disabled || !spells[1].Disabled || spells[1].ID != 78 {
		t.Fatalf("spells = %+v", spells)
	}
	learnedBody := binary.LittleEndian.AppendUint32(nil, 2050)
	learnedBody = binary.LittleEndian.AppendUint16(learnedBody, 0)
	learned, err := ParseLearnedSpell(learnedBody)
	if err != nil || learned != 2050 {
		t.Fatalf("learned = %d, %v", learned, err)
	}
}

func TestGameObjectCastUsesGameObjectTargetMask(t *testing.T) {
	body := EncodeCastSpellGameObject(0, 2366, 55)
	if len(body) < 11 || binary.LittleEndian.Uint32(body[6:10]) != 0x0800 {
		t.Fatalf("game-object cast = %x", body)
	}
}

func TestGameObjectUseFixture(t *testing.T) {
	if got := EncodeGameObjectUse(0x102030405); len(got) != 8 || binary.LittleEndian.Uint64(got) != 0x102030405 {
		t.Fatalf("game object use = %x", got)
	}
}
