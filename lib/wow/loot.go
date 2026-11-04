package wow

import "encoding/binary"

// LootSlotType trails every item in SMSG_LOOT_RESPONSE and says whether the
// viewer may take it. Solo play reports LootSlotOwner; group loot reports
// LootSlotAllowLoot for items the viewer has won or is next in line for.
type LootSlotType uint8

const (
	LootSlotAllowLoot   LootSlotType = 0
	LootSlotRollOngoing LootSlotType = 1
	LootSlotMaster      LootSlotType = 2
	LootSlotLocked      LootSlotType = 3
	LootSlotOwner       LootSlotType = 4
	LootSlotNone        LootSlotType = 5
)

// Takeable reports whether the server will accept an autostore for a slot.
func (t LootSlotType) Takeable() bool {
	return t == LootSlotAllowLoot || t == LootSlotOwner || t == LootSlotMaster
}

// LootTypeNone marks SMSG_LOOT_RESPONSE as an error reply: the byte after it
// is the LootError code, not gold.
const LootTypeNone uint8 = 0

// LootTypeCorpse is a normal creature corpse.
const LootTypeCorpse uint8 = 1

// LootItem is one row of an open loot window. Slot is the byte the server
// expects back in CMSG_AUTOSTORE_LOOT_ITEM; it is an index into the loot's
// item array and is not contiguous when some rows are hidden.
type LootItem struct {
	Slot             uint8
	ItemID           uint32
	Count            uint32
	DisplayInfoID    uint32
	RandomSuffix     uint32
	RandomPropertyID uint32
	SlotType         LootSlotType
}

// LootResponse is a decoded SMSG_LOOT_RESPONSE.
type LootResponse struct {
	GUID     GUID
	LootType uint8
	// Error is the LootError code and only meaningful when LootType is 0.
	Error uint8
	Gold  uint32
	Items []LootItem
}

// EncodeLoot builds CMSG_LOOT: the raw 64-bit GUID of the creature to open.
func EncodeLoot(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// EncodeAutostoreLootItem builds CMSG_AUTOSTORE_LOOT_ITEM: one slot byte,
// copied from the response.
func EncodeAutostoreLootItem(slot uint8) []byte {
	return []byte{slot}
}

// EncodeLootRelease builds CMSG_LOOT_RELEASE: the raw 64-bit GUID. The server
// ignores it unless it matches the loot currently open, which is why the
// module remembers the GUID it opened.
func EncodeLootRelease(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// ParseLootResponse decodes SMSG_LOOT_RESPONSE. The 3.3.5a body
// (Player::SendLoot -> LootView) is:
//
//	uint64 source guid
//	uint8  loot type          (0 = error, 1 = corpse, ...)
//	uint8  error              (type 0 only)
//	uint32 gold               (otherwise)
//	uint8  item count
//	count x { uint8 slot, uint32 itemId, uint32 count, uint32 displayInfoId,
//	          uint32 randomSuffix, uint32 randomPropertyId, uint8 slotType }
//
// The GUID is written unpacked here, unlike most other 3.3.5a packets.
func ParseLootResponse(body []byte) (LootResponse, error) {
	r := &reader{b: body}
	guid, err := r.u64()
	if err != nil {
		return LootResponse{}, err
	}
	lootType, err := r.u8()
	if err != nil {
		return LootResponse{}, err
	}
	out := LootResponse{GUID: GUID(guid), LootType: lootType}
	if lootType == LootTypeNone {
		out.Error, err = r.u8()
		return out, err
	}
	if out.Gold, err = r.u32(); err != nil {
		return LootResponse{}, err
	}
	count, err := r.u8()
	if err != nil {
		return LootResponse{}, err
	}
	out.Items = make([]LootItem, 0, count)
	for i := 0; i < int(count); i++ {
		var item LootItem
		if item.Slot, err = r.u8(); err != nil {
			return LootResponse{}, err
		}
		if item.ItemID, err = r.u32(); err != nil {
			return LootResponse{}, err
		}
		if item.Count, err = r.u32(); err != nil {
			return LootResponse{}, err
		}
		if item.DisplayInfoID, err = r.u32(); err != nil {
			return LootResponse{}, err
		}
		if item.RandomSuffix, err = r.u32(); err != nil {
			return LootResponse{}, err
		}
		if item.RandomPropertyID, err = r.u32(); err != nil {
			return LootResponse{}, err
		}
		slotType, err := r.u8()
		if err != nil {
			return LootResponse{}, err
		}
		item.SlotType = LootSlotType(slotType)
		out.Items = append(out.Items, item)
	}
	return out, nil
}
