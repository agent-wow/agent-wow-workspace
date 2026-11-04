package service

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/quest/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

type questSession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
	err  error
}

func (f questSession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	if f.err != nil {
		return nil, f.err
	}
	f.sent <- req
	return &emptypb.Empty{}, nil
}

func expectPacket(t *testing.T, session questSession, opcode uint32) *modv1.SendPacketRequest {
	t.Helper()
	select {
	case got := <-session.sent:
		if got.GetOpcode() != opcode {
			t.Fatalf("opcode = 0x%x, want 0x%x", got.GetOpcode(), opcode)
		}
		return got
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for opcode 0x%x", opcode)
		return nil
	}
}

func questList(guid uint64, questID uint32) []byte {
	body := binary.LittleEndian.AppendUint64(nil, guid)
	body = append(body, "Greeting\x00"...)
	body = binary.LittleEndian.AppendUint32(body, 0) // emote delay
	body = binary.LittleEndian.AppendUint32(body, 1) // emote type
	body = append(body, 1)                           // quest count
	for _, value := range []uint32{questID, 8, 1, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	body = append(body, 0)
	body = append(body, "A Threat Within\x00"...)
	return body
}

func questRef(guid uint64, questID uint32) []byte {
	body := binary.LittleEndian.AppendUint64(nil, guid)
	return binary.LittleEndian.AppendUint32(body, questID)
}

func TestGossipAcceptAndProgress(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	const guid = uint64(0xF13000000000079E)
	const questID = uint32(783)

	if _, err := s.Gossip(t.Context(), &modulev1.GossipRequest{Guid: guid}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverHello)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestgiverQuestList, Payload: questList(guid, questID),
	}); err != nil {
		t.Fatal(err)
	}
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if len(state.GetMenu()) != 1 || state.GetMenu()[0].GetQuestId() != questID || state.GetMenu()[0].GetTitle() != "A Threat Within" {
		t.Fatalf("unexpected menu: %+v", state.GetMenu())
	}

	if _, err := s.Accept(t.Context(), &modulev1.QuestRequest{Guid: guid, QuestId: questID}); err != nil {
		t.Fatal(err)
	}
	accept := expectPacket(t, session, opcode.CMSGQuestgiverAcceptQuest)
	if len(accept.GetPayload()) != 16 || binary.LittleEndian.Uint32(accept.GetPayload()[8:12]) != questID {
		t.Fatalf("bad accept payload: %x", accept.GetPayload())
	}

	kill := make([]byte, 20)
	binary.LittleEndian.PutUint32(kill[0:4], questID)
	binary.LittleEndian.PutUint32(kill[4:8], 525)
	binary.LittleEndian.PutUint32(kill[8:12], 3)
	binary.LittleEndian.PutUint32(kill[12:16], 6)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGQuestupdateAddKill, Payload: kill}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestupdateComplete, Payload: binary.LittleEndian.AppendUint32(nil, questID),
	}); err != nil {
		t.Fatal(err)
	}
	state, _ = s.GetState(t.Context(), &emptypb.Empty{})
	if len(state.GetActive()) != 1 || !state.GetActive()[0].GetComplete() {
		t.Fatalf("quest not complete: %+v", state.GetActive())
	}
	objective := state.GetActive()[0].GetObjectives()[0]
	if objective.GetEntry() != 525 || objective.GetCurrent() != 3 || objective.GetRequired() != 6 {
		t.Fatalf("unexpected objective: %+v", objective)
	}
}

func TestAcceptInvalidReconcilesOptimisticActiveQuest(t *testing.T) {
	for _, test := range []struct {
		name   string
		reason uint32
		active bool
	}{
		{name: "already on", reason: 13, active: true},
		{name: "already on alternate", reason: 18, active: true},
		{name: "already done", reason: 7, active: false},
		{name: "requirements refused", reason: 0, active: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			session := questSession{sent: make(chan *modv1.SendPacketRequest, 1)}
			s := New(session)
			if _, err := s.Accept(t.Context(), &modulev1.QuestRequest{
				Guid: 99, QuestId: 788,
			}); err != nil {
				t.Fatal(err)
			}
			expectPacket(t, session, opcode.CMSGQuestgiverAcceptQuest)
			if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
				Opcode:  opcode.SMSGQuestgiverQuestInvalid,
				Payload: binary.LittleEndian.AppendUint32(nil, test.reason),
			}); err != nil {
				t.Fatal(err)
			}
			state, _ := s.GetState(t.Context(), &emptypb.Empty{})
			if got := len(state.GetActive()) == 1; got != test.active {
				t.Fatalf("active = %v, want %v: %+v", got, test.active, state)
			}
			if state.GetLastError() != fmt.Sprintf("quest invalid: reason %d", test.reason) {
				t.Fatalf("last error = %q", state.GetLastError())
			}
		})
	}
}

