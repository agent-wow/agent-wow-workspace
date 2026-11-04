// Package service implements the move module: it walks and turns the character
// with WotLK movement packets and keeps a local position estimate.
package service

import (
	"context"
	"log/slog"
	"math"
	"os"
	"strconv"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/move/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	defaultRunSpeed     = 7.0
	maxRunSpeed         = 20.0
	heartbeat           = 500 * time.Millisecond
	maxYards            = 40
	maxMoveDuration     = 8 * time.Second
	maxGroundCorrection = 8.0
)

type groundSampler interface {
	GroundHeight(mapID uint32, x, y float32) (float32, error)
}

// Service owns the character's movement state. Movement RPCs are serialized;
// quick reads use a separate lock so they never wait for a whole walk.
type Service struct {
	modulev1.UnimplementedModuleServer
	session  modv1.SessionClient
	selfGUID wow.GUID
	terrain  groundSampler

	moveMu sync.Mutex // serializes Forward/Backward/Turn/Stop

	mu          sync.RWMutex
	pos         wow.Position
	hasPos      bool
	moving      bool
	runSpeed    float32
	packets     uint32
	parseErrors uint32
	// worldEpoch increments on every authoritative world entry
	// (SMSG_LOGIN_VERIFY_WORLD and SMSG_NEW_WORLD). In-flight moves capture it
	// and abort instead of storing or sending old-map coordinates. worldCh is
	// closed and replaced on each reset to wake a blocked walk immediately.
	worldEpoch uint64
	mapID      uint32
	hasMap     bool
	worldCh    chan struct{}
}

// New reads the identity and local terrain path injected into the container.
func New(session modv1.SessionClient) *Service {
	mapsDir := os.Getenv("AGENT_WOW_MAPS_DIR")
	if mapsDir == "" {
		mapsDir = "/data/maps"
	}
	return newWithTerrain(session, wow.NewMapTerrain(mapsDir))
}

func newWithTerrain(session modv1.SessionClient, terrain groundSampler) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	return &Service{
		session:  session,
		selfGUID: wow.GUID(guid),
		terrain:  terrain,
		runSpeed: defaultRunSpeed,
		worldCh:  make(chan struct{}),
	}
}

func (s *Service) GetPosition(context.Context, *emptypb.Empty) (*modulev1.PositionResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.snapshotLocked(), nil
}

func (s *Service) Forward(ctx context.Context, req *modulev1.MoveRequest) (*modulev1.PositionResponse, error) {
	return s.travel(ctx, req.GetYards(), false)
}

func (s *Service) Backward(ctx context.Context, req *modulev1.MoveRequest) (*modulev1.PositionResponse, error) {
	return s.travel(ctx, req.GetYards(), true)
}

func (s *Service) TurnLeft(ctx context.Context, req *modulev1.TurnRequest) (*modulev1.PositionResponse, error) {
	return s.turn(ctx, req.GetRadians())
}

func (s *Service) TurnRight(ctx context.Context, req *modulev1.TurnRequest) (*modulev1.PositionResponse, error) {
	return s.turn(ctx, -req.GetRadians())
}

func (s *Service) TurnTo(ctx context.Context, req *modulev1.TurnToRequest) (*modulev1.PositionResponse, error) {
	if !finite(req.GetOrientation()) {
		return nil, status.Error(codes.InvalidArgument, "orientation must be finite")
	}
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	s.mu.Lock()
	if !s.hasPos {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "position unknown: wait for the character to enter the world")
	}
	s.pos.O = float32(wow.NormalizeOrientation(float64(req.GetOrientation())))
	pos := s.pos
	epoch := s.worldEpoch
	s.mu.Unlock()
	if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveSetFacing, 0, pos); err != nil {
		return nil, err
	}
	return &modulev1.PositionResponse{Position: toProto(pos)}, nil
}

func (s *Service) Stop(ctx context.Context, _ *emptypb.Empty) (*modulev1.PositionResponse, error) {
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	s.mu.RLock()
	pos := s.pos
	hasPos := s.hasPos
	epoch := s.worldEpoch
	s.mu.RUnlock()
	if !hasPos {
		return nil, status.Error(codes.FailedPrecondition, "position unknown: wait for the character to enter the world")
	}
	if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveStop, 0, pos); err != nil {
		return nil, err
	}
	return &modulev1.PositionResponse{Position: toProto(pos)}, nil
}

