package wow

import (
	"encoding/binary"
	"fmt"
)

type TalentRank struct {
	ID   uint32
	Rank uint8 // zero-based current rank on the wire
}

type TalentGroup struct {
	Active  bool
	Talents []TalentRank
	Glyphs  [6]uint16
}

type TalentsInfo struct {
	Pet         bool
	FreePoints  uint32
	ActiveGroup uint8
	Groups      []TalentGroup
}

func EncodeLearnTalent(talentID uint32, requestedRank uint32) []byte {
	out := binary.LittleEndian.AppendUint32(nil, talentID)
	return binary.LittleEndian.AppendUint32(out, requestedRank)
}

// ParseTalentsInfo decodes SMSG_TALENTS_INFO as emitted by AzerothCore's
// Player::BuildPlayerTalentsInfoData and BuildPetTalentsInfoData. Player talent
// counts and glyph counts are bytes, and talent ranks are zero-based.
func ParseTalentsInfo(body []byte) (TalentsInfo, error) {
	r := &reader{b: body}
	kind, err := r.u8()
	if err != nil {
		return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
	}
	points, err := r.u32()
	if err != nil {
		return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
	}
	out := TalentsInfo{Pet: kind != 0, FreePoints: points}
	if out.Pet {
		count, err := r.u8()
		if err != nil {
			return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
		}
		group := TalentGroup{Active: true, Talents: make([]TalentRank, 0, count)}
		for range count {
			id, rank, err := parseTalentRank(r)
			if err != nil {
				return TalentsInfo{}, err
			}
			group.Talents = append(group.Talents, TalentRank{ID: id, Rank: rank})
		}
		out.Groups = []TalentGroup{group}
	} else {
		groupCount, err := r.u8()
		if err != nil {
			return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
		}
		if groupCount > 2 {
			return TalentsInfo{}, fmt.Errorf("talents info: invalid group count %d", groupCount)
		}
		active, err := r.u8()
		if err != nil {
			return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
		}
		if int(active) >= int(groupCount) && groupCount != 0 {
			return TalentsInfo{}, fmt.Errorf("talents info: invalid active group %d", active)
		}
		out.ActiveGroup = active
		out.Groups = make([]TalentGroup, 0, groupCount)
		for range groupCount {
			count, err := r.u8()
			if err != nil {
				return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
			}
			group := TalentGroup{Active: len(out.Groups) == int(active), Talents: make([]TalentRank, 0, count)}
			for range count {
				id, rank, err := parseTalentRank(r)
				if err != nil {
					return TalentsInfo{}, err
				}
				group.Talents = append(group.Talents, TalentRank{ID: id, Rank: rank})
			}
			glyphCount, err := r.u8()
			if err != nil {
				return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
			}
			if glyphCount > uint8(len(group.Glyphs)) {
				return TalentsInfo{}, fmt.Errorf("talents info: invalid glyph count %d", glyphCount)
			}
			for i := range glyphCount {
				if group.Glyphs[i], err = r.u16(); err != nil {
					return TalentsInfo{}, fmt.Errorf("talents info: %w", err)
				}
			}
			out.Groups = append(out.Groups, group)
		}
	}
	if r.off != len(body) {
		return TalentsInfo{}, fmt.Errorf("talents info: %d trailing bytes", len(body)-r.off)
	}
	return out, nil
}

func parseTalentRank(r *reader) (uint32, uint8, error) {
	id, err := r.u32()
	if err != nil {
		return 0, 0, fmt.Errorf("talents info: %w", err)
	}
	rank, err := r.u8()
	if err != nil {
		return 0, 0, fmt.Errorf("talents info: %w", err)
	}
	return id, rank, nil
}
