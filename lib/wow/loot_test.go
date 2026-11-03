package wow

import (
	"encoding/binary"
	"testing"
)

func TestParseLootResponseEmptyCorpse(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, uint64(0xF130000000001234))
	body = append(body, LootTypeCorpse, 0, 0, 0, 0, 0) // gold 0, no rows
	got, err := ParseLootResponse(body)
	if err != nil {
		t.Fatalf("ParseLootResponse: %v", err)
	}
	if got.GUID != 0xF130000000001234 || got.LootType != LootTypeCorpse || got.Gold != 0 || len(got.Items) != 0 {
		t.Fatalf("unexpected response: %+v", got)
	}
}

func TestParseLootResponseError(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, uint64(0xF130000000001234))
	body = append(body, LootTypeNone, 6)
	got, err := ParseLootResponse(body)
	if err != nil {
		t.Fatalf("ParseLootResponse: %v", err)
	}
	if got.LootType != LootTypeNone || got.Error != 6 {
		t.Fatalf("unexpected error response: %+v", got)
	}
}

func TestParseLootResponseItems(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, uint64(0xF13000000000ABCD))
	body = append(body, LootTypeCorpse)
	body = binary.LittleEndian.AppendUint32(body, 37) // 37 copper
	body = append(body, 2)                            // two rows

	// Row 0: slot 0, item 2589 (linen cloth) x3, display 1, no randoms, OWNER.
	body = append(body, 0)
	for _, v := range []uint32{2589, 3, 1, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, v)
	}
	body = append(body, uint8(LootSlotOwner))

	// Row 1: slot 2 (slot 1 was hidden), item 117 (tough jerky) x1, LOCKED.
	body = append(body, 2)
	for _, v := range []uint32{117, 1, 2, 0, 0} {
		body = binary.LittleEndian.AppendUint32(body, v)
	}
	body = append(body, uint8(LootSlotLocked))

	got, err := ParseLootResponse(body)
	if err != nil {
		t.Fatalf("ParseLootResponse: %v", err)
	}
	if got.Gold != 37 || len(got.Items) != 2 {
		t.Fatalf("unexpected header: %+v", got)
	}
	if got.Items[0].Slot != 0 || got.Items[0].ItemID != 2589 || got.Items[0].Count != 3 || !got.Items[0].SlotType.Takeable() {
		t.Fatalf("row 0: %+v", got.Items[0])
	}
	if got.Items[1].Slot != 2 || got.Items[1].ItemID != 117 || got.Items[1].SlotType.Takeable() {
		t.Fatalf("row 1: %+v", got.Items[1])
	}
}

func TestTakeable(t *testing.T) {
	for _, tt := range []struct {
		slot LootSlotType
		want bool
	}{
		{LootSlotAllowLoot, true},
		{LootSlotOwner, true},
		{LootSlotMaster, true},
		{LootSlotRollOngoing, false},
		{LootSlotLocked, false},
		{LootSlotNone, false},
	} {
		if got := tt.slot.Takeable(); got != tt.want {
			t.Errorf("%d.Takeable() = %v, want %v", tt.slot, got, tt.want)
		}
	}
}
