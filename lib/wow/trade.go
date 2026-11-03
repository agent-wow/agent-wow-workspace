package wow

import "encoding/binary"

const (
	TradeStatusCancelled uint32 = 3
	TradeStatusAccepted  uint32 = 4
	TradeStatusBegin     uint32 = 1
	TradeStatusOpen      uint32 = 2
	TradeStatusComplete  uint32 = 8
)

func EncodeInitiateTrade(target GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(target))
}

func EncodeSetTradeGold(copper uint32) []byte {
	return binary.LittleEndian.AppendUint32(nil, copper)
}

func EncodeSetTradeItem(tradeSlot, bag, slot uint8) []byte {
	return []byte{tradeSlot, bag, slot}
}

func EncodeClearTradeItem(tradeSlot uint8) []byte { return []byte{tradeSlot} }

type TradeStatus struct {
	Status         uint32
	TraderGUID     GUID
	TradeID        uint32
	Result         uint32
	IsTargetResult bool
	LimitedItemID  uint32
	Slot           uint8
}

func ParseTradeStatus(body []byte) (TradeStatus, error) {
	r := &reader{b: body}
	var out TradeStatus
	var err error
	if out.Status, err = r.u32(); err != nil {
		return out, err
	}
	switch out.Status {
	case TradeStatusBegin:
		guid, err := r.u64()
		out.TraderGUID = GUID(guid)
		return out, err
	case TradeStatusOpen:
		out.TradeID, err = r.u32()
		return out, err
	case 12: // TRADE_STATUS_CLOSE_WINDOW
		if out.Result, err = r.u32(); err != nil {
			return out, err
		}
		target, err := r.u8()
		if err != nil {
			return out, err
		}
		out.IsTargetResult = target != 0
		out.LimitedItemID, err = r.u32()
		return out, err
	case 22, 23: // WRONG_REALM, NOT_ON_TAPLIST
		out.Slot, err = r.u8()
		return out, err
	default:
		return out, nil
	}
}

type TradeItem struct {
	Slot           uint8
	ItemID         uint32
	DisplayInfoID  uint32
	Count          uint32
	Wrapped        bool
	GiftCreator    GUID
	Enchantments   [4]uint32
	Creator        GUID
	SpellCharges   uint32
	SuffixFactor   uint32
	RandomProperty int32
	LockID         uint32
	MaxDurability  uint32
	Durability     uint32
}

type TradeExtended struct {
	TraderData  bool
	TradeID     uint32
	SlotCount   uint32
	CurrentSlot uint32
	Money       uint32
	SpellID     uint32
	Items       []TradeItem
}

// ParseTradeExtended decodes the fixed seven-slot, 532-byte 3.3.5a view. The
// packet has no item GUID; inventory fields must reconfirm identity by bag/slot.
func ParseTradeExtended(body []byte) (TradeExtended, error) {
	r := &reader{b: body}
	var out TradeExtended
	trader, err := r.u8()
	if err != nil {
		return out, err
	}
	out.TraderData = trader != 0
	fields := []*uint32{&out.TradeID, &out.SlotCount, &out.CurrentSlot, &out.Money, &out.SpellID}
	for _, field := range fields {
		if *field, err = r.u32(); err != nil {
			return out, err
		}
	}
	out.Items = make([]TradeItem, 0, 7)
	for i := 0; i < 7; i++ {
		var item TradeItem
		if item.Slot, err = r.u8(); err != nil {
			return out, err
		}
		if item.ItemID, err = r.u32(); err != nil {
			return out, err
		}
		if item.ItemID == 0 {
			if _, err = r.take(68); err != nil {
				return out, err
			}
			continue
		}
		fields := []*uint32{&item.DisplayInfoID, &item.Count}
		for _, field := range fields {
			if *field, err = r.u32(); err != nil {
				return out, err
			}
		}
		wrapped, err := r.u32()
		if err != nil {
			return out, err
		}
		item.Wrapped = wrapped != 0
		guid, err := r.u64()
		if err != nil {
			return out, err
		}
		item.GiftCreator = GUID(guid)
		for j := range item.Enchantments {
			if item.Enchantments[j], err = r.u32(); err != nil {
				return out, err
			}
		}
		guid, err = r.u64()
		if err != nil {
			return out, err
		}
		item.Creator = GUID(guid)
		if item.SpellCharges, err = r.u32(); err != nil {
			return out, err
		}
		if item.SuffixFactor, err = r.u32(); err != nil {
			return out, err
		}
		random, err := r.u32()
		if err != nil {
			return out, err
		}
		item.RandomProperty = int32(random)
		for _, field := range []*uint32{&item.LockID, &item.MaxDurability, &item.Durability} {
			if *field, err = r.u32(); err != nil {
				return out, err
			}
		}
		out.Items = append(out.Items, item)
	}
	return out, nil
}
