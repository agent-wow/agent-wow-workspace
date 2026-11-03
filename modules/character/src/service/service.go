// Package service implements trainer, talent and game-object protocol actions.
// Selection and progression policy belong to the growth worker.
package service

import (
	"context"
	"log/slog"
	"sync"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/character/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type Service struct {
	modulev1.UnimplementedModuleServer
	session modv1.SessionClient

	mu               sync.RWMutex
	trainer          *wow.TrainerList
	lastBuySpell     uint32
	lastBuyResult    uint32
	lastBuyKnown     bool
	lastBuySucceeded bool
	talents          *wow.TalentsInfo
	lastGameObject   wow.GUID
	gameObjectUses   uint32
	lastError        string
	packets          uint32
	parseErrors      uint32
	trainerLists     uint32
	trainerBuys      uint32
	talentSnapshots  uint32
}

func New(session modv1.SessionClient) *Service { return &Service{session: session} }

func (s *Service) Start(context.Context) {}

func (s *Service) OpenTrainer(ctx context.Context, req *modulev1.TrainerRequest) (*modulev1.CharacterState, error) {
	guid := wow.GUID(req.GetTrainerGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "trainer_guid must not be zero")
	}
	s.mu.Lock()
	s.trainer = nil
	s.lastBuyKnown = false
	s.lastError = ""
	s.mu.Unlock()
	if _, err := s.send(ctx, opcode.CMSGTrainerList, wow.EncodeTrainerList(guid)); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) BuySpell(ctx context.Context, req *modulev1.BuySpellRequest) (*modulev1.CharacterState, error) {
	guid := wow.GUID(req.GetTrainerGuid())
	if guid == 0 || req.GetSpellId() == 0 {
		return nil, status.Error(codes.InvalidArgument, "trainer_guid and spell_id must not be zero")
	}
	s.mu.RLock()
	listed := s.trainer != nil && s.trainer.GUID == guid
	if listed {
		listed = false
		for _, row := range s.trainer.Spells {
			if row.SpellID == req.GetSpellId() {
				listed = true
				break
			}
		}
	}
	s.mu.RUnlock()
	if !listed {
		return nil, status.Error(codes.FailedPrecondition, "spell is not in the current server trainer list")
	}
	s.mu.Lock()
	s.lastBuyKnown = false
	s.lastError = ""
	s.mu.Unlock()
	if _, err := s.send(ctx, opcode.CMSGTrainerBuySpell, wow.EncodeTrainerBuySpell(guid, req.GetSpellId())); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.trainerBuys++
	s.mu.Unlock()
	return s.snapshot(), nil
}

func (s *Service) LearnTalent(ctx context.Context, req *modulev1.TalentLearnRequest) (*modulev1.CharacterState, error) {
	if req.GetTalentId() == 0 {
		return nil, status.Error(codes.InvalidArgument, "talent_id must not be zero")
	}
	if _, err := s.send(ctx, opcode.CMSGLearnTalent, wow.EncodeLearnTalent(req.GetTalentId(), req.GetRequestedRank())); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.lastError = ""
	s.mu.Unlock()
	return s.snapshot(), nil
}

