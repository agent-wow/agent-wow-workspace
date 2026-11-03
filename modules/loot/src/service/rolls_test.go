package service

import (
	"encoding/binary"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/loot/src/api"
)

func startRollPacket(guid uint64, slot uint32) *modv1.WorldPacket {
	body := binary.LittleEndian.AppendUint64(nil, guid)
	for _, value := range []uint32{1, slot, 19019, 0, 0, 1, 60000} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	body = append(body, 7)
	return &modv1.WorldPacket{Opcode: opcode.SMSGLootStartRoll, Payload: body}
}

func TestRollVoteIsSingleUseAndServerConfirmed(t *testing.T) {
	session := lootSession{sent: make(chan *modv1.SendPacketRequest, 2)}
	s := New(session)
	s.selfGUID = 101
	s.now = func() time.Time { return time.UnixMilli(1000) }
	_, _ = s.OnPacket(t.Context(), startRollPacket(700, 2))
	request := &modulev1.RollRequest{ItemGuid: 700, Slot: 2, Vote: 1}
	state, err := s.Roll(t.Context(), request)
	if err != nil || len(state.GetRolls()) != 1 || state.GetRolls()[0].GetOwnVote() != 1 || state.GetRolls()[0].GetVoteConfirmed() {
		t.Fatalf("roll = %+v, %v", state.GetRolls(), err)
	}
	if _, err := s.Roll(t.Context(), request); err == nil {
		t.Fatal("duplicate vote dispatched")
	}
	if packet := <-session.sent; packet.GetOpcode() != opcode.CMSGLootRoll || len(packet.GetPayload()) != 13 {
		t.Fatalf("vote packet = %v", packet)
	}
	body := binary.LittleEndian.AppendUint64(nil, 700)
	body = binary.LittleEndian.AppendUint32(body, 2)
	body = binary.LittleEndian.AppendUint64(body, 101)
	for _, value := range []uint32{19019, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	body = append(body, 90, 0, 0) // server roll type 0 means need
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGLootRoll, Payload: body})
	if !s.state().GetRolls()[0].GetVoteConfirmed() {
		t.Fatal("server vote did not confirm the dispatched choice")
	}
}

func TestLateVoteAndDuplicateStartAreRejected(t *testing.T) {
	session := lootSession{sent: make(chan *modv1.SendPacketRequest, 2)}
	s := New(session)
	_, _ = s.OnPacket(t.Context(), startRollPacket(700, 2))
	_, _ = s.Roll(t.Context(), &modulev1.RollRequest{ItemGuid: 700, Slot: 2, Vote: 0})
	_, _ = s.OnPacket(t.Context(), startRollPacket(700, 2))
	if got := s.state().GetRolls()[0].GetOwnVote(); got != 0 {
		t.Fatalf("duplicate start reset vote to %d", got)
	}
	won := binary.LittleEndian.AppendUint64(nil, 700)
	for _, value := range []uint32{2, 19019, 0, 0} {
		won = binary.LittleEndian.AppendUint32(won, value)
	}
	won = binary.LittleEndian.AppendUint64(won, 999)
	won = append(won, 1, 0)
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGLootRollWon, Payload: won})
	if _, err := s.Roll(t.Context(), &modulev1.RollRequest{ItemGuid: 700, Slot: 2, Vote: 1}); err == nil {
		t.Fatal("late vote accepted after resolution")
	}
	if len(session.sent) != 1 {
		t.Fatalf("sent %d vote packets, want 1", len(session.sent))
	}
}

func TestAllPassedResolvesRoll(t *testing.T) {
	s := New(nil)
	_, _ = s.OnPacket(t.Context(), startRollPacket(700, 2))
	body := binary.LittleEndian.AppendUint64(nil, 700)
	for _, value := range []uint32{2, 19019, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGLootAllPassed, Payload: body})
	roll := s.state().GetRolls()[0]
	if roll.GetActive() || roll.GetOutcome() != "all_passed" {
		t.Fatalf("roll = %+v", roll)
	}
}
