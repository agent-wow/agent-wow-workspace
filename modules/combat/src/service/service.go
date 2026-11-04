// Package service implements the combat module: melee attack control and a
// small combat/progression state fed by server packets.
package service

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/combat/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Service owns the character's combat state. OnPacket runs on the session
// owner loop; RPCs run concurrently, so every field is guarded.
type Service struct {
	modulev1.UnimplementedModuleServer
	session  modv1.SessionClient
	selfGUID wow.GUID

	mu              sync.RWMutex
	target          wow.GUID
	targetHealth    uint32
	targetMaxHP     uint32
	targetLevel     uint32
	targetEntry     uint32
	targetDead      bool
	attacking       bool
	inCombat        bool
	selfHealth      uint32
	selfMaxHP       uint32
	selfHealthOK    bool
	selfLevel       uint32
	selfXP          uint32
	selfNextXP      uint32
	selfFlags       uint32
	selfPlayerFlags uint32
	selfPowerType   uint32
	selfPowers      [wow.MaxPowers]uint32
	selfMaxPowers   [wow.MaxPowers]uint32
	selfDead        bool
	selfGhost       bool

	kills          uint32
	deaths         uint32
	lastXP         uint32
	lastVictim     wow.GUID
	lastError      string
	refusals       uint32
	levelUps       uint32
	castFailures   uint32
	castCount      uint8
	knownSpells    map[uint32]bool
	spellbookKnown bool

	corpseDelay    time.Duration
	corpseReceived time.Time

	healerMapID  uint32
	healerPos    wow.Position
	hasHealerLoc bool

	packets          uint32
	parseErrors      uint32
	attackStarts     uint32
	attackStops      uint32
	gossipMessages   uint32
	spiritConfirms   uint32
	corpseDelays     uint32
	deathReleaseLocs uint32
	worldResets      uint32
}

// New reads the identity agent-wow injects into the container.
func New(session modv1.SessionClient) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	return &Service{session: session, selfGUID: wow.GUID(guid), knownSpells: make(map[uint32]bool)}
}

// Start exists so the shared server scaffolding can call it. Combat needs no
// background worker yet.
func (s *Service) Start(context.Context) {}

func (s *Service) Attack(ctx context.Context, req *modulev1.AttackRequest) (*modulev1.CombatState, error) {
	guid := wow.GUID(req.GetGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid must not be zero")
	}
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGSetSelection,
		Payload: wow.EncodeSetSelection(guid),
	}); err != nil {
		return nil, err
	}
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGAttackswing,
		Payload: wow.EncodeAttackSwing(guid),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	if s.target != guid {
		s.target = guid
		s.targetHealth = 0
		s.targetMaxHP = 0
	}
	s.targetDead = false
	s.attacking = true
	s.inCombat = true
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) StopAttack(ctx context.Context, _ *emptypb.Empty) (*modulev1.CombatState, error) {
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGAttackstop,
		Payload: wow.EncodeAttackStop(),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.attacking = false
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) Cast(ctx context.Context, req *modulev1.CastRequest) (*modulev1.CombatState, error) {
	if req.GetSpellId() == 0 {
		return nil, status.Error(codes.InvalidArgument, "spell_id must not be zero")
	}
	s.mu.Lock()
	target := wow.GUID(req.GetTargetGuid())
	if target == 0 && !req.GetPreserveEmptyTarget() {
		target = s.target
	}
	count := s.castCount
	s.castCount++
	s.mu.Unlock()
	payload := wow.EncodeCastSpell(count, req.GetSpellId(), target)
	if req.GetTargetIsGameObject() {
		if target == 0 {
			return nil, status.Error(codes.InvalidArgument, "game-object target must not be zero")
		}
		payload = wow.EncodeCastSpellGameObject(count, req.GetSpellId(), target)
	}
	if target != 0 && !req.GetTargetIsGameObject() {
		if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
			Opcode:  opcode.CMSGSetSelection,
			Payload: wow.EncodeSetSelection(target),
		}); err != nil {
			return nil, err
		}
	}
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGCastSpell,
		Payload: payload,
	}); err != nil {
		return nil, err
	}
	if target != 0 && !req.GetTargetIsGameObject() {
		s.mu.Lock()
		if s.target != target {
			s.target = target
			s.targetHealth = 0
			s.targetMaxHP = 0
		}
		s.targetDead = false
		s.mu.Unlock()
	}
	return s.state(), nil
}

