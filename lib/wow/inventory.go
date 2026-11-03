package wow

// Item/container and player-inventory update fields (AzerothCore d80ce1d8).
const (
	ItemFieldOwner         uint32 = 0x0006
	ItemFieldContained     uint32 = 0x0008
	ItemFieldStackCount    uint32 = 0x000E
	ItemFieldFlags         uint32 = 0x0015
	ItemFieldDurability    uint32 = 0x003C
	ItemFieldMaxDurability uint32 = 0x003D

	ContainerFieldNumSlots uint32 = 0x0040
	ContainerFieldSlot1    uint32 = 0x0042

	PlayerFieldInvSlotHead        uint32 = 0x0144
	PlayerFieldPackSlot1          uint32 = 0x0172
	PlayerFieldVendorBuybackSlot1 uint32 = 0x01D8
	PlayerFieldBuybackPrice1      uint32 = 0x04B1

	InventorySlotBag0 uint8 = 255
	InventoryNullSlot uint8 = 255
)

// PlayerSlot is one logical player slot. Bag 255 is the wire representation of
// equipped/backpack/buyback positions; container contents use their bag's index.
type PlayerSlot struct {
	Bag  uint8
	Slot uint8
	GUID GUID
}

// DecodePlayerSlots returns equipment, backpack-bag, backpack and buyback GUIDs
// whose two update-field halves are both present and non-zero.
func DecodePlayerSlots(fields UpdateFields) []PlayerSlot {
	out := make([]PlayerSlot, 0, 63)
	appendRange := func(base uint32, first, count int) {
		for i := 0; i < count; i++ {
			guid, ok := fields.Uint64(base + uint32(i*2))
			if !ok || guid == 0 {
				continue
			}
			out = append(out, PlayerSlot{
				Bag: InventorySlotBag0, Slot: uint8(first + i), GUID: GUID(guid),
			})
		}
	}
	appendRange(PlayerFieldInvSlotHead, 0, 23)         // equipment + backpack bags
	appendRange(PlayerFieldPackSlot1, 23, 16)          // backpack
	appendRange(PlayerFieldVendorBuybackSlot1, 74, 12) // buyback
	return out
}

// DecodeContainerSlots maps each non-zero container slot to its item GUID.
func DecodeContainerSlots(fields UpdateFields) map[uint8]GUID {
	out := make(map[uint8]GUID)
	for slot := uint8(0); slot < 36; slot++ {
		guid, ok := fields.Uint64(ContainerFieldSlot1 + uint32(slot*2))
		if ok && guid != 0 {
			out[slot] = GUID(guid)
		}
	}
	return out
}

// BuybackPrice returns the copper price associated with absolute slot 74..85.
func BuybackPrice(fields UpdateFields, slot uint8) (uint32, bool) {
	if slot < 74 || slot >= 86 {
		return 0, false
	}
	return fields.Uint32(PlayerFieldBuybackPrice1 + uint32(slot-74))
}
