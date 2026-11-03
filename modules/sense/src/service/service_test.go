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
	// Cold-start identity gates read these at the snapshot root.
	if self.Guid != 42 || self.Name != "Reachbot" || self.Unit.GetGuid() != 42 {
		t.Fatalf("identity = %d/%q unit=%d", self.Guid, self.Name, self.Unit.GetGuid())
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

func loginPayload(mapID uint32, x, y, z, o float32) []byte {
	body := binary.LittleEndian.AppendUint32(nil, mapID)
	body = f32(body, x)
	body = f32(body, y)
	body = f32(body, z)
	body = f32(body, o)
	return body
}

func sendPacket(t *testing.T, s *Service, op uint32, body []byte) {
	t.Helper()
	if _, err := s.OnPacket(context.Background(), &modv1.WorldPacket{
		Opcode: op, Payload: body,
	}); err != nil {
		t.Fatal(err)
	}
}

func TestPrimaryPowerUsesClassSlot(t *testing.T) {
	cases := []struct {
		name       string
		classID    uint32
		powerType  uint32
		powerIndex uint32
		power      uint32
		maxPower   uint32
	}{
		{name: "warrior rage", classID: 1, powerType: wow.PowerRage, powerIndex: wow.UnitFieldPower2, power: 35, maxPower: 100},
		{name: "priest mana", classID: 5, powerType: wow.PowerMana, powerIndex: wow.UnitFieldPower1, power: 480, maxPower: 500},
		{name: "rogue energy", classID: 4, powerType: wow.PowerEnergy, powerIndex: wow.UnitFieldPower4, power: 80, maxPower: 100},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
			s := New(nil)
			sendPacket(t, s, opcode.SMSGLoginVerifyWorld, loginPayload(1, 1, 2, 3, 0))

			// The primary slot is POWER1 + power_type, not always POWER1: a
			// warrior's rage is POWER2 and a rogue's energy is POWER4.
			bytes0 := (tc.powerType << 24) | (tc.classID << 8) | 1
			sendPacket(t, s, opcode.SMSGUpdateObject, valuesUpdate(42, map[uint32]uint32{
				wow.UnitFieldBytes0:    bytes0,
				wow.UnitFieldHealth:    100,
				wow.UnitFieldMaxHealth: 100,
				wow.UnitFieldLevel:     10,
				tc.powerIndex:          tc.power,
				tc.powerIndex + 8:      tc.maxPower, // MAXPOWER1 is eight slots after POWER1
			}))

			self, err := s.GetSelf(context.Background(), &emptypb.Empty{})
			if err != nil {
				t.Fatal(err)
			}
			if self.Unit.PowerType != tc.powerType {
				t.Fatalf("power type = %d, want %d", self.Unit.PowerType, tc.powerType)
			}
			if self.Unit.Power != tc.power || self.Unit.MaxPower != tc.maxPower {
				t.Fatalf("power = %d/%d, want %d/%d", self.Unit.Power, self.Unit.MaxPower, tc.power, tc.maxPower)
			}
			if !self.Ready {
				t.Fatal("snapshot with map, health, level and primary power must be ready")
			}
		})
	}
}

func TestReadyGateWaitsForSelfFields(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)

	// Before world entry nothing is ready, even if update packets arrive.
	sendPacket(t, s, opcode.SMSGUpdateObject, valuesUpdate(42, map[uint32]uint32{
		wow.UnitFieldHealth: 100,
	}))
	self, err := s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.Ready {
		t.Fatal("ready before the map was known")
	}

	sendPacket(t, s, opcode.SMSGLoginVerifyWorld, loginPayload(1, 10, 20, 30, 0))
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.Ready {
		t.Fatal("ready immediately after SMSG_LOGIN_VERIFY_WORLD without self fields")
	}
	if !self.InWorld || !self.HasMap || self.Epoch != 1 || self.MapChanges != 0 {
		t.Fatalf("world entry state = %+v", self)
	}

	// Health and level without bytes0/power cannot drive a class worker.
	sendPacket(t, s, opcode.SMSGUpdateObject, valuesUpdate(42, map[uint32]uint32{
		wow.UnitFieldHealth:    100,
		wow.UnitFieldMaxHealth: 100,
		wow.UnitFieldLevel:     10,
	}))
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.Ready {
		t.Fatal("ready without class/power fields")
	}

	sendPacket(t, s, opcode.SMSGUpdateObject, valuesUpdate(42, map[uint32]uint32{
		wow.UnitFieldBytes0:    1 << 24, // warrior, rage
		wow.UnitFieldPower2:    0,       // empty rage is still a real low-resource fact
		wow.UnitFieldMaxPower2: 100,
	}))
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if !self.Ready {
		t.Fatal("ready after all fields arrived across incremental updates")
	}
	if self.Unit.Power != 0 || self.Unit.MaxPower != 100 {
		t.Fatalf("low-resource power = %d/%d", self.Unit.Power, self.Unit.MaxPower)
	}
}