func (s *Service) UseGameObject(ctx context.Context, req *modulev1.GameObjectUseRequest) (*modulev1.CharacterState, error) {
	guid := wow.GUID(req.GetGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid must not be zero")
	}
	if _, err := s.send(ctx, opcode.CMSGGameobjUse, wow.EncodeGameObjectUse(guid)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.lastGameObject = guid
	s.gameObjectUses++
	s.lastError = ""
	s.mu.Unlock()
	return s.snapshot(), nil
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.CharacterState, error) {
	return s.snapshot(), nil
}

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &modulev1.StatusResponse{
		Packets: s.packets, ParseErrors: s.parseErrors,
		TrainerLists: s.trainerLists, TrainerBuys: s.trainerBuys,
		TalentSnapshots: s.talentSnapshots, GameObjectUses: s.gameObjectUses,
		LastError: s.lastError,
	}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()
	switch packet.GetOpcode() {
	case opcode.SMSGLoginVerifyWorld, opcode.SMSGNewWorld:
		s.mu.Lock()
		s.trainer = nil
		s.talents = nil
		s.lastBuyKnown = false
		s.lastGameObject = 0
		s.lastError = ""
		s.mu.Unlock()
	case opcode.SMSGTrainerList:
		list, err := wow.ParseTrainerList(packet.GetPayload())
		if err != nil {
			s.recordParseError("trainer list", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.trainer = &list
		s.trainerLists++
		s.lastError = ""
		s.mu.Unlock()
	case opcode.SMSGTrainerBuySucceeded, opcode.SMSGTrainerBuyFailed:
		result, err := wow.ParseTrainerBuyResult(packet.GetPayload())
		if err != nil {
			s.recordParseError("trainer buy result", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.lastBuySpell = result.SpellID
		s.lastBuyResult = result.Result
		s.lastBuyKnown = true
		s.lastBuySucceeded = result.Succeeded
		if !result.Succeeded {
			s.lastError = "trainer purchase refused by server"
		}
		s.mu.Unlock()
	case opcode.SMSGTalentsInfo:
		talents, err := wow.ParseTalentsInfo(packet.GetPayload())
		if err != nil {
			s.recordParseError("talents info", err)
			return &emptypb.Empty{}, nil
		}
		if talents.Pet {
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.talents = &talents
		s.talentSnapshots++
		s.lastError = ""
		s.mu.Unlock()
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) send(ctx context.Context, op uint32, payload []byte) (*emptypb.Empty, error) {
	if s.session == nil {
		return nil, status.Error(codes.Unavailable, "session is unavailable")
	}
	return s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: op, Payload: payload})
}

func (s *Service) snapshot() *modulev1.CharacterState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := &modulev1.CharacterState{
		LastBuySpellId: s.lastBuySpell, LastBuyResult: s.lastBuyResult,
		LastBuySucceeded:   s.lastBuyKnown && s.lastBuySucceeded,
		LastBuyResultKnown: s.lastBuyKnown,
		HasTalents:         s.talents != nil, LastGameObjectGuid: uint64(s.lastGameObject),
		GameObjectUses: s.gameObjectUses, LastError: s.lastError,
	}
	if s.trainer != nil {
		out.TrainerGuid = uint64(s.trainer.GUID)
		out.TrainerType = s.trainer.TrainerType
		out.Greeting = s.trainer.Greeting
		out.TrainerOpen = true
		for _, row := range s.trainer.Spells {
			out.TrainerSpells = append(out.TrainerSpells, &modulev1.TrainerSpell{
				SpellId: row.SpellID, Usable: uint32(row.Usable), MoneyCost: row.MoneyCost,
				ProfessionPointCost: row.PointCost[1], RequiredLevel: uint32(row.ReqLevel),
				RequiredSkillLine: uint32(row.ReqSkillLine), RequiredSkillRank: uint32(row.ReqSkillRank),
				RequiredAbilities: append([]uint32(nil), row.ReqAbility[:]...),
			})
		}
	}
	if s.talents != nil {
		out.FreeTalentPoints = s.talents.FreePoints
		out.ActiveTalentGroup = uint32(s.talents.ActiveGroup)
		for index, group := range s.talents.Groups {
			row := &modulev1.TalentGroup{Index: uint32(index), Active: group.Active}
			for _, talent := range group.Talents {
				row.Talents = append(row.Talents, &modulev1.TalentRank{
					TalentId: talent.ID, Rank: uint32(talent.Rank),
				})
			}
			out.TalentGroups = append(out.TalentGroups, row)
		}
	}
	return out
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.lastError = err.Error()
	s.mu.Unlock()
	slog.Warn("dropped malformed packet", "packet", what, "error", err)
}
