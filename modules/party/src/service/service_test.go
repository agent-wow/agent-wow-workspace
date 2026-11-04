package service

import (
	"context"
	"encoding/binary"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/party/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

type partySession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
}

func (f partySession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	f.sent <- req
	return &emptypb.Empty{}, nil
}

func invitePacket(name string) *modv1.WorldPacket {
	body := append([]byte{0}, name...)
	body = append(body, 0)
	body = append(body, make([]byte, 9)...)
	return &modv1.WorldPacket{Opcode: opcode.SMSGGroupInvite, Payload: body}
}

func TestInviteNeverAutomaticallyAccepted(t *testing.T) {
	session := partySession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	if _, err := s.OnPacket(t.Context(), invitePacket("Alpha")); err != nil {
		t.Fatal(err)
	}
	select {
	case packet := <-session.sent:
		t.Fatalf("invite caused an automatic packet: %v", packet)
	default:
	}
	state, _ := s.GetState(t.Context(), &emptypb.Empty{})
	if state.GetPendingInvite().GetInviter() != "Alpha" || state.GetPendingInvite().GetInviteId() == 0 {
		t.Fatalf("invite not exposed for explicit consent: %+v", state)
	}
}

func TestInviteResponseRequiresCurrentIDInviterAndTTL(t *testing.T) {
	session := partySession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	s.now = func() time.Time { return time.Unix(1000, 0) }
	_, _ = s.OnPacket(t.Context(), invitePacket("Alpha"))
	invite := s.state().GetPendingInvite()
	if _, err := s.RespondInvite(t.Context(), &modulev1.InviteResponse{InviteId: invite.GetInviteId() + 1, Inviter: "Alpha", Accept: true}); err == nil {
		t.Fatal("stale invite id accepted")
	}
	if _, err := s.RespondInvite(t.Context(), &modulev1.InviteResponse{InviteId: invite.GetInviteId(), Inviter: "Mallory", Accept: true}); err == nil {
		t.Fatal("wrong inviter accepted")
	}
	s.now = func() time.Time { return time.Unix(1031, 0) }
	if _, err := s.RespondInvite(t.Context(), &modulev1.InviteResponse{InviteId: invite.GetInviteId(), Inviter: "Alpha", Accept: true}); err == nil {
		t.Fatal("expired invite accepted")
	}
	if len(session.sent) != 0 {
		t.Fatal("invalid responses sent packets")
	}
}

func TestExplicitAcceptIsSingleUse(t *testing.T) {
	session := partySession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	_, _ = s.OnPacket(t.Context(), invitePacket("Alpha"))
	invite := s.state().GetPendingInvite()
	request := &modulev1.InviteResponse{InviteId: invite.GetInviteId(), Inviter: "Alpha", Accept: true}
	if _, err := s.RespondInvite(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	if _, err := s.RespondInvite(t.Context(), request); err == nil {
		t.Fatal("same consent accepted twice")
	}
	packet := <-session.sent
	if packet.GetOpcode() != opcode.CMSGGroupAccept || len(packet.GetPayload()) != 4 {
		t.Fatalf("accept = %v", packet)
	}
}

func TestPartyChatUsesServerSenderAndModuleEpoch(t *testing.T) {
	s := New(nil)
	body := []byte{wow.ChatMsgParty}
	body = binary.LittleEndian.AppendUint32(body, wow.LangAddon)
	body = binary.LittleEndian.AppendUint64(body, 777)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint64(body, 0)
	message := "AGW1|PROPOSE|sender-is-not-in-text"
	body = binary.LittleEndian.AppendUint32(body, uint32(len(message)+1))
	body = append(body, message...)
	body = append(body, 0, 0)
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGMessagechat, Payload: body})
	messages := s.state().GetMessages()
	if len(messages) != 1 || messages[0].GetSenderGuid() != 777 || messages[0].GetEpoch() != s.epoch || messages[0].GetSequence() != 1 {
		t.Fatalf("messages = %+v", messages)
	}
}

func TestGroupRosterIncludesOwnServerSessionIdentity(t *testing.T) {
	s := New(nil)
	s.selfGUID = 101
	s.selfName = "Alpha"
	body := []byte{0, 0, 0, 0}
	body = binary.LittleEndian.AppendUint64(body, 501)
	body = binary.LittleEndian.AppendUint32(body, 1)
	body = binary.LittleEndian.AppendUint32(body, 1)
	body = append(body, "Bravo\x00"...)
	body = binary.LittleEndian.AppendUint64(body, 102)
	body = append(body, 1, 0, 0, 0)
	body = binary.LittleEndian.AppendUint64(body, 101)
	body = append(body, 2)
	body = binary.LittleEndian.AppendUint64(body, 0)
	body = append(body, 2, 0, 0, 0)
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGGroupList, Payload: body})
	state := s.state()
	if !state.GetInGroup() || len(state.GetMembers()) != 2 || !state.GetMembers()[0].GetIsSelf() || state.GetLeaderGuid() != 101 || state.GetLootMethod() != 2 {
		t.Fatalf("group = %+v", state)
	}
}
