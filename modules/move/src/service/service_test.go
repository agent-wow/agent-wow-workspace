package service

import (
	"context"
	"encoding/binary"
	"errors"
	"math"
	"sync"
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

func selfMovementUpdate(guid wow.GUID, pos wow.Position) []byte {
	body := binary.LittleEndian.AppendUint32(nil, 1)
	body = append(body, wow.UpdateMovement)
	body = append(body, wow.EncodePackedGUID(guid)...)
	body = binary.LittleEndian.AppendUint16(body, wow.UpdateFlagSelf|wow.UpdateFlagLiving)
	body = binary.LittleEndian.AppendUint32(body, 0) // movement flags
	body = binary.LittleEndian.AppendUint16(body, 0) // movement flags2
	body = binary.LittleEndian.AppendUint32(body, 55)
	body = f32(body, pos.X)
	body = f32(body, pos.Y)
	body = f32(body, pos.Z)
	body = f32(body, pos.O)
	body = binary.LittleEndian.AppendUint32(body, 0) // fall time
	for _, speed := range []float32{2.5, 7, 4.5, 4.72, 2.5, 7, 4.5, math.Pi, math.Pi} {
		body = f32(body, speed)
	}
	return body
}

func TestSelfObjectUpdateResynchronizesPosition(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(1, 1, 2, 3, 0),
	}); err != nil {
		t.Fatal(err)
	}
	want := wow.Position{X: 100, Y: 200, Z: 65, O: 1.5}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: selfMovementUpdate(42, want),
	}); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetPosition(t.Context(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if got.Position.X != want.X || got.Position.Y != want.Y ||
		got.Position.Z != want.Z || got.Position.Orientation != want.O {
		t.Fatalf("position = %+v, want %+v", got.Position, want)
	}

	s.mu.Lock()
	s.moving = true
	s.mu.Unlock()
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode:  opcode.SMSGUpdateObject,
		Payload: selfMovementUpdate(42, wow.Position{X: 900, Y: 900, Z: 900}),
	}); err != nil {
		t.Fatal(err)
	}
	got, _ = s.GetPosition(t.Context(), &emptypb.Empty{})
	if got.Position.X != want.X {
		t.Fatalf("in-flight update replaced driven position: %+v", got.Position)
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

type terrainFunc func(mapID uint32, x, y float32) (float32, error)

func (f terrainFunc) GroundHeight(mapID uint32, x, y float32) (float32, error) {
	return f(mapID, x, y)
}

func flatTerrain(mapID uint32, x, y float32) (float32, error) {
	return 30, nil
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

func worldPayload(mapID uint32, x, y, z, o float32) []byte {
	body := binary.LittleEndian.AppendUint32(nil, mapID)
	body = f32(body, x)
	body = f32(body, y)
	body = f32(body, z)
	body = f32(body, o)
	return body
}

func TestNewWorldResetsMapAndPosition(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(1, 10, 20, 30, 0),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGNewWorld, Payload: worldPayload(0, -8949.95, -132.49, 83.53, math.Pi/2),
	}); err != nil {
		t.Fatal(err)
	}
	st, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if st.WorldEpoch != 2 || st.MapId != 0 || !st.HasMap {
		t.Fatalf("world state = epoch %d map %d/%v", st.WorldEpoch, st.MapId, st.HasMap)
	}
	if st.Position.X != -8949.95 || st.Position.Y != -132.49 ||
		st.Position.Orientation != float32(math.Pi/2) || st.Moving {
		t.Fatalf("position after transition = %+v moving=%v", st.Position, st.Moving)
	}
}

// recordingSession records every movement packet sent by the module.
type recordingSession struct {
	modv1.SessionClient
	mu        sync.Mutex
	sent      []*modv1.SendPacketRequest
	firstSent chan struct{}
}

func (f *recordingSession) GetClock(context.Context, *emptypb.Empty, ...grpc.CallOption) (*modv1.ClockResponse, error) {
	return &modv1.ClockResponse{ClientTimeMs: 1234}, nil
}

func (f *recordingSession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	f.mu.Lock()
	f.sent = append(f.sent, req)
	f.mu.Unlock()
	select {
	case f.firstSent <- struct{}{}:
	default:
	}
	return &emptypb.Empty{}, nil
}

