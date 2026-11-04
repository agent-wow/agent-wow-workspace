package wow

import (
	"encoding/binary"
	"testing"
)

func TestQuestPacketEncoders(t *testing.T) {
	guid := GUID(0xF130000000001234)
	if got := EncodeQuestGiverHello(guid); len(got) != 8 || binary.LittleEndian.Uint64(got) != uint64(guid) {
		t.Fatalf("hello: %x", got)
	}
	accept := EncodeQuestGiverAcceptQuest(guid, 783)
	if len(accept) != 16 || binary.LittleEndian.Uint32(accept[8:12]) != 783 || binary.LittleEndian.Uint32(accept[12:16]) != 0 {
		t.Fatalf("accept: %x", accept)
	}
	complete := EncodeQuestGiverCompleteQuest(guid, 783)
	if len(complete) != 12 || binary.LittleEndian.Uint32(complete[8:12]) != 783 {
		t.Fatalf("complete: %x", complete)
	}
	reward := EncodeQuestGiverRequestReward(guid, 783)
	if len(reward) != 12 || binary.LittleEndian.Uint32(reward[8:12]) != 783 {
		t.Fatalf("request reward: %x", reward)
	}
	choose := EncodeQuestGiverChooseReward(guid, 783, 2)
	if len(choose) != 16 || binary.LittleEndian.Uint32(choose[12:16]) != 2 {
		t.Fatalf("choose reward: %x", choose)
	}
}

func TestParseQuestGiverQuestList(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 0xF13000000000079E)
	body = append(body, "Welcome to Northshire\x00"...)
	body = binary.LittleEndian.AppendUint32(body, 0) // emote delay
	body = binary.LittleEndian.AppendUint32(body, 1) // emote type
	body = append(body, 2)                           // quest count
	for _, row := range []struct {
		id, icon, level, flags uint32
		repeatable             byte
		title                  string
	}{
		{783, 0, 1, 8, 0, "A Threat Within"},
		{7, 1, 2, 0, 1, "Kobold Camp Cleanup"},
	} {
		for _, value := range []uint32{row.id, row.icon, row.level, row.flags} {
			body = binary.LittleEndian.AppendUint32(body, value)
		}
		body = append(body, row.repeatable)
		body = append(body, row.title...)
		body = append(body, 0)
	}

	got, err := ParseQuestGiverQuestList(body)
	if err != nil {
		t.Fatalf("ParseQuestGiverQuestList: %v", err)
	}
	if got.GUID != 0xF13000000000079E || got.GreetingText != "Welcome to Northshire" || len(got.Quests) != 2 {
		t.Fatalf("unexpected list: %+v", got)
	}
	if got.Quests[0].QuestID != 783 || got.Quests[0].Title != "A Threat Within" || got.Quests[0].Repeatable {
		t.Fatalf("quest 0: %+v", got.Quests[0])
	}
	if got.Quests[1].QuestID != 7 || got.Quests[1].Level != 2 || !got.Quests[1].Repeatable {
		t.Fatalf("quest 1: %+v", got.Quests[1])
	}
}

func TestParseQuestUpdates(t *testing.T) {
	body := make([]byte, 20)
	binary.LittleEndian.PutUint32(body[0:4], 783)
	binary.LittleEndian.PutUint32(body[4:8], 525)
	binary.LittleEndian.PutUint32(body[8:12], 3)
	binary.LittleEndian.PutUint32(body[12:16], 6)
	kill, err := ParseQuestKillUpdate(body)
	if err != nil {
		t.Fatalf("ParseQuestKillUpdate: %v", err)
	}
	if kill.QuestID != 783 || kill.Entry != 525 || kill.Current != 3 || kill.Required != 6 {
		t.Fatalf("unexpected kill update: %+v", kill)
	}
	id, err := ParseQuestID(binary.LittleEndian.AppendUint32(nil, 783))
	if err != nil || id != 783 {
		t.Fatalf("ParseQuestID: id=%d err=%v", id, err)
	}
}

func TestParseQuestGiverQuestRef(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 0xF13000000000079E)
	body = binary.LittleEndian.AppendUint32(body, 783)
	guid, questID, err := ParseQuestGiverQuestRef(body)
	if err != nil || guid != 0xF13000000000079E || questID != 783 {
		t.Fatalf("guid=%x quest=%d err=%v", guid, questID, err)
	}
}

func TestQuestParsersRejectTruncation(t *testing.T) {
	if _, err := ParseQuestGiverQuestList(make([]byte, 8)); err == nil {
		t.Fatal("expected truncated quest list to fail")
	}
	if _, err := ParseQuestKillUpdate(make([]byte, 19)); err == nil {
		t.Fatal("expected truncated kill update to fail")
	}
	if _, _, err := ParseQuestGiverQuestRef(make([]byte, 11)); err == nil {
		t.Fatal("expected truncated quest-giver ref to fail")
	}
	if _, err := ParseQuestID(make([]byte, 3)); err == nil {
		t.Fatal("expected truncated quest ID to fail")
	}
}
