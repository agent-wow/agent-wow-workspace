package service

import (
	"context"
	"encoding/binary"
	"math"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/move/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

func f32(b []byte, v float32) []byte {
	return binary.LittleEndian.AppendUint32(b, math.Float32bits(v))
}

func TestLoginVerifySeedsPosition(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)

	login := binary.LittleEndian.AppendUint32(nil, 1)
	login = f32(login, 10)
	login = f32(login, 20)
	login = f32(login, 30)
	login = f32(login, 1)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: login,
	}); err != nil {
		t.Fatal(err)
	}
	pos, err := s.GetPosition(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if pos.Position.X != 10 || pos.Position.Y != 20 || pos.Position.Z != 30 {
		t.Fatalf("position = %+v", pos.Position)
	}
}

func TestSelfMovementEchoUpdatesPosition(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	body := wow.EncodeMovement(42, wow.MoveState{
		Flags:    wow.MovementFlagForward,
		Position: wow.Position{X: 5, Y: 6, Z: 7, O: 1},
	})
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.MSGMoveHeartbeat, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}
	pos, _ := s.GetPosition(context.Background(), &emptypb.Empty{})
	if pos.Position.X != 5 || pos.Position.Y != 6 || pos.Position.Orientation != 1 {
		t.Fatalf("position = %+v", pos.Position)
	}
}

func TestNonFiniteRPCInputsAreRejected(t *testing.T) {
	s := New(nil)
	ctx := context.Background()
	nan := float32(math.NaN())
	inf := float32(math.Inf(1))
	tests := []struct {
		name string
		call func() error
	}{
		{"forward NaN", func() error { _, err := s.Forward(ctx, &modulev1.MoveRequest{Yards: nan}); return err }},
		{"backward infinity", func() error { _, err := s.Backward(ctx, &modulev1.MoveRequest{Yards: inf}); return err }},
		{"turn left NaN", func() error { _, err := s.TurnLeft(ctx, &modulev1.TurnRequest{Radians: nan}); return err }},
		{"turn right infinity", func() error { _, err := s.TurnRight(ctx, &modulev1.TurnRequest{Radians: inf}); return err }},
		{"turn to infinity", func() error { _, err := s.TurnTo(ctx, &modulev1.TurnToRequest{Orientation: -inf}); return err }},
		{"move X NaN", func() error { _, err := s.MoveTo(ctx, &modulev1.MoveToRequest{X: nan}); return err }},
		{"move Y infinity", func() error { _, err := s.MoveTo(ctx, &modulev1.MoveToRequest{Y: inf}); return err }},
		{"move Z infinity", func() error { _, err := s.MoveTo(ctx, &modulev1.MoveToRequest{Z: -inf}); return err }},
		{"speed NaN", func() error { _, err := s.SetRunSpeed(ctx, &modulev1.RunSpeedRequest{YardsPerSecond: nan}); return err }},
		{"speed infinity", func() error { _, err := s.SetRunSpeed(ctx, &modulev1.RunSpeedRequest{YardsPerSecond: inf}); return err }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if code := status.Code(test.call()); code != codes.InvalidArgument {
				t.Fatalf("code = %s, want InvalidArgument", code)
			}
		})
	}
	if s.runSpeed != defaultRunSpeed || s.hasPos || s.moving {
		t.Fatalf("invalid input changed state: %+v", s)
	}
}

type moveSession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
}

func (f moveSession) GetClock(context.Context, *emptypb.Empty, ...grpc.CallOption) (*modv1.ClockResponse, error) {
	return &modv1.ClockResponse{ClientTimeMs: 1234}, nil
}

func (f moveSession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	f.sent <- req
	return &emptypb.Empty{}, nil
}

func TestTeleportUpdatesPositionAndAcknowledges(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := moveSession{sent: make(chan *modv1.SendPacketRequest, 1)}
	s := New(session)
	prefix := wow.EncodePackedGUID(42)
	movement := wow.EncodeMovement(42, wow.MoveState{
		Position: wow.Position{X: 100, Y: 200, Z: 30, O: 1.5},
	})
	body := append(prefix, 7, 0, 0, 0)
	body = append(body, movement[len(prefix):]...)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.MSGMoveTeleportAck, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}
	pos, err := s.GetPosition(t.Context(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if pos.Position.X != 100 || pos.Position.Y != 200 || pos.Position.Orientation != 1.5 {
		t.Fatalf("position = %+v", pos.Position)
	}
	select {
	case ack := <-session.sent:
		want := wow.EncodeTeleportAck(42, 7, 1234)
		if ack.GetOpcode() != opcode.MSGMoveTeleportAck || string(ack.GetPayload()) != string(want) {
			t.Fatalf("ack = %x/%x, want payload %x", ack.GetOpcode(), ack.GetPayload(), want)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for teleport ack")
	}
}

func TestMalformedMovementDoesNotFail(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.MSGMoveHeartbeat, Payload: []byte{0xff, 1},
	}); err != nil {
		t.Fatal(err)
	}
	st, _ := s.Status(context.Background(), &emptypb.Empty{})
	if st.ParseErrors != 1 {
		t.Fatalf("status = %+v", st)
	}
}
