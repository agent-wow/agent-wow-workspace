package wow

import (
	"encoding/binary"
	"testing"
)

func TestVendorRequestLayouts(t *testing.T) {
	if got := EncodeListInventory(0x11); len(got) != 8 || binary.LittleEndian.Uint64(got) != 0x11 {
		t.Fatalf("bad list request: %x", got)
	}
	buy := EncodeBuyItem(1, 25, 3, 2)
	if len(buy) != 21 || binary.LittleEndian.Uint32(buy[12:16]) != 3 ||
		binary.LittleEndian.Uint32(buy[16:20]) != 2 {
		t.Fatalf("bad buy request: %x", buy)
	}
	sell := EncodeSellItem(1, 2, 0)
	if len(sell) != 20 || binary.LittleEndian.Uint64(sell[8:16]) != 2 {
		t.Fatalf("bad sell request: %x", sell)
	}
	repair := EncodeRepairItem(1, 0, false)
	if len(repair) != 17 || repair[16] != 0 {
		t.Fatalf("bad repair request: %x", repair)
	}
	equip := EncodeAutoequipItemSlot(99, 15)
	if len(equip) != 9 || binary.LittleEndian.Uint64(equip[:8]) != 99 || equip[8] != 15 {
		t.Fatalf("bad equip request: %x", equip)
	}
	buyback := EncodeBuybackItem(1, 74)
	if len(buyback) != 12 || binary.LittleEndian.Uint32(buyback[8:12]) != 74 {
		t.Fatalf("bad buyback request: %x", buyback)
	}
	ammo := EncodeSetAmmo(2512)
	if len(ammo) != 4 || binary.LittleEndian.Uint32(ammo) != 2512 {
		t.Fatalf("bad ammo request: %x", ammo)
	}
}

func TestParseVendorList(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 99)
	body = append(body, 1)
	for _, value := range []uint32{1, 25, 123, 0xffffffff, 50, 20, 2, 0} {
		body = binary.LittleEndian.AppendUint32(body, value)
	}
	list, err := ParseListInventory(body)
	if err != nil {
		t.Fatal(err)
	}
	if list.Vendor != 99 || len(list.Items) != 1 {
		t.Fatalf("unexpected list: %+v", list)
	}
	row := list.Items[0]
	if row.Slot != 1 || row.ItemID != 25 || row.Stock != -1 || row.Price != 50 || row.BuyCount != 2 {
		t.Fatalf("unexpected vendor row: %+v", row)
	}
}

func TestParseVendorAndEquipmentFailures(t *testing.T) {
	buy := binary.LittleEndian.AppendUint64(nil, 1)
	buy = binary.LittleEndian.AppendUint32(buy, 25)
	buy = binary.LittleEndian.AppendUint32(buy, 0)
	buy = append(buy, 4)
	failure, err := ParseBuyFailed(buy)
	if err != nil || failure.ItemID != 25 || failure.Result != 4 {
		t.Fatalf("unexpected buy failure: %+v, %v", failure, err)
	}
	sell := binary.LittleEndian.AppendUint64(nil, 1)
	sell = binary.LittleEndian.AppendUint64(sell, 2)
	sell = binary.LittleEndian.AppendUint32(sell, 0)
	sell = append(sell, 3)
	sellFailure, err := ParseSellItem(sell)
	if err != nil || sellFailure.Item != 2 || sellFailure.Result != 3 {
		t.Fatalf("unexpected sell failure: %+v, %v", sellFailure, err)
	}
	equip := []byte{12}
	equip = binary.LittleEndian.AppendUint64(equip, 42)
	equip = binary.LittleEndian.AppendUint64(equip, 0)
	equip = append(equip, 0)
	equip = binary.LittleEndian.AppendUint32(equip, 8)
	equipFailure, err := ParseInventoryChangeFailure(equip)
	if err != nil || equipFailure.Item1 != 42 || equipFailure.RequiredLevel != 8 {
		t.Fatalf("unexpected equip failure: %+v, %v", equipFailure, err)
	}
}