// RecoverGround repairs an operator-confirmed invalid saved Z. Unlike normal
// locomotion, this explicit action bypasses the 8-yard correction guard.
func (s *Service) RecoverGround(ctx context.Context, _ *emptypb.Empty) (*modulev1.PositionResponse, error) {
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	start, _, epoch, mapID, _, err := s.startMove()
	if err != nil {
		return nil, err
	}
	defer s.endMove()
	grounded, err := s.sampleGround(mapID, start)
	if err != nil {
		return nil, err
	}
	if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveStop, 0, grounded); err != nil {
		return nil, err
	}
	s.storeIfEpoch(epoch, grounded)
	return &modulev1.PositionResponse{Position: toProto(grounded)}, nil
}

func (s *Service) SetRunSpeed(_ context.Context, req *modulev1.RunSpeedRequest) (*modulev1.StatusResponse, error) {
	speed := req.GetYardsPerSecond()
	if !finite(speed) || speed < 1 || speed > maxRunSpeed {
		return nil, status.Errorf(codes.InvalidArgument, "yards_per_second must be between 1 and %.0f", maxRunSpeed)
	}
	s.mu.Lock()
	s.runSpeed = speed
	s.mu.Unlock()
	return s.Status(context.Background(), &emptypb.Empty{})
}

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.StatusResponse{
		Position:    toProto(s.pos),
		HasPosition: s.hasPos,
		Moving:      s.moving,
		RunSpeed:    s.runSpeed,
		Packets:     s.packets,
		ParseErrors: s.parseErrors,
		WorldEpoch:  s.worldEpoch,
		MapId:       s.mapID,
		HasMap:      s.hasMap,
	}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()

	switch packet.GetOpcode() {
	case opcode.SMSGLoginVerifyWorld:
		mapID, pos, err := wow.ParseLoginVerifyWorld(packet.GetPayload())
		if err != nil {
			s.recordParseError("login verify", err)
			return &emptypb.Empty{}, nil
		}
		s.resetWorld(mapID, pos)
	case opcode.SMSGNewWorld:
		// Every map transition invalidates the old position, map id and any
		// in-flight walk. New-world coordinates are authoritative; the next
		// movement packet starts from here, not from the old map.
		mapID, pos, err := wow.ParseLoginVerifyWorld(packet.GetPayload())
		if err != nil {
			s.recordParseError("new world", err)
			return &emptypb.Empty{}, nil
		}
		s.resetWorld(mapID, pos)
		slog.Info("world changed", "map_id", mapID, "x", pos.X, "y", pos.Y, "z", pos.Z)
	case opcode.SMSGUpdateObject, opcode.SMSGCompressedUpdateObject:
		var update *wow.Update
		var err error
		if packet.GetOpcode() == opcode.SMSGCompressedUpdateObject {
			update, err = wow.ParseCompressedUpdateObject(packet.GetPayload())
		} else {
			update, err = wow.ParseUpdateObject(packet.GetPayload())
		}
		if err != nil {
			s.recordParseError("object update", err)
			return &emptypb.Empty{}, nil
		}
		// Login position can be stale after a module-only restart. Object
		// updates carry the server's current self position, including terrain
		// corrections that never produce a self movement echo.
		s.mu.Lock()
		if !s.moving {
			for _, obj := range update.Objects {
				if obj.GUID == s.selfGUID && obj.HasPos {
					s.pos, s.hasPos = obj.Position, true
				}
			}
		}
		s.mu.Unlock()
	case opcode.MSGMoveTeleportAck:
		guid, counter, pos, err := wow.ParseTeleportAck(packet.GetPayload())
		if err != nil {
			s.recordParseError("teleport", err)
			return &emptypb.Empty{}, nil
		}
		if guid != s.selfGUID {
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.pos, s.hasPos = pos, true
		s.mu.Unlock()
		// SendPacket waits on the session owner loop, so acknowledge off the
		// packet callback just like the other modules' asynchronous sends.
		go s.acknowledgeTeleport(guid, counter)
	case opcode.MSGMoveStartForward, opcode.MSGMoveStartBackward, opcode.MSGMoveStop,
		opcode.MSGMoveHeartbeat, opcode.MSGMoveSetFacing:
		guid, _, pos, err := wow.ParseMovement(packet.GetPayload())
		if err != nil {
			s.recordParseError("movement", err)
			return &emptypb.Empty{}, nil
		}
		// The server relays other players' movement. Only trust a self echo
		// while we are not driving the character ourselves.
		s.mu.Lock()
		if guid == s.selfGUID && !s.moving {
			s.pos, s.hasPos = pos, true
		}
		s.mu.Unlock()
	}
	return &emptypb.Empty{}, nil
}