// ReleaseSpirit asks the server to turn the corpse into a ghost at the nearest
// graveyard. The character loses the ability to act until it reclaims the body.
func (s *Service) ReleaseSpirit(ctx context.Context, _ *emptypb.Empty) (*modulev1.CombatState, error) {
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGRepopRequest,
		Payload: wow.EncodeRepopRequest(),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.attacking = false
	s.inCombat = false
	s.lastError = ""
	s.mu.Unlock()
	return s.state(), nil
}

// ReclaimCorpse revives the character at its corpse with partial health.
func (s *Service) ReclaimCorpse(ctx context.Context, _ *emptypb.Empty) (*modulev1.CombatState, error) {
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGReclaimCorpse,
		Payload: wow.EncodeReclaimCorpse(s.selfGUID),
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.lastError = ""
	s.mu.Unlock()
	return s.state(), nil
}

// ResurrectAtSpiritHealer asks the graveyard healer to revive the character.
// Gossip is opened first so the server has a menu to attach the activation to.
func (s *Service) ResurrectAtSpiritHealer(ctx context.Context, req *modulev1.SpiritHealerRequest) (*modulev1.CombatState, error) {
	guid := wow.GUID(req.GetHealerGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "healer_guid must not be zero")
	}
	var payload []byte
	if req.GetPackedGuid() {
		payload = wow.EncodePackedGUID(guid)
	} else {
		payload = binary.LittleEndian.AppendUint64(nil, uint64(guid))
	}
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGGossipHello,
		Payload: payload,
	}); err != nil {
		return nil, err
	}
	time.Sleep(400 * time.Millisecond)
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGSpiritHealerActivate,
		Payload: payload,
	}); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.attacking = false
	s.inCombat = false
	s.lastError = ""
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.CombatState, error) {
	return s.state(), nil
}

