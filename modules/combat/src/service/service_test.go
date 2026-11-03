package service

import (
	"context"
	"encoding/binary"
	"math"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/combat/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

func packedGUID(b []byte, guid uint64) []byte {
	var mask byte
	for i := 0; i < 8; i++ {
		if byte(guid>>(8*i)) != 0 {
			mask |= 1 << i
		}
	}
	b = append(b, mask)
	for i := 0; i < 8; i++ {
		if v := byte(guid >> (8 * i)); v != 0 {
			b = append(b, v)
		}
	}
	return b
}

func selfValuesUpdate(guid uint64, values map[uint32]uint32) []byte {
	var maxIndex uint32
	for index := range values {
		if index > maxIndex {
			maxIndex = index
		}
	}
	blocks := int(maxIndex/32) + 1
	masks := make([]uint32, blocks)
	for index := range values {
		masks[index/32] |= 1 << (index % 32)
	}
	body := binary.LittleEndian.AppendUint32(nil, 1)
	body = append(body, wow.UpdateValues)
	body = packedGUID(body, guid)
	body = append(body, byte(blocks))
	for _, mask := range masks {
		body = binary.LittleEndian.AppendUint32(body, mask)
	}
	for block, mask := range masks {
		for bit := 0; bit < 32; bit++ {
			if mask&(1<<bit) == 0 {
				continue
			}
			index := uint32(block*32 + bit)
			body = binary.LittleEndian.AppendUint32(body, values[index])
		}
	}
	return body
}

func TestAttackStartAndStop(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)

	start := binary.LittleEndian.AppendUint64(nil, 42)
	start = binary.LittleEndian.AppendUint64(start, 99)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackstart, Payload: start,
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.TargetGuid != 99 || !state.Attacking || !state.InCombat {
		t.Fatalf("state after start = %+v", state)
	}

	stop := packedGUID(nil, 42)
	stop = packedGUID(stop, 99)
	stop = binary.LittleEndian.AppendUint32(stop, 1)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackstop, Payload: stop,
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if state.Attacking || !state.InCombat || !state.TargetDead {
		t.Fatalf("state after stop = %+v", state)
	}

	clearCombat := selfValuesUpdate(42, map[uint32]uint32{wow.UnitFieldFlags: 0})
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: clearCombat,
	}); err != nil {
		t.Fatal(err)
	}
	if state = mustState(t, s); state.InCombat {
		t.Fatalf("state after combat flag clear = %+v", state)
	}
}

func TestXPGainAndLevelUp(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)

	xp := binary.LittleEndian.AppendUint64(nil, 99)
	xp = binary.LittleEndian.AppendUint32(xp, 55)
	xp = append(xp, 0)
	xp = binary.LittleEndian.AppendUint32(xp, 50)
	xp = binary.LittleEndian.AppendUint32(xp, 0)
	xp = append(xp, 0)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLogXpgain, Payload: xp,
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.Kills != 1 || state.LastXp != 55 || state.LastVictim != 99 {
		t.Fatalf("state after xp = %+v", state)
	}

	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLevelupInfo, Payload: binary.LittleEndian.AppendUint32(nil, 2),
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if state.LevelUps != 1 || state.SelfLevel != 2 {
		t.Fatalf("state after level = %+v", state)
	}
}

func TestSelfUpdateFieldsAndDeath(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)

	body := selfValuesUpdate(42, map[uint32]uint32{
		wow.UnitFieldHealth:    30,
		wow.UnitFieldMaxHealth: 60,
		wow.UnitFieldLevel:     2,
		wow.PlayerXP:           123,
		wow.PlayerNextLevelXP:  400,
	})
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.SelfHealth != 30 || state.SelfMaxHealth != 60 || state.SelfLevel != 2 ||
		state.SelfXp != 123 || state.SelfNextLevelXp != 400 || state.SelfDead {
		t.Fatalf("state = %+v", state)
	}

	dead := selfValuesUpdate(42, map[uint32]uint32{wow.UnitFieldHealth: 0})
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: dead,
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if !state.SelfDead || state.Deaths != 1 {
		t.Fatalf("state after death = %+v", state)
	}
}

func TestAttackRefusal(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackswingNotinrange,
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.Refusals != 1 || state.Attacking || state.LastError == "" {
		t.Fatalf("state = %+v", state)
	}
}

