package service

import (
	"context"
	"encoding/binary"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/character/src/api"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakeSession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
}

func (s *fakeSession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	s.sent <- req
	return &emptypb.Empty{}, nil
}

func trainerPacket(guid uint64, spell uint32, usable byte, cost uint32) *modv1.WorldPacket {
	body := binary.LittleEndian.AppendUint64(nil, guid)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 1)
	body = binary.LittleEndian.AppendUint32(body, spell)
	body = append(body, usable)
	body = binary.LittleEndian.AppendUint32(body, cost)
	body = append(body, make([]byte, 8)...)
	body = append(body, 6)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = append(body, make([]byte, 12)...)
	body = append(body, 0)
	return &modv1.WorldPacket{Opcode: opcode.SMSGTrainerList, Payload: body}
}

func TestTrainerBuyRequiresCurrentServerRow(t *testing.T) {
	session := &fakeSession{sent: make(chan *modv1.SendPacketRequest, 2)}
	s := New(session)
	if _, err := s.OpenTrainer(t.Context(), &modulev1.TrainerRequest{TrainerGuid: 9}); err != nil {
		t.Fatal(err)
	}
	if (<-session.sent).GetOpcode() != opcode.CMSGTrainerList {
		t.Fatal("trainer list was not requested")
	}
	if _, err := s.OnPacket(t.Context(), trainerPacket(9, 585, 0, 10)); err != nil {
		t.Fatal(err)
	}
	if _, err := s.BuySpell(t.Context(), &modulev1.BuySpellRequest{TrainerGuid: 9, SpellId: 999}); err == nil {
		t.Fatal("unlisted trainer spell was purchased")
	}
	if _, err := s.BuySpell(t.Context(), &modulev1.BuySpellRequest{TrainerGuid: 9, SpellId: 585}); err != nil {
		t.Fatal(err)
	}
	packet := <-session.sent
	if packet.GetOpcode() != opcode.CMSGTrainerBuySpell || len(packet.GetPayload()) != 12 {
		t.Fatalf("trainer buy packet = %+v", packet)
	}
	if s.snapshot().GetTrainerSpells()[0].GetMoneyCost() != 10 {
		t.Fatal("server trainer cost was not preserved")
	}
}

func TestTrainerBuyResultsDistinguishSuccessFromUnavailable(t *testing.T) {
	s := New(nil)
	success := binary.LittleEndian.AppendUint64(nil, 9)
	success = binary.LittleEndian.AppendUint32(success, 585)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGTrainerBuySucceeded, Payload: success}); err != nil {
		t.Fatal(err)
	}
	if state := s.snapshot(); !state.GetLastBuyResultKnown() || !state.GetLastBuySucceeded() {
		t.Fatalf("success state = %+v", state)
	}
	failed := binary.LittleEndian.AppendUint64(nil, 9)
	failed = binary.LittleEndian.AppendUint32(failed, 999)
	failed = binary.LittleEndian.AppendUint32(failed, 0)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGTrainerBuyFailed, Payload: failed}); err != nil {
		t.Fatal(err)
	}
	if state := s.snapshot(); !state.GetLastBuyResultKnown() || state.GetLastBuySucceeded() || state.GetLastBuySpellId() != 999 {
		t.Fatalf("failure state = %+v", state)
	}
}

func TestLearnTalentSendsTalentAndZeroBasedRank(t *testing.T) {
	session := &fakeSession{sent: make(chan *modv1.SendPacketRequest, 1)}
	s := New(session)
	if _, err := s.LearnTalent(t.Context(), &modulev1.TalentLearnRequest{}); err == nil {
		t.Fatal("zero talent id was accepted")
	}
	select {
	case packet := <-session.sent:
		t.Fatalf("invalid request sent packet %+v", packet)
	default:
	}
	if _, err := s.LearnTalent(t.Context(), &modulev1.TalentLearnRequest{TalentId: 130, RequestedRank: 0}); err != nil {
		t.Fatal(err)
	}
	packet := <-session.sent
	if packet.GetOpcode() != opcode.CMSGLearnTalent || len(packet.GetPayload()) != 8 ||
		binary.LittleEndian.Uint32(packet.GetPayload()) != 130 ||
		binary.LittleEndian.Uint32(packet.GetPayload()[4:]) != 0 {
		t.Fatalf("learn talent packet = %+v", packet)
	}
}

func TestTalentSnapshotKeepsZeroBasedRank(t *testing.T) {
	s := New(nil)
	body := []byte{0, 2, 0, 0, 0, 1, 0, 1}
	body = binary.LittleEndian.AppendUint32(body, 1001)
	body = append(body, 0)
	body = append(body, 6)
	body = append(body, make([]byte, 12)...)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGTalentsInfo, Payload: body}); err != nil {
		t.Fatal(err)
	}
	state := s.snapshot()
	if !state.GetHasTalents() || state.GetFreeTalentPoints() != 2 || len(state.GetTalentGroups()) != 1 || state.GetTalentGroups()[0].GetTalents()[0].GetRank() != 0 {
		t.Fatalf("talent snapshot = %+v", state)
	}
}

func TestGameObjectUseSendsOnlyGuid(t *testing.T) {
	session := &fakeSession{sent: make(chan *modv1.SendPacketRequest, 1)}
	s := New(session)
	if _, err := s.UseGameObject(t.Context(), &modulev1.GameObjectUseRequest{Guid: 77}); err != nil {
		t.Fatal(err)
	}
	packet := <-session.sent
	if packet.GetOpcode() != opcode.CMSGGameobjUse || len(packet.GetPayload()) != 8 || binary.LittleEndian.Uint64(packet.GetPayload()) != 77 {
		t.Fatalf("game object packet = %+v", packet)
	}
	if s.snapshot().GetLastGameObjectGuid() != 77 {
		t.Fatal("game object request was not exposed")
	}
}
