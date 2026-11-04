package wow

import (
	"encoding/binary"
	"fmt"
)

// Trainer spell wire states. These are server facts, not policy decisions.
const (
	TrainerSpellAvailable   uint8 = 0
	TrainerSpellUnavailable uint8 = 1
	TrainerSpellKnown       uint8 = 2
)

type TrainerSpell struct {
	SpellID      uint32
	Usable       uint8
	MoneyCost    uint32
	PointCost    [2]uint32
	ReqLevel     uint8
	ReqSkillLine uint32
	ReqSkillRank uint32
	ReqAbility   [3]uint32
}

type TrainerList struct {
	GUID        GUID
	TrainerType uint32
	Greeting    string
	Spells      []TrainerSpell
}

func EncodeTrainerList(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

func EncodeTrainerBuySpell(guid GUID, spellID uint32) []byte {
	out := binary.LittleEndian.AppendUint64(nil, uint64(guid))
	return binary.LittleEndian.AppendUint32(out, spellID)
}

// ParseTrainerList decodes AzerothCore's WorldPackets::NPC::TrainerList.
// Every spell row is 38 bytes, followed by one NUL-terminated greeting.
func ParseTrainerList(body []byte) (TrainerList, error) {
	r := &reader{b: body}
	guid, err := r.u64()
	if err != nil {
		return TrainerList{}, fmt.Errorf("trainer list: %w", err)
	}
	typ, err := r.u32()
	if err != nil {
		return TrainerList{}, fmt.Errorf("trainer list: %w", err)
	}
	count, err := r.u32()
	if err != nil {
		return TrainerList{}, fmt.Errorf("trainer list: %w", err)
	}
	if count > 512 || uint64(count)*38 > uint64(len(body)-r.off) {
		return TrainerList{}, fmt.Errorf("trainer list: invalid spell count %d", count)
	}
	out := TrainerList{GUID: GUID(guid), TrainerType: typ, Spells: make([]TrainerSpell, 0, count)}
	for range count {
		var row TrainerSpell
		if row.SpellID, err = r.u32(); err != nil {
			return TrainerList{}, err
		}
		if row.Usable, err = r.u8(); err != nil {
			return TrainerList{}, err
		}
		if row.MoneyCost, err = r.u32(); err != nil {
			return TrainerList{}, err
		}
		for i := range row.PointCost {
			if row.PointCost[i], err = r.u32(); err != nil {
				return TrainerList{}, err
			}
		}
		if row.ReqLevel, err = r.u8(); err != nil {
			return TrainerList{}, err
		}
		if row.ReqSkillLine, err = r.u32(); err != nil {
			return TrainerList{}, err
		}
		if row.ReqSkillRank, err = r.u32(); err != nil {
			return TrainerList{}, err
		}
		for i := range row.ReqAbility {
			if row.ReqAbility[i], err = r.u32(); err != nil {
				return TrainerList{}, err
			}
		}
		out.Spells = append(out.Spells, row)
	}
	if out.Greeting, err = r.cstring(); err != nil {
		return TrainerList{}, fmt.Errorf("trainer list greeting: %w", err)
	}
	if r.off != len(body) {
		return TrainerList{}, fmt.Errorf("trainer list: %d trailing bytes", len(body)-r.off)
	}
	return out, nil
}

type TrainerBuyResult struct {
	GUID      GUID
	SpellID   uint32
	Succeeded bool
	Result    uint32
}

func ParseTrainerBuyResult(body []byte) (TrainerBuyResult, error) {
	if len(body) != 12 && len(body) != 16 {
		return TrainerBuyResult{}, fmt.Errorf("trainer buy result: body length %d", len(body))
	}
	result := TrainerBuyResult{
		GUID:      GUID(binary.LittleEndian.Uint64(body)),
		SpellID:   binary.LittleEndian.Uint32(body[8:12]),
		Succeeded: len(body) == 12,
	}
	if len(body) == 16 {
		result.Result = binary.LittleEndian.Uint32(body[12:16])
	}
	return result, nil
}
