package service

import (
	"bytes"
	"context"
	"encoding/binary"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/combat/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/protobuf/types/known/emptypb"
)

func TestTradeSkillCastPreservesAnEmptyTarget(t *testing.T) {
	var sent []byte
	session := combatSession{send: func(_ context.Context, req *modv1.SendPacketRequest) (*emptypb.Empty, error) {
		if req.GetOpcode() == opcode.CMSGCastSpell {
			sent = req.GetPayload()
		}
		return &emptypb.Empty{}, nil
	}}
	s := New(session)
	if _, err := s.Attack(t.Context(), &modulev1.AttackRequest{Guid: 99}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Cast(t.Context(), &modulev1.CastRequest{
		SpellId: 3275, PreserveEmptyTarget: true,
	}); err != nil {
		t.Fatal(err)
	}
	if want := wow.EncodeCastSpell(0, 3275, 0); !bytes.Equal(sent, want) {
		t.Fatalf("cast payload = %x, want %x", sent, want)
	}
}

func TestGatheringCastTargetsTheGameObject(t *testing.T) {
	var sent []byte
	session := combatSession{send: func(_ context.Context, req *modv1.SendPacketRequest) (*emptypb.Empty, error) {
		sent = req.GetPayload()
		return &emptypb.Empty{}, nil
	}}
	s := New(session)
	if _, err := s.Cast(t.Context(), &modulev1.CastRequest{
		SpellId: 2366, TargetGuid: 55, TargetIsGameObject: true,
	}); err != nil {
		t.Fatal(err)
	}
	if want := wow.EncodeCastSpellGameObject(0, 2366, 55); !bytes.Equal(sent, want) {
		t.Fatalf("cast payload = %x, want %x", sent, want)
	}
	if _, err := s.Cast(t.Context(), &modulev1.CastRequest{
		SpellId: 2366, TargetIsGameObject: true,
	}); err == nil {
		t.Fatal("zero game-object target was accepted")
	}
}

func TestSpellbookUsesInitialAndLearnedServerPackets(t *testing.T) {
	s := New(nil)
	initial, err := s.GetSpells(t.Context(), &emptypb.Empty{})
	if err != nil || initial.GetInitialized() {
		t.Fatalf("fresh spellbook initialized = %v, %v", initial.GetInitialized(), err)
	}
	body := []byte{0, 1, 0}
	body = binary.LittleEndian.AppendUint32(body, 585)
	body = binary.LittleEndian.AppendUint16(body, 0)
	body = binary.LittleEndian.AppendUint16(body, 0)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGInitialSpells, Payload: body}); err != nil {
		t.Fatal(err)
	}
	learned := binary.LittleEndian.AppendUint32(nil, 2050)
	learned = binary.LittleEndian.AppendUint16(learned, 0)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGLearnedSpell, Payload: learned}); err != nil {
		t.Fatal(err)
	}
	spells, err := s.GetSpells(t.Context(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(spells.GetSpells()) != 2 || spells.GetSpells()[0].GetSpellId() != 585 || spells.GetSpells()[1].GetSpellId() != 2050 {
		t.Fatalf("spellbook = %+v", spells)
	}
	if !spells.GetInitialized() {
		t.Fatal("initial spells did not initialize the spellbook")
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGNewWorld}); err != nil {
		t.Fatal(err)
	}
	spells, _ = s.GetSpells(t.Context(), &emptypb.Empty{})
	if len(spells.GetSpells()) != 2 || !spells.GetInitialized() {
		t.Fatal("map transition discarded the current login's spellbook")
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGLoginVerifyWorld}); err != nil {
		t.Fatal(err)
	}
	spells, _ = s.GetSpells(t.Context(), &emptypb.Empty{})
	if len(spells.GetSpells()) != 0 || spells.GetInitialized() {
		t.Fatal("new login retained a stale spellbook")
	}
}
