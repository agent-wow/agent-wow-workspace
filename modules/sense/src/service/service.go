// Package service implements the sense module: a read-only snapshot of the
// character, nearby units, and the character's target.
package service

import (
	"context"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/sense/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	defaultNearbyLimit = 32
	maxNearbyLimit     = 200
	nameQueueSize      = 64
	creatureQueueSize  = 256
)

type unit struct {
	guid        wow.GUID
	name        string
	typ         wow.ObjectType
	pos         wow.Position
	hasPos      bool
	moving      bool
	moveFrom    wow.Position
	moveTo      wow.Position
	moveStart   time.Time
	moveEnd     time.Time
	health      uint32
	maxHealth   uint32
	level       uint32
	classID     uint32
	raceID      uint32
	entry       uint32
	target      wow.GUID
	xp          uint32
	nextXP      uint32
	flags       uint32
	playerFlags uint32
	faction     uint32
	money       uint64
}

type creatureQuery struct {
	entry uint32
	guid  wow.GUID
}

// Service owns the session snapshot. OnPacket runs on the session owner loop;
// RPCs and the name-query worker run concurrently, so every field is guarded.
type Service struct {
	modulev1.UnimplementedModuleServer
	session  modv1.SessionClient
	selfGUID wow.GUID
	selfName string

	mu              sync.RWMutex
	mapID           uint32
	self            unit
	selfFields      wow.UpdateFields
	units           map[wow.GUID]*unit
	target          wow.GUID
	packets         uint32
	parseErrors     uint32
	nameQueried     map[wow.GUID]struct{}
	nameQueries     uint32
	creatureQueried map[uint32]struct{}
	creatureNames   map[uint32]string
	creatureQueries uint32
	names           chan wow.GUID
	creatures       chan creatureQuery
	moveSamples     []string
}

// livePos returns where the unit is now. Creature move packets only carry the
// spline's start and end, so an in-flight move is interpolated along the
// straight line. Without this, callers chase the destination ghost of a mob
// that has already walked past them.
func (u *unit) livePos(now time.Time) wow.Position {
	if !u.moving {
		return u.pos
	}
	if !now.Before(u.moveEnd) {
		return u.moveTo
	}
	span := u.moveEnd.Sub(u.moveStart)
	if span <= 0 {
		return u.moveTo
	}
	t := float64(now.Sub(u.moveStart)) / float64(span)
	if t < 0 {
		t = 0
	}
	if t > 1 {
		t = 1
	}
	return wow.Position{
		X: float32(float64(u.moveFrom.X) + (float64(u.moveTo.X)-float64(u.moveFrom.X))*t),
		Y: float32(float64(u.moveFrom.Y) + (float64(u.moveTo.Y)-float64(u.moveFrom.Y))*t),
		Z: float32(float64(u.moveFrom.Z) + (float64(u.moveTo.Z)-float64(u.moveFrom.Z))*t),
		O: u.pos.O,
	}
}

// applyMove records a monster spline. The caller holds s.mu.
func (u *unit) applyMove(move wow.MonsterMove, now time.Time) {
	if move.Stopped || move.Duration == 0 || move.Start.Distance2D(move.Destination) < 0.05 {
		u.moving = false
		u.pos = move.Destination
		if move.Stopped {
			u.pos = move.Start
		}
		return
	}
	u.moveFrom = move.Start
	u.moveTo = move.Destination
	u.moveStart = now
	u.moveEnd = now.Add(time.Duration(move.Duration) * time.Millisecond)
	u.moving = true
}

// New reads the identity agent-wow injects into the container.
func New(session modv1.SessionClient) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	return &Service{
		session:         session,
		selfGUID:        wow.GUID(guid),
		selfName:        os.Getenv("AGENT_WOW_CHARACTER_NAME"),
		self:            unit{guid: wow.GUID(guid), name: os.Getenv("AGENT_WOW_CHARACTER_NAME"), typ: wow.ObjectTypePlayer},
		units:           make(map[wow.GUID]*unit),
		nameQueried:     make(map[wow.GUID]struct{}),
		creatureQueried: make(map[uint32]struct{}),
		creatureNames:   make(map[uint32]string),
		names:           make(chan wow.GUID, nameQueueSize),
		creatures:       make(chan creatureQuery, creatureQueueSize),
	}
}

