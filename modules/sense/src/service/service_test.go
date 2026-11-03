package service

import (
	"context"
	"encoding/binary"
	"errors"
	"math"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/sense/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

func f32(b []byte, v float32) []byte {
	return binary.LittleEndian.AppendUint32(b, math.Float32bits(v))
}

func packedGUID(b []byte, guid uint64) []byte {
	var mask byte
	for i := 0; i < 8; i++ {
		if byte(guid>>(8*i)) != 0 {
			mask |= 1 << i
		}
	}
	b = append(b, mask)
	for i := 0; i < 8; i++ {
		if v := byte(guid >> (8 * i)); v != 0 {
			b = append(b, v)
		}
	}
	return b
}

func TestLoginVerifyAndNearby(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	t.Setenv("AGENT_WOW_CHARACTER_NAME", "Reachbot")
	s := New(nil)

	login := binary.LittleEndian.AppendUint32(nil, 1)
	login = f32(login, 10)
	login = f32(login, 20)
	login = f32(login, 30)
	login = f32(login, 1)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: login,
	}); err != nil {
		t.Fatal(err)
	}

	self, err := s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if !self.InWorld || self.MapId != 1 || self.Unit.GetPosition().GetX() != 10 {
		t.Fatalf("self = %+v", self)
	}

	// A nearby creature create block with position and health.
	guid := uint64(0xF130000000002A00)
	var body []byte
	body = binary.LittleEndian.AppendUint32(body, 1)
	body = append(body, wow.UpdateCreate)
	body = packedGUID(body, guid)
	body = append(body, byte(wow.ObjectTypeUnit))
	body = binary.LittleEndian.AppendUint16(body, wow.UpdateFlagLiving)
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint16(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 1)
	body = f32(body, 13)
	body = f32(body, 24)
	body = f32(body, 30)
	body = f32(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 0) // fall time
	for i := 0; i < 9; i++ {
		body = f32(body, 1)
	}
	body = append(body, 1) // one mask word
	body = binary.LittleEndian.AppendUint32(body, 1<<24)
	body = binary.LittleEndian.AppendUint32(body, 77) // UNIT_FIELD_HEALTH
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}

	nearby, err := s.GetNearby(context.Background(), &modulev1.NearbyRequest{Radius: 5})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearby.Units) != 1 {
		t.Fatalf("nearby = %d units", len(nearby.Units))
	}
	if got := nearby.Units[0]; got.Guid != guid || got.Health != 77 {
		t.Fatalf("unit = %+v", got)
	}

	far, err := s.GetNearby(context.Background(), &modulev1.NearbyRequest{Radius: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(far.Units) != 0 {
		t.Fatalf("radius filter returned %d units", len(far.Units))
	}
}

type senseSession struct {
	modv1.SessionClient
	send func(context.Context, *modv1.SendPacketRequest) (*emptypb.Empty, error)
}

func (f senseSession) SendPacket(ctx context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	return f.send(ctx, req)
}

func valuesUpdate(guid uint64, values map[uint32]uint32) []byte {
	var maxIndex uint32
	for index := range values {
		if index > maxIndex {
			maxIndex = index
		}
	}
	blocks := int(maxIndex/32) + 1
	masks := make([]uint32, blocks)
	for index := range values {
		masks[index/32] |= 1 << (index % 32)
	}
	body := binary.LittleEndian.AppendUint32(nil, 1)
	body = append(body, wow.UpdateValues)
	body = packedGUID(body, guid)
	body = append(body, byte(blocks))
	for _, mask := range masks {
		body = binary.LittleEndian.AppendUint32(body, mask)
	}
	for block, mask := range masks {
		for bit := 0; bit < 32; bit++ {
			if mask&(1<<bit) != 0 {
				body = binary.LittleEndian.AppendUint32(body, values[uint32(block*32+bit)])
			}
		}
	}
	return body
}

func TestFailedQueriesCanRetry(t *testing.T) {
	session := senseSession{send: func(context.Context, *modv1.SendPacketRequest) (*emptypb.Empty, error) {
		return nil, errors.New("session offline")
	}}
	s := New(session)
	s.nameQueried[99] = struct{}{}
	s.creatureQueried[123] = struct{}{}
	s.names <- 99
	s.creatures <- creatureQuery{entry: 123, guid: 100}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	s.Start(ctx)

	deadline := time.Now().Add(2 * time.Second)
	for {
		s.mu.RLock()
		_, namePending := s.nameQueried[99]
		_, creaturePending := s.creatureQueried[123]
		s.mu.RUnlock()
		if !namePending && !creaturePending {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("failed queries remained marked as sent")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestDroppedQueriesCanRetry(t *testing.T) {
	s := New(nil)
	for i := 0; i < nameQueueSize; i++ {
		s.names <- wow.GUID(i + 1)
	}
	for i := 0; i < creatureQueueSize; i++ {
		s.creatures <- creatureQuery{entry: uint32(i + 1)}
	}
	playerGUID := uint64(99)
	creatureGUID := uint64(0xF130000000002A00)
	for _, body := range [][]byte{
		valuesUpdate(playerGUID, nil),
		valuesUpdate(creatureGUID, map[uint32]uint32{wow.ObjectFieldEntry: 123}),
	} {
		if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
			Opcode: opcode.SMSGUpdateObject, Payload: body,
		}); err != nil {
			t.Fatal(err)
		}
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	if _, asked := s.nameQueried[wow.GUID(playerGUID)]; asked {
		t.Fatal("dropped player query remained marked as sent")
	}
	if _, asked := s.creatureQueried[123]; asked {
		t.Fatal("dropped creature query remained marked as sent")
	}
}

func TestMalformedUpdateDoesNotFail(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: opcode.SMSGUpdateObject, Payload: []byte{1, 2, 3},
	}); err != nil {
		t.Fatal(err)
	}
	status, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if status.ParseErrors != 1 || status.Packets != 1 {
		t.Fatalf("status = %+v", status)
	}
}
