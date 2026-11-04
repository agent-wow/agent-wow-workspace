// Package service exposes server-backed social, group and party-chat facts.
// It never accepts an invitation without an explicit, current RPC decision.
package service

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/binary"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/party/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	inviteTTL   = 30 * time.Second
	chatHistory = 64
)

type pendingInvite struct {
	id         uint64
	inviter    string
	receivedAt time.Time
	expiresAt  time.Time
}

type Service struct {
	modulev1.UnimplementedModuleServer
	session  modv1.SessionClient
	selfGUID wow.GUID
	selfName string
	epoch    uint64
	now      func() time.Time

	mu          sync.Mutex
	revision    uint64
	contacts    map[wow.GUID]*modulev1.Contact
	who         []*modulev1.WhoPlayer
	invite      *pendingInvite
	nextInvite  uint64
	group       *wow.GroupList
	messages    []*modulev1.ChatMessage
	chatSeq     uint64
	parseErrors uint32
	sentActions uint32
}

func New(session modv1.SessionClient) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	var random [8]byte
	_, _ = cryptorand.Read(random[:])
	epoch := binary.LittleEndian.Uint64(random[:])
	if epoch == 0 {
		epoch = uint64(time.Now().UnixNano())
	}
	return &Service{
		session:  session,
		selfGUID: wow.GUID(guid),
		selfName: os.Getenv("AGENT_WOW_CHARACTER_NAME"),
		epoch:    epoch,
		now:      time.Now,
		contacts: make(map[wow.GUID]*modulev1.Contact),
	}
}

// Start is retained for the module lifecycle; packet callbacks never send.
func (s *Service) Start(context.Context) {}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.PartyState, error) {
	return s.state(), nil
}