// Start launches the asynchronous name-query worker. It is kept off the packet
// path because SendPacket waits on the session owner loop.
func (s *Service) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case guid := <-s.names:
				callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				_, err := s.session.SendPacket(callCtx, &modv1.SendPacketRequest{
					Opcode:  opcode.CMSGNameQuery,
					Payload: wow.EncodeNameQuery(guid),
				})
				cancel()
				if err != nil {
					s.mu.Lock()
					delete(s.nameQueried, guid)
					s.mu.Unlock()
					slog.Warn("name query failed", "guid", guid, "error", err)
				}
			case q := <-s.creatures:
				callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				_, err := s.session.SendPacket(callCtx, &modv1.SendPacketRequest{
					Opcode:  opcode.CMSGCreatureQuery,
					Payload: wow.EncodeCreatureQuery(q.entry, q.guid),
				})
				cancel()
				if err != nil {
					s.mu.Lock()
					delete(s.creatureQueried, q.entry)
					s.mu.Unlock()
					slog.Warn("creature query failed", "entry", q.entry, "error", err)
				}
			}
		}
	}()
}

func (s *Service) GetSelf(context.Context, *emptypb.Empty) (*modulev1.SelfResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	resp := &modulev1.SelfResponse{MapId: s.mapID, InWorld: s.self.hasPos}
	if s.self.guid != 0 || s.self.name != "" {
		self := s.self
		self.pos = self.livePos(time.Now())
		resp.Unit = toProto(&self, s.selfGUID)
	}
	resp.Skills = toProtoSkills(wow.DecodeSkills(s.selfFields))
	return resp, nil
}

// GetSkills returns the character's skill lines. The server sends them in the
// player's own skill block, so they are only known once the character is in
// the world.
func (s *Service) GetSkills(context.Context, *emptypb.Empty) (*modulev1.SkillsResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.SkillsResponse{Skills: toProtoSkills(wow.DecodeSkills(s.selfFields))}, nil
}

func (s *Service) GetNearby(_ context.Context, req *modulev1.NearbyRequest) (*modulev1.NearbyResponse, error) {
	limit := int(req.GetLimit())
	if limit <= 0 {
		limit = defaultNearbyLimit
	}
	if limit > maxNearbyLimit {
		limit = maxNearbyLimit
	}
	radius := req.GetRadius()

	s.mu.RLock()
	now := time.Now()
	origin := s.self.livePos(now)
	hasOrigin := s.self.hasPos
	candidates := make([]unit, 0, len(s.units))
	for guid, u := range s.units {
		if guid == s.selfGUID {
			continue
		}
		if u.typ != wow.ObjectTypeUnit && u.typ != wow.ObjectTypePlayer {
			continue
		}
		pos := u.livePos(now)
		if radius > 0 && hasOrigin && u.hasPos {
			if pos.Distance2D(origin) > float64(radius) {
				continue
			}
		}
		copied := *u
		copied.pos = pos
		candidates = append(candidates, copied)
	}
	known := uint32(len(s.units))
	s.mu.RUnlock()

	if hasOrigin {
		sort.Slice(candidates, func(i, j int) bool {
			di := candidates[i].pos.Distance2D(origin)
			dj := candidates[j].pos.Distance2D(origin)
			return di < dj
		})
	}
	if len(candidates) > limit {
		candidates = candidates[:limit]
	}
	resp := &modulev1.NearbyResponse{KnownUnits: known, Units: make([]*modulev1.Unit, 0, len(candidates))}
	for i := range candidates {
		resp.Units = append(resp.Units, toProto(&candidates[i], s.selfGUID))
	}
	return resp, nil
}

func (s *Service) GetTarget(context.Context, *emptypb.Empty) (*modulev1.TargetResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	resp := &modulev1.TargetResponse{TargetGuid: uint64(s.target), HasTarget: s.target != 0}
	if u, ok := s.units[s.target]; ok {
		resp.Unit = toProto(u, s.selfGUID)
	}
	return resp, nil
}

func (s *Service) SelectTarget(ctx context.Context, req *modulev1.SelectTargetRequest) (*emptypb.Empty, error) {
	guid := wow.GUID(req.GetGuid())
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGSetSelection,
		Payload: wow.EncodeSetSelection(guid),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.target = guid
	s.mu.Unlock()
	return &emptypb.Empty{}, nil
}

