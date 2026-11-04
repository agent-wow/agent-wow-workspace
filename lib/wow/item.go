package wow

import (
	"encoding/binary"
	"fmt"
	"math"
)

// ItemStat is one server item-template modifier.
type ItemStat struct {
	Type  uint32
	Name  string
	Value int32
}

// ItemTemplate is the static server data exposed by SMSG_ITEM_QUERY_SINGLE_RESPONSE.
type ItemTemplate struct {
	ItemID            uint32
	ClassID           uint32
	Subclass          uint32
	Name              string
	Quality           uint32
	BuyPrice          int32
	SellPrice         uint32
	InventoryType     uint32
	AllowableClass    uint32
	RequiredLevel     uint32
	RequiredSkill     uint32
	RequiredSkillRank uint32
	ContainerSlots    uint32
	Stats             []ItemStat
	Armor             uint32
	DPS               float64
	Bonding           uint32
	StartQuest        uint32
	MaxDurability     uint32
	EquipmentSlot     *uint8
	Unknown           bool
}

// EquipmentSlotForInventoryType maps WotLK INVTYPE values to equipment slots.
// Weapons use their natural hand; the agent still compares two-handed items
// against both hands before choosing an upgrade.
func EquipmentSlotForInventoryType(inventoryType uint32) (uint8, bool) {
	var slot uint8
	switch inventoryType {
	case 1, 2, 3, 4, 5, 6, 7, 8, 9, 10:
		slot = uint8(inventoryType - 1)
	case 11:
		slot = 10
	case 12:
		slot = 12
	case 13, 17, 21:
		slot = 15
	case 14, 22, 23:
		slot = 16
	case 15, 25, 26, 28:
		slot = 17
	case 16:
		slot = 14
	case 19:
		slot = 18
	case 20:
		slot = 5
	default:
		return 0, false
	}
	return slot, true
}

// EncodeItemQuerySingle requests the static server template for an item entry.
func EncodeItemQuerySingle(itemID uint32) []byte {
	return binary.LittleEndian.AppendUint32(nil, itemID)
}

