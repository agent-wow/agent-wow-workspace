// Package service implements quest-giver interaction and session-local quest
// progress. Route planning and reward selection stay in the calling agent.
package service

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/quest/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

const turnInTimeout = 15 * time.Second

type turnIn struct {
	guid        wow.GUID
	questID     uint32
	rewardIndex uint32
	started     time.Time
}

type rewardJob struct {
	turnIn turnIn
	choose bool
}

// Service owns quest state. Packet callbacks only decode and enqueue work;
// SendPacket always runs in an RPC or the reward worker.
type Service struct {
	modulev1.UnimplementedModuleServer
	session modv1.SessionClient

	mu          sync.Mutex
	npcGUID     wow.GUID
	menu        []*modulev1.Quest
	active      map[uint32]*modulev1.Quest
	turningIn   *turnIn
	accepts     uint32
	completed   uint32
	errors      uint32
	lastError   string
	lastQuestID uint32

	packets        uint32
	parseErrors    uint32
	gossips        uint32
	acceptRequests uint32
	rewardRequests uint32
	rewardChoices  uint32
	turnIns        uint32

	jobs chan rewardJob
}

func New(session modv1.SessionClient) *Service {
	return &Service{
		session: session,
		active:  make(map[uint32]*modulev1.Quest),
		jobs:    make(chan rewardJob, 8),
	}
}

func (s *Service) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case job := <-s.jobs:
				s.sendRewardStep(ctx, job)
			}
		}
	}()
}

func (s *Service) Gossip(ctx context.Context, req *modulev1.GossipRequest) (*modulev1.QuestState, error) {
	guid := wow.GUID(req.GetGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid must not be zero")
	}
	s.mu.Lock()
	s.expireTurnInLocked(time.Now())
	if s.turningIn != nil {
		pending := s.turningIn.questID
		s.mu.Unlock()
		return nil, status.Errorf(codes.FailedPrecondition, "quest %d is turning in", pending)
	}
	// Clear before sending: a fast response may arrive before SendPacket returns.
	s.npcGUID = guid
	s.menu = nil
	s.lastError = ""
	s.mu.Unlock()
	if _, err := s.send(ctx, opcode.CMSGQuestgiverHello, wow.EncodeQuestGiverHello(guid)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.gossips++
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) Accept(ctx context.Context, req *modulev1.QuestRequest) (*modulev1.QuestState, error) {
	guid := wow.GUID(req.GetGuid())
	questID := req.GetQuestId()
	if guid == 0 || questID == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid and quest_id must not be zero")
	}
	s.mu.Lock()
	s.expireTurnInLocked(time.Now())
	if s.turningIn != nil {
		pending := s.turningIn.questID
		s.mu.Unlock()
		return nil, status.Errorf(codes.FailedPrecondition, "quest %d is turning in", pending)
	}
	s.mu.Unlock()
	if _, err := s.send(ctx, opcode.CMSGQuestgiverAcceptQuest, wow.EncodeQuestGiverAcceptQuest(guid, questID)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	quest := s.active[questID]
	if quest == nil {
		quest = &modulev1.Quest{QuestId: questID}
		for _, row := range s.menu {
			if row.GetQuestId() == questID {
				quest = cloneQuest(row)
				break
			}
		}
		s.active[questID] = quest
	}
	s.accepts++
	s.acceptRequests++
	s.lastQuestID = questID
	s.lastError = ""
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) TurnIn(ctx context.Context, req *modulev1.TurnInRequest) (*modulev1.QuestState, error) {
	job := turnIn{
		guid: wow.GUID(req.GetGuid()), questID: req.GetQuestId(),
		rewardIndex: req.GetRewardIndex(), started: time.Now(),
	}
	if job.guid == 0 || job.questID == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid and quest_id must not be zero")
	}
	s.mu.Lock()
	s.expireTurnInLocked(time.Now())
	if s.turningIn != nil {
		pending := s.turningIn.questID
		s.mu.Unlock()
		return nil, status.Errorf(codes.FailedPrecondition, "quest %d is already turning in", pending)
	}
	s.turningIn = &job
	s.lastQuestID = job.questID
	s.lastError = ""
	s.mu.Unlock()
	if _, err := s.send(ctx, opcode.CMSGQuestgiverCompleteQuest, wow.EncodeQuestGiverCompleteQuest(job.guid, job.questID)); err != nil {
		s.mu.Lock()
		s.turningIn = nil
		s.mu.Unlock()
		return nil, err
	}
	s.mu.Lock()
	s.turnIns++
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.QuestState, error) {
	return s.state(), nil
}