func (s *Service) GetSpells(context.Context, *emptypb.Empty) (*modulev1.SpellsResponse, error) {
	s.mu.RLock()
	ids := make([]int, 0, len(s.knownSpells))
	for id := range s.knownSpells {
		ids = append(ids, int(id))
	}
	sort.Ints(ids)
	out := &modulev1.SpellsResponse{
		Spells:      make([]*modulev1.Spell, 0, len(ids)),
		Initialized: s.spellbookKnown,
	}
	for _, raw := range ids {
		id := uint32(raw)
		out.Spells = append(out.Spells, &modulev1.Spell{SpellId: id, Disabled: s.knownSpells[id]})
	}
	s.mu.RUnlock()
	return out, nil
}

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.StatusResponse{
		Packets:               s.packets,
		ParseErrors:           s.parseErrors,
		AttackStarts:          s.attackStarts,
		AttackStops:           s.attackStops,
		LastError:             s.lastError,
		GossipMessages:        s.gossipMessages,
		SpiritHealerConfirms:  s.spiritConfirms,
		CorpseReclaimDelays:   s.corpseDelays,
		DeathReleaseLocations: s.deathReleaseLocs,
		WorldResets:           s.worldResets,
	}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()

	switch packet.GetOpcode() {
	case opcode.SMSGLoginVerifyWorld:
		s.mu.Lock()
		s.knownSpells = make(map[uint32]bool)
		s.spellbookKnown = false
		s.mu.Unlock()
		s.resetWorld()
	case opcode.SMSGNewWorld:
		// Spell knowledge persists across map transitions; only a fresh login
		// invalidates the previous spellbook snapshot.
		s.resetWorld()
	case opcode.SMSGDeathReleaseLoc:
		loc, err := wow.ParseDeathReleaseLoc(packet.GetPayload())
		if err != nil {
			s.recordParseError("death release location", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		if loc.HasLocation {
			s.healerMapID = loc.MapID
			s.healerPos = loc.Position
			s.hasHealerLoc = true
		} else {
			s.healerMapID = 0
			s.healerPos = wow.Position{}
			s.hasHealerLoc = false
		}
		s.deathReleaseLocs++
		s.mu.Unlock()
	case opcode.SMSGAttackstart:
		ev, err := wow.ParseAttackStart(packet.GetPayload())
		if err != nil {
			s.recordParseError("attack start", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.attackStarts++
		if ev.Attacker == s.selfGUID {
			s.target = ev.Victim
			s.targetDead = false
			s.attacking = true
			s.inCombat = true
			s.lastError = ""
		}
		if ev.Victim == s.selfGUID || ev.Attacker == s.selfGUID {
			s.inCombat = true
		}
		s.mu.Unlock()
	case opcode.SMSGAttackstop:
		ev, err := wow.ParseAttackStop(packet.GetPayload())
		if err != nil {
			s.recordParseError("attack stop", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.attackStops++
		if ev.Attacker == s.selfGUID {
			s.attacking = false
		}
		if ev.NowDead && ev.Victim == s.target {
			s.targetDead = true
			s.attacking = false
		}
		s.mu.Unlock()
	case opcode.SMSGAttackswingNotinrange:
		s.refuse("target not in range")
	case opcode.SMSGAttackswingBadfacing:
		s.refuse("target is behind the character")
	case opcode.SMSGAttackswingDeadtarget:
		s.refuse("target is dead")
	case opcode.SMSGAttackswingCantAttack:
		s.refuse("target cannot be attacked")
	case opcode.SMSGSpiritHealerConfirm:
		s.mu.Lock()
		s.spiritConfirms++
		s.mu.Unlock()
	case opcode.SMSGCorpseReclaimDelay:
		delay, err := wow.ParseCorpseReclaimDelay(packet.GetPayload())
		if err != nil {
			s.recordParseError("corpse reclaim delay", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.corpseDelay = time.Duration(delay) * time.Millisecond
		s.corpseReceived = time.Now()
		s.corpseDelays++
		s.mu.Unlock()
	case opcode.SMSGGossipMessage:
		s.mu.Lock()
		s.gossipMessages++
		s.mu.Unlock()
	case opcode.SMSGLogXpgain:
		xp, err := wow.ParseLogXPGain(packet.GetPayload())
		if err != nil {
			s.recordParseError("log xp gain", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.lastXP = xp.Total
		if xp.Kill {
			s.kills++
			s.lastVictim = xp.Victim
		}
		s.mu.Unlock()
	case opcode.SMSGLevelupInfo:
		level, err := wow.ParseLevelUpInfo(packet.GetPayload())
		if err != nil {
			s.recordParseError("level up", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.levelUps++
		s.selfLevel = level
		s.lastError = ""
		s.mu.Unlock()
	case opcode.SMSGInitialSpells:
		spells, err := wow.ParseInitialSpells(packet.GetPayload())
		if err != nil {
			s.recordParseError("initial spells", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.knownSpells = make(map[uint32]bool, len(spells))
		s.spellbookKnown = true
		for _, spell := range spells {
			s.knownSpells[spell.ID] = spell.Disabled
		}
		s.mu.Unlock()
	case opcode.SMSGLearnedSpell:
		spellID, err := wow.ParseLearnedSpell(packet.GetPayload())
		if err != nil {
			s.recordParseError("learned spell", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.knownSpells[spellID] = false
		s.mu.Unlock()
	case opcode.SMSGSpellStart, opcode.SMSGSpellGo,
		opcode.SMSGSpellFailure, opcode.SMSGSpellFailedOther,
		opcode.SMSGAttackerstateupdate:
		payload := packet.GetPayload()
		if len(payload) > 96 {
			payload = payload[:96]
		}
		slog.Info("observed combat packet", "opcode", packet.GetOpcode(),
			"length", len(packet.GetPayload()), "payload", fmt.Sprintf("%x", payload))
	case opcode.SMSGCastFailed:
		failure, err := wow.ParseCastFailed(packet.GetPayload())
		if err != nil {
			s.recordParseError("cast failed", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.castFailures++
		s.lastError = fmt.Sprintf("cast failed: spell %d result %d", failure.SpellID, failure.Result)
		s.mu.Unlock()
	case opcode.SMSGUpdateObject:
		s.applyUpdate(packet.GetPayload(), false)
	case opcode.SMSGCompressedUpdateObject:
		s.applyUpdate(packet.GetPayload(), true)
	case opcode.SMSGDestroyObject:
		guid, err := wow.ParseDestroyObject(packet.GetPayload())
		if err != nil {
			s.recordParseError("destroy object", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		if guid == s.target && guid != 0 {
			s.targetDead = true
			s.attacking = false
		}
		s.mu.Unlock()
	}
	return &emptypb.Empty{}, nil
}

// resetWorld clears target and transient combat/death facts on an
// authoritative world entry. Progression counters (kills, deaths, XP) and the
// last level survive because the character keeps them across maps.
func (s *Service) resetWorld() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.target = 0
	s.targetHealth = 0
	s.targetMaxHP = 0
	s.targetLevel = 0
	s.targetEntry = 0
	s.targetDead = false
	s.attacking = false
	s.inCombat = false
	s.selfHealth = 0
	s.selfMaxHP = 0
	s.selfHealthOK = false
	s.selfPowerType = 0
	s.selfPowers = [wow.MaxPowers]uint32{}
	s.selfMaxPowers = [wow.MaxPowers]uint32{}
	s.selfFlags = 0
	s.selfPlayerFlags = 0
	s.selfDead = false
	s.selfGhost = false
	s.corpseDelay = 0
	s.corpseReceived = time.Time{}
	s.healerMapID = 0
	s.healerPos = wow.Position{}
	s.hasHealerLoc = false
	s.lastError = ""
	s.worldResets++
}

func (s *Service) applyUpdate(body []byte, compressed bool) {
	var (
		update *wow.Update
		err    error
	)
	if compressed {
		update, err = wow.ParseCompressedUpdateObject(body)
	} else {
		update, err = wow.ParseUpdateObject(body)
	}
	if err != nil {
		s.recordParseError("update object", err)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, obj := range update.Objects {
		switch obj.GUID {
		case s.selfGUID:
			s.applySelfFields(obj.Fields)
		case s.target:
			s.applyTargetFields(obj.Fields)
		}
	}
}

// applySelfFields merges self health, level, XP and flags. The caller holds mu.
func (s *Service) applySelfFields(fields wow.UpdateFields) {
	if fields == nil {
		return
	}
	if v, ok := fields.Uint32(wow.UnitFieldHealth); ok {
		s.selfHealth = v
		s.selfHealthOK = true
	}
	if v, ok := fields.Uint32(wow.UnitFieldMaxHealth); ok {
		s.selfMaxHP = v
	}
	// Every class keeps its primary resource in POWER1 + power type. Reading
	// only POWER1 reports mana for everyone and hides a warrior's rage or a
	// rogue's energy.
	if v, ok := fields.Uint32(wow.UnitFieldBytes0); ok {
		s.selfPowerType = wow.PowerTypeFromBytes0(v)
	}
	for powerType := uint32(0); powerType < wow.MaxPowers; powerType++ {
		if v, ok := fields.Uint32(wow.UnitFieldPower1 + powerType); ok {
			s.selfPowers[powerType] = v
		}
		if v, ok := fields.Uint32(wow.UnitFieldMaxPower1 + powerType); ok {
			s.selfMaxPowers[powerType] = v
		}
	}
	if v, ok := fields.Uint32(wow.UnitFieldLevel); ok {
		s.selfLevel = v
	}
	if v, ok := fields.Uint32(wow.PlayerXP); ok {
		s.selfXP = v
	}
	if v, ok := fields.Uint32(wow.PlayerNextLevelXP); ok {
		s.selfNextXP = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldFlags); ok {
		s.selfFlags = v
		s.inCombat = v&wow.UnitFlagInCombat != 0
	}
	if v, ok := fields.Uint32(wow.PlayerFlags); ok {
		s.selfPlayerFlags = v
	}
	s.selfGhost = s.selfPlayerFlags&wow.PlayerFlagGhost != 0
	dead := (s.selfHealthOK && s.selfHealth == 0) || s.selfGhost
	if dead && !s.selfDead {
		s.deaths++
		s.attacking = false
	}
	s.selfDead = dead
}

// applyTargetFields merges the tracked target's health. The caller holds mu.
func (s *Service) applyTargetFields(fields wow.UpdateFields) {
	if fields == nil {
		return
	}
	if v, ok := fields.Uint32(wow.UnitFieldHealth); ok {
		s.targetHealth = v
		if v == 0 {
			s.targetDead = true
			s.attacking = false
		}
	}
	if v, ok := fields.Uint32(wow.UnitFieldMaxHealth); ok {
		s.targetMaxHP = v
	}
	if v, ok := fields.Uint32(wow.UnitFieldLevel); ok {
		s.targetLevel = v
	}
	if v, ok := fields.Uint32(wow.ObjectFieldEntry); ok {
		s.targetEntry = v
	}
}

func (s *Service) refuse(reason string) {
	s.mu.Lock()
	s.attacking = false
	s.refusals++
	s.lastError = reason
	s.mu.Unlock()
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.mu.Unlock()
	slog.Warn("dropped malformed packet", "packet", what, "error", err)
}

func (s *Service) state() *modulev1.CombatState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := &modulev1.CombatState{
		HasTarget:       s.target != 0,
		TargetGuid:      uint64(s.target),
		Attacking:       s.attacking,
		InCombat:        s.inCombat,
		TargetHealth:    s.targetHealth,
		TargetMaxHealth: s.targetMaxHP,
		TargetDead:      s.targetDead,
		TargetLevel:     s.targetLevel,
		TargetEntry:     s.targetEntry,
		SelfHealth:      s.selfHealth,
		SelfMaxHealth:   s.selfMaxHP,
		SelfDead:        s.selfDead,
		SelfGhost:       s.selfGhost,
		SelfLevel:       s.selfLevel,
		SelfXp:          s.selfXP,
		SelfNextLevelXp: s.selfNextXP,
		Kills:           s.kills,
		Deaths:          s.deaths,
		LastXp:          s.lastXP,
		LastVictim:      uint64(s.lastVictim),
		LastError:       s.lastError,
		Refusals:        s.refusals,
		LevelUps:        s.levelUps,
		CastFailures:    s.castFailures,
		SelfRage:        s.selfPowers[wow.PowerRage],
		SelfPowerType:   s.selfPowerType,
	}
	if s.selfPowerType < wow.MaxPowers {
		out.SelfPower = s.selfPowers[s.selfPowerType]
		out.SelfMaxPower = s.selfMaxPowers[s.selfPowerType]
	}
	if s.corpseDelay > 0 {
		out.CorpseReclaimDelayMs = uint32(s.corpseDelay / time.Millisecond)
		remaining := s.corpseDelay - time.Since(s.corpseReceived)
		if remaining < 0 {
			remaining = 0
		}
		out.CorpseReclaimRemainingMs = uint32(remaining / time.Millisecond)
	}
	if s.hasHealerLoc {
		out.HasSpiritHealerLocation = true
		out.SpiritHealerMapId = s.healerMapID
		out.SpiritHealerPosition = &modulev1.Vector3{
			X: s.healerPos.X, Y: s.healerPos.Y, Z: s.healerPos.Z,
		}
	}
	return out
}
