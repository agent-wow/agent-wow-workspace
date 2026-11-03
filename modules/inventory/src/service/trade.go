package service

import (
	"context"
	"fmt"

	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type tradeSession struct {
	state                 modulev1.TradeState
	acceptAttemptRevision uint64
}

func (s *Service) Trade(ctx context.Context, req *modulev1.TradeRequest) (*modulev1.InventoryState, error) {
	action := req.GetAction()
	if action == "initiate" {
		if req.GetTargetGuid() == 0 {
			return nil, status.Error(codes.InvalidArgument, "target_guid is required")
		}
		s.mu.Lock()
		if s.trade != nil && (s.trade.state.Active || s.trade.state.Pending) {
			s.mu.Unlock()
			return nil, status.Error(codes.FailedPrecondition, "another trade is pending or active")
		}
		s.mu.Unlock()
		if _, err := s.send(ctx, opcode.CMSGInitiateTrade, wow.EncodeInitiateTrade(wow.GUID(req.GetTargetGuid()))); err != nil {
			return nil, err
		}
		return s.snapshot(), nil
	}

	s.mu.Lock()
	trade := s.trade
	if trade == nil || (!trade.state.Active && !((action == "begin" || action == "cancel") && trade.state.Pending)) {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "trade is not in a state that allows this action")
	}
	if action != "begin" && action != "cancel" && req.GetTradeId() != trade.state.TradeId {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "trade id does not match the current server trade")
	}
	var op uint32
	var body []byte
	switch action {
	case "begin":
		op = opcode.CMSGBeginTrade
	case "set_item":
		if req.GetTradeSlot() >= 7 || req.GetBag() > 255 || req.GetSlot() > 255 || req.GetItemGuid() == 0 {
			s.mu.Unlock()
			return nil, status.Error(codes.InvalidArgument, "trade_slot 0..6, bag, slot and item_guid are required")
		}
		op = opcode.CMSGSetTradeItem
		body = wow.EncodeSetTradeItem(uint8(req.GetTradeSlot()), uint8(req.GetBag()), uint8(req.GetSlot()))
	case "clear":
		if req.GetTradeSlot() >= 7 {
			s.mu.Unlock()
			return nil, status.Error(codes.InvalidArgument, "trade_slot must be 0..6")
		}
		op = opcode.CMSGClearTradeItem
		body = wow.EncodeClearTradeItem(uint8(req.GetTradeSlot()))
	case "set_gold":
		op = opcode.CMSGSetTradeGold
		body = wow.EncodeSetTradeGold(req.GetCopper())
	case "accept":
		if req.GetExpectedRevision() == 0 || req.GetExpectedRevision() != trade.state.Revision {
			s.mu.Unlock()
			return nil, status.Error(codes.FailedPrecondition, "trade revision changed after the offer was read")
		}
		if trade.acceptAttemptRevision == trade.state.Revision {
			s.mu.Unlock()
			return nil, status.Error(codes.AlreadyExists, "accept was already dispatched for this trade revision")
		}
		trade.acceptAttemptRevision = trade.state.Revision
		trade.state.OwnAccepted = true
		trade.state.Revision++
		s.revision++
		op = opcode.CMSGAcceptTrade
	case "unaccept":
		trade.state.OwnAccepted = false
		trade.acceptAttemptRevision = 0
		trade.state.Revision++
		s.revision++
		op = opcode.CMSGUnacceptTrade
	case "cancel":
		op = opcode.CMSGCancelTrade
	default:
		s.mu.Unlock()
		return nil, status.Error(codes.InvalidArgument, fmt.Sprintf("unknown trade action %q", action))
	}
	s.mu.Unlock()

	if action == "set_item" || action == "set_gold" {
		current := s.snapshot()
		if action == "set_gold" && req.GetCopper() > current.GetMoney() {
			return nil, status.Error(codes.FailedPrecondition, "trade gold exceeds current money")
		}
		if action == "set_item" && !inventoryIdentityMatches(current, req) {
			return nil, status.Error(codes.FailedPrecondition, "item guid no longer matches the supplied bag and slot")
		}
	}
	if _, err := s.send(ctx, op, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func inventoryIdentityMatches(state *modulev1.InventoryState, req *modulev1.TradeRequest) bool {
	for _, item := range state.GetItems() {
		if !item.GetEquipped() && item.GetBag() == req.GetBag() && item.GetSlot() == req.GetSlot() && item.GetGuid() == req.GetItemGuid() {
			return true
		}
	}
	return false
}

func (s *Service) onTradeStatus(status wow.TradeStatus) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.trade == nil {
		s.trade = &tradeSession{}
	}
	trade := &s.trade.state
	trade.LastStatus = status.Status
	switch status.Status {
	case wow.TradeStatusBegin:
		*s.trade = tradeSession{state: modulev1.TradeState{Pending: true, TraderGuid: uint64(status.TraderGUID), LastStatus: status.Status, Revision: trade.Revision + 1}}
	case wow.TradeStatusOpen:
		trade.Pending = false
		trade.Active = true
		trade.Complete = false
		trade.Cancelled = false
		trade.TradeId = status.TradeID
	case wow.TradeStatusAccepted:
		trade.TraderAccepted = true
	case wow.TradeStatusComplete:
		trade.Pending = false
		trade.Active = false
		trade.Complete = true
		trade.Cancelled = false
		trade.OwnAccepted = true
		trade.TraderAccepted = true
	case 7: // BACK_TO_TRADE: partner reopened after an accept
		trade.TraderAccepted = false
	case 3, 9, 12: // cancelled, rejected, closed
		trade.Pending = false
		trade.Active = false
		trade.Cancelled = true
		trade.CloseResult = status.Result
		trade.OwnAccepted = false
		trade.TraderAccepted = false
	}
	trade.Revision++
	s.revision++
}

func (s *Service) onTradeExtended(view wow.TradeExtended) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.trade == nil {
		s.trade = &tradeSession{}
	}
	trade := &s.trade.state
	trade.TradeId = view.TradeID
	trade.Pending = false
	trade.Active = true
	trade.Complete = false
	trade.Cancelled = false
	// Any offer revision invalidates both clients' previous acceptance.
	trade.OwnAccepted = false
	trade.TraderAccepted = false
	s.trade.acceptAttemptRevision = 0
	items := make([]*modulev1.TradeItem, 0, len(view.Items))
	for _, item := range view.Items {
		items = append(items, &modulev1.TradeItem{Slot: uint32(item.Slot), ItemId: item.ItemID, Count: item.Count, RandomProperty: item.RandomProperty})
	}
	if view.TraderData {
		trade.TraderItems = items
		trade.TraderMoney = view.Money
	} else {
		trade.OwnItems = items
		trade.OwnMoney = view.Money
	}
	trade.Revision++
	s.revision++
}

func (s *Service) tradeSnapshotLocked() *modulev1.TradeState {
	if s.trade == nil {
		return &modulev1.TradeState{}
	}
	copy := s.trade.state
	copy.OwnItems = append([]*modulev1.TradeItem(nil), s.trade.state.OwnItems...)
	copy.TraderItems = append([]*modulev1.TradeItem(nil), s.trade.state.TraderItems...)
	return &copy
}
