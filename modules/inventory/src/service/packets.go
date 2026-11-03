package service

import (
	"context"
	"fmt"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/protobuf/types/known/emptypb"
)

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	switch packet.GetOpcode() {
	case opcode.SMSGLoginVerifyWorld, opcode.SMSGNewWorld:
		s.resetWorld()
		return &emptypb.Empty{}, nil
	case opcode.SMSGUpdateObject, opcode.SMSGCompressedUpdateObject:
		s.applyUpdatePacket(packet.GetOpcode(), packet.GetPayload())
		return &emptypb.Empty{}, nil
	case opcode.SMSGDestroyObject:
		guid, err := wow.ParseDestroyObject(packet.GetPayload())
		if err != nil {
			s.recordParseError("destroy object", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		if guid != s.selfGUID {
			delete(s.objects, guid)
			s.revision++
		}
		s.mu.Unlock()
		return &emptypb.Empty{}, nil
	case opcode.SMSGItemQuerySingleResponse:
		template, err := wow.ParseItemQuerySingleResponse(packet.GetPayload())
		if err != nil {
			s.recordParseError("item query", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.templates[template.ItemID] = template
		s.revision++
		s.mu.Unlock()
		return &emptypb.Empty{}, nil
	case opcode.SMSGListInventory:
		list, err := wow.ParseListInventory(packet.GetPayload())
		if err != nil {
			s.recordParseError("vendor list", err)
			return &emptypb.Empty{}, nil
		}
		s.mu.Lock()
		s.vendor = list.Vendor
		s.vendorLoaded = true
		s.vendorItems = append(s.vendorItems[:0], list.Items...)
		s.revision++
		queries := make([]uint32, 0, len(list.Items))
		for _, row := range list.Items {
			queries = s.queueQueryLocked(queries, row.ItemID)
		}
		s.mu.Unlock()
		s.enqueueQueries(queries)
		return &emptypb.Empty{}, nil
	case opcode.SMSGBuyFailed:
		failure, err := wow.ParseBuyFailed(packet.GetPayload())
		if err != nil {
			s.recordParseError("buy failure", err)
			return &emptypb.Empty{}, nil
		}
		s.recordFailure(&modulev1.Failure{
			Operation: "buy", Result: uint32(failure.Result), ItemId: failure.ItemID,
			Detail: failureDetail("buy_failed", failure.Result),
		})
		return &emptypb.Empty{}, nil
	case opcode.SMSGSellItem:
		failure, err := wow.ParseSellItem(packet.GetPayload())
		if err != nil {
			s.recordParseError("sell reply", err)
			return &emptypb.Empty{}, nil
		}
		if failure.Result != 0 {
			s.recordFailure(&modulev1.Failure{
				Operation: "sell", Result: uint32(failure.Result), ItemGuid: uint64(failure.Item),
				Detail: failureDetail("sell_failed", failure.Result),
			})
		}
		return &emptypb.Empty{}, nil
	case opcode.SMSGInventoryChangeFailure:
		failure, err := wow.ParseInventoryChangeFailure(packet.GetPayload())
		if err != nil {
			s.recordParseError("inventory change failure", err)
			return &emptypb.Empty{}, nil
		}
		if failure.Result != 0 {
			detail := failureDetail("equip_failed", failure.Result)
			if failure.RequiredLevel > 0 {
				detail = fmt.Sprintf("%s required_level=%d", detail, failure.RequiredLevel)
			}
			s.recordFailure(&modulev1.Failure{
				Operation: "equip", Result: uint32(failure.Result), ItemGuid: uint64(failure.Item1),
				RequiredLevel: failure.RequiredLevel, Detail: detail,
			})
		}
		return &emptypb.Empty{}, nil
	default:
		return &emptypb.Empty{}, nil
	}
}

func (s *Service) resetWorld() {
	s.mu.Lock()
	s.objects = make(map[wow.GUID]*trackedObject)
	s.vendor = 0
	s.vendorLoaded = false
	s.vendorItems = nil
	s.lastFailure = nil
	s.revision++
	s.mu.Unlock()
}

func (s *Service) applyUpdatePacket(op uint32, body []byte) {
	var update *wow.Update
	var err error
	if op == opcode.SMSGCompressedUpdateObject {
		update, err = wow.ParseCompressedUpdateObject(body)
	} else {
		update, err = wow.ParseUpdateObject(body)
	}
	if err != nil {
		s.recordParseError("object update", err)
		return
	}

	s.mu.Lock()
	for _, guid := range update.OutOfRange {
		if guid != s.selfGUID {
			delete(s.objects, guid)
		}
	}
	queries := make([]uint32, 0)
	changed := len(update.OutOfRange) > 0
	for _, obj := range update.Objects {
		tracked := s.applyObjectLocked(obj)
		if tracked == nil {
			continue
		}
		changed = true
		if tracked.typ == wow.ObjectTypeItem || tracked.typ == wow.ObjectTypeContainer {
			if entry, ok := tracked.fields.Uint32(wow.ObjectFieldEntry); ok && entry != 0 {
				queries = s.queueQueryLocked(queries, entry)
			}
		}
	}
	if changed {
		s.revision++
	}
	s.mu.Unlock()
	s.enqueueQueries(queries)
}

// applyObjectLocked merges sparse fields and ignores unrelated world objects.
func (s *Service) applyObjectLocked(obj wow.ObjectUpdate) *trackedObject {
	if obj.GUID == 0 {
		return nil
	}
	tracked := s.objects[obj.GUID]
	isSelf := obj.GUID == s.selfGUID
	if tracked == nil {
		isItem := obj.Kind == wow.UpdateCreate || obj.Kind == wow.UpdateCreate2
		isItem = isItem && (obj.Type == wow.ObjectTypeItem || obj.Type == wow.ObjectTypeContainer)
		if !isSelf && !isItem {
			return nil
		}
		typ := obj.Type
		if isSelf {
			typ = wow.ObjectTypePlayer
		}
		tracked = &trackedObject{typ: typ, fields: wow.UpdateFields{}}
		s.objects[obj.GUID] = tracked
	}
	if obj.Kind == wow.UpdateCreate || obj.Kind == wow.UpdateCreate2 {
		tracked.typ = obj.Type
		if isSelf {
			tracked.typ = wow.ObjectTypePlayer
		}
	}
	for index, value := range obj.Fields {
		tracked.fields[index] = value
	}
	return tracked
}

func (s *Service) queueQueryLocked(queries []uint32, itemID uint32) []uint32 {
	if itemID == 0 {
		return queries
	}
	if _, ok := s.templates[itemID]; ok {
		return queries
	}
	if _, ok := s.queried[itemID]; ok {
		return queries
	}
	s.queried[itemID] = struct{}{}
	s.itemQueries++
	return append(queries, itemID)
}

func (s *Service) enqueueQueries(queries []uint32) {
	for _, itemID := range queries {
		select {
		case s.queries <- itemID:
		default:
			s.mu.Lock()
			delete(s.queried, itemID)
			s.mu.Unlock()
		}
	}
}
