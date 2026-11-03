// Package service implements the loot module: open a corpse on request, then
// take every row the server offers so an unattended character empties every
// kill without a human clicking.
package service

import (
	"context"
	"fmt"
	"log/slog"
	"sync"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/loot/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Service owns the loot state. OnPacket runs on the session owner loop, so it
// never calls SendPacket directly: the take sequence is queued to a worker.
type Service struct {
	modulev1.UnimplementedModuleServer
	session modv1.SessionClient

	mu        sync.Mutex
	opened    uint32
	items     uint32
	money     uint32
	errors    uint32
	autoLoot  bool
	lastError string
	lastItems []*modulev1.LootItem

	jobs chan lootJob
}

// lootJob is one open window that still has rows or money to take.
type lootJob struct {
	guid  wow.GUID
	gold  uint32
	slots []uint8
}

func New(session modv1.SessionClient) *Service {
	return &Service{
		session:  session,
		autoLoot: true,
		jobs:     make(chan lootJob, 8),
	}
}

// Start launches the take worker. It is kept off the packet path because
// SendPacket waits on the session owner loop.
func (s *Service) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case job := <-s.jobs:
				s.take(ctx, job)
			}
		}
	}()
}

func (s *Service) GetState(context.Context, *emptypb.Empty) (*modulev1.LootState, error) {
	return s.state(), nil
}

// Loot asks the server to open a corpse. The rows arrive later as
// SMSG_LOOT_RESPONSE and are taken by the worker.
func (s *Service) Loot(ctx context.Context, req *modulev1.LootRequest) (*modulev1.LootState, error) {
	guid := wow.GUID(req.GetGuid())
	if guid == 0 {
		return nil, status.Error(codes.InvalidArgument, "guid is required")
	}
	if _, err := s.send(ctx, opcode.CMSGLoot, wow.EncodeLoot(guid)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.opened++
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) SetAutoLoot(_ context.Context, req *modulev1.AutoLootRequest) (*modulev1.LootState, error) {
	s.mu.Lock()
	s.autoLoot = req.GetEnabled()
	s.mu.Unlock()
	return s.state(), nil
}

func (s *Service) OnPacket(_ context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	if packet.GetOpcode() != opcode.SMSGLootResponse {
		return &emptypb.Empty{}, nil
	}
	resp, err := wow.ParseLootResponse(packet.GetPayload())
	if err != nil {
		slog.Warn("bad loot response", "error", err)
		return &emptypb.Empty{}, nil
	}
	if resp.LootType == wow.LootTypeNone {
		name := lootErrorName(resp.Error)
		s.mu.Lock()
		s.errors++
		s.lastError = name
		s.mu.Unlock()
		slog.Info("loot refused", "guid", uint64(resp.GUID), "error", name)
		return &emptypb.Empty{}, nil
	}

	slots := make([]uint8, 0, len(resp.Items))
	rows := make([]*modulev1.LootItem, 0, len(resp.Items))
	for _, item := range resp.Items {
		rows = append(rows, &modulev1.LootItem{ItemId: item.ItemID, Count: item.Count})
		if item.SlotType.Takeable() {
			slots = append(slots, item.Slot)
		}
	}
	s.mu.Lock()
	s.lastItems = rows
	s.mu.Unlock()
	slog.Info("loot window", "guid", uint64(resp.GUID), "type", resp.LootType,
		"gold", resp.Gold, "rows", len(resp.Items), "takeable", len(slots))
	// Every valid window goes through the worker, including empty ones and
	// windows received with auto-loot disabled: take always releases the window.
	select {
	case s.jobs <- lootJob{guid: resp.GUID, gold: resp.Gold, slots: slots}:
	default:
		slog.Warn("loot queue full; dropping window", "guid", uint64(resp.GUID))
	}
	return &emptypb.Empty{}, nil
}

// take empties one window and releases it. Release always runs, even after a
// partial failure, so the character never stays stuck in the looting state.
func (s *Service) take(ctx context.Context, job lootJob) {
	taken := uint32(0)
	moneyTaken := false
	for _, slot := range job.slots {
		if !s.autoLootEnabled() {
			break
		}
		if _, err := s.send(ctx, opcode.CMSGAutostoreLootItem, wow.EncodeAutostoreLootItem(slot)); err != nil {
			slog.Warn("take loot row failed", "slot", slot, "error", err)
			break
		}
		taken++
	}
	if job.gold > 0 && s.autoLootEnabled() {
		if _, err := s.send(ctx, opcode.CMSGLootMoney, nil); err != nil {
			slog.Warn("take loot money failed", "error", err)
		} else {
			moneyTaken = true
			s.mu.Lock()
			s.money += job.gold
			s.mu.Unlock()
		}
	}
	if _, err := s.send(ctx, opcode.CMSGLootRelease, wow.EncodeLootRelease(job.guid)); err != nil {
		slog.Warn("release loot failed", "guid", uint64(job.guid), "error", err)
	}
	s.mu.Lock()
	s.items += taken
	s.mu.Unlock()
	if taken > 0 || moneyTaken {
		slog.Info("looted", "guid", uint64(job.guid), "items", taken, "gold", job.gold)
	}
}

func (s *Service) autoLootEnabled() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.autoLoot
}

func (s *Service) send(ctx context.Context, op uint32, payload []byte) (*emptypb.Empty, error) {
	return s.session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: op, Payload: payload})
}

func (s *Service) state() *modulev1.LootState {
	s.mu.Lock()
	defer s.mu.Unlock()
	items := make([]*modulev1.LootItem, len(s.lastItems))
	copy(items, s.lastItems)
	return &modulev1.LootState{
		Opened:    s.opened,
		Items:     s.items,
		Money:     s.money,
		Errors:    s.errors,
		AutoLoot:  s.autoLoot,
		LastError: s.lastError,
		LastItems: items,
	}
}

// lootErrorName maps the LootError codes in LootMgr.h. The enum is sparse, so
// a switch beats an array.
func lootErrorName(code uint8) string {
	switch code {
	case 0:
		return "didnt_kill"
	case 4:
		return "too_far"
	case 5:
		return "bad_facing"
	case 6:
		return "locked"
	case 8:
		return "not_standing"
	case 9:
		return "stunned"
	case 10:
		return "player_not_found"
	case 11:
		return "play_time_exceeded"
	case 12:
		return "master_inventory_full"
	case 13:
		return "master_unique_item"
	case 14:
		return "master_other"
	case 15:
		return "already_pickpocketed"
	case 16:
		return "not_while_shapeshifted"
	default:
		return fmt.Sprintf("unknown(%d)", code)
	}
}