func (s *Service) Status(context.Context, *emptypb.Empty) (*modulev1.StatusResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expireTurnInLocked(time.Now())
	return &modulev1.StatusResponse{
		Packets:        s.packets,
		ParseErrors:    s.parseErrors,
		Gossips:        s.gossips,
		AcceptRequests: s.acceptRequests,
		RewardRequests: s.rewardRequests,
		RewardChoices:  s.rewardChoices,
		TurnIns:        s.turnIns,
		LastError:      s.lastError,
	}, nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.packets++
	s.mu.Unlock()

	switch packet.GetOpcode() {
	case opcode.SMSGQuestgiverQuestList:
		list, err := wow.ParseQuestGiverQuestList(packet.GetPayload())
		if err != nil {
			s.recordParseError("quest list", err)
			break
		}
		rows := make([]*modulev1.Quest, 0, len(list.Quests))
		for _, row := range list.Quests {
			rows = append(rows, &modulev1.Quest{
				QuestId: row.QuestID, Title: row.Title, Level: row.Level,
				Icon: row.Icon, Flags: row.Flags, Repeatable: row.Repeatable,
			})
		}
		s.mu.Lock()
		s.npcGUID = list.GUID
		s.menu = rows
		s.mu.Unlock()
	case opcode.SMSGQuestgiverRequestItems:
		s.queueRewardStep(false, packet.GetPayload())
	case opcode.SMSGQuestgiverOfferReward:
		s.queueRewardStep(true, packet.GetPayload())
	case opcode.SMSGQuestupdateAddKill:
		update, err := wow.ParseQuestKillUpdate(packet.GetPayload())
		if err != nil {
			s.recordParseError("quest kill update", err)
			break
		}
		s.mu.Lock()
		quest := s.active[update.QuestID]
		if quest == nil {
			quest = &modulev1.Quest{QuestId: update.QuestID}
			s.active[update.QuestID] = quest
		}
		found := false
		for _, objective := range quest.Objectives {
			if objective.GetEntry() == update.Entry {
				objective.Current = update.Current
				objective.Required = update.Required
				found = true
				break
			}
		}
		if !found {
			quest.Objectives = append(quest.Objectives, &modulev1.QuestObjective{
				Entry: update.Entry, Current: update.Current, Required: update.Required,
			})
		}
		s.lastQuestID = update.QuestID
		s.mu.Unlock()
	case opcode.SMSGQuestupdateComplete:
		questID, err := wow.ParseQuestID(packet.GetPayload())
		if err != nil {
			s.recordParseError("quest complete update", err)
			break
		}
		s.mu.Lock()
		if quest := s.active[questID]; quest != nil {
			quest.Complete = true
		} else {
			s.active[questID] = &modulev1.Quest{QuestId: questID, Complete: true}
		}
		s.lastQuestID = questID
		s.mu.Unlock()
	case opcode.SMSGQuestgiverQuestComplete:
		questID, err := wow.ParseQuestID(packet.GetPayload())
		if err != nil {
			s.recordParseError("quest rewarded", err)
			break
		}
		s.mu.Lock()
		delete(s.active, questID)
		s.completed++
		s.lastQuestID = questID
		s.lastError = ""
		if s.turningIn != nil && s.turningIn.questID == questID {
			s.turningIn = nil
		}
		s.mu.Unlock()
	case opcode.SMSGQuestgiverQuestInvalid:
		reason := "quest invalid"
		if len(packet.GetPayload()) >= 4 {
			reason = fmt.Sprintf("quest invalid: reason %d", packet.GetPayload()[0])
		}
		s.failTurnIn(reason)
	case opcode.SMSGQuestgiverQuestFailed:
		s.questError("quest failed", packet.GetPayload())
	case opcode.SMSGQuestupdateFailed:
		s.questError("quest update failed", packet.GetPayload())
	case opcode.SMSGQuestupdateFailedtimer:
		s.questError("quest failed: timer", packet.GetPayload())
	case opcode.SMSGQuestlogFull:
		s.recordError("quest log full")
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) queueRewardStep(choose bool, body []byte) {
	guid, questID, err := wow.ParseQuestGiverQuestRef(body)
	if err != nil {
		what := "quest request items"
		if choose {
			what = "quest offer reward"
		}
		s.recordParseError(what, err)
		return
	}
	s.mu.Lock()
	s.expireTurnInLocked(time.Now())
	if s.turningIn == nil || s.turningIn.guid != guid || s.turningIn.questID != questID {
		s.mu.Unlock()
		return
	}
	job := rewardJob{turnIn: *s.turningIn, choose: choose}
	s.mu.Unlock()
	select {
	case s.jobs <- job:
	default:
		s.failTurnIn("quest reward queue full")
	}
}

func (s *Service) sendRewardStep(ctx context.Context, job rewardJob) {
	s.mu.Lock()
	s.expireTurnInLocked(time.Now())
	if s.turningIn == nil || *s.turningIn != job.turnIn {
		s.mu.Unlock()
		return
	}
	s.mu.Unlock()

	var op uint32 = opcode.CMSGQuestgiverRequestReward
	var payload []byte
	if job.choose {
		op = opcode.CMSGQuestgiverChooseReward
		payload = wow.EncodeQuestGiverChooseReward(job.turnIn.guid, job.turnIn.questID, job.turnIn.rewardIndex)
	} else {
		payload = wow.EncodeQuestGiverRequestReward(job.turnIn.guid, job.turnIn.questID)
	}
	if _, err := s.send(ctx, op, payload); err != nil {
		s.mu.Lock()
		if s.turningIn != nil && *s.turningIn == job.turnIn {
			s.turningIn = nil
		}
		s.mu.Unlock()
		s.recordError(fmt.Sprintf("quest reward send: %v", err))
		return
	}
	s.mu.Lock()
	if job.choose {
		s.rewardChoices++
	} else {
		s.rewardRequests++
	}
	s.mu.Unlock()
}

func (s *Service) questError(prefix string, body []byte) {
	questID, err := wow.ParseQuestID(body)
	if err != nil {
		s.recordParseError(prefix, err)
		return
	}
	s.mu.Lock()
	s.lastQuestID = questID
	if s.turningIn != nil && s.turningIn.questID == questID {
		s.turningIn = nil
	}
	s.errors++
	s.lastError = fmt.Sprintf("%s: quest %d", prefix, questID)
	s.mu.Unlock()
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.lastError = fmt.Sprintf("%s: %v", what, err)
	s.mu.Unlock()
	slog.Warn("dropped malformed packet", "packet", what, "error", err)
}

func (s *Service) failTurnIn(message string) {
	s.mu.Lock()
	s.turningIn = nil
	s.errors++
	s.lastError = message
	s.mu.Unlock()
}

// expireTurnInLocked releases silently refused or disconnected turn-ins. The
// caller holds mu; polling GetState or Status is enough to observe the timeout.
func (s *Service) expireTurnInLocked(now time.Time) {
	if s.turningIn == nil || now.Sub(s.turningIn.started) < turnInTimeout {
		return
	}
	s.errors++
	s.lastError = fmt.Sprintf("quest %d turn-in timed out", s.turningIn.questID)
	s.turningIn = nil
}

func (s *Service) recordError(message string) {
	s.mu.Lock()
	s.errors++
	s.lastError = message
	s.mu.Unlock()
}

func (s *Service) send(ctx context.Context, op uint32, payload []byte) (*emptypb.Empty, error) {
	return s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: op, Payload: payload})
}

func cloneQuest(quest *modulev1.Quest) *modulev1.Quest {
	return proto.Clone(quest).(*modulev1.Quest)
}

func (s *Service) state() *modulev1.QuestState {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expireTurnInLocked(time.Now())
	out := &modulev1.QuestState{
		NpcGuid: uint64(s.npcGUID), Accepts: s.accepts, Completed: s.completed,
		Errors: s.errors, LastError: s.lastError, LastQuestId: s.lastQuestID,
	}
	for _, quest := range s.menu {
		out.Menu = append(out.Menu, cloneQuest(quest))
	}
	ids := make([]int, 0, len(s.active))
	for id := range s.active {
		ids = append(ids, int(id))
	}
	sort.Ints(ids)
	for _, id := range ids {
		out.Active = append(out.Active, cloneQuest(s.active[uint32(id)]))
	}
	if s.turningIn != nil {
		out.TurningInQuestId = s.turningIn.questID
	}
	return out
}
