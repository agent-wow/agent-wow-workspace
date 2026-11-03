// Package service exposes server-backed inventory facts and bounded item actions.
// It deliberately contains no equipment, purchase or sale strategy.
package service

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"sync"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const itemQueryQueueSize = 256

type trackedObject struct {
	typ    wow.ObjectType
	fields wow.UpdateFields
}

// Service accumulates sparse update fields. Packet callbacks only update state
// and enqueue item-template queries; a worker performs the blocking session RPC.
type Service struct {
	modulev1.UnimplementedModuleServer
	session  modv1.SessionClient
	selfGUID wow.GUID

	mu           sync.RWMutex
	objects      map[wow.GUID]*trackedObject
	templates    map[uint32]wow.ItemTemplate
	queried      map[uint32]struct{}
	queries      chan uint32
	vendor       wow.GUID
	vendorLoaded bool
	vendorItems  []wow.VendorItem
	lastFailure  *modulev1.Failure
	failureCount uint32
	itemQueries  uint32
	parseErrors  uint32
	revision     uint64
	trade        *tradeSession
}

func New(session modv1.SessionClient) *Service {
	guid, _ := strconv.ParseUint(os.Getenv("AGENT_WOW_CHARACTER_GUID"), 10, 64)
	return &Service{
		session:   session,
		selfGUID:  wow.GUID(guid),
		objects:   make(map[wow.GUID]*trackedObject),
		templates: make(map[uint32]wow.ItemTemplate),
		queried:   make(map[uint32]struct{}),
		queries:   make(chan uint32, itemQueryQueueSize),
	}
}

func (s *Service) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case itemID := <-s.queries:
				callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				_, err := s.session.SendPacket(callCtx, &modv1.SendPacketRequest{
					Opcode: opcode.CMSGItemQuerySingle, Payload: wow.EncodeItemQuerySingle(itemID),
				})
				cancel()
				if err != nil {
					s.mu.Lock()
					delete(s.queried, itemID)
					s.mu.Unlock()
					slog.Warn("item query failed", "item_id", itemID, "error", err)
				}
			}
		}
	}()
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.InventoryState, error) {
	return s.snapshot(), nil
}

func (s *Service) ListInventory(ctx context.Context, req *modulev1.VendorRequest) (*modulev1.InventoryState, error) {
	vendor := wow.GUID(req.GetVendorGuid())
	if vendor == 0 {
		return nil, status.Error(codes.InvalidArgument, "vendor_guid is required")
	}
	if _, err := s.send(ctx, opcode.CMSGListInventory, wow.EncodeListInventory(vendor)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.vendor = vendor
	s.vendorLoaded = false
	s.vendorItems = nil
	s.revision++
	s.mu.Unlock()
	return s.snapshot(), nil
}

func (s *Service) Buy(ctx context.Context, req *modulev1.BuyRequest) (*modulev1.InventoryState, error) {
	if req.GetVendorGuid() == 0 || req.GetItemId() == 0 || req.GetSlot() == 0 || req.GetCount() == 0 {
		return nil, status.Error(codes.InvalidArgument, "vendor_guid, item_id, one-based slot and positive count are required")
	}
	body := wow.EncodeBuyItem(wow.GUID(req.GetVendorGuid()), req.GetItemId(), req.GetSlot(), req.GetCount())
	if _, err := s.send(ctx, opcode.CMSGBuyItem, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) Sell(ctx context.Context, req *modulev1.SellRequest) (*modulev1.InventoryState, error) {
	if req.GetVendorGuid() == 0 || req.GetItemGuid() == 0 {
		return nil, status.Error(codes.InvalidArgument, "vendor_guid and item_guid are required")
	}
	body := wow.EncodeSellItem(wow.GUID(req.GetVendorGuid()), wow.GUID(req.GetItemGuid()), req.GetCount())
	if _, err := s.send(ctx, opcode.CMSGSellItem, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) Repair(ctx context.Context, req *modulev1.RepairRequest) (*modulev1.InventoryState, error) {
	if req.GetVendorGuid() == 0 {
		return nil, status.Error(codes.InvalidArgument, "vendor_guid is required")
	}
	body := wow.EncodeRepairItem(wow.GUID(req.GetVendorGuid()), wow.GUID(req.GetItemGuid()), req.GetUseGuildBank())
	if _, err := s.send(ctx, opcode.CMSGRepairItem, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) Equip(ctx context.Context, req *modulev1.EquipRequest) (*modulev1.InventoryState, error) {
	if req.GetItemGuid() == 0 || req.GetDestinationSlot() > 18 {
		return nil, status.Error(codes.InvalidArgument, "item_guid and destination_slot 0..18 are required")
	}
	body := wow.EncodeAutoequipItemSlot(wow.GUID(req.GetItemGuid()), uint8(req.GetDestinationSlot()))
	if _, err := s.send(ctx, opcode.CMSGAutoequipItemSlot, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) Buyback(ctx context.Context, req *modulev1.BuybackRequest) (*modulev1.InventoryState, error) {
	if req.GetVendorGuid() == 0 || req.GetSlot() < 74 || req.GetSlot() >= 86 {
		return nil, status.Error(codes.InvalidArgument, "vendor_guid and absolute buyback slot 74..85 are required")
	}
	body := wow.EncodeBuybackItem(wow.GUID(req.GetVendorGuid()), uint8(req.GetSlot()))
	if _, err := s.send(ctx, opcode.CMSGBuybackItem, body); err != nil {
		return nil, err
	}
	return s.snapshot(), nil
}

func (s *Service) QueryItem(_ context.Context, req *modulev1.ItemQuery) (*modulev1.InventoryState, error) {
	if req.GetItemId() == 0 {
		return nil, status.Error(codes.InvalidArgument, "item_id is required")
	}
	s.mu.Lock()
	queries := s.queueQueryLocked(nil, req.GetItemId())
	s.mu.Unlock()
	s.enqueueQueries(queries)
	return s.snapshot(), nil
}

func (s *Service) send(ctx context.Context, op uint32, payload []byte) (*emptypb.Empty, error) {
	return s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: op, Payload: payload})
}

func (s *Service) recordFailure(failure *modulev1.Failure) {
	s.mu.Lock()
	s.lastFailure = failure
	s.failureCount++
	s.revision++
	s.mu.Unlock()
	slog.Info("inventory action refused", "operation", failure.GetOperation(), "result", failure.GetResult(), "detail", failure.GetDetail())
}

func (s *Service) recordParseError(what string, err error) {
	s.mu.Lock()
	s.parseErrors++
	s.mu.Unlock()
	slog.Warn("dropped malformed inventory packet", "packet", what, "error", err)
}

func failureDetail(operation string, result uint8) string {
	return fmt.Sprintf("%s result %d", operation, result)
}