func TestMalformedCombatPacketDoesNotFail(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackstart, Payload: []byte{1},
	}); err != nil {
		t.Fatal(err)
	}
	status, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if status.ParseErrors != 1 {
		t.Fatalf("status = %+v", status)
	}
}

type combatSession struct {
	modv1.SessionClient
	send func(context.Context, *modv1.SendPacketRequest) (*emptypb.Empty, error)
}

func (f combatSession) SendPacket(ctx context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	return f.send(ctx, req)
}

func TestAttackDoesNotEraseImmediateRefusal(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	var s *Service
	session := combatSession{send: func(ctx context.Context, req *modv1.SendPacketRequest) (*emptypb.Empty, error) {
		if req.GetOpcode() == opcode.CMSGAttackswing {
			if _, err := s.OnPacket(ctx, &modv1.WorldPacket{Opcode: opcode.SMSGAttackswingNotinrange}); err != nil {
				t.Fatal(err)
			}
		}
		return &emptypb.Empty{}, nil
	}}
	s = New(session)
	state, err := s.Attack(context.Background(), &modulev1.AttackRequest{Guid: 99})
	if err != nil {
		t.Fatal(err)
	}
	if state.LastError == "" || state.Refusals != 1 {
		t.Fatalf("attack return erased refusal: %+v", state)
	}
}

func TestRefusalDoesNotClearCombat(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	body := selfValuesUpdate(42, map[uint32]uint32{wow.UnitFieldFlags: wow.UnitFlagInCombat})
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackswingNotinrange,
	}); err != nil {
		t.Fatal(err)
	}
	if state := mustState(t, s); !state.InCombat || state.Attacking {
		t.Fatalf("state after refusal = %+v", state)
	}
}

func TestPrimaryPowerUsesClassSlot(t *testing.T) {
	cases := []struct {
		name      string
		classID   uint32
		powerType uint32
		slot      uint32
		power     uint32
		maxPower  uint32
		wantRage  uint32
	}{
		{name: "warrior rage", classID: 1, powerType: wow.PowerRage, slot: wow.UnitFieldPower2, power: 40, maxPower: 100, wantRage: 40},
		{name: "priest mana", classID: 5, powerType: wow.PowerMana, slot: wow.UnitFieldPower1, power: 450, maxPower: 500, wantRage: 0},
		{name: "rogue energy", classID: 4, powerType: wow.PowerEnergy, slot: wow.UnitFieldPower4, power: 60, maxPower: 100, wantRage: 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
			s := New(nil)
			bytes0 := (tc.powerType << 24) | (tc.classID << 8) | 1
			values := map[uint32]uint32{
				wow.UnitFieldBytes0: bytes0,
				wow.UnitFieldHealth: 100,
				tc.slot:             tc.power,
			}
			// MAXPOWER1 is eight slots after POWER1.
			values[tc.slot+8] = tc.maxPower
			if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
				Opcode: opcode.SMSGUpdateObject, Payload: selfValuesUpdate(42, values),
			}); err != nil {
				t.Fatal(err)
			}
			state := mustState(t, s)
			if state.SelfPowerType != tc.powerType || state.SelfPower != tc.power || state.SelfMaxPower != tc.maxPower {
				t.Fatalf("power = type %d %d/%d, want type %d %d/%d",
					state.SelfPowerType, state.SelfPower, state.SelfMaxPower,
					tc.powerType, tc.power, tc.maxPower)
			}
			if state.SelfRage != tc.wantRage {
				t.Fatalf("legacy self_rage = %d, want %d", state.SelfRage, tc.wantRage)
			}
		})
	}
}

func TestGhostAndDeathFacts(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject,
		Payload: selfValuesUpdate(42, map[uint32]uint32{
			wow.UnitFieldHealth: 0, wow.UnitFieldMaxHealth: 100,
		}),
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if !state.SelfDead || state.Deaths != 1 {
		t.Fatalf("state after health zero = %+v", state)
	}

	// Releasing the spirit sets PLAYER_FLAGS ghost while health may be full.
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject,
		Payload: selfValuesUpdate(42, map[uint32]uint32{
			wow.UnitFieldHealth: 100,
			wow.PlayerFlags:     wow.PlayerFlagGhost,
		}),
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if !state.SelfDead || !state.SelfGhost || state.Deaths != 1 {
		t.Fatalf("state as ghost = %+v", state)
	}
}