func (s *Service) ClearTarget(ctx context.Context, _ *emptypb.Empty) (*emptypb.Empty, error) {
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGSetSelection,
		Payload: wow.EncodeSetSelection(0),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.target = 0
	s.mu.Unlock()
	return &emptypb.Empty{}, nil
}

func (s *Service) QueryName(ctx context.Context, req *modulev1.QueryNameRequest) (*emptypb.Empty, error) {
	guid := wow.GUID(req.GetGuid())
	if guid == 0 {
		return nil, fmt.Errorf("guid must not be zero")
	}
	if err := s.queryName(ctx, guid); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.nameQueried[guid] = struct{}{}
	s.nameQueries++
	s.mu.Unlock()
	return &emptypb.Empty{}, nil
}

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.StatusResponse{
		Packets:         s.packets,
		ParseErrors:     s.parseErrors,
		KnownUnits:      uint32(len(s.units)),
		NameQueries:     s.nameQueries,
		CreatureQueries: s.creatureQueries,
	}, nil
}

// DebugMonsterMoves exposes raw SMSG_MONSTER_MOVE payloads for wire-format
// verification. It is diagnostic only.
func (s *Service) DebugMonsterMoves(context.Context, *emptypb.Empty) (*modulev1.MonsterMoveSamples, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]string, len(s.moveSamples))
	copy(out, s.moveSamples)
	return &modulev1.MonsterMoveSamples{Payloads: out}, nil
}

