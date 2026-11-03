package wow

import "encoding/binary"

func EncodeGroupInvite(name string) []byte {
	out := append([]byte(nil), name...)
	out = append(out, 0)
	return binary.LittleEndian.AppendUint32(out, 0)
}

func EncodeGroupAccept() []byte { return make([]byte, 4) }

func EncodeLootMethod(method uint32, master GUID, threshold uint32) []byte {
	out := binary.LittleEndian.AppendUint32(nil, method)
	out = binary.LittleEndian.AppendUint64(out, uint64(master))
	return binary.LittleEndian.AppendUint32(out, threshold)
}

type GroupMember struct {
	Name     string
	GUID     GUID
	Status   uint8
	Subgroup uint8
	Flags    uint8
	Roles    uint8
}

type GroupList struct {
	GroupType         uint8
	OwnSubgroup       uint8
	OwnFlags          uint8
	OwnRoles          uint8
	LFGState          uint8
	LFGDungeon        uint32
	GroupGUID         GUID
	Counter           uint32
	Members           []GroupMember
	LeaderGUID        GUID
	LootMethod        uint8
	MasterLooterGUID  GUID
	LootThreshold     uint8
	DungeonDifficulty uint8
	RaidDifficulty    uint8
	DynamicRaidHeroic bool
}

// ParseGroupList decodes the 3.3.5a roster. The server omits the recipient's
// own member row; Own* fields describe that omitted row.
func ParseGroupList(body []byte) (GroupList, error) {
	r := &reader{b: body}
	var out GroupList
	var err error
	if out.GroupType, err = r.u8(); err != nil {
		return out, err
	}
	if out.OwnSubgroup, err = r.u8(); err != nil {
		return out, err
	}
	if out.OwnFlags, err = r.u8(); err != nil {
		return out, err
	}
	if out.OwnRoles, err = r.u8(); err != nil {
		return out, err
	}
	if out.GroupType&0x08 != 0 { // GROUPTYPE_LFG
		if out.LFGState, err = r.u8(); err != nil {
			return out, err
		}
		if out.LFGDungeon, err = r.u32(); err != nil {
			return out, err
		}
	}
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.GroupGUID = GUID(guid)
	if out.Counter, err = r.u32(); err != nil {
		return out, err
	}
	count, err := r.u32()
	if err != nil {
		return out, err
	}
	out.Members = make([]GroupMember, 0, count)
	for i := uint32(0); i < count; i++ {
		var m GroupMember
		if m.Name, err = r.cstring(); err != nil {
			return out, err
		}
		guid, err := r.u64()
		if err != nil {
			return out, err
		}
		m.GUID = GUID(guid)
		if m.Status, err = r.u8(); err != nil {
			return out, err
		}
		if m.Subgroup, err = r.u8(); err != nil {
			return out, err
		}
		if m.Flags, err = r.u8(); err != nil {
			return out, err
		}
		if m.Roles, err = r.u8(); err != nil {
			return out, err
		}
		out.Members = append(out.Members, m)
	}
	guid, err = r.u64()
	if err != nil {
		return out, err
	}
	out.LeaderGUID = GUID(guid)
	if count == 0 {
		return out, nil
	}
	if out.LootMethod, err = r.u8(); err != nil {
		return out, err
	}
	guid, err = r.u64()
	if err != nil {
		return out, err
	}
	out.MasterLooterGUID = GUID(guid)
	if out.LootThreshold, err = r.u8(); err != nil {
		return out, err
	}
	if out.DungeonDifficulty, err = r.u8(); err != nil {
		return out, err
	}
	if out.RaidDifficulty, err = r.u8(); err != nil {
		return out, err
	}
	heroic, err := r.u8()
	out.DynamicRaidHeroic = heroic != 0
	return out, err
}
