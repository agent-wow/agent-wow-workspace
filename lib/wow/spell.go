package wow

import (
	"encoding/binary"
	"fmt"
)

type KnownSpell struct {
	ID       uint32
	Disabled bool
}

// ParseInitialSpells decodes SMSG_INITIAL_SPELLS: a visual-cooldown byte,
// uint16 spell count, (uint32 spell, uint16 disabled) rows and trailing
// cooldown rows, which sense already owns and this parser deliberately ignores.
func ParseInitialSpells(body []byte) ([]KnownSpell, error) {
	if len(body) < 3 {
		return nil, fmt.Errorf("initial spells: body length %d", len(body))
	}
	count := int(binary.LittleEndian.Uint16(body[1:3]))
	if len(body) < 3+count*6+2 {
		return nil, fmt.Errorf("initial spells: truncated spell rows")
	}
	out := make([]KnownSpell, 0, count)
	for i := range count {
		off := 3 + i*6
		out = append(out, KnownSpell{
			ID:       binary.LittleEndian.Uint32(body[off:]),
			Disabled: binary.LittleEndian.Uint16(body[off+4:]) != 0,
		})
	}
	return out, nil
}

func ParseLearnedSpell(body []byte) (uint32, error) {
	if len(body) != 6 {
		return 0, fmt.Errorf("learned spell: body length %d", len(body))
	}
	return binary.LittleEndian.Uint32(body), nil
}