func TestWorldChangeAbortsInFlightMove(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := &recordingSession{firstSent: make(chan struct{}, 1)}
	s := newWithTerrain(session, terrainFunc(flatTerrain))
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(1, 10, 20, 30, 0),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetRunSpeed(context.Background(), &modulev1.RunSpeedRequest{YardsPerSecond: 1}); err != nil {
		t.Fatal(err)
	}

	errCh := make(chan error, 1)
	go func() {
		_, err := s.Forward(context.Background(), &modulev1.MoveRequest{Yards: 40})
		errCh <- err
	}()
	select {
	case <-session.firstSent:
	case <-time.After(2 * time.Second):
		t.Fatal("move never sent its start packet")
	}
	// Only the start packet has been sent; no heartbeat can fire before the
	// 500 ms ticker, so every later packet would belong to the old map.
	session.mu.Lock()
	before := len(session.sent)
	session.mu.Unlock()

	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGNewWorld, Payload: worldPayload(0, 100, 200, 300, 0),
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-errCh:
		if status.Code(err) != codes.Aborted {
			t.Fatalf("move error = %v, want Aborted", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("in-flight move was not aborted by world change")
	}

	// Give a stale heartbeat a full ticker window to surface.
	time.Sleep(600 * time.Millisecond)
	session.mu.Lock()
	after := len(session.sent)
	session.mu.Unlock()
	if after != before {
		t.Fatalf("module sent %d old-map packet(s) after the world change", after-before)
	}

	pos, _ := s.GetPosition(context.Background(), &emptypb.Empty{})
	if pos.Position.X != 100 || pos.Position.Y != 200 || pos.Moving {
		t.Fatalf("position after aborted move = %+v moving=%v", pos.Position, pos.Moving)
	}
}

func TestMovementPacketsFollowTerrain(t *testing.T) {
	tests := []struct {
		name string
		move func(context.Context, *Service) (*modulev1.PositionResponse, error)
	}{
		{"forward", func(ctx context.Context, s *Service) (*modulev1.PositionResponse, error) {
			return s.Forward(ctx, &modulev1.MoveRequest{Yards: 4})
		}},
		{"backward", func(ctx context.Context, s *Service) (*modulev1.PositionResponse, error) {
			return s.Backward(ctx, &modulev1.MoveRequest{Yards: 4})
		}},
		{"move to", func(ctx context.Context, s *Service) (*modulev1.PositionResponse, error) {
			return s.MoveTo(ctx, &modulev1.MoveToRequest{X: 14, Y: 20, Z: 32})
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
			session := &recordingSession{firstSent: make(chan struct{}, 1)}
			ground := func(mapID uint32, x, y float32) (float32, error) {
				return 30 + 0.5*(x-10), nil
			}
			s := newWithTerrain(session, terrainFunc(ground))
			if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
				Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(0, 10, 20, 30, 0),
			}); err != nil {
				t.Fatal(err)
			}
			if _, err := s.SetRunSpeed(t.Context(), &modulev1.RunSpeedRequest{YardsPerSecond: 4}); err != nil {
				t.Fatal(err)
			}
			if _, err := test.move(t.Context(), s); err != nil {
				t.Fatal(err)
			}

			session.mu.Lock()
			packets := append([]*modv1.SendPacketRequest(nil), session.sent...)
			session.mu.Unlock()
			heartbeats := 0
			for _, packet := range packets {
				_, _, pos, err := wow.ParseMovement(packet.GetPayload())
				if err != nil {
					t.Fatal(err)
				}
				wantZ, _ := ground(0, pos.X, pos.Y)
				if math.Abs(float64(pos.Z-wantZ)) > 0.001 {
					t.Errorf("opcode %d position = %+v, ground Z = %.3f", packet.GetOpcode(), pos, wantZ)
				}
				if packet.GetOpcode() == opcode.MSGMoveHeartbeat {
					heartbeats++
				}
			}
			if heartbeats == 0 {
				t.Fatal("test did not exercise a heartbeat")
			}
		})
	}
}

func TestMovementStopsWhenTerrainBecomesUnavailable(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := &recordingSession{firstSent: make(chan struct{}, 1)}
	s := newWithTerrain(session, terrainFunc(func(mapID uint32, x, y float32) (float32, error) {
		if x > 11 {
			return 0, errors.New("missing tile")
		}
		return 30, nil
	}))
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(0, 10, 20, 30, 0),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetRunSpeed(t.Context(), &modulev1.RunSpeedRequest{YardsPerSecond: 4}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Forward(t.Context(), &modulev1.MoveRequest{Yards: 4}); status.Code(err) != codes.Unavailable {
		t.Fatalf("move error = %v, want Unavailable", err)
	}
	session.mu.Lock()
	packets := append([]*modv1.SendPacketRequest(nil), session.sent...)
	session.mu.Unlock()
	if len(packets) != 2 || packets[0].GetOpcode() != opcode.MSGMoveStartForward || packets[1].GetOpcode() != opcode.MSGMoveStop {
		t.Fatalf("packets = %d, want start followed by stop", len(packets))
	}
	_, _, pos, err := wow.ParseMovement(packets[1].GetPayload())
	if err != nil || pos.X != 10 || pos.Z != 30 {
		t.Fatalf("stop position = %+v, %v; want last valid terrain position", pos, err)
	}
}

func TestMovementRejectsUnsafeGroundCorrection(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := &recordingSession{firstSent: make(chan struct{}, 1)}
	s := newWithTerrain(session, terrainFunc(func(mapID uint32, x, y float32) (float32, error) {
		return 100, nil
	}))
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(0, 10, 20, 30, 0),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Forward(t.Context(), &modulev1.MoveRequest{Yards: 1}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("move error = %v, want FailedPrecondition", err)
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if len(session.sent) != 0 {
		t.Fatalf("sent %d movement packets despite unsafe correction", len(session.sent))
	}
}

func TestRecoverGroundExplicitlyRepairsSavedPosition(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := &recordingSession{firstSent: make(chan struct{}, 1)}
	s := newWithTerrain(session, terrainFunc(func(mapID uint32, x, y float32) (float32, error) {
		return 100, nil
	}))
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(1, 10, 20, 30, 0.5),
	}); err != nil {
		t.Fatal(err)
	}
	response, err := s.RecoverGround(t.Context(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if response.Position.Z != 100 || response.Moving {
		t.Fatalf("recovery response = %+v", response)
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if len(session.sent) != 1 || session.sent[0].GetOpcode() != opcode.MSGMoveStop {
		t.Fatalf("packets = %d, want one stop", len(session.sent))
	}
	_, _, pos, err := wow.ParseMovement(session.sent[0].GetPayload())
	if err != nil || pos.X != 10 || pos.Y != 20 || pos.Z != 100 || pos.O != 0.5 {
		t.Fatalf("recovery position = %+v, %v", pos, err)
	}
	st, err := s.Status(t.Context(), &emptypb.Empty{})
	if err != nil || st.Position.Z != 100 || st.Moving {
		t.Fatalf("status after recovery = %+v, %v", st, err)
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