func TestNewWorldInvalidatesSnapshot(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	sendPacket(t, s, opcode.SMSGLoginVerifyWorld, loginPayload(1, 10, 20, 30, 0))
	sendPacket(t, s, opcode.SMSGUpdateObject, valuesUpdate(42, map[uint32]uint32{
		wow.UnitFieldHealth: 100, wow.UnitFieldMaxHealth: 100,
		wow.UnitFieldLevel: 10, wow.UnitFieldBytes0: 5 << 8,
		wow.UnitFieldPower1: 500, wow.UnitFieldMaxPower1: 500,
	}))
	// New(nil) has no session; selection is exercised through the state
	// fields instead of the RPC in this test.
	s.mu.Lock()
	s.target = 99
	s.units[99] = &unit{guid: 99, typ: wow.ObjectTypeUnit, hasPos: true, pos: wow.Position{X: 1, Y: 2}}
	s.mu.Unlock()

	sendPacket(t, s, opcode.SMSGNewWorld, loginPayload(0, 100, 200, 300, math.Pi/2))

	self, err := s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.Epoch != 2 || self.MapChanges != 1 {
		t.Fatalf("epoch/map changes = %d/%d, want 2/1", self.Epoch, self.MapChanges)
	}
	if !self.HasMap || self.MapId != 0 {
		t.Fatalf("map = %v/%d, want known map 0", self.HasMap, self.MapId)
	}
	if self.Ready {
		t.Fatal("map transition must clear readiness until fresh self fields arrive")
	}
	if !self.InWorld || self.Unit.GetPosition().GetX() != 100 || self.Unit.Orientation != float32(math.Pi/2) {
		t.Fatalf("position after transition = %+v", self.Unit)
	}
	if self.Unit.Health != 0 || self.Unit.Power != 0 {
		t.Fatal("map transition leaked fields from the old map")
	}
	target, err := s.GetTarget(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if target.HasTarget || target.TargetGuid != 0 {
		t.Fatalf("target survived map transition: %+v", target)
	}
	nearby, err := s.GetNearby(context.Background(), &modulev1.NearbyRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearby.Units) != 0 || nearby.KnownUnits != 0 {
		t.Fatalf("old-map units survived transition: %+v", nearby)
	}
}

func TestGameObjectsInNearbyAndTarget(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	sendPacket(t, s, opcode.SMSGLoginVerifyWorld, loginPayload(1, 0, 0, 0, 0))

	goGUID := uint64(0xF110000000001234)
	body := binary.LittleEndian.AppendUint32(nil, 1)
	body = append(body, wow.UpdateCreate)
	body = packedGUID(body, goGUID)
	body = append(body, byte(wow.ObjectTypeGameObject))
	body = binary.LittleEndian.AppendUint16(body, wow.UpdateFlagStationaryPos)
	body = f32(body, 5)
	body = f32(body, 6)
	body = f32(body, 7)
	body = f32(body, 0)
	fields := map[uint32]uint32{
		wow.ObjectFieldEntry:    1617, // Peacebloom
		wow.GameObjectDisplayID: 261,
		wow.GameObjectFlags:     0,
		wow.GameObjectFaction:   0,
		wow.GameObjectLevel:     1,
		wow.GameObjectBytes1:    2<<8 | 3, // type 2, state 3
	}
	var maxIndex uint32
	for index := range fields {
		if index > maxIndex {
			maxIndex = index
		}
	}
	blocks := int(maxIndex/32) + 1
	masks := make([]uint32, blocks)
	for index := range fields {
		masks[index/32] |= 1 << (index % 32)
	}
	body = append(body, byte(blocks))
	for _, mask := range masks {
		body = binary.LittleEndian.AppendUint32(body, mask)
	}
	for block, mask := range masks {
		for bit := 0; bit < 32; bit++ {
			if mask&(1<<bit) != 0 {
				body = binary.LittleEndian.AppendUint32(body, fields[uint32(block*32+bit)])
			}
		}
	}
	sendPacket(t, s, opcode.SMSGUpdateObject, body)

	unitsOnly, err := s.GetNearby(context.Background(), &modulev1.NearbyRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(unitsOnly.GameObjects) != 0 || unitsOnly.KnownGameObjects != 1 {
		t.Fatalf("game object leaked into unit-only nearby: %+v", unitsOnly)
	}
	withObjects, err := s.GetNearby(context.Background(), &modulev1.NearbyRequest{IncludeGameObjects: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(withObjects.GameObjects) != 1 {
		t.Fatalf("game objects = %d", len(withObjects.GameObjects))
	}
	goObj := withObjects.GameObjects[0]
	if goObj.Guid != goGUID || goObj.Entry != 1617 || goObj.TypeId != 2 ||
		goObj.State != 3 || goObj.DisplayId != 261 || goObj.Position.GetX() != 5 {
		t.Fatalf("game object = %+v", goObj)
	}

	s.mu.Lock()
	s.target = wow.GUID(goGUID)
	s.mu.Unlock()
	target, err := s.GetTarget(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if !target.HasTarget || target.Unit != nil || target.GameObject.GetEntry() != 1617 {
		t.Fatalf("game object target = %+v", target)
	}
}

func TestAuraCooldownAndComboFacts(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	sendPacket(t, s, opcode.SMSGLoginVerifyWorld, loginPayload(1, 10, 20, 30, 0))

	// Aura before the first self update must not erase the world-entry position.
	aura := packedGUID(nil, 42)
	aura = append(aura, 1)
	aura = binary.LittleEndian.AppendUint32(aura, 1234)
	aura = append(aura, wow.AuraFlagPositive|wow.AuraFlagDuration, 60, 1)
	aura = packedGUID(aura, 42)
	aura = binary.LittleEndian.AppendUint32(aura, 30000)
	aura = binary.LittleEndian.AppendUint32(aura, 15000)
	sendPacket(t, s, opcode.SMSGAuraUpdate, aura)

	self, err := s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.Unit.GetPosition().GetX() != 10 {
		t.Fatalf("aura packet erased position: %+v", self.Unit)
	}
	if len(self.Auras) != 1 || self.Auras[0].SpellId != 1234 || !self.Auras[0].Positive {
		t.Fatalf("auras = %+v", self.Auras)
	}

	remove := packedGUID(nil, 42)
	remove = append(remove, 1)
	remove = binary.LittleEndian.AppendUint32(remove, 0)
	sendPacket(t, s, opcode.SMSGAuraUpdate, remove)
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(self.Auras) != 0 {
		t.Fatalf("removed aura still present: %+v", self.Auras)
	}

	cd := binary.LittleEndian.AppendUint64(nil, 42)
	cd = append(cd, 0)
	cd = binary.LittleEndian.AppendUint32(cd, 78)
	cd = binary.LittleEndian.AppendUint32(cd, 1500)
	cd = binary.LittleEndian.AppendUint32(cd, 100)
	cd = binary.LittleEndian.AppendUint32(cd, 1_000_000)
	sendPacket(t, s, opcode.SMSGSpellCooldown, cd)
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(self.Cooldowns) != 2 || self.Cooldowns[0].SpellId != 78 {
		t.Fatalf("cooldowns = %+v", self.Cooldowns)
	}
	if self.Cooldowns[0].RemainingMs == 0 || self.Cooldowns[0].RemainingMs > 1500 {
		t.Fatalf("remaining cooldown = %d", self.Cooldowns[0].RemainingMs)
	}

	// A zero duration removes the cooldown instead of reporting it as expired.
	cd = binary.LittleEndian.AppendUint64(nil, 42)
	cd = append(cd, 0)
	cd = binary.LittleEndian.AppendUint32(cd, 78)
	cd = binary.LittleEndian.AppendUint32(cd, 0)
	sendPacket(t, s, opcode.SMSGSpellCooldown, cd)
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(self.Cooldowns) != 1 || self.Cooldowns[0].SpellId != 100 {
		t.Fatalf("cooldowns after removal = %+v", self.Cooldowns)
	}

	combo := packedGUID(nil, 99)
	combo = append(combo, 5)
	sendPacket(t, s, opcode.SMSGUpdateComboPoints, combo)
	self, err = s.GetSelf(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if self.ComboPoints != 5 || self.ComboTargetGuid != 99 {
		t.Fatalf("combo = %d@%d", self.ComboPoints, self.ComboTargetGuid)
	}

	status, err := s.Status(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if status.AuraUpdates != 2 || status.CooldownPackets != 2 {
		t.Fatalf("status counters = %+v", status)
	}
}

func TestDestroySelectedTargetClearsIt(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	s := New(nil)
	s.mu.Lock()
	s.target = 99
	s.units[99] = &unit{guid: 99, typ: wow.ObjectTypeUnit, hasPos: true}
	s.mu.Unlock()

	sendPacket(t, s, opcode.SMSGDestroyObject, binary.LittleEndian.AppendUint64(nil, 99))
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.target != 0 {
		t.Fatalf("destroyed target remained selected: %d", s.target)
	}
	if _, ok := s.units[99]; ok {
		t.Fatal("destroyed unit remained tracked")
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
