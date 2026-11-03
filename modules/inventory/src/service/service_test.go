package service

import (
	"context"
	"encoding/binary"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

type inventorySession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
}

func (f inventorySession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	f.sent <- req
	return &emptypb.Empty{}, nil
}

func TestSnapshotAccumulatesBagsEquipmentAndBuyback(t *testing.T) {
	s := New(nil)
	s.selfGUID = 1
	s.objects[1] = &trackedObject{typ: wow.ObjectTypePlayer, fields: wow.UpdateFields{
		wow.PlayerCoinage:                     1234,
		wow.PlayerFieldInvSlotHead:            100,
		wow.PlayerFieldInvSlotHead + 1:        0,
		wow.PlayerFieldInvSlotHead + 19*2:     200,
		wow.PlayerFieldInvSlotHead + 19*2 + 1: 0,
		wow.PlayerFieldPackSlot1:              300,
		wow.PlayerFieldPackSlot1 + 1:          0,
		wow.PlayerFieldVendorBuybackSlot1:     400,
		wow.PlayerFieldVendorBuybackSlot1 + 1: 0,
		wow.PlayerFieldBuybackPrice1:          77,
	}}
	s.objects[100] = &trackedObject{typ: wow.ObjectTypeItem, fields: wow.UpdateFields{wow.ObjectFieldEntry: 10}}
	s.objects[200] = &trackedObject{typ: wow.ObjectTypeContainer, fields: wow.UpdateFields{
		wow.ObjectFieldEntry: 20, wow.ContainerFieldNumSlots: 4,
		wow.ContainerFieldSlot1: 301, wow.ContainerFieldSlot1 + 1: 0,
	}}
	s.objects[300] = &trackedObject{typ: wow.ObjectTypeItem, fields: wow.UpdateFields{
		wow.ObjectFieldEntry: 30, wow.ItemFieldStackCount: 2,
	}}
	s.objects[301] = &trackedObject{typ: wow.ObjectTypeItem, fields: wow.UpdateFields{wow.ObjectFieldEntry: 40}}
	s.objects[400] = &trackedObject{typ: wow.ObjectTypeItem, fields: wow.UpdateFields{wow.ObjectFieldEntry: 50}}
	for id, name := range map[uint32]string{10: "head", 20: "bag", 30: "food", 40: "ore", 50: "sold"} {
		s.templates[id] = wow.ItemTemplate{ItemID: id, Name: name, SellPrice: id}
	}

	state := s.snapshot()
	if state.GetMoney() != 1234 || state.GetFreeSlots() != 18 || len(state.GetItems()) != 4 {
		t.Fatalf("unexpected inventory totals: money=%d free=%d items=%d", state.GetMoney(), state.GetFreeSlots(), len(state.GetItems()))
	}
	if len(state.GetBuyback()) != 1 || state.GetBuyback()[0].GetBuyPrice() != 77 {
		t.Fatalf("unexpected buyback: %+v", state.GetBuyback())
	}
	var foundContained bool
	for _, item := range state.GetItems() {
		if item.GetGuid() == 301 {
			foundContained = item.GetBag() == 19 && item.GetSlot() == 0 && item.GetTemplateKnown()
		}
		if item.GetGuid() == 300 && item.GetCount() != 2 {
			t.Fatalf("stack count not merged: %+v", item)
		}
	}
	if !foundContained {
		t.Fatal("container item missing from snapshot")
	}
}

func TestSparseItemUpdateRetainsEntry(t *testing.T) {
	s := New(nil)
	created := s.applyObjectLocked(wow.ObjectUpdate{
		Kind: wow.UpdateCreate, GUID: 100, Type: wow.ObjectTypeItem,
		Fields: wow.UpdateFields{wow.ObjectFieldEntry: 25},
	})
	s.applyObjectLocked(wow.ObjectUpdate{
		Kind: wow.UpdateValues, GUID: 100, Fields: wow.UpdateFields{wow.ItemFieldStackCount: 3},
	})
	if created.fields[wow.ObjectFieldEntry] != 25 || created.fields[wow.ItemFieldStackCount] != 3 {
		t.Fatalf("sparse fields were replaced: %+v", created.fields)
	}
}

func TestVendorListAndEquipmentFailure(t *testing.T) {
	s := New(nil)
	body := binary.LittleEndian.AppendUint64(nil, 99)
	body = append(body, 1)
	for _, value := range []uint32{1, 25, 123, 0xffffffff, 50, 20, 2, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGListInventory, Payload: body}); err != nil {
		t.Fatal(err)
	}
	state := s.snapshot()
	if !state.GetVendorLoaded() || state.GetVendorGuid() != 99 || len(state.GetVendorItems()) != 1 {
		t.Fatalf("vendor state not applied: %+v", state)
	}
	select {
	case itemID := <-s.queries:
		if itemID != 25 {
			t.Fatalf("queried item %d, want 25", itemID)
		}
	default:
		t.Fatal("vendor item template was not queued")
	}

	failure := []byte{12}
	failure = binary.LittleEndian.AppendUint64(failure, 42)
	failure = binary.LittleEndian.AppendUint64(failure, 0)
	failure = append(failure, 0)
	failure = binary.LittleEndian.AppendUint32(failure, 8)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGInventoryChangeFailure, Payload: failure}); err != nil {
		t.Fatal(err)
	}
	last := s.snapshot().GetLastFailure()
	if last.GetOperation() != "equip" || last.GetItemGuid() != 42 || last.GetRequiredLevel() != 8 {
		t.Fatalf("equipment refusal not exposed: %+v", last)
	}
}

func TestQueryItemQueuesOnceAndExposesKnownTemplate(t *testing.T) {
	s := New(nil)
	if _, err := s.QueryItem(t.Context(), &modulev1.ItemQuery{}); err == nil {
		t.Fatal("zero item id accepted")
	}
	if _, err := s.QueryItem(t.Context(), &modulev1.ItemQuery{ItemId: 25}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.QueryItem(t.Context(), &modulev1.ItemQuery{ItemId: 25}); err != nil {
		t.Fatal(err)
	}
	if len(s.queries) != 1 || <-s.queries != 25 {
		t.Fatal("item query was not deduplicated")
	}
	s.templates[25] = wow.ItemTemplate{ItemID: 25, Name: "known"}
	state := s.snapshot()
	if len(state.GetItemTemplates()) != 1 || !state.GetItemTemplates()[0].GetTemplateKnown() {
		t.Fatalf("known template missing: %+v", state.GetItemTemplates())
	}
}

func TestActionValidationAndPackets(t *testing.T) {
	session := inventorySession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	if _, err := s.Buy(t.Context(), &modulev1.BuyRequest{VendorGuid: 1, ItemId: 25, Count: 1}); err == nil {
		t.Fatal("zero vendor slot accepted")
	}
	if _, err := s.Buy(t.Context(), &modulev1.BuyRequest{VendorGuid: 1, ItemId: 25, Slot: 3, Count: 2}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Sell(t.Context(), &modulev1.SellRequest{VendorGuid: 1, ItemGuid: 42}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Equip(t.Context(), &modulev1.EquipRequest{ItemGuid: 42, DestinationSlot: 15}); err != nil {
		t.Fatal(err)
	}
	want := []uint32{opcode.CMSGBuyItem, opcode.CMSGSellItem, opcode.CMSGAutoequipItemSlot}
	for _, op := range want {
		select {
		case packet := <-session.sent:
			if packet.GetOpcode() != op {
				t.Fatalf("opcode 0x%x, want 0x%x", packet.GetOpcode(), op)
			}
		default:
			t.Fatalf("missing opcode 0x%x", op)
		}
	}
}