func TestCorpseReclaimAndSpiritHealerLocation(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode:  opcode.SMSGCorpseReclaimDelay,
		Payload: binary.LittleEndian.AppendUint32(nil, 5000),
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.CorpseReclaimDelayMs != 5000 || state.CorpseReclaimRemainingMs == 0 ||
		state.CorpseReclaimRemainingMs > 5000 {
		t.Fatalf("corpse reclaim = %d/%d", state.CorpseReclaimDelayMs, state.CorpseReclaimRemainingMs)
	}

	loc := binary.LittleEndian.AppendUint32(nil, 1)
	loc = binary.LittleEndian.AppendUint32(loc, math.Float32bits(-8949.95))
	loc = binary.LittleEndian.AppendUint32(loc, math.Float32bits(-132.49))
	loc = binary.LittleEndian.AppendUint32(loc, math.Float32bits(83.53))
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGDeathReleaseLoc, Payload: loc,
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if !state.HasSpiritHealerLocation || state.SpiritHealerMapId != 1 ||
		state.SpiritHealerPosition.GetZ() != 83.53 {
		t.Fatalf("healer location = %+v", state)
	}

	clear := binary.LittleEndian.AppendUint32(nil, ^uint32(0))
	for i := 0; i < 3; i++ {
		clear = binary.LittleEndian.AppendUint32(clear, 0)
	}
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGDeathReleaseLoc, Payload: clear,
	}); err != nil {
		t.Fatal(err)
	}
	state = mustState(t, s)
	if state.HasSpiritHealerLocation || state.SpiritHealerPosition != nil {
		t.Fatalf("cleared healer location = %+v", state)
	}

	status, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if status.DeathReleaseLocations != 2 || status.CorpseReclaimDelays != 1 {
		t.Fatalf("status = %+v", status)
	}
}

func TestTargetUpdateFacts(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	start := binary.LittleEndian.AppendUint64(nil, 42)
	start = binary.LittleEndian.AppendUint64(start, 99)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackstart, Payload: start,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject,
		Payload: selfValuesUpdate(99, map[uint32]uint32{
			wow.ObjectFieldEntry:   299,
			wow.UnitFieldLevel:     5,
			wow.UnitFieldHealth:    20,
			wow.UnitFieldMaxHealth: 40,
		}),
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.TargetEntry != 299 || state.TargetLevel != 5 ||
		state.TargetHealth != 20 || state.TargetMaxHealth != 40 {
		t.Fatalf("target facts = %+v", state)
	}
}

func TestWorldResetClearsTargetAndKeepsProgression(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	start := binary.LittleEndian.AppendUint64(nil, 42)
	start = binary.LittleEndian.AppendUint64(start, 99)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGAttackstart, Payload: start,
	}); err != nil {
		t.Fatal(err)
	}
	xp := binary.LittleEndian.AppendUint64(nil, 99)
	xp = binary.LittleEndian.AppendUint32(xp, 55)
	xp = append(xp, 0)
	xp = binary.LittleEndian.AppendUint32(xp, 50)
	xp = binary.LittleEndian.AppendUint32(xp, 0)
	xp = append(xp, 0)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLogXpgain, Payload: xp,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject,
		Payload: selfValuesUpdate(42, map[uint32]uint32{
			wow.UnitFieldHealth: 10, wow.PlayerFlags: wow.PlayerFlagGhost,
		}),
	}); err != nil {
		t.Fatal(err)
	}

	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGNewWorld, Payload: nil,
	}); err != nil {
		t.Fatal(err)
	}
	state := mustState(t, s)
	if state.HasTarget || state.TargetGuid != 0 || state.Attacking || state.InCombat {
		t.Fatalf("target survived world change: %+v", state)
	}
	if state.SelfGhost || state.SelfDead || state.SelfHealth != 0 {
		t.Fatalf("stale self death facts survived: %+v", state)
	}
	if state.Kills != 1 || state.LastXp != 55 {
		t.Fatalf("progression was reset: %+v", state)
	}
	status, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if status.WorldResets != 1 {
		t.Fatalf("world resets = %d", status.WorldResets)
	}

	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: nil,
	}); err != nil {
		t.Fatal(err)
	}
	status, _ = s.Status(context.Background(), &emptypb.Empty{})
	if status.WorldResets != 2 {
		t.Fatalf("world resets after login = %d", status.WorldResets)
	}
}

func mustState(t *testing.T, s *Service) *modulev1.CombatState {
	t.Helper()
	state, err := s.GetState(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	return state
}
