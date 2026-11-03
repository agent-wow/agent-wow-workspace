package service

import (
	"context"
	"encoding/binary"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/loot/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

type lootSession struct {
	modv1.SessionClient
	sent chan *modv1.SendPacketRequest
}

func (f lootSession) SendPacket(_ context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	f.sent <- req
	return &emptypb.Empty{}, nil
}

func lootWindow(guid uint64, gold uint32, withItem bool) []byte {
	body := binary.LittleEndian.AppendUint64(nil, guid)
	body = append(body, wow.LootTypeCorpse)
	body = binary.LittleEndian.AppendUint32(body, gold)
	if !withItem {
		return append(body, 0)
	}
	body = append(body, 1, 0)
	body = binary.LittleEndian.AppendUint32(body, 25) // item ID
	body = binary.LittleEndian.AppendUint32(body, 1)  // count
	body = binary.LittleEndian.AppendUint32(body, 0)  // display info
	body = binary.LittleEndian.AppendUint32(body, 0)  // random suffix
	body = binary.LittleEndian.AppendUint32(body, 0)  // random property
	return append(body, byte(wow.LootSlotOwner))
}

func TestLootWindowSequence(t *testing.T) {
	tests := []struct {
		name     string
		autoLoot bool
		gold     uint32
		withItem bool
		want     []uint32
	}{
		{
			name: "take item money and release", autoLoot: true, gold: 7, withItem: true,
			want: []uint32{opcode.CMSGAutostoreLootItem, opcode.CMSGLootMoney, opcode.CMSGLootRelease},
		},
		{
			name: "release empty window", autoLoot: true,
			want: []uint32{opcode.CMSGLootRelease},
		},
		{
			name: "keep window open when disabled", autoLoot: false, gold: 7, withItem: true,
			want: nil,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			session := lootSession{sent: make(chan *modv1.SendPacketRequest, 8)}
			s := New(session)
			if _, err := s.SetAutoLoot(t.Context(), &modulev1.AutoLootRequest{Enabled: test.autoLoot}); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			s.Start(ctx)
			if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
				Opcode: opcode.SMSGLootResponse, Payload: lootWindow(99, test.gold, test.withItem),
			}); err != nil {
				t.Fatal(err)
			}
			for _, want := range test.want {
				select {
				case got := <-session.sent:
					if got.GetOpcode() != want {
						t.Fatalf("opcode = 0x%x, want 0x%x", got.GetOpcode(), want)
					}
				case <-time.After(2 * time.Second):
					t.Fatalf("timed out waiting for opcode 0x%x", want)
				}
			}
		})
	}
}

func lootWindowWithTypes(types ...wow.LootSlotType) []byte {
	body := binary.LittleEndian.AppendUint64(nil, 99)
	body = append(body, wow.LootTypeCorpse)
	body = binary.LittleEndian.AppendUint32(body, 7)
	body = append(body, byte(len(types)))
	for slot, slotType := range types {
		body = append(body, byte(slot))
		body = binary.LittleEndian.AppendUint32(body, uint32(25+slot))
		body = binary.LittleEndian.AppendUint32(body, 1)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = binary.LittleEndian.AppendUint32(body, 0)
		body = append(body, byte(slotType))
	}
	return body
}

func TestGroupRollMasterAndLockedRowsAreNotAutoTaken(t *testing.T) {
	session := lootSession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLootResponse,
		Payload: lootWindowWithTypes(
			wow.LootSlotAllowLoot, wow.LootSlotRollOngoing,
			wow.LootSlotMaster, wow.LootSlotOwner, wow.LootSlotLocked,
		),
	}); err != nil {
		t.Fatal(err)
	}
	wantOps := []uint32{
		opcode.CMSGAutostoreLootItem, opcode.CMSGAutostoreLootItem,
		opcode.CMSGLootMoney, opcode.CMSGLootRelease,
	}
	wantSlots := []byte{0, 3}
	for i, want := range wantOps {
		select {
		case got := <-session.sent:
			if got.GetOpcode() != want {
				t.Fatalf("opcode = 0x%x, want 0x%x", got.GetOpcode(), want)
			}
			if i < len(wantSlots) && got.GetPayload()[0] != wantSlots[i] {
				t.Fatalf("took slot %d, want %d", got.GetPayload()[0], wantSlots[i])
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("timed out waiting for opcode 0x%x", want)
		}
	}
}

func TestDisabledWindowSupersedesPendingAutomaticWindow(t *testing.T) {
	session := lootSession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	s.pending = true // an older automatic job is still running
	s.currentGUID = 98
	if _, err := s.SetAutoLoot(t.Context(), &modulev1.AutoLootRequest{Enabled: false}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLootResponse, Payload: lootWindowWithTypes(wow.LootSlotOwner),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Take(t.Context(), &modulev1.TakeRequest{
		Guid: 99, Slots: []uint32{0}, TakeMoney: true,
	}); err != nil {
		t.Fatalf("new manual window inherited the old pending state: %v", err)
	}
}

func TestManualTakeUsesSelectedSlot(t *testing.T) {
	session := lootSession{sent: make(chan *modv1.SendPacketRequest, 8)}
	s := New(session)
	if _, err := s.SetAutoLoot(t.Context(), &modulev1.AutoLootRequest{Enabled: false}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLootResponse, Payload: lootWindowWithTypes(wow.LootSlotOwner),
	}); err != nil {
		t.Fatal(err)
	}
	state, err := s.GetState(t.Context(), &emptypb.Empty{})
	if err != nil || state.GetCurrentGuid() != 99 || state.GetLastItems()[0].GetSlotType() != uint32(wow.LootSlotOwner) {
		t.Fatalf("current loot facts missing: %+v, %v", state, err)
	}
	if _, err := s.Take(t.Context(), &modulev1.TakeRequest{Guid: 99, Slots: []uint32{0}, TakeMoney: true}); err != nil {
		t.Fatal(err)
	}
	for _, want := range []uint32{opcode.CMSGAutostoreLootItem, opcode.CMSGLootMoney, opcode.CMSGLootRelease} {
		select {
		case got := <-session.sent:
			if got.GetOpcode() != want {
				t.Fatalf("opcode = 0x%x, want 0x%x", got.GetOpcode(), want)
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("timed out waiting for opcode 0x%x", want)
		}
	}
}
