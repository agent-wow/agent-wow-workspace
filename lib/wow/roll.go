package wow

import "encoding/binary"

const (
	LootRollPass  uint8 = 0
	LootRollNeed  uint8 = 1
	LootRollGreed uint8 = 2
)

func EncodeLootRoll(itemGUID GUID, slot uint32, vote uint8) []byte {
	out := binary.LittleEndian.AppendUint64(nil, uint64(itemGUID))
	out = binary.LittleEndian.AppendUint32(out, slot)
	return append(out, vote)
}

type LootStartRoll struct {
	ItemGUID       GUID
	MapID          uint32
	Slot           uint32
	ItemID         uint32
	RandomSuffix   uint32
	RandomProperty uint32
	Count          uint32
	CountdownMS    uint32
	VoteMask       uint8
}

func ParseLootStartRoll(body []byte) (LootStartRoll, error) {
	r := &reader{b: body}
	var out LootStartRoll
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.ItemGUID = GUID(guid)
	fields := []*uint32{&out.MapID, &out.Slot, &out.ItemID, &out.RandomSuffix, &out.RandomProperty, &out.Count, &out.CountdownMS}
	for _, field := range fields {
		if *field, err = r.u32(); err != nil {
			return out, err
		}
	}
	out.VoteMask, err = r.u8()
	return out, err
}

type LootRoll struct {
	ItemGUID       GUID
	Slot           uint32
	PlayerGUID     GUID
	ItemID         uint32
	RandomSuffix   uint32
	RandomProperty uint32
	Number         uint8
	Vote           uint8
	AutoPass       bool
}

func ParseLootRoll(body []byte) (LootRoll, error) {
	r := &reader{b: body}
	var out LootRoll
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.ItemGUID = GUID(guid)
	if out.Slot, err = r.u32(); err != nil {
		return out, err
	}
	guid, err = r.u64()
	if err != nil {
		return out, err
	}
	out.PlayerGUID = GUID(guid)
	fields := []*uint32{&out.ItemID, &out.RandomSuffix, &out.RandomProperty}
	for _, field := range fields {
		if *field, err = r.u32(); err != nil {
			return out, err
		}
	}
	if out.Number, err = r.u8(); err != nil {
		return out, err
	}
	if out.Vote, err = r.u8(); err != nil {
		return out, err
	}
	autoPass, err := r.u8()
	out.AutoPass = autoPass != 0
	return out, err
}

type LootRollWon struct {
	ItemGUID       GUID
	Slot           uint32
	ItemID         uint32
	RandomSuffix   uint32
	RandomProperty uint32
	WinnerGUID     GUID
	Number         uint8
	Vote           uint8
}

func ParseLootRollWon(body []byte) (LootRollWon, error) {
	r := &reader{b: body}
	var out LootRollWon
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.ItemGUID = GUID(guid)
	fields := []*uint32{&out.Slot, &out.ItemID, &out.RandomSuffix, &out.RandomProperty}
	for _, field := range fields {
		if *field, err = r.u32(); err != nil {
			return out, err
		}
	}
	guid, err = r.u64()
	if err != nil {
		return out, err
	}
	out.WinnerGUID = GUID(guid)
	if out.Number, err = r.u8(); err != nil {
		return out, err
	}
	out.Vote, err = r.u8()
	return out, err
}

type LootAllPassed struct {
	ItemGUID       GUID
	Slot           uint32
	ItemID         uint32
	RandomProperty uint32
	RandomSuffix   uint32
}

func ParseLootAllPassed(body []byte) (LootAllPassed, error) {
	r := &reader{b: body}
	var out LootAllPassed
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.ItemGUID = GUID(guid)
	fields := []*uint32{&out.Slot, &out.ItemID, &out.RandomProperty, &out.RandomSuffix}
	for _, field := range fields {
		if *field, err = r.u32(); err != nil {
			return out, err
		}
	}
	return out, nil
}