func (s *Service) RequestContacts(ctx context.Context, _ *emptypb.Empty) (*modulev1.PartyState, error) {
	if err := s.send(ctx, opcode.CMSGContactList, wow.EncodeContactList(wow.SocialFlagFriend)); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) AddFriend(ctx context.Context, req *modulev1.AddFriendRequest) (*modulev1.PartyState, error) {
	if err := validName(req.GetName()); err != nil {
		return nil, err
	}
	if strings.ContainsRune(req.GetNote(), 0) || len(req.GetNote()) > 128 {
		return nil, status.Error(codes.InvalidArgument, "note must be NUL-free and at most 128 bytes")
	}
	if err := s.send(ctx, opcode.CMSGAddFriend, wow.EncodeAddFriend(req.GetName(), req.GetNote())); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) Who(ctx context.Context, req *modulev1.WhoRequest) (*modulev1.PartyState, error) {
	if err := validName(req.GetName()); err != nil {
		return nil, err
	}
	if req.GetMinLevel() > req.GetMaxLevel() || req.GetMaxLevel() > 255 {
		return nil, status.Error(codes.InvalidArgument, "require 0 <= min_level <= max_level <= 255")
	}
	body := wow.EncodeWho(req.GetName(), req.GetMinLevel(), req.GetMaxLevel())
	if err := s.send(ctx, opcode.CMSGWho, body); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) Invite(ctx context.Context, req *modulev1.NameRequest) (*modulev1.PartyState, error) {
	if err := validName(req.GetName()); err != nil {
		return nil, err
	}
	if err := s.send(ctx, opcode.CMSGGroupInvite, wow.EncodeGroupInvite(req.GetName())); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) RespondInvite(ctx context.Context, req *modulev1.InviteResponse) (*modulev1.PartyState, error) {
	s.mu.Lock()
	invite := s.invite
	valid := invite != nil && invite.id == req.GetInviteId() && invite.inviter == req.GetInviter() && s.now().Before(invite.expiresAt)
	if !valid {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "invite id, inviter or expiry does not match the current invitation")
	}
	// A response is single-use even if its response is lost. A new server invite
	// gets a new id and may then receive a fresh explicit decision.
	s.invite = nil
	s.revision++
	s.mu.Unlock()
	op := uint32(opcode.CMSGGroupDecline)
	var body []byte
	if req.GetAccept() {
		op = opcode.CMSGGroupAccept
		body = wow.EncodeGroupAccept()
	}
	if err := s.send(ctx, op, body); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) SetLootMethod(ctx context.Context, req *modulev1.LootMethodRequest) (*modulev1.PartyState, error) {
	if req.GetMethod() > 4 || req.GetThreshold() > 6 {
		return nil, status.Error(codes.InvalidArgument, "loot method must be 0..4 and threshold 0..6")
	}
	body := wow.EncodeLootMethod(req.GetMethod(), wow.GUID(req.GetMasterGuid()), req.GetThreshold())
	if err := s.send(ctx, opcode.CMSGLootMethod, body); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) LeaveParty(ctx context.Context, _ *emptypb.Empty) (*modulev1.PartyState, error) {
	if err := s.send(ctx, opcode.CMSGGroupDisband, nil); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) SendPartyChat(ctx context.Context, req *modulev1.ChatRequest) (*modulev1.PartyState, error) {
	if req.GetMessage() == "" || len(req.GetMessage()) > 512 || strings.ContainsRune(req.GetMessage(), 0) {
		return nil, status.Error(codes.InvalidArgument, "message must be non-empty, NUL-free and at most 512 bytes")
	}
	if err := s.send(ctx, opcode.CMSGMessagechat, wow.EncodeChat(wow.ChatMsgParty, wow.LangAddon, req.GetMessage())); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	switch packet.GetOpcode() {
	case opcode.SMSGGroupInvite:
		name, err := wow.ParseGroupInvite(packet.GetPayload())
		if err != nil {
			s.parseError("group invite", err)
			break
		}
		now := s.now()
		s.mu.Lock()
		s.nextInvite++
		s.invite = &pendingInvite{id: s.nextInvite, inviter: name, receivedAt: now, expiresAt: now.Add(inviteTTL)}
		s.revision++
		s.mu.Unlock()
		slog.Info("group invite pending explicit decision", "inviter", name)
	case opcode.SMSGContactList:
		contacts, err := wow.ParseContactList(packet.GetPayload())
		if err != nil {
			s.parseError("contact list", err)
			break
		}
		s.mu.Lock()
		for _, contact := range contacts {
			s.contacts[contact.GUID] = contactProto(contact)
		}
		s.revision++
		s.mu.Unlock()
	case opcode.SMSGFriendStatus:
		friend, err := wow.ParseFriendStatus(packet.GetPayload())
		if err != nil {
			s.parseError("friend status", err)
			break
		}
		s.applyFriendStatus(friend)
	case opcode.SMSGWho:
		players, err := wow.ParseWho(packet.GetPayload())
		if err != nil {
			s.parseError("who", err)
			break
		}
		s.mu.Lock()
		s.who = s.who[:0]
		for _, player := range players {
			s.who = append(s.who, &modulev1.WhoPlayer{Name: player.Name, Guild: player.Guild, Level: player.Level, ClassId: player.Class, Race: player.Race, Gender: uint32(player.Gender), Zone: player.Zone})
		}
		s.revision++
		s.mu.Unlock()
	case opcode.SMSGGroupList:
		group, err := wow.ParseGroupList(packet.GetPayload())
		if err != nil {
			s.parseError("group list", err)
			break
		}
		s.mu.Lock()
		s.group = &group
		s.revision++
		s.mu.Unlock()
	case opcode.SMSGGroupDestroyed, opcode.SMSGGroupUninvite:
		s.mu.Lock()
		s.group = nil
		s.revision++
		s.mu.Unlock()
	case opcode.SMSGMessagechat:
		chat, err := wow.ParseChatMessage(packet.GetPayload())
		if err != nil {
			s.parseError("party chat", err)
			break
		}
		if chat.Type != wow.ChatMsgParty {
			break
		}
		s.mu.Lock()
		s.chatSeq++
		s.messages = append(s.messages, &modulev1.ChatMessage{Epoch: s.epoch, Sequence: s.chatSeq, SenderGuid: uint64(chat.SenderGUID), ReceiverGuid: uint64(chat.ReceiverGUID), Message: chat.Message, ReceivedAt: s.now().Unix()})
		if len(s.messages) > chatHistory {
			s.messages = append([]*modulev1.ChatMessage(nil), s.messages[len(s.messages)-chatHistory:]...)
		}
		s.revision++
		s.mu.Unlock()
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) applyFriendStatus(friend wow.FriendStatus) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch friend.Result {
	case wow.FriendRemoved:
		delete(s.contacts, friend.GUID)
	case wow.FriendAddedOnline, wow.FriendAddedOffline:
		s.contacts[friend.GUID] = &modulev1.Contact{Guid: uint64(friend.GUID), Flags: wow.SocialFlagFriend, Note: friend.Note, Status: uint32(friend.Status), Area: friend.Area, Level: friend.Level, ClassId: friend.Class}
	case wow.FriendOnline:
		if contact := s.contacts[friend.GUID]; contact != nil {
			contact.Status = uint32(friend.Status)
			contact.Area = friend.Area
			contact.Level = friend.Level
			contact.ClassId = friend.Class
		}
	case wow.FriendOffline:
		if contact := s.contacts[friend.GUID]; contact != nil {
			contact.Status = 0
			contact.Area = 0
			contact.Level = 0
			contact.ClassId = 0
		}
	}
	s.revision++
}

