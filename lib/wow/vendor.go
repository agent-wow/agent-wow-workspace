package wow

import (
	"encoding/binary"
	"fmt"
)

// VendorItem is one row from SMSG_LIST_INVENTORY. Slot is one-based.
type VendorItem struct {
	Slot          uint32
	ItemID        uint32
	DisplayID     uint32
	Stock         int32
	Price         uint32
	MaxDurability uint32
	BuyCount      uint32
	ExtendedCost  uint32
}

// VendorList is the server's current inventory for one vendor.
type VendorList struct {
	Vendor GUID
	Items  []VendorItem
}

func EncodeListInventory(vendor GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(vendor))
}

func EncodeBuyItem(vendor GUID, itemID, slot, count uint32) []byte {
	body := binary.LittleEndian.AppendUint64(nil, uint64(vendor))
	body = binary.LittleEndian.AppendUint32(body, itemID)
	body = binary.LittleEndian.AppendUint32(body, slot)
	body = binary.LittleEndian.AppendUint32(body, count)
	return append(body, 0)
}

func EncodeSellItem(vendor, item GUID, count uint32) []byte {
	body := binary.LittleEndian.AppendUint64(nil, uint64(vendor))
	body = binary.LittleEndian.AppendUint64(body, uint64(item))
	return binary.LittleEndian.AppendUint32(body, count)
}

func EncodeRepairItem(vendor, item GUID, useGuildBank bool) []byte {
	body := binary.LittleEndian.AppendUint64(nil, uint64(vendor))
	body = binary.LittleEndian.AppendUint64(body, uint64(item))
	var guildBank byte
	if useGuildBank {
		guildBank = 1
	}
	return append(body, guildBank)
}

func EncodeAutoequipItemSlot(item GUID, destinationSlot uint8) []byte {
	body := binary.LittleEndian.AppendUint64(nil, uint64(item))
	return append(body, destinationSlot)
}

func EncodeBuybackItem(vendor GUID, slot uint8) []byte {
	body := binary.LittleEndian.AppendUint64(nil, uint64(vendor))
	return binary.LittleEndian.AppendUint32(body, uint32(slot))
}

func ParseListInventory(body []byte) (VendorList, error) {
	r := &reader{b: body}
	vendor, err := r.u64()
	if err != nil {
		return VendorList{}, err
	}
	count, err := r.u8()
	if err != nil {
		return VendorList{}, err
	}
	if len(body)-r.off != int(count)*32 {
		return VendorList{}, fmt.Errorf("vendor list: got %d bytes for %d rows", len(body)-r.off, count)
	}
	out := VendorList{Vendor: GUID(vendor), Items: make([]VendorItem, 0, count)}
	for i := byte(0); i < count; i++ {
		values := make([]uint32, 8)
		for j := range values {
			if values[j], err = r.u32(); err != nil {
				return VendorList{}, err
			}
		}
		out.Items = append(out.Items, VendorItem{
			Slot: values[0], ItemID: values[1], DisplayID: values[2],
			Stock: int32(values[3]), Price: values[4], MaxDurability: values[5],
			BuyCount: values[6], ExtendedCost: values[7],
		})
	}
	return out, nil
}

// ActionFailure is the common vendor/equipment failure payload.
type ActionFailure struct {
	Vendor        GUID
	ItemID        uint32
	Item          GUID
	Item1         GUID
	Item2         GUID
	Result        uint8
	BagType       uint8
	RequiredLevel uint32
}

func ParseBuyFailed(body []byte) (ActionFailure, error) {
	if len(body) != 17 {
		return ActionFailure{}, fmt.Errorf("buy failure: got %d bytes, want 17", len(body))
	}
	return ActionFailure{
		Vendor: GUID(binary.LittleEndian.Uint64(body)),
		ItemID: binary.LittleEndian.Uint32(body[8:12]),
		Result: body[16],
	}, nil
}

func ParseSellItem(body []byte) (ActionFailure, error) {
	if len(body) != 17 && len(body) != 21 {
		return ActionFailure{}, fmt.Errorf("sell reply: got %d bytes, want 17 or 21", len(body))
	}
	return ActionFailure{
		Vendor: GUID(binary.LittleEndian.Uint64(body)),
		Item:   GUID(binary.LittleEndian.Uint64(body[8:16])),
		Result: body[len(body)-1],
	}, nil
}

func ParseInventoryChangeFailure(body []byte) (ActionFailure, error) {
	if len(body) == 1 {
		return ActionFailure{Result: body[0]}, nil
	}
	if len(body) < 18 {
		return ActionFailure{}, fmt.Errorf("inventory change failure: got %d bytes", len(body))
	}
	out := ActionFailure{
		Result:  body[0],
		Item1:   GUID(binary.LittleEndian.Uint64(body[1:9])),
		Item2:   GUID(binary.LittleEndian.Uint64(body[9:17])),
		BagType: body[17],
	}
	// EQUIP_ERR_LEVEL and related failures append the required level.
	if len(body) >= 22 {
		out.RequiredLevel = binary.LittleEndian.Uint32(body[18:22])
	}
	return out, nil
}
