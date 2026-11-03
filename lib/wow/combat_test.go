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
