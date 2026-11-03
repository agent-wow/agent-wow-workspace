package service

import (
	"context"
	"fmt"
	"log/slog"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/loot/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type rollEntry struct {
	offer     modulev1.RollOffer
	confirmed bool
}

func (s *Service) Roll(ctx context.Context, req *modulev1.RollRequest) (*modulev1.LootState, error) {
	if req.GetItemGuid() == 0 || req.GetVote() > 2 {
		return nil, status.Error(codes.InvalidArgument, "item_guid and vote 0=pass, 1=need, 2=greed are required")
	}
	s.mu.Lock()
	entry := s.findRollLocked(wow.GUID(req.GetItemGuid()), req.GetSlot())
	if entry == nil || !entry.offer.Active {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "roll is not active")
	}
	if entry.offer.OwnVote >= 0 {
		s.mu.Unlock()
		return nil, status.Error(codes.AlreadyExists, "a vote was already dispatched for this roll")
	}
	requiredMask := uint32(4)
	if req.GetVote() == 1 {
		requiredMask = 1
	} else if req.GetVote() == 2 {
		requiredMask = 2
	}
	if entry.offer.VoteMask&requiredMask == 0 {
		s.mu.Unlock()
		return nil, status.Error(codes.PermissionDenied, "server vote mask does not allow this choice")
	}
	// Dispatch is single-use even when the RPC result is ambiguous. The next
	// server vote packet confirms it; otherwise the growth layer reconciles or
	// abandons rather than voting again.
	entry.offer.OwnVote = int32(req.GetVote())
	s.mu.Unlock()
	body := wow.EncodeLootRoll(wow.GUID(req.GetItemGuid()), req.GetSlot(), uint8(req.GetVote()))
	if _, err := s.send(ctx, opcode.CMSGLootRoll, body); err != nil {
		return nil, err
	}
	return s.state(), nil
}

func (s *Service) onRollPacket(packet *modv1.WorldPacket) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch packet.GetOpcode() {
	case opcode.SMSGLootStartRoll:
		start, err := wow.ParseLootStartRoll(packet.GetPayload())
		if err != nil {
			s.rollParseErrorLocked("start", err)
			return
		}
		if existing := s.findRollLocked(start.ItemGUID, start.Slot); existing != nil && existing.offer.Active {
			return // duplicate start must not erase a dispatched vote
		}
		entry := &rollEntry{offer: modulev1.RollOffer{
			ItemGuid: uint64(start.ItemGUID), MapId: start.MapID, Slot: start.Slot,
			ItemId: start.ItemID, RandomSuffix: start.RandomSuffix,
			RandomProperty: start.RandomProperty, Count: start.Count,
			CountdownMs: start.CountdownMS, DeadlineAt: s.now().UnixMilli() + int64(start.CountdownMS),
			VoteMask: uint32(start.VoteMask), OwnVote: -1, Active: true,
		}}
		s.rolls = append(s.rolls, entry)
		if len(s.rolls) > 16 {
			s.rolls = append([]*rollEntry(nil), s.rolls[len(s.rolls)-16:]...)
		}
	case opcode.SMSGLootRoll:
		vote, err := wow.ParseLootRoll(packet.GetPayload())
		if err != nil {
			s.rollParseErrorLocked("vote", err)
			return
		}
		entry := s.findRollLocked(vote.ItemGUID, vote.Slot)
		if entry == nil || vote.PlayerGUID != s.selfGUID {
			return
		}
		mapped, ok := serverVote(vote.Vote)
		if ok && (entry.offer.OwnVote < 0 || entry.offer.OwnVote == mapped) {
			entry.offer.OwnVote = mapped
			entry.confirmed = true
		}
	case opcode.SMSGLootRollWon:
		won, err := wow.ParseLootRollWon(packet.GetPayload())
		if err != nil {
			s.rollParseErrorLocked("won", err)
			return
		}
		if entry := s.findRollLocked(won.ItemGUID, won.Slot); entry != nil {
			entry.offer.Active = false
			entry.offer.Outcome = "won"
			entry.offer.WinnerGuid = uint64(won.WinnerGUID)
		}
	case opcode.SMSGLootAllPassed:
		passed, err := wow.ParseLootAllPassed(packet.GetPayload())
		if err != nil {
			s.rollParseErrorLocked("all passed", err)
			return
		}
		if entry := s.findRollLocked(passed.ItemGUID, passed.Slot); entry != nil {
			entry.offer.Active = false
			entry.offer.Outcome = "all_passed"
		}
	}
}

func (s *Service) findRollLocked(guid wow.GUID, slot uint32) *rollEntry {
	for i := len(s.rolls) - 1; i >= 0; i-- {
		if s.rolls[i].offer.ItemGuid == uint64(guid) && s.rolls[i].offer.Slot == slot {
			return s.rolls[i]
		}
	}
	return nil
}

func (s *Service) rollProtosLocked() []*modulev1.RollOffer {
	out := make([]*modulev1.RollOffer, 0, len(s.rolls))
	for _, entry := range s.rolls {
		copy := entry.offer
		copy.VoteConfirmed = entry.confirmed
		out = append(out, &copy)
	}
	return out
}

func (s *Service) rollParseErrorLocked(what string, err error) {
	s.errors++
	s.lastError = fmt.Sprintf("roll %s: %v", what, err)
	slog.Warn("dropped malformed roll packet", "packet", what, "error", err)
}

func serverVote(vote uint8) (int32, bool) {
	switch vote {
	case 0:
		return 1, true // need
	case 1:
		return 2, true // greed
	case 2:
		return 0, true // pass
	default:
		return 0, false
	}
}
