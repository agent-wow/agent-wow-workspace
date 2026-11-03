// Package service implements the party module: it watches SMSG_GROUP_INVITE
// and answers with CMSG_GROUP_ACCEPT so an unattended character joins any
// group that invites it.
package service

import (
	"context"
	"log/slog"
	"sync"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/party/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Service owns the invitation state. OnPacket runs on the session owner loop,
// so it never calls SendPacket directly: accepts are queued to a worker.
type Service struct {
	modulev1.UnimplementedModuleServer
	session modv1.SessionClient

	mu          sync.Mutex
	invites     uint32
	accepts     uint32
	autoAccept  bool
	lastInviter string

	acceptsIn chan struct{}
}

func New(session modv1.SessionClient) *Service {
	return &Service{
		session:    session,
		autoAccept: true,
		acceptsIn:  make(chan struct{}, 4),
	}
}

// Start launches the accept worker. It is kept off the packet path because
// SendPacket waits on the session owner loop.
func (s *Service) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-s.acceptsIn:
				if err := s.sendAccept(ctx); err != nil {
					slog.Warn("group accept failed", "error", err)
				}
			}
		}
	}()
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.PartyState, error) {
	return s.state(), nil
}

func (s *Service) SetAutoAccept(_ context.Context, req *modulev1.AutoAcceptRequest) (*modulev1.PartyState, error) {
	s.mu.Lock()
	s.autoAccept = req.GetEnabled()
	s.mu.Unlock()
	return s.state(), nil
}

// AcceptInvite re-sends an accept, for the case where the automatic reply was
// lost (for example while the session was reconnecting).
func (s *Service) AcceptInvite(ctx context.Context, _ *emptypb.Empty) (*modulev1.PartyState, error) {
	if err := s.sendAccept(ctx); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) OnPacket(ctx context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	if packet.GetOpcode() != opcode.SMSGGroupInvite {
		return &emptypb.Empty{}, nil
	}
	inviter := parseInviteName(packet.GetPayload())
	s.mu.Lock()
	s.invites++
	s.lastInviter = inviter
	accept := s.autoAccept
	s.mu.Unlock()
	slog.Info("group invite received", "inviter", inviter, "auto_accept", accept)
	if accept {
		select {
		case s.acceptsIn <- struct{}{}:
		default:
		}
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) sendAccept(ctx context.Context) error {
	// 3.3.5a HandleGroupAcceptOpcode skips one uint32 before reading the invite.
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGGroupAccept,
		Payload: make([]byte, 4),
	}); err != nil {
		return err
	}
	s.mu.Lock()
	s.accepts++
	s.mu.Unlock()
	slog.Info("group invite accepted")
	return nil
}

func (s *Service) state() *modulev1.PartyState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return &modulev1.PartyState{
		Invites:     s.invites,
		Accepts:     s.accepts,
		AutoAccept:  s.autoAccept,
		LastInviter: s.lastInviter,
	}
}

// parseInviteName reads the inviter out of SMSG_GROUP_INVITE:
// uint8 flag, NUL-terminated name string, then a fixed tail. A payload that
// does not fit that shape still counts as an invitation; only the name is lost.
func parseInviteName(body []byte) string {
	name, err := wow.ParseGroupInvite(body)
	if err != nil {
		return ""
	}
	return name
}
