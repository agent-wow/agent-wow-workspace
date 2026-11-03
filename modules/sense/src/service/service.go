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
	powerType   uint32
	powers      [7]uint32
	maxPowers   [7]uint32
	displayID   uint32
	bytes1      uint32
	dynamicFlag uint32
	npcFlags    uint32
	auras       map[uint8]wow.Aura

	// Game object fields, from EGameObjectFields.
	goTypeID       uint32
	goState        uint32
	goAnimProgress uint32
	goFlags        uint32
	goDisplayID    uint32
	goLevel        uint32
	goFaction      uint32
}

// cooldownEntry remembers when the server reported a cooldown and how long it
// lasts. Remaining time is derived at read time; the module never guesses.
type cooldownEntry struct {
	duration time.Duration
	received time.Time
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
	mapKnown        bool
	mapChanges      uint32
	epoch           uint64
	ready           bool
	sawInitial      bool
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
	cooldowns       map[uint32]cooldownEntry
	cooldownPackets uint32
	cooldownEvents  uint32
	auraUpdates     uint32
	comboPoints     uint8
	comboTarget     wow.GUID
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
		cooldowns:       make(map[uint32]cooldownEntry),
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
	now := time.Now()
	resp := &modulev1.SelfResponse{
		Guid:            uint64(s.selfGUID),
		Name:            s.selfName,
		MapId:           s.mapID,
		InWorld:         s.self.hasPos,
		Epoch:           s.epoch,
		Ready:           s.ready,
		MapChanges:      s.mapChanges,
		HasMap:          s.mapKnown,
		ComboPoints:     uint32(s.comboPoints),
		ComboTargetGuid: uint64(s.comboTarget),
		Cooldowns:       s.cooldownsLocked(now),
	}
	self := s.selfUnitLocked()
	if self != nil && (self.guid != 0 || self.name != "") {
		snapshot := *self
		snapshot.pos = snapshot.livePos(now)
		resp.Guid = uint64(self.guid)
		resp.Name = self.name
		resp.Unit = toProto(&snapshot, s.selfGUID)
		resp.Auras = toProtoAuras(snapshot.auras)
	}
	resp.Skills = toProtoSkills(wow.DecodeSkills(s.selfFields))
	return resp, nil
}

// selfUnitLocked returns the tracked self object. The env-provided identity is
// used before the first update block creates it. The caller holds s.mu.
func (s *Service) selfUnitLocked() *unit {
	if u := s.units[s.selfGUID]; u != nil {
		return u
	}
	if s.self.guid == 0 && s.self.name == "" {
		return nil
	}
	return &s.self
}

