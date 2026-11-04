package wow

import (
	"encoding/binary"
	"math"
	"testing"
)

func TestPlayerAndContainerInventorySlots(t *testing.T) {
	fields := UpdateFields{
		PlayerFieldInvSlotHead:     0x11,
		PlayerFieldInvSlotHead + 1: 0x22,
		PlayerFieldPackSlot1:       0x33,
		PlayerFieldPackSlot1 + 1:   0x44,
		PlayerFieldBuybackPrice1:   99,
	}
	slots := DecodePlayerSlots(fields)
	if len(slots) != 2 || slots[0].Slot != 0 || slots[0].GUID != 0x2200000011 {
		t.Fatalf("unexpected equipment slots: %+v", slots)
	}
	if slots[1].Slot != 23 || slots[1].GUID != 0x4400000033 {
		t.Fatalf("unexpected backpack slot: %+v", slots[1])
	}
	container := UpdateFields{
		ContainerFieldSlot1:     0x55,
		ContainerFieldSlot1 + 1: 0x66,
	}
	contents := DecodeContainerSlots(container)
	if len(contents) != 1 || contents[0] != 0x6600000055 {
		t.Fatalf("unexpected container contents: %+v", contents)
	}
}

func TestParseItemQueryResponse(t *testing.T) {
	body := binary.LittleEndian.AppendUint32(nil, 25) // entry
	for _, value := range []uint32{2, 7, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	body = append(body, "Stiletto\x00\x00\x00\x00"...)
	for _, value := range []uint32{
		123, 2, 0, 0, 1000, 250, 21, 1, 0xffffffff,
		15, 5, 44, 10, 0, 0, 0, 0, 0, 0, 4, 0, 2,
	} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	for _, stat := range []struct{ typ, value uint32 }{{4, 3}, {7, 2}} {
		body = binary.LittleEndian.AppendUint32(body, stat.typ)
		body = binary.LittleEndian.AppendUint32(body, stat.value)
	}
	body = binary.LittleEndian.AppendUint32(body, 0) // scaling distribution
	body = binary.LittleEndian.AppendUint32(body, 0) // scaling value
	for _, damage := range []struct {
		min, max float32
		typ      uint32
	}{{4, 8, 0}, {0, 0, 0}} {
		body = binary.LittleEndian.AppendUint32(body, math.Float32bits(damage.min))
		body = binary.LittleEndian.AppendUint32(body, math.Float32bits(damage.max))
		body = binary.LittleEndian.AppendUint32(body, damage.typ)
	}
	for _, resistance := range []uint32{0, 0, 0, 0, 0, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, resistance)
	}
	body = binary.LittleEndian.AppendUint32(body, 1500) // delay
	body = binary.LittleEndian.AppendUint32(body, 0)    // ammo
	body = binary.LittleEndian.AppendUint32(body, 0)    // range mod
	for i := 0; i < 5*6; i++ {
		body = binary.LittleEndian.AppendUint32(body, 0)
	}
	body = binary.LittleEndian.AppendUint32(body, 1) // bonding
	body = append(body, 0)                           // description
	for _, value := range []uint32{
		0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 55, 0, 0, 0, 0,
	} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	for i := 0; i < 3*2; i++ {
		body = binary.LittleEndian.AppendUint32(body, 0)
	}
	for _, value := range []uint32{0, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	body = binary.LittleEndian.AppendUint32(body, 0) // armor damage modifier
	for _, value := range []uint32{0, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	item, err := ParseItemQuerySingleResponse(body)
	if err != nil {
		t.Fatal(err)
	}
	if item.Name != "Stiletto" || item.InventoryType != 21 || item.RequiredSkill != 44 {
		t.Fatalf("unexpected item: %+v", item)
	}
	if len(item.Stats) != 2 || item.Stats[0].Name != "strength" || item.DPS != 4 {
		t.Fatalf("unexpected stats/dps: %+v %.2f", item.Stats, item.DPS)
	}
	if item.EquipmentSlot == nil || *item.EquipmentSlot != 15 {
		t.Fatalf("unexpected equipment slot: %+v", item.EquipmentSlot)
	}
}

func TestUnknownItemQuery(t *testing.T) {
	body := binary.LittleEndian.AppendUint32(nil, 0x80000019)
	item, err := ParseItemQuerySingleResponse(body)
	if err != nil || !item.Unknown || item.ItemID != 25 {
		t.Fatalf("unexpected unknown response: %+v, %v", item, err)
	}
}
