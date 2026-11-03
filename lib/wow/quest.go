package wow

import (
	"encoding/binary"
	"fmt"
)

// Quest packet codecs for WotLK 3.3.5a (build 12340). Layouts follow
// AzerothCore's QuestHandler.cpp. GUIDs in the quest-giver packets are raw
// little-endian uint64 values, not packed GUIDs.

func appendQuestGUIDQuest(out []byte, guid GUID, questID uint32) []byte {
	out = binary.LittleEndian.AppendUint64(out, uint64(guid))
	return binary.LittleEndian.AppendUint32(out, questID)
}

// EncodeQuestGiverHello builds CMSG_QUESTGIVER_HELLO.
func EncodeQuestGiverHello(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// EncodeQuestGiverAcceptQuest builds CMSG_QUESTGIVER_ACCEPT_QUEST. The final
// uint32 is the client's unused quest-gossip field and is zero for NPC quests.
func EncodeQuestGiverAcceptQuest(guid GUID, questID uint32) []byte {
	out := appendQuestGUIDQuest(nil, guid, questID)
	return binary.LittleEndian.AppendUint32(out, 0)
}

// EncodeQuestGiverCompleteQuest builds CMSG_QUESTGIVER_COMPLETE_QUEST.
func EncodeQuestGiverCompleteQuest(guid GUID, questID uint32) []byte {
	return appendQuestGUIDQuest(nil, guid, questID)
}

// EncodeQuestGiverRequestReward builds CMSG_QUESTGIVER_REQUEST_REWARD.
func EncodeQuestGiverRequestReward(guid GUID, questID uint32) []byte {
	return appendQuestGUIDQuest(nil, guid, questID)
}

// EncodeQuestGiverChooseReward builds CMSG_QUESTGIVER_CHOOSE_REWARD. Index 0
// also selects the no-choice path when a quest has fixed rewards only.
func EncodeQuestGiverChooseReward(guid GUID, questID, rewardIndex uint32) []byte {
	out := appendQuestGUIDQuest(nil, guid, questID)
	return binary.LittleEndian.AppendUint32(out, rewardIndex)
}

// ParseQuestGiverQuestRef decodes the shared prefix of quest-giver responses
// such as SMSG_QUESTGIVER_REQUEST_ITEMS and SMSG_QUESTGIVER_OFFER_REWARD.
// Matching both values prevents a delayed response from driving another quest.
func ParseQuestGiverQuestRef(body []byte) (GUID, uint32, error) {
	if len(body) < 12 {
		return 0, 0, fmt.Errorf("quest-giver quest ref: body length %d", len(body))
	}
	return GUID(binary.LittleEndian.Uint64(body[0:8])), binary.LittleEndian.Uint32(body[8:12]), nil
}

// QuestGiverQuest is one row in SMSG_QUESTGIVER_QUEST_LIST.
type QuestGiverQuest struct {
	QuestID    uint32
	Icon       uint32
	Level      int32
	Flags      uint32
	Repeatable bool
	Title      string
}

// QuestGiverQuestList is the available/completed quest menu for one NPC.
type QuestGiverQuestList struct {
	GUID         GUID
	GreetingText string
	Quests       []QuestGiverQuest
}

// ParseQuestGiverQuestList decodes SMSG_QUESTGIVER_QUEST_LIST. After the
// greeting come emote delay, emote type and a uint8 quest count.
func ParseQuestGiverQuestList(body []byte) (QuestGiverQuestList, error) {
	r := &reader{b: body}
	guid, err := r.u64()
	if err != nil {
		return QuestGiverQuestList{}, err
	}
	out := QuestGiverQuestList{GUID: GUID(guid)}
	if out.GreetingText, err = r.cstring(); err != nil {
		return QuestGiverQuestList{}, err
	}
	if _, err = r.u32(); err != nil {
		return QuestGiverQuestList{}, err
	}
	if _, err = r.u32(); err != nil {
		return QuestGiverQuestList{}, err
	}
	count, err := r.u8()
	if err != nil {
		return QuestGiverQuestList{}, err
	}
	out.Quests = make([]QuestGiverQuest, 0, count)
	for i := uint8(0); i < count; i++ {
		var quest QuestGiverQuest
		if quest.QuestID, err = r.u32(); err != nil {
			return QuestGiverQuestList{}, err
		}
		if quest.Icon, err = r.u32(); err != nil {
			return QuestGiverQuestList{}, err
		}
		level, err := r.u32()
		if err != nil {
			return QuestGiverQuestList{}, err
		}
		quest.Level = int32(level)
		if quest.Flags, err = r.u32(); err != nil {
			return QuestGiverQuestList{}, err
		}
		repeatable, err := r.u8()
		if err != nil {
			return QuestGiverQuestList{}, err
		}
		quest.Repeatable = repeatable != 0
		if quest.Title, err = r.cstring(); err != nil {
			return QuestGiverQuestList{}, err
		}
		out.Quests = append(out.Quests, quest)
	}
	return out, nil
}

// QuestKillUpdate is SMSG_QUESTUPDATE_ADD_KILL.
type QuestKillUpdate struct {
	QuestID  uint32
	Entry    uint32
	Current  uint32
	Required uint32
}

// ParseQuestKillUpdate decodes SMSG_QUESTUPDATE_ADD_KILL.
func ParseQuestKillUpdate(body []byte) (QuestKillUpdate, error) {
	if len(body) < 20 {
		return QuestKillUpdate{}, fmt.Errorf("quest kill update: body length %d", len(body))
	}
	return QuestKillUpdate{
		QuestID:  binary.LittleEndian.Uint32(body[0:4]),
		Entry:    binary.LittleEndian.Uint32(body[4:8]),
		Current:  binary.LittleEndian.Uint32(body[8:12]),
		Required: binary.LittleEndian.Uint32(body[12:16]),
	}, nil
}

// ParseQuestID decodes a packet whose body starts with a quest ID. It is used
// for SMSG_QUESTUPDATE_COMPLETE, SMSG_QUESTUPDATE_FAILED and related notices.
func ParseQuestID(body []byte) (uint32, error) {
	if len(body) < 4 {
		return 0, fmt.Errorf("quest ID: body length %d", len(body))
	}
	return binary.LittleEndian.Uint32(body), nil
}