// ParseItemQuerySingleResponse parses the variable-length static item reply.
// It intentionally consumes the complete layout so a fixture cannot accidentally
// shift stats into damage or durability fields.
func ParseItemQuerySingleResponse(body []byte) (ItemTemplate, error) {
	r := &reader{b: body}
	entry, err := r.u32()
	if err != nil {
		return ItemTemplate{}, err
	}
	if entry&0x80000000 != 0 {
		return ItemTemplate{ItemID: entry & 0x7fffffff, Unknown: true}, nil
	}
	out := ItemTemplate{ItemID: entry}
	if out.ClassID, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	if out.Subclass, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	if _, err = r.u32(); err != nil { // sound override subclass
		return ItemTemplate{}, err
	}
	if out.Name, err = readItemCString(r); err != nil {
		return ItemTemplate{}, err
	}
	for i := 0; i < 3; i++ {
		if _, err = readItemCString(r); err != nil {
			return ItemTemplate{}, err
		}
	}
	if _, err = r.u32(); err != nil { // display id
		return ItemTemplate{}, err
	}
	if out.Quality, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	for i := 0; i < 2; i++ {
		if _, err = r.u32(); err != nil { // flags, flags2
			return ItemTemplate{}, err
		}
	}
	buyPrice, err := r.u32()
	if err != nil {
		return ItemTemplate{}, err
	}
	out.BuyPrice = int32(buyPrice)
	if out.SellPrice, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	fields := make([]uint32, 12)
	for i := range fields {
		if fields[i], err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	out.InventoryType = fields[0]
	out.AllowableClass = fields[1]
	out.RequiredLevel = fields[4]
	out.RequiredSkill = fields[5]
	out.RequiredSkillRank = fields[6]
	if _, err = r.u32(); err != nil { // max count
		return ItemTemplate{}, err
	}
	if _, err = r.u32(); err != nil { // stackable
		return ItemTemplate{}, err
	}
	if out.ContainerSlots, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	statsCount, err := r.u32()
	if err != nil {
		return ItemTemplate{}, err
	}
	if statsCount > 10 {
		return ItemTemplate{}, fmt.Errorf("item query: stats count %d exceeds 10", statsCount)
	}
	out.Stats = make([]ItemStat, 0, statsCount)
	for i := uint32(0); i < statsCount; i++ {
		typ, readErr := r.u32()
		if readErr != nil {
			return ItemTemplate{}, readErr
		}
		value, readErr := r.u32()
		if readErr != nil {
			return ItemTemplate{}, readErr
		}
		out.Stats = append(out.Stats, ItemStat{
			Type: typ, Name: ItemStatName(typ), Value: int32(value),
		})
	}
	for i := 0; i < 2; i++ {
		if _, err = r.u32(); err != nil { // scaling stat fields
			return ItemTemplate{}, err
		}
	}
	var totalMin, totalMax, delay float64
	for i := 0; i < 2; i++ {
		minBits, readErr := r.u32()
		if readErr != nil {
			return ItemTemplate{}, readErr
		}
		maxBits, readErr := r.u32()
		if readErr != nil {
			return ItemTemplate{}, readErr
		}
		if _, readErr = r.u32(); readErr != nil {
			return ItemTemplate{}, readErr
		}
		totalMin += float64(math.Float32frombits(minBits))
		totalMax += float64(math.Float32frombits(maxBits))
	}
	resistances := make([]uint32, 7)
	for i := range resistances {
		if resistances[i], err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	out.Armor = resistances[0]
	delayValue, err := r.u32()
	if err != nil {
		return ItemTemplate{}, err
	}
	delay = float64(delayValue)
	if _, err = r.u32(); err != nil { // ammo type
		return ItemTemplate{}, err
	}
	if _, err = r.u32(); err != nil { // ranged modification
		return ItemTemplate{}, err
	}
	for i := 0; i < 5*6; i++ {
		if _, err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	if out.Bonding, err = r.u32(); err != nil {
		return ItemTemplate{}, err
	}
	if _, err = readItemCString(r); err != nil { // description
		return ItemTemplate{}, err
	}
	trailing := make([]uint32, 16)
	for i := range trailing {
		if trailing[i], err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	out.StartQuest = trailing[3]
	out.MaxDurability = trailing[11]
	for i := 0; i < 3*2+3; i++ { // sockets, bonus, gem props, disenchant skill
		if _, err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	for i := 0; i < 4; i++ { // armor modifier, duration, limit category, holiday
		if _, err = r.u32(); err != nil {
			return ItemTemplate{}, err
		}
	}
	if r.off != len(r.b) {
		return ItemTemplate{}, fmt.Errorf("item query: %d trailing bytes", len(r.b)-r.off)
	}
	if delay > 0 {
		out.DPS = (totalMin + totalMax) / 2 / (delay / 1000)
	}
	if slot, ok := EquipmentSlotForInventoryType(out.InventoryType); ok {
		out.EquipmentSlot = &slot
	}
	return out, nil
}

func readItemCString(r *reader) (string, error) {
	start := r.off
	for {
		value, err := r.u8()
		if err != nil {
			return "", err
		}
		if value == 0 {
			return string(r.b[start : r.off-1]), nil
		}
	}
}

// ItemStatName maps ITEM_MOD_TYPE to the profile stat vocabulary.
func ItemStatName(typ uint32) string {
	names := map[uint32]string{
		0: "mana", 1: "health", 3: "agility", 4: "strength",
		5: "intellect", 6: "spirit", 7: "stamina", 12: "defense",
		13: "dodge", 14: "parry", 15: "block", 31: "hit",
		32: "crit", 35: "resilience", 36: "haste", 37: "expertise",
		38: "attack_power", 43: "mana_regen", 44: "armor_penetration",
		45: "spell_power", 46: "health_regen", 47: "spell_penetration",
		48: "block_value",
	}
	if name := names[typ]; name != "" {
		return name
	}
	return fmt.Sprintf("stat_%d", typ)
}