// cooldownsLocked returns server-reported cooldowns that have not expired,
// sorted by spell id. The caller holds s.mu.
func (s *Service) cooldownsLocked(now time.Time) []*modulev1.Cooldown {
	if len(s.cooldowns) == 0 {
		return nil
	}
	out := make([]*modulev1.Cooldown, 0, len(s.cooldowns))
	for spellID, entry := range s.cooldowns {
		remaining := entry.duration - now.Sub(entry.received)
		if remaining <= 0 {
			continue
		}
		out = append(out, &modulev1.Cooldown{
			SpellId:     spellID,
			CooldownMs:  uint32(entry.duration / time.Millisecond),
			RemainingMs: uint32(remaining / time.Millisecond),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].SpellId < out[j].SpellId })
	return out
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
	self := s.selfUnitLocked()
	origin := wow.Position{}
	hasOrigin := false
	if self != nil {
		origin = self.livePos(now)
		hasOrigin = self.hasPos
	}
	var unitCandidates, gameObjectCandidates []unit
	var knownUnits, knownGameObjects uint32
	for guid, u := range s.units {
		if guid == s.selfGUID {
			continue
		}
		switch u.typ {
		case wow.ObjectTypeUnit, wow.ObjectTypePlayer:
			knownUnits++
		case wow.ObjectTypeGameObject:
			knownGameObjects++
			if !req.GetIncludeGameObjects() {
				continue
			}
		default:
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
		if u.typ == wow.ObjectTypeGameObject {
			gameObjectCandidates = append(gameObjectCandidates, copied)
		} else {
			unitCandidates = append(unitCandidates, copied)
		}
	}
	s.mu.RUnlock()

	sortByDistance(unitCandidates, origin, hasOrigin)
	sortByDistance(gameObjectCandidates, origin, hasOrigin)
	if len(unitCandidates) > limit {
		unitCandidates = unitCandidates[:limit]
	}
	if len(gameObjectCandidates) > limit {
		gameObjectCandidates = gameObjectCandidates[:limit]
	}
	resp := &modulev1.NearbyResponse{
		KnownUnits:       knownUnits,
		KnownGameObjects: knownGameObjects,
		Units:            make([]*modulev1.Unit, 0, len(unitCandidates)),
		GameObjects:      make([]*modulev1.GameObject, 0, len(gameObjectCandidates)),
	}
	for i := range unitCandidates {
		resp.Units = append(resp.Units, toProto(&unitCandidates[i], s.selfGUID))
	}
	for i := range gameObjectCandidates {
		resp.GameObjects = append(resp.GameObjects, toProtoGameObject(&gameObjectCandidates[i]))
	}
	return resp, nil
}

func sortByDistance(units []unit, origin wow.Position, hasOrigin bool) {
	if !hasOrigin {
		return
	}
	sort.Slice(units, func(i, j int) bool {
		return units[i].pos.Distance2D(origin) < units[j].pos.Distance2D(origin)
	})
}

func (s *Service) GetTarget(context.Context, *emptypb.Empty) (*modulev1.TargetResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	resp := &modulev1.TargetResponse{TargetGuid: uint64(s.target), HasTarget: s.target != 0}
	if u, ok := s.units[s.target]; ok {
		if u.typ == wow.ObjectTypeGameObject {
			resp.GameObject = toProtoGameObject(u)
		} else {
			resp.Unit = toProto(u, s.selfGUID)
		}
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
	var knownUnits uint32
	for _, u := range s.units {
		if u.typ == wow.ObjectTypeUnit || u.typ == wow.ObjectTypePlayer {
			knownUnits++
		}
	}
	return &modulev1.StatusResponse{
		Packets:         s.packets,
		ParseErrors:     s.parseErrors,
		KnownUnits:      knownUnits,
		NameQueries:     s.nameQueries,
		CreatureQueries: s.creatureQueries,
		Epoch:           s.epoch,
		Ready:           s.ready,
		MapId:           s.mapID,
		MapChanges:      s.mapChanges,
		AuraUpdates:     s.auraUpdates,
		CooldownPackets: s.cooldownPackets,
		CooldownEvents:  s.cooldownEvents,
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
		s.applyWorldReset(packet.GetPayload(), false)
		return &emptypb.Empty{}, nil
	case opcode.SMSGNewWorld:
		s.applyWorldReset(packet.GetPayload(), true)
		return &emptypb.Empty{}, nil
	case opcode.SMSGAuraUpdate, opcode.SMSGAuraUpdateAll:
		s.applyAuraUpdate(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGSpellCooldown:
		s.applySpellCooldown(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGCooldownEvent:
		s.applyCooldownEvent(packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGUpdateComboPoints:
		s.applyComboPoints(packet.GetPayload())
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
			if guid == s.target {
				// Never keep a selected target the server has removed: the
				// GUID may belong to an object that no longer exists.
				s.target = 0
			}
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
	s.markReadyLocked(len(update.Objects) > 0)
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

// applyWorldReset handles the two authoritative world-entry packets. A login
// starts a new epoch; SMSG_NEW_WORLD also starts a new epoch because every
// GUID, position and cursor from the old map is invalid. In both cases the
// snapshot accumulators are discarded so readiness can only be restored by
// fresh server fields.
func (s *Service) applyWorldReset(body []byte, mapChange bool) {
	mapID, pos, err := wow.ParseLoginVerifyWorld(body)
	if err != nil {
		s.recordParseError("world entry", err)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.epoch++
	if mapChange {
		s.mapChanges++
	}
	s.mapID = mapID
	s.mapKnown = true
	s.ready = false
	s.sawInitial = false
	s.selfFields = nil
	s.units = make(map[wow.GUID]*unit)
	s.self = unit{
		guid:   s.selfGUID,
		name:   s.selfName,
		typ:    wow.ObjectTypePlayer,
		pos:    pos,
		hasPos: true,
		auras:  make(map[uint8]wow.Aura),
	}
	s.target = 0
	s.nameQueried = make(map[wow.GUID]struct{})
	s.cooldowns = make(map[uint32]cooldownEntry)
	s.comboPoints = 0
	s.comboTarget = 0
}

// markReadyLocked promotes the snapshot to ready only when the post-transition
// update actually carried the fields a class-driven worker needs. It never
// uses a timer or a caller hint. The caller holds s.mu.
func (s *Service) markReadyLocked(hadObjects bool) {
	if s.ready || !s.mapKnown || !hadObjects {
		return
	}
	s.sawInitial = true
	fields := s.selfFields
	if fields == nil || !s.self.hasPos {
		return
	}
	_, hasHealth := fields.Uint32(wow.UnitFieldHealth)
	_, hasMaxHealth := fields.Uint32(wow.UnitFieldMaxHealth)
	_, hasLevel := fields.Uint32(wow.UnitFieldLevel)
	bytes0, hasBytes0 := fields.Uint32(wow.UnitFieldBytes0)
	if !hasHealth || !hasMaxHealth || !hasLevel || !hasBytes0 {
		return
	}
	powerType := wow.PowerTypeFromBytes0(bytes0)
	powerIndex, okPower := wow.PowerFieldIndex(powerType)
	maxPowerIndex, okMaxPower := wow.MaxPowerFieldIndex(powerType)
	if !okPower || !okMaxPower {
		return
	}
	if _, ok := fields.Uint32(powerIndex); !ok {
		return
	}
	if _, ok := fields.Uint32(maxPowerIndex); !ok {
		return
	}
	s.ready = true
}

func (s *Service) applyAuraUpdate(body []byte) {
	update, err := wow.ParseAuraUpdate(body)
	if err != nil {
		s.recordParseError("aura update", err)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	u := s.units[update.Target]
	if u == nil {
		// Only self may create a unit out of an aura packet. Unknown GUIDs are
		// ignored so a burst cannot grow the map without bound.
		if update.Target != s.selfGUID {
			return
		}
		u = s.newUnitLocked(update.Target, wow.ObjectTypePlayer)
		s.units[update.Target] = u
	}
	if u.auras == nil {
		u.auras = make(map[uint8]wow.Aura)
	}
	for _, aura := range update.Auras {
		if aura.SpellID == 0 {
			delete(u.auras, aura.Slot)
		} else {
			u.auras[aura.Slot] = aura
		}
	}
	s.auraUpdates++
	s.refreshSelf(u)
}

func (s *Service) applySpellCooldown(body []byte) {
	guid, _, cooldowns, err := wow.ParseSpellCooldown(body)
	if err != nil {
		s.recordParseError("spell cooldown", err)
		return
	}
	if guid != s.selfGUID {
		return
	}
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cooldowns == nil {
		s.cooldowns = make(map[uint32]cooldownEntry)
	}
	for _, cooldown := range cooldowns {
		if cooldown.CooldownMS == 0 {
			delete(s.cooldowns, cooldown.SpellID)
			continue
		}
		s.cooldowns[cooldown.SpellID] = cooldownEntry{
			duration: time.Duration(cooldown.CooldownMS) * time.Millisecond,
			received: now,
		}
	}
	s.cooldownPackets++
}

func (s *Service) applyCooldownEvent(body []byte) {
	event, err := wow.ParseCooldownEvent(body)
	if err != nil {
		s.recordParseError("cooldown event", err)
		return
	}
	if event.GUID != s.selfGUID {
		return
	}
	s.mu.Lock()
	s.cooldownEvents++
	s.mu.Unlock()
}

func (s *Service) applyComboPoints(body []byte) {
	combo, err := wow.ParseComboPoints(body)
	if err != nil {
		s.recordParseError("combo points", err)
		return
	}
	s.mu.Lock()
	s.comboPoints = combo.Points
	s.comboTarget = combo.Target
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

// newUnitLocked creates a unit, seeding the self entry with the last
// world-entry position and identity so an incremental self update does not
// erase facts learned from SMSG_NEW_WORLD/SMSG_LOGIN_VERIFY_WORLD. The caller
// holds s.mu.
func (s *Service) newUnitLocked(guid wow.GUID, typ wow.ObjectType) *unit {
	u := &unit{guid: guid, typ: typ}
	if guid != s.selfGUID {
		return u
	}
	u.typ = wow.ObjectTypePlayer
	u.name = s.selfName
	u.pos = s.self.pos
	u.hasPos = s.self.hasPos
	if s.self.auras != nil {
		u.auras = s.self.auras
	}
	return u
}

// applyObject merges one update block. The caller holds s.mu.
func (s *Service) applyObject(obj wow.ObjectUpdate) *unit {
	if obj.GUID == 0 {
		return nil
	}
	if obj.Kind == wow.UpdateCreate || obj.Kind == wow.UpdateCreate2 {
		u := s.units[obj.GUID]
		if u == nil {
			u = s.newUnitLocked(obj.GUID, obj.Type)
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
		u = s.newUnitLocked(obj.GUID, typ)
		if obj.GUID == s.selfGUID {
			u.typ = wow.ObjectTypePlayer
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
	if u.typ == wow.ObjectTypeGameObject {
		if v, ok := fields.Uint32(wow.GameObjectDisplayID); ok {
			u.goDisplayID = v
		}
		if v, ok := fields.Uint32(wow.GameObjectFlags); ok {
			u.goFlags = v
		}
		if v, ok := fields.Uint32(wow.GameObjectFaction); ok {
			u.goFaction = v
		}
		if v, ok := fields.Uint32(wow.GameObjectLevel); ok {
			u.goLevel = v
		}
		if v, ok := fields.Uint32(wow.GameObjectBytes1); ok {
			u.goState = wow.GameObjectState(v)
			u.goTypeID = wow.GameObjectTypeID(v)
			u.goAnimProgress = wow.GameObjectAnimProgress(v)
		}
		return
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
		u.powerType = wow.PowerTypeFromBytes0(v)
	}
	for powerType := uint32(0); powerType < wow.MaxPowers; powerType++ {
		if v, ok := fields.Uint32(wow.UnitFieldPower1 + powerType); ok {
			u.powers[powerType] = v
		}
		if v, ok := fields.Uint32(wow.UnitFieldMaxPower1 + powerType); ok {
			u.maxPowers[powerType] = v
		}
	}
	if v, ok := fields.Uint32(wow.UnitFieldDisplayID); ok {
		u.displayID = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldBytes1); ok {
		u.bytes1 = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldDynamicFlags); ok {
		u.dynamicFlag = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldNpcFlags); ok {
		u.npcFlags = v
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
		Guid:         uint64(u.guid),
		Name:         u.name,
		Type:         uint32(u.typ),
		Position:     &modulev1.Vector3{X: u.pos.X, Y: u.pos.Y, Z: u.pos.Z},
		Orientation:  u.pos.O,
		HasPosition:  u.hasPos,
		Health:       u.health,
		MaxHealth:    u.maxHealth,
		Level:        u.level,
		ClassId:      u.classID,
		RaceId:       u.raceID,
		TargetGuid:   uint64(u.target),
		IsSelf:       u.guid == self,
		Entry:        u.entry,
		Xp:           u.xp,
		NextLevelXp:  u.nextXP,
		Flags:        u.flags,
		PlayerFlags:  u.playerFlags,
		Dead:         (u.maxHealth > 0 && u.health == 0) || (u.guid == self && u.playerFlags&wow.PlayerFlagGhost != 0),
		Faction:      u.faction,
		InCombat:     u.flags&wow.UnitFlagInCombat != 0,
		Money:        u.money,
		PowerType:    u.powerType,
		DisplayId:    u.displayID,
		DynamicFlags: u.dynamicFlag,
		NpcFlags:     u.npcFlags,
		Auras:        toProtoAuras(u.auras),
	}
	if u.powerType < wow.MaxPowers {
		out.Power = u.powers[u.powerType]
		out.MaxPower = u.maxPowers[u.powerType]
	}
	return out
}

func toProtoAuras(auras map[uint8]wow.Aura) []*modulev1.Aura {
	if len(auras) == 0 {
		return nil
	}
	slots := make([]int, 0, len(auras))
	for slot := range auras {
		slots = append(slots, int(slot))
	}
	sort.Ints(slots)
	out := make([]*modulev1.Aura, 0, len(slots))
	for _, slot := range slots {
		aura := auras[uint8(slot)]
		out = append(out, &modulev1.Aura{
			Slot:          uint32(aura.Slot),
			SpellId:       aura.SpellID,
			Flags:         uint32(aura.Flags),
			CasterLevel:   uint32(aura.CasterLevel),
			Stacks:        uint32(aura.Stacks),
			CasterGuid:    uint64(aura.Caster),
			HasCaster:     aura.HasCaster,
			MaxDurationMs: aura.MaxDurationMS,
			DurationMs:    aura.DurationMS,
			HasDuration:   aura.HasDuration,
			Positive:      aura.Positive(),
			Negative:      aura.Negative(),
		})
	}
	return out
}

func toProtoGameObject(u *unit) *modulev1.GameObject {
	return &modulev1.GameObject{
		Guid:         uint64(u.guid),
		Entry:        u.entry,
		TypeId:       u.goTypeID,
		DisplayId:    u.goDisplayID,
		Flags:        u.goFlags,
		State:        u.goState,
		AnimProgress: u.goAnimProgress,
		Level:        u.goLevel,
		Faction:      u.goFaction,
		Position:     &modulev1.Vector3{X: u.pos.X, Y: u.pos.Y, Z: u.pos.Z},
		Orientation:  u.pos.O,
		HasPosition:  u.hasPos,
	}
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
