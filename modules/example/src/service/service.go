// Package service implements the example module's RPC, packet, and lifecycle handlers.
package service

import (
	"context"
	"encoding/binary"
	"log/slog"
	"sync"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/agent-wow/go-module-template/src/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Service handles server-time queries and stores the latest packet response.
type Service struct {
	modulev1.UnimplementedModuleServer
	session modv1.SessionClient

	mu         sync.Mutex
	packets    uint32
	serverTime *uint32
}

// New creates a service using the session callbacks supplied by agent-wow.
func New(session modv1.SessionClient) *Service {
	return &Service{session: session}
}

func (s *Service) Status(ctx context.Context, _ *emptypb.Empty) (*modulev1.StatusResponse, error) {
	clock, err := s.session.GetClock(ctx, &emptypb.Empty{})
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	response := &modulev1.StatusResponse{ClientTimeMs: clock.ClientTimeMs, PacketsReceived: s.packets}
	if s.serverTime != nil {
		response.ServerTimeUnix = proto.Uint32(*s.serverTime)
	}
	return response, nil
}

func (s *Service) QueryTime(ctx context.Context, _ *emptypb.Empty) (*emptypb.Empty, error) {
	// Send exactly once: a successful write does not mean a reply has arrived.
	return s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: opcode.CMSGQueryTime})
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	if packet.GetOpcode() != opcode.SMSGQueryTimeResponse || len(packet.GetPayload()) != 8 {
		return nil, status.Error(codes.InvalidArgument, "expected an eight-byte SMSG_QUERY_TIME_RESPONSE")
	}
	// Two little-endian uint32s: Unix time and seconds until the daily quest reset.
	serverTime := binary.LittleEndian.Uint32(packet.Payload[:4])
	s.mu.Lock()
	s.serverTime = &serverTime
	s.packets++
	s.mu.Unlock()
	return &emptypb.Empty{}, nil
}

func (s *Service) BeforeLogout(_ context.Context, _ *emptypb.Empty) (*emptypb.Empty, error) {
	s.mu.Lock()
	packets := s.packets
	s.mu.Unlock()
	slog.Info("preparing logout", "packets_received", packets)
	// Do any module cleanup here. The session may resume if logout is rejected.
	return &emptypb.Empty{}, nil
}