func TestTurnInSequence(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)
	const guid = uint64(99)
	const questID = uint32(783)

	if _, err := s.Accept(t.Context(), &modulev1.QuestRequest{Guid: guid, QuestId: questID}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverAcceptQuest)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: guid, QuestId: questID, RewardIndex: 2}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGQuestgiverRequestItems, Payload: questRef(guid, questID)}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverRequestReward)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGQuestgiverOfferReward, Payload: questRef(guid, questID)}); err != nil {
		t.Fatal(err)
	}
	choose := expectPacket(t, session, opcode.CMSGQuestgiverChooseReward)
	if got := binary.LittleEndian.Uint32(choose.GetPayload()[12:16]); got != 2 {
		t.Fatalf("reward index = %d, want 2", got)
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestgiverQuestComplete, Payload: binary.LittleEndian.AppendUint32(nil, questID),
	}); err != nil {
		t.Fatal(err)
	}
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if state.GetCompleted() != 1 || len(state.GetActive()) != 0 || state.GetTurningInQuestId() != 0 {
		t.Fatalf("unexpected final state: %+v", state)
	}
	status, _ := s.Status(t.Context(), &emptypb.Empty{})
	if status.GetRewardRequests() != 1 || status.GetRewardChoices() != 1 || status.GetTurnIns() != 1 {
		t.Fatalf("unexpected counters: %+v", status)
	}
}

func TestRewardResponseMustMatchTurnIn(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 783}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestgiverRequestItems, Payload: questRef(99, 7),
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case packet := <-session.sent:
		t.Fatalf("mismatched response sent opcode 0x%x", packet.GetOpcode())
	case <-time.After(50 * time.Millisecond):
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestgiverRequestItems, Payload: questRef(99, 783),
	}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverRequestReward)
}

func TestRepeatedRewardOfferDoesNotLoop(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 788}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
	for range 2 {
		if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
			Opcode: opcode.SMSGQuestgiverOfferReward, Payload: questRef(99, 788),
		}); err != nil {
			t.Fatal(err)
		}
	}
	expectPacket(t, session, opcode.CMSGQuestgiverChooseReward)
	select {
	case packet := <-session.sent:
		t.Fatalf("duplicate offer sent opcode 0x%x", packet.GetOpcode())
	case <-time.After(50 * time.Millisecond):
	}
	status, _ := s.Status(t.Context(), &emptypb.Empty{})
	if status.GetRewardChoices() != 1 {
		t.Fatalf("reward choices = %d, want 1", status.GetRewardChoices())
	}
}

func TestTurnInExpiresAndCanBeRetried(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 2)}
	s := New(session)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 783}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
	s.mu.Lock()
	s.turningIn.started = time.Now().Add(-turnInTimeout)
	s.mu.Unlock()
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if state.GetTurningInQuestId() != 0 || state.GetErrors() != 1 {
		t.Fatalf("turn-in did not expire: %+v", state)
	}
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 783}); err != nil {
		t.Fatalf("retry after timeout: %v", err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
}

func TestTurnInSendFailureDoesNotCount(t *testing.T) {
	s := New(questSession{err: errors.New("session closed")})
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 783}); err == nil {
		t.Fatal("expected send failure")
	}
	status, _ := s.Status(t.Context(), &emptypb.Empty{})
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if status.GetTurnIns() != 0 || state.GetTurningInQuestId() != 0 {
		t.Fatalf("failed send left state behind: status=%+v state=%+v", status, state)
	}
}

func TestTurnInRejectsConcurrentRequests(t *testing.T) {
	session := questSession{sent: make(chan *modv1.SendPacketRequest, 2)}
	s := New(session)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 783}); err != nil {
		t.Fatal(err)
	}
	expectPacket(t, session, opcode.CMSGQuestgiverCompleteQuest)
	if _, err := s.TurnIn(t.Context(), &modulev1.TurnInRequest{Guid: 99, QuestId: 7}); err == nil {
		t.Fatal("expected concurrent turn-in to fail")
	}
}

func TestQuestFailureDoesNotCountAsCompletion(t *testing.T) {
	s := New(questSession{sent: make(chan *modv1.SendPacketRequest, 2)})
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGQuestgiverQuestFailed, Payload: binary.LittleEndian.AppendUint32(nil, 783),
	}); err != nil {
		t.Fatal(err)
	}
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if state.GetCompleted() != 0 || state.GetErrors() != 1 || state.GetLastQuestId() != 783 {
		t.Fatalf("unexpected failure state: %+v", state)
	}
}

func TestQuestPacketLayout(t *testing.T) {
	// Guard the shared codec assumption at the module boundary as well.
	got := wow.EncodeQuestGiverAcceptQuest(1, 2)
	if len(got) != 16 || binary.LittleEndian.Uint32(got[12:16]) != 0 {
		t.Fatalf("bad accept layout: %x", got)
	}
}