// resetWorld applies one authoritative world entry. It bumps the epoch before
// any caller can observe the new position, clears the moving flag and wakes an
// in-flight walk so it cannot store or send coordinates from the old map.
func (s *Service) resetWorld(mapID uint32, pos wow.Position) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.worldEpoch++
	s.mapID = mapID
	s.hasMap = true
	s.pos, s.hasPos = pos, true
	s.moving = false
	if s.worldCh != nil {
		close(s.worldCh)
	}
	s.worldCh = make(chan struct{})
}

func (s *Service) acknowledgeTeleport(guid wow.GUID, counter uint32) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	clock, err := s.session.GetClock(ctx, &emptypb.Empty{})
	if err != nil {
		slog.Warn("teleport ack clock failed", "error", err)
		return
	}
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.MSGMoveTeleportAck,
		Payload: wow.EncodeTeleportAck(guid, counter, clock.ClientTimeMs),
	}); err != nil {
		slog.Warn("teleport ack failed", "error", err)
	}
}

func (s *Service) travel(ctx context.Context, yards float32, backward bool) (*modulev1.PositionResponse, error) {
	if !finite(yards) || yards <= 0 || yards > maxYards {
		return nil, status.Errorf(codes.InvalidArgument, "yards must be greater than 0 and at most %d", maxYards)
	}
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	start, speed, epoch, mapID, worldCh, err := s.startMove()
	if err != nil {
		return nil, err
	}
	sign := 1.0
	var startOp uint32 = opcode.MSGMoveStartForward
	moveFlags := wow.MovementFlagForward
	if backward {
		sign = -1
		startOp = opcode.MSGMoveStartBackward
		moveFlags = wow.MovementFlagBackward
	}
	end := wow.ForwardPosition(start, sign*float64(yards))
	return s.walk(ctx, epoch, mapID, worldCh, start, end, moveFlags, startOp, speed)
}

// MoveTo walks straight toward a world position and samples outdoor terrain
// height along the way. It does not pathfind or avoid obstacles.
func (s *Service) MoveTo(ctx context.Context, req *modulev1.MoveToRequest) (*modulev1.PositionResponse, error) {
	if !finite(req.GetX()) || !finite(req.GetY()) || !finite(req.GetZ()) {
		return nil, status.Error(codes.InvalidArgument, "coordinates must be finite")
	}
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	start, speed, epoch, mapID, worldCh, err := s.startMove()
	if err != nil {
		return nil, err
	}
	end := wow.Position{X: req.GetX(), Y: req.GetY(), Z: req.GetZ(), O: start.O}
	dx := float64(end.X - start.X)
	dy := float64(end.Y - start.Y)
	distance := math.Hypot(dx, dy)
	if distance < 0.05 {
		s.endMove()
		return &modulev1.PositionResponse{Position: toProto(start)}, nil
	}
	if distance > maxYards {
		s.endMove()
		return nil, status.Errorf(codes.InvalidArgument, "distance %.1f exceeds the %.0f yard step limit", distance, float64(maxYards))
	}
	end, err = s.groundPosition(mapID, end)
	if err != nil {
		s.endMove()
		return nil, err
	}
	// Facing is atan2(dy, dx) on the server; atan2(dx, dy) leaves the
	// character ~90 degrees off and swings get refused.
	end.O = float32(wow.NormalizeOrientation(math.Atan2(dy, dx)))
	start.O = end.O
	if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveSetFacing, 0, start); err != nil {
		s.endMove()
		return nil, err
	}
	return s.walk(ctx, epoch, mapID, worldCh, start, end, wow.MovementFlagForward, opcode.MSGMoveStartForward, speed)
}

