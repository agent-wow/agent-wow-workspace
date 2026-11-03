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
			name: "release without taking when disabled", autoLoot: false, gold: 7, withItem: true,
			want: []uint32{opcode.CMSGLootRelease},
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