// DebugFields dumps the raw update-field slots captured for the character, so a
// new field offset can be identified against the live server before it gets a
// name in the shared library. It is diagnostic only.
func (s *Service) DebugFields(context.Context, *emptypb.Empty) (*modulev1.FieldDump, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	indices := make([]uint32, 0, len(s.selfFields))
	for index := range s.selfFields {
		indices = append(indices, index)
	}
	sort.Slice(indices, func(i, j int) bool { return indices[i] < indices[j] })
	slots := make([]*modulev1.FieldSlot, 0, len(indices))
	for _, index := range indices {
		slots = append(slots, &modulev1.FieldSlot{Index: index, Value: s.selfFields[index]})
	}
	return &modulev1.FieldDump{Slots: slots}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()

	var (
		update *wow.Update
		err    error
	)
	switch packet.GetOpcode() {
	case opcode.SMSGLoginVerifyWorld:
		s.applyLoginVerify(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGUpdateObject:
		update, err = wow.ParseUpdateObject(packet.GetPayload())
	case opcode.SMSGCompressedUpdateObject:
		update, err = wow.ParseCompressedUpdateObject(packet.GetPayload())
	case opcode.SMSGDestroyObject:
		guid, destroyErr := wow.ParseDestroyObject(packet.GetPayload())
		if destroyErr != nil {
			s.recordParseError("destroy object", destroyErr)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		if guid != s.selfGUID {
			delete(s.units, guid)
		}
		s.mu.Unlock()
		return &emptypb.Empty{}, nil
	case opcode.SMSGNameQueryResponse:
		s.applyNameQuery(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGCreatureQueryResponse:
		s.applyCreatureQuery(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGMonsterMove:
		move, moveErr := wow.ParseMonsterMove(packet.GetPayload())
		if moveErr != nil {
			s.recordParseError("monster move", moveErr)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		if move.GUID == s.selfGUID {
			s.self.pos = move.Start
			s.self.hasPos = true
			s.self.applyMove(move, time.Now())
		} else if u := s.units[move.GUID]; u != nil {
			u.pos = move.Start
			u.hasPos = true
			u.applyMove(move, time.Now())
		}
		if len(s.moveSamples) >= 4 {
			s.moveSamples = s.moveSamples[1:]
		}
		s.moveSamples = append(s.moveSamples, hex.EncodeToString(packet.GetPayload()))
		s.mu.Unlock()
		return &emptypb.Empty{}, nil
	default:
		return &emptypb.Empty{}, nil
	}
	if err != nil {
		// A malformed packet must not take down the session; record and move on.
		s.recordParseError(fmt.Sprintf("opcode 0x%03x", packet.GetOpcode()), err)
		return &emptypb.Empty{}, nil
	}

	var toQuery []wow.GUID
	var toCreature []creatureQuery
	s.mu.Lock()
	for _, guid := range update.OutOfRange {
		if guid != s.selfGUID {
			delete(s.units, guid)
		}
	}
	for _, obj := range update.Objects {
		switch obj.Kind {
		case wow.UpdateValues, wow.UpdateMovement, wow.UpdateCreate, wow.UpdateCreate2:
			u := s.applyObject(obj)
			if u == nil {
				continue
			}
			if u.name == "" && u.typ == wow.ObjectTypePlayer && u.guid != s.selfGUID {
				if _, asked := s.nameQueried[u.guid]; !asked {
					s.nameQueried[u.guid] = struct{}{}
					s.nameQueries++
					toQuery = append(toQuery, u.guid)
				}
			}
			if u.name == "" && u.typ == wow.ObjectTypeUnit && u.entry != 0 {
				if _, asked := s.creatureQueried[u.entry]; !asked {
					s.creatureQueried[u.entry] = struct{}{}
					s.creatureQueries++
					toCreature = append(toCreature, creatureQuery{entry: u.entry, guid: u.guid})
				}
			}
		}
	}
	s.mu.Unlock()
	for _, guid := range toQuery {
		select {
		case s.names <- guid:
		default:
			s.mu.Lock()
			delete(s.nameQueried, guid)
			s.mu.Unlock()
		}
	}
	for _, query := range toCreature {
		select {
		case s.creatures <- query:
		default:
			s.mu.Lock()
			delete(s.creatureQueried, query.entry)
			s.mu.Unlock()
		}
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) applyLoginVerify(body []byte) {
	mapID, pos, err := wow.ParseLoginVerifyWorld(body)
	if err != nil {
		s.recordParseError("login verify", err)
		return
	}
	s.mu.Lock()
	s.mapID = mapID
	s.self.pos, s.self.hasPos = pos, true
	s.mu.Unlock()
}

func (s *Service) applyNameQuery(body []byte) {
	resp, ok, err := wow.ParseNameQueryResponse(body)
	if err != nil {
		s.recordParseError("name query response", err)
		return
	}
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	name := wow.TrimName(resp.Name)
	if u, exists := s.units[resp.GUID]; exists {
		u.name = name
		if u.raceID == 0 {
			u.raceID = uint32(resp.Race)
		}
		if u.classID == 0 {
			u.classID = uint32(resp.Class)
		}
	}
	if resp.GUID == s.selfGUID {
		s.self.name = name
		if s.self.raceID == 0 {
			s.self.raceID = uint32(resp.Race)
		}
		if s.self.classID == 0 {
			s.self.classID = uint32(resp.Class)
		}
	}
}

func (s *Service) applyCreatureQuery(body []byte) {
	resp, err := wow.ParseCreatureQueryResponse(body)
	if err != nil {
		s.recordParseError("creature query response", err)
		return
	}
	name := wow.TrimName(resp.Name)
	if resp.Unknown || name == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.creatureNames[resp.Entry] = name
	for _, u := range s.units {
		if u.entry == resp.Entry && u.name == "" {
			u.name = name
			if u.guid == s.selfGUID {
				s.self.name = name
			}
		}
	}
}

// applyObject merges one update block. The caller holds s.mu.
func (s *Service) applyObject(obj wow.ObjectUpdate) *unit {
	if obj.GUID == 0 {
		return nil
	}
	if obj.Kind == wow.UpdateCreate || obj.Kind == wow.UpdateCreate2 {
		u := s.units[obj.GUID]
		if u == nil {
			u = &unit{guid: obj.GUID}
			s.units[obj.GUID] = u
		}
		u.typ = obj.Type
		if obj.GUID == s.selfGUID {
			u.typ = wow.ObjectTypePlayer
			if u.name == "" {
				u.name = s.selfName
			}
		}
		s.applyPosition(u, obj)
		s.applyFields(u, obj.Fields)
		s.refreshSelf(u)
		return u
	}

	u := s.units[obj.GUID]
	if u == nil {
		// Values-only updates do not carry a TypeID; GUID high bits distinguish
		// players (0x0000) from server-created units.
		typ := wow.ObjectTypeUnit
		if uint16(uint64(obj.GUID)>>48) == 0 {
			typ = wow.ObjectTypePlayer
		}
		u = &unit{guid: obj.GUID, typ: typ}
		if obj.GUID == s.selfGUID {
			u.typ = wow.ObjectTypePlayer
			u.name = s.selfName
		}
		s.units[obj.GUID] = u
	}
	s.applyPosition(u, obj)
	s.applyFields(u, obj.Fields)
	s.refreshSelf(u)
	return u
}

func (s *Service) applyPosition(u *unit, obj wow.ObjectUpdate) {
	if obj.HasPos {
		u.pos, u.hasPos = obj.Position, true
		u.moving = false
	}
}

func (s *Service) applyFields(u *unit, fields wow.UpdateFields) {
	if fields == nil {
		return
	}
	if v, ok := fields.Uint32(wow.ObjectFieldEntry); ok {
		u.entry = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldHealth); ok {
		u.health = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldMaxHealth); ok {
		u.maxHealth = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldLevel); ok {
		u.level = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldBytes0); ok {
		u.raceID = v & 0xff
		u.classID = (v >> 8) & 0xff
	}
	if v, ok := fields.Uint64(wow.UnitFieldTarget); ok {
		u.target = wow.GUID(v)
	}
	if v, ok := fields.Uint32(wow.UnitFieldFlags); ok {
		u.flags = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldFaction); ok {
		u.faction = v
	}
	if u.guid == s.selfGUID {
		// Update blocks carry only the fields that changed, so the snapshot has
		// to accumulate instead of being replaced.
		if s.selfFields == nil {
			s.selfFields = wow.UpdateFields{}
		}
		for index, value := range fields {
			s.selfFields[index] = value
		}
		if v, ok := fields.Uint32(wow.PlayerFlags); ok {
			u.playerFlags = v
		}
		if v, ok := fields.Uint32(wow.PlayerXP); ok {
			u.xp = v
		}
		if v, ok := fields.Uint32(wow.PlayerNextLevelXP); ok {
			u.nextXP = v
		}
		if v, ok := fields.Uint32(wow.PlayerCoinage); ok {
			u.money = uint64(v)
		}
	}
	if u.entry != 0 && u.name == "" {
		if name, ok := s.creatureNames[u.entry]; ok {
			u.name = name
		}
	}
}

func (s *Service) refreshSelf(u *unit) {
	if u.guid != s.selfGUID {
		return
	}
	s.self = *u
	if s.self.name == "" {
		s.self.name = s.selfName
	}
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.mu.Unlock()
	slog.Warn("dropped malformed packet", "packet", what, "error", err)
}

func (s *Service) queryName(ctx context.Context, guid wow.GUID) error {
	_, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGNameQuery,
		Payload: wow.EncodeNameQuery(guid),
	})
	return err
}

func toProto(u *unit, self wow.GUID) *modulev1.Unit {
	out := &modulev1.Unit{
		Guid:        uint64(u.guid),
		Name:        u.name,
		Type:        uint32(u.typ),
		Position:    &modulev1.Vector3{X: u.pos.X, Y: u.pos.Y, Z: u.pos.Z},
		Orientation: u.pos.O,
		HasPosition: u.hasPos,
		Health:      u.health,
		MaxHealth:   u.maxHealth,
		Level:       u.level,
		ClassId:     u.classID,
		RaceId:      u.raceID,
		TargetGuid:  uint64(u.target),
		IsSelf:      u.guid == self,
		Entry:       u.entry,
		Xp:          u.xp,
		NextLevelXp: u.nextXP,
		Flags:       u.flags,
		PlayerFlags: u.playerFlags,
		Dead:        (u.maxHealth > 0 && u.health == 0) || (u.guid == self && u.playerFlags&wow.PlayerFlagGhost != 0),
		Faction:     u.faction,
		InCombat:    u.flags&wow.UnitFlagInCombat != 0,
		Money:       u.money,
	}
	return out
}

// toProtoSkills turns the decoded skill lines into their wire form.
func toProtoSkills(skills []wow.Skill) []*modulev1.Skill {
	out := make([]*modulev1.Skill, 0, len(skills))
	for _, s := range skills {
		out = append(out, &modulev1.Skill{
			Id:         uint32(s.ID),
			Name:       s.Name(),
			Category:   uint32(s.Category()),
			Value:      uint32(s.Value),
			Max:        uint32(s.Max),
			Step:       uint32(s.Step),
			Profession: s.IsProfession(),
		})
	}
	return out
}