// startMove locks in the start position and marks the character as moving. It
// also snapshots the world epoch and reset channel so the walk can abort if
// the map changes mid-step. The caller must hold moveMu and must call endMove
// when the walk finishes.
func (s *Service) startMove() (wow.Position, float32, uint64, uint32, <-chan struct{}, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.hasPos || !s.hasMap {
		return wow.Position{}, 0, 0, 0, nil, status.Error(codes.FailedPrecondition, "position and map unknown: wait for the character to enter the world")
	}
	if s.moving {
		return wow.Position{}, 0, 0, 0, nil, status.Error(codes.FailedPrecondition, "already moving")
	}
	s.moving = true
	return s.pos, s.runSpeed, s.worldEpoch, s.mapID, s.worldCh, nil
}

func (s *Service) endMove() {
	s.mu.Lock()
	s.moving = false
	s.mu.Unlock()
}

// walk drives one straight-line movement from start to end. The caller holds
// moveMu and has marked the character as moving. epoch and worldCh are the
// snapshot taken by startMove; a world entry closes worldCh and aborts the walk
// without sending a stop or storing a position that belongs to the old map.
func (s *Service) walk(ctx context.Context, epoch uint64, mapID uint32, worldCh <-chan struct{}, start, end wow.Position, moveFlags uint32, startOp uint32, speed float32) (*modulev1.PositionResponse, error) {
	defer s.endMove()
	start, err := s.groundPosition(mapID, start)
	if err != nil {
		return nil, err
	}
	s.storeIfEpoch(epoch, start)
	if err := s.sendForEpoch(ctx, epoch, startOp, moveFlags, start); err != nil {
		return nil, err
	}
	dx := float64(end.X - start.X)
	dy := float64(end.Y - start.Y)
	distance := math.Hypot(dx, dy)
	duration := time.Duration(distance / float64(speed) * float64(time.Second))
	if duration > maxMoveDuration {
		duration = maxMoveDuration
	}
	ticker := time.NewTicker(heartbeat)
	defer ticker.Stop()
	deadline := time.NewTimer(duration)
	defer deadline.Stop()
	started := time.Now()
	pos := start

	for {
		select {
		case <-worldCh:
			// The server already placed the character on the new map. Sending a
			// stop with old-map coordinates would drag it back, so just abort.
			return nil, status.Error(codes.Aborted, "world changed during move")
		case <-ctx.Done():
			// The context is gone, so a detached stop is the only way to tell
			// the server to halt; it is still epoch-checked.
			s.storeIfEpoch(epoch, pos)
			s.stopDetached(epoch, pos)
			return nil, ctx.Err()
		case <-ticker.C:
			if err := s.ensureEpoch(epoch); err != nil {
				return nil, err
			}
			traveled := float64(speed) * time.Since(started).Seconds()
			fraction := 0.0
			if distance > 0 {
				fraction = traveled / distance
			}
			if fraction > 1 {
				fraction = 1
			}
			next, err := s.groundPosition(mapID, lerpPosition(start, end, fraction))
			if err != nil {
				s.storeIfEpoch(epoch, pos)
				s.stopDetached(epoch, pos)
				return nil, err
			}
			pos = next
			s.storeIfEpoch(epoch, pos)
			if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveHeartbeat, moveFlags, pos); err != nil {
				if status.Code(err) != codes.Aborted {
					s.stopDetached(epoch, pos)
				}
				return nil, err
			}
		case <-deadline.C:
			end, err = s.groundPosition(mapID, end)
			if err != nil {
				s.storeIfEpoch(epoch, pos)
				s.stopDetached(epoch, pos)
				return nil, err
			}
			s.storeIfEpoch(epoch, end)
			if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveStop, 0, end); err != nil {
				return nil, err
			}
			return &modulev1.PositionResponse{Position: toProto(end)}, nil
		}
	}
}

func (s *Service) groundPosition(mapID uint32, pos wow.Position) (wow.Position, error) {
	grounded, err := s.sampleGround(mapID, pos)
	if err != nil {
		return pos, err
	}
	if math.Abs(float64(grounded.Z-pos.Z)) > maxGroundCorrection {
		return pos, status.Errorf(codes.FailedPrecondition, "terrain height %.2f differs from estimated %.2f by more than %.0f yards; possible cliff, bridge or indoor area", grounded.Z, pos.Z, maxGroundCorrection)
	}
	return grounded, nil
}

