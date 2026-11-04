package wow

import (
	"encoding/binary"
	"math"
	"testing"
)

func TestEncodeAttackSwing(t *testing.T) {
	got := EncodeAttackSwing(0x0102030405060708)
	if len(got) != 8 || binary.LittleEndian.Uint64(got) != 0x0102030405060708 {
		t.Fatalf("attack swing = %x", got)
	}
}

func TestEncodeCastSpellUnitTarget(t *testing.T) {
	got := EncodeCastSpell(1, 78, 0x42)
	want := []byte{1, 78, 0, 0, 0, 0, 2, 0, 0, 0, 1, 0x42}
	if len(got) != len(want) {
		t.Fatalf("cast spell length = %d, want %d (%x)", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("cast spell = %x, want %x", got, want)
		}
	}
}

func TestParseAttackStart(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 42)
	body = binary.LittleEndian.AppendUint64(body, 99)
	ev, err := ParseAttackStart(body)
	if err != nil {
		t.Fatal(err)
	}
	if ev.Attacker != 42 || ev.Victim != 99 {
		t.Fatalf("event = %+v", ev)
	}
}

func TestParseAttackStopPacked(t *testing.T) {
	body := packedGUID(nil, 42)
	body = packedGUID(body, 99)
	body = binary.LittleEndian.AppendUint32(body, 1)
	ev, err := ParseAttackStop(body)
	if err != nil {
		t.Fatal(err)
	}
	if ev.Attacker != 42 || ev.Victim != 99 || !ev.NowDead {
		t.Fatalf("event = %+v", ev)
	}
}

func TestParseLogXPGainKill(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 7)
	body = binary.LittleEndian.AppendUint32(body, 55)
	body = append(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 50)
	body = binary.LittleEndian.AppendUint32(body, math.Float32bits(1.1))
	body = append(body, 0)
	xp, err := ParseLogXPGain(body)
	if err != nil {
		t.Fatal(err)
	}
	if xp.Victim != 7 || xp.Total != 55 || xp.Base != 50 || !xp.Kill {
		t.Fatalf("xp = %+v", xp)
	}
}

func TestParseLogXPGainOther(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 0)
	body = binary.LittleEndian.AppendUint32(body, 12)
	body = append(body, 1, 1)
	xp, err := ParseLogXPGain(body)
	if err != nil {
		t.Fatal(err)
	}
	if xp.Kill || xp.Total != 12 {
		t.Fatalf("xp = %+v", xp)
	}
}

func TestParseCastFailed(t *testing.T) {
	body := []byte{1, 78, 0, 0, 0, 0x12}
	f, err := ParseCastFailed(body)
	if err != nil {
		t.Fatal(err)
	}
	if f.CastCount != 1 || f.SpellID != 78 || f.Result != 0x12 {
		t.Fatalf("failure = %+v", f)
	}
}

func TestParseSpellCooldown(t *testing.T) {
	// AzerothCore's Unit::BuildCooldownPacket writes a raw caster GUID, a flag
	// byte and (spell, remaining) pairs with no count field.
	body := binary.LittleEndian.AppendUint64(nil, 42)
	body = append(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 78)
	body = binary.LittleEndian.AppendUint32(body, 1500)
	body = binary.LittleEndian.AppendUint32(body, 100)
	body = binary.LittleEndian.AppendUint32(body, 0)

	guid, flags, cooldowns, err := ParseSpellCooldown(body)
	if err != nil {
		t.Fatal(err)
	}
	if guid != 42 || flags != 0 || len(cooldowns) != 2 {
		t.Fatalf("cooldown packet = %x/%d/%v", guid, flags, cooldowns)
	}
	if cooldowns[0].SpellID != 78 || cooldowns[0].CooldownMS != 1500 {
		t.Fatalf("first cooldown = %+v", cooldowns[0])
	}
	if _, _, _, err := ParseSpellCooldown(body[:len(body)-2]); err == nil {
		t.Fatal("expected truncated cooldown error")
	}
}

func TestParseCooldownEvent(t *testing.T) {
	body := binary.LittleEndian.AppendUint32(nil, 78)
	body = binary.LittleEndian.AppendUint64(body, 42)
	ev, err := ParseCooldownEvent(body)
	if err != nil {
		t.Fatal(err)
	}
	if ev.SpellID != 78 || ev.GUID != 42 {
		t.Fatalf("event = %+v", ev)
	}
}

