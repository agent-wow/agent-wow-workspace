// Package service implements the combat module: melee attack control and a
// small combat/progression state fed by server packets.
package service

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"os"
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
	selfRage        uint32
	selfDead        bool
	kills           uint32
	deaths          uint32
	lastXP          uint32
	lastVictim      wow.GUID
	lastError       string
	refusals        uint32
	levelUps        uint32
	castFailures    uint32
	castCount       uint8
	packets         uint32
	parseErrors     uint32
	attackStarts    uint32
	attackStops     uint32
	gossipMessages  uint32
	spiritConfirms  uint32
	corpseDelays    uint32
}

// New reads the identity agent-wow injects into the container.
func New(session modv1.SessionClient) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	return &Service{session: session, selfGUID: wow.GUID(guid)}
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
	if target == 0 {
		target = s.target
	}
	count := s.castCount
	s.castCount++
	s.mu.Unlock()
	if _, err := s.session.SendPacket(ctx, &modv1.SendPacketRequest{
		Opcode:  opcode.CMSGCastSpell,
		Payload: wow.EncodeCastSpell(count, req.GetSpellId(), target),
	}); err != nil {
		return nil, err
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

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.StatusResponse{
		Packets:              s.packets,
		ParseErrors:          s.parseErrors,
		AttackStarts:         s.attackStarts,
		AttackStops:          s.attackStops,
		LastError:            s.lastError,
		GossipMessages:       s.gossipMessages,
		SpiritHealerConfirms: s.spiritConfirms,
		CorpseReclaimDelays:  s.corpseDelays,
	}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()

	switch packet.GetOpcode() {
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
		s.mu.Lock()
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
	if v, ok := fields.Uint32(wow.UnitFieldPower1); ok {
		s.selfRage = v
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
	dead := (s.selfHealthOK && s.selfHealth == 0) || s.selfPlayerFlags&wow.PlayerFlagGhost != 0
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
	return &modulev1.CombatState{
		HasTarget:       s.target != 0,
		TargetGuid:      uint64(s.target),
		Attacking:       s.attacking,
		InCombat:        s.inCombat,
		TargetHealth:    s.targetHealth,
		TargetMaxHealth: s.targetMaxHP,
		TargetDead:      s.targetDead,
		SelfHealth:      s.selfHealth,
		SelfMaxHealth:   s.selfMaxHP,
		SelfDead:        s.selfDead,
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
		SelfRage:        s.selfRage,
	}
}