func (s *Service) sampleGround(mapID uint32, pos wow.Position) (wow.Position, error) {
	if s.terrain == nil {
		return pos, status.Error(codes.FailedPrecondition, "terrain sampler is not configured")
	}
	z, err := s.terrain.GroundHeight(mapID, pos.X, pos.Y)
	if err != nil {
		return pos, status.Errorf(codes.Unavailable, "terrain height at map %d (%.2f, %.2f): %v", mapID, pos.X, pos.Y, err)
	}
	pos.Z = z
	return pos, nil
}

func lerpPosition(a, b wow.Position, t float64) wow.Position {
	return wow.Position{
		X: float32(float64(a.X) + (float64(b.X)-float64(a.X))*t),
		Y: float32(float64(a.Y) + (float64(b.Y)-float64(a.Y))*t),
		Z: float32(float64(a.Z) + (float64(b.Z)-float64(a.Z))*t),
		O: b.O,
	}
}

func (s *Service) turn(ctx context.Context, radians float32) (*modulev1.PositionResponse, error) {
	if !finite(radians) || radians == 0 {
		return nil, status.Error(codes.InvalidArgument, "radians must not be zero")
	}
	if math.Abs(float64(radians)) > 2*math.Pi {
		return nil, status.Error(codes.InvalidArgument, "radians must be at most one full turn")
	}
	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	s.mu.Lock()
	if !s.hasPos {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "position unknown: wait for the character to enter the world")
	}
	s.pos.O = float32(wow.NormalizeOrientation(float64(s.pos.O) + float64(radians)))
	pos := s.pos
	epoch := s.worldEpoch
	s.mu.Unlock()
	if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveSetFacing, 0, pos); err != nil {
		return nil, err
	}
	return &modulev1.PositionResponse{Position: toProto(pos)}, nil
}

// ensureEpoch returns Aborted after a world entry. Callers check it before
// mutating shared state or sending a packet so an old-map move cannot leak
// across a map transition.
func (s *Service) ensureEpoch(epoch uint64) error {
	s.mu.RLock()
	current := s.worldEpoch
	s.mu.RUnlock()
	if current != epoch {
		return status.Error(codes.Aborted, "world changed during move")
	}
	return nil
}

// sendForEpoch validates the world epoch before and after fetching the clock,
// narrowing the window in which a reset can race a queued movement packet.
func (s *Service) sendForEpoch(ctx context.Context, epoch uint64, op uint32, flags uint32, pos wow.Position) error {
	if err := s.ensureEpoch(epoch); err != nil {
		return err
	}
	clock, err := s.session.GetClock(ctx, &emptypb.Empty{})
	if err != nil {
		return err
	}
	if err := s.ensureEpoch(epoch); err != nil {
		return err
	}
	_, err = s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode: op,
		Payload: wow.EncodeMovement(s.selfGUID, wow.MoveState{
			Flags:     flags,
			Timestamp: clock.ClientTimeMs,
			Position:  pos,
		}),
	})
	return err
}

// stopDetached sends a best-effort stop after the caller's context is gone.
// It is skipped when the world already changed.
func (s *Service) stopDetached(epoch uint64, pos wow.Position) {
	if s.ensureEpoch(epoch) != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = s.sendForEpoch(ctx, epoch, opcode.MSGMoveStop, 0, pos)
}

func (s *Service) storeIfEpoch(epoch uint64, pos wow.Position) {
	s.mu.Lock()
	if s.worldEpoch == epoch {
		s.pos = pos
	}
	s.mu.Unlock()
}

func (s *Service) snapshotLocked() *modulev1.PositionResponse {
	return &modulev1.PositionResponse{Position: toProto(s.pos), Moving: s.moving}
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.mu.Unlock()
	slog.Warn("dropped malformed packet", "packet", what, "error", err)
}

func finite(v float32) bool {
	return !math.IsNaN(float64(v)) && !math.IsInf(float64(v), 0)
}

func toProto(p wow.Position) *modulev1.Position {
	return &modulev1.Position{X: p.X, Y: p.Y, Z: p.Z, Orientation: p.O}
}