func TestParseAuraUpdate(t *testing.T) {
	body := packedGUID(nil, 42)
	// A buff with a duration and a packed caster GUID.
	body = append(body, 3)                              // slot
	body = binary.LittleEndian.AppendUint32(body, 1234) // spell
	body = append(body, AuraFlagPositive|AuraFlagDuration, 60, 2)
	body = packedGUID(body, 99)
	body = binary.LittleEndian.AppendUint32(body, 30000)
	body = binary.LittleEndian.AppendUint32(body, 15000)
	// An empty slot removes the aura.
	body = append(body, 4)
	body = binary.LittleEndian.AppendUint32(body, 0)

	update, err := ParseAuraUpdate(body)
	if err != nil {
		t.Fatal(err)
	}
	if update.Target != 42 || len(update.Auras) != 2 {
		t.Fatalf("update = %+v", update)
	}
	buff := update.Auras[0]
	if buff.Slot != 3 || buff.SpellID != 1234 || buff.CasterLevel != 60 ||
		buff.Stacks != 2 || !buff.HasCaster || buff.Caster != 99 ||
		!buff.HasDuration || buff.DurationMS != 15000 || buff.MaxDurationMS != 30000 ||
		!buff.Positive() || buff.Negative() {
		t.Fatalf("buff = %+v", buff)
	}
	if removed := update.Auras[1]; removed.Slot != 4 || removed.SpellID != 0 || removed.HasDuration {
		t.Fatalf("removed = %+v", removed)
	}
}

func TestParseAuraUpdateCasterFlagOmitted(t *testing.T) {
	// AFLAG_CASTER means the caster GUID is the target and is not written.
	body := packedGUID(nil, 7)
	body = append(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 55)
	body = append(body, AuraFlagCaster, 10, 1)
	update, err := ParseAuraUpdate(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(update.Auras) != 1 || update.Auras[0].HasCaster {
		t.Fatalf("auras = %+v", update.Auras)
	}
}

func TestParseComboPoints(t *testing.T) {
	body := packedGUID(nil, 99)
	body = append(body, 4)
	combo, err := ParseComboPoints(body)
	if err != nil {
		t.Fatal(err)
	}
	if combo.Target != 99 || combo.Points != 4 {
		t.Fatalf("combo = %+v", combo)
	}
}

func TestParseCorpseReclaimDelay(t *testing.T) {
	delay, err := ParseCorpseReclaimDelay(binary.LittleEndian.AppendUint32(nil, 5000))
	if err != nil {
		t.Fatal(err)
	}
	if delay != 5000 {
		t.Fatalf("delay = %d", delay)
	}
}

func TestParseDeathReleaseLoc(t *testing.T) {
	body := binary.LittleEndian.AppendUint32(nil, 1)
	body = binary.LittleEndian.AppendUint32(body, math.Float32bits(-8949.95))
	body = binary.LittleEndian.AppendUint32(body, math.Float32bits(-132.49))
	body = binary.LittleEndian.AppendUint32(body, math.Float32bits(83.53))
	loc, err := ParseDeathReleaseLoc(body)
	if err != nil {
		t.Fatal(err)
	}
	if !loc.HasLocation || loc.MapID != 1 || loc.Position.Z != 83.53 {
		t.Fatalf("location = %+v", loc)
	}

	clearBody := binary.LittleEndian.AppendUint32(nil, ^uint32(0))
	for i := 0; i < 3; i++ {
		clearBody = binary.LittleEndian.AppendUint32(clearBody, 0)
	}
	cleared, err := ParseDeathReleaseLoc(clearBody)
	if err != nil {
		t.Fatal(err)
	}
	if cleared.HasLocation {
		t.Fatalf("cleared location = %+v", cleared)
	}
}

func TestCreatureQueryRoundTrip(t *testing.T) {
	body := binary.LittleEndian.AppendUint32(nil, 299)
	body = append(body, []byte("Young Wolf\x00")...)
	for i := 0; i < 3; i++ {
		body = append(body, 0)
	}
	body = append(body, []byte("Wolf\x00")...)
	resp, err := ParseCreatureQueryResponse(body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.Entry != 299 || resp.Name != "Young Wolf" || resp.SubName != "Wolf" || resp.Unknown {
		t.Fatalf("response = %+v", resp)
	}
}