func (s *Service) send(ctx context.Context, op uint32, body []byte) error {
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: op, Payload: body}); err != nil {
		return err
	}
	s.mu.Lock()
	s.sentActions++
	s.mu.Unlock()
	return nil
}

func (s *Service) parseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.mu.Unlock()
	slog.Warn("dropped malformed party packet", "packet", what, "error", err)
}

func (s *Service) state() *modulev1.PartyState {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.invite != nil && !s.now().Before(s.invite.expiresAt) {
		s.invite = nil
		s.revision++
	}
	out := &modulev1.PartyState{Epoch: s.epoch, Revision: s.revision, ParseErrors: s.parseErrors, SentActions: s.sentActions}
	for _, contact := range s.contacts {
		copy := *contact
		out.Contacts = append(out.Contacts, &copy)
	}
	sort.Slice(out.Contacts, func(i, j int) bool { return out.Contacts[i].GetGuid() < out.Contacts[j].GetGuid() })
	for _, player := range s.who {
		copy := *player
		out.Who = append(out.Who, &copy)
	}
	if s.invite != nil {
		out.PendingInvite = &modulev1.Invite{InviteId: s.invite.id, Inviter: s.invite.inviter, ReceivedAt: s.invite.receivedAt.Unix(), ExpiresAt: s.invite.expiresAt.Unix()}
	}
	for _, message := range s.messages {
		copy := *message
		out.Messages = append(out.Messages, &copy)
	}
	if s.group != nil && s.group.LeaderGUID != 0 {
		out.InGroup = true
		out.GroupGuid = uint64(s.group.GroupGUID)
		out.LeaderGuid = uint64(s.group.LeaderGUID)
		out.LootMethod = uint32(s.group.LootMethod)
		out.MasterLooterGuid = uint64(s.group.MasterLooterGUID)
		out.LootThreshold = uint32(s.group.LootThreshold)
		out.DungeonDifficulty = uint32(s.group.DungeonDifficulty)
		if s.selfGUID != 0 {
			out.Members = append(out.Members, &modulev1.GroupMember{Name: s.selfName, Guid: uint64(s.selfGUID), Status: 1, Subgroup: uint32(s.group.OwnSubgroup), Flags: uint32(s.group.OwnFlags), Roles: uint32(s.group.OwnRoles), IsSelf: true})
		}
		for _, member := range s.group.Members {
			out.Members = append(out.Members, &modulev1.GroupMember{Name: member.Name, Guid: uint64(member.GUID), Status: uint32(member.Status), Subgroup: uint32(member.Subgroup), Flags: uint32(member.Flags), Roles: uint32(member.Roles)})
		}
	}
	return out
}

func contactProto(contact wow.Contact) *modulev1.Contact {
	return &modulev1.Contact{Guid: uint64(contact.GUID), Flags: contact.Flags, Note: contact.Note, Status: uint32(contact.Status), Area: contact.Area, Level: contact.Level, ClassId: contact.Class}
}

func validName(name string) error {
	if name == "" || len(name) > 64 || strings.ContainsRune(name, 0) {
		return status.Error(codes.InvalidArgument, "name must be non-empty, NUL-free and at most 64 bytes")
	}
	return nil
}
