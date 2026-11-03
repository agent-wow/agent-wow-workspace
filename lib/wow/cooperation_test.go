package wow

import (
	"encoding/binary"
	"reflect"
	"testing"
)

func u32buf(values ...uint32) []byte {
	var out []byte
	for _, value := range values {
		out = binary.LittleEndian.AppendUint32(out, value)
	}
	return out
}

func TestContactCodecs(t *testing.T) {
	if got := EncodeAddFriend("Alpha", "roster"); !reflect.DeepEqual(got, []byte("Alpha\x00roster\x00")) {
		t.Fatalf("add friend = %x", got)
	}
	body := u32buf(SocialFlagFriend, 2)
	body = binary.LittleEndian.AppendUint64(body, 101)
	body = append(body, u32buf(1)...)
	body = append(body, "note\x00"...)
	body = append(body, 1)
	body = append(body, u32buf(309, 12, 1)...)
	body = binary.LittleEndian.AppendUint64(body, 102)
	body = append(body, u32buf(1)...)
	body = append(body, 0, 0)
	contacts, err := ParseContactList(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 2 || contacts[0].GUID != 101 || contacts[0].Level != 12 || contacts[1].Status != 0 {
		t.Fatalf("contacts = %+v", contacts)
	}

	statusBody := append([]byte{FriendAddedOnline}, binary.LittleEndian.AppendUint64(nil, 101)...)
	statusBody = append(statusBody, "note\x00"...)
	statusBody = append(statusBody, 1)
	statusBody = append(statusBody, u32buf(309, 12, 1)...)
	status, err := ParseFriendStatus(statusBody)
	if err != nil || status.GUID != 101 || status.Class != 1 {
		t.Fatalf("status = %+v, %v", status, err)
	}
}

func TestWhoUsesDisplayedCount(t *testing.T) {
	body := u32buf(1, 3)
	body = append(body, "Alpha\x00Guild\x00"...)
	body = append(body, u32buf(20, 2, 1)...)
	body = append(body, 0)
	body = append(body, u32buf(309)...)
	players, err := ParseWho(body)
	if err != nil || len(players) != 1 || players[0].Name != "Alpha" || players[0].Level != 20 {
		t.Fatalf("players = %+v, %v", players, err)
	}
}

func TestGroupListAndActions(t *testing.T) {
	if got := EncodeGroupInvite("Alpha"); !reflect.DeepEqual(got, append([]byte("Alpha\x00"), 0, 0, 0, 0)) {
		t.Fatalf("invite = %x", got)
	}
	if got := EncodeGroupAccept(); !reflect.DeepEqual(got, []byte{0, 0, 0, 0}) {
		t.Fatalf("accept = %x", got)
	}
	body := []byte{0, 0, 0, 0}
	body = binary.LittleEndian.AppendUint64(body, 501)
	body = append(body, u32buf(9, 1)...)
	body = append(body, "Bravo\x00"...)
	body = binary.LittleEndian.AppendUint64(body, 102)
	body = append(body, 1, 0, 0, 0)
	body = binary.LittleEndian.AppendUint64(body, 101)
	body = append(body, 2)
	body = binary.LittleEndian.AppendUint64(body, 0)
	body = append(body, 2, 0, 0, 0)
	group, err := ParseGroupList(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(group.Members) != 1 || group.Members[0].Name != "Bravo" || group.LeaderGUID != 101 || group.LootMethod != 2 || group.LootThreshold != 2 {
		t.Fatalf("group = %+v", group)
	}
}

func TestPartyChatCodec(t *testing.T) {
	if got := EncodeChat(ChatMsgParty, LangAddon, "AGW1|P"); !reflect.DeepEqual(got, append([]byte{2, 0, 0, 0, 0xff, 0xff, 0xff, 0xff}, []byte("AGW1|P\x00")...)) {
		t.Fatalf("chat = %x", got)
	}
	body := []byte{ChatMsgParty}
	body = append(body, u32buf(LangAddon)...)
	body = binary.LittleEndian.AppendUint64(body, 101)
	body = append(body, u32buf(0)...)
	body = binary.LittleEndian.AppendUint64(body, 0)
	message := "AGW1|PROPOSE|abc"
	body = append(body, u32buf(uint32(len(message)+1))...)
	body = append(body, message...)
	body = append(body, 0, 0)
	chat, err := ParseChatMessage(body)
	if err != nil || chat.SenderGUID != 101 || chat.Message != message {
		t.Fatalf("chat = %+v, %v", chat, err)
	}
}

func TestRollCodecs(t *testing.T) {
	body := binary.LittleEndian.AppendUint64(nil, 700)
	body = append(body, u32buf(1, 2, 19019, 3, 4, 1, 60000)...)
	body = append(body, 7)
	roll, err := ParseLootStartRoll(body)
	if err != nil || roll.ItemGUID != 700 || roll.ItemID != 19019 || roll.CountdownMS != 60000 || roll.VoteMask != 7 {
		t.Fatalf("start = %+v, %v", roll, err)
	}
	if got := EncodeLootRoll(700, 2, LootRollGreed); len(got) != 13 || got[12] != 2 {
		t.Fatalf("vote = %x", got)
	}

	voteBody := binary.LittleEndian.AppendUint64(nil, 700)
	voteBody = append(voteBody, u32buf(2)...)
	voteBody = binary.LittleEndian.AppendUint64(voteBody, 101)
	voteBody = append(voteBody, u32buf(19019, 3, 4)...)
	voteBody = append(voteBody, 80, 1, 0)
	vote, err := ParseLootRoll(voteBody)
	if err != nil || vote.PlayerGUID != 101 || vote.Number != 80 {
		t.Fatalf("vote = %+v, %v", vote, err)
	}

	wonBody := binary.LittleEndian.AppendUint64(nil, 700)
	wonBody = append(wonBody, u32buf(2, 19019, 3, 4)...)
	wonBody = binary.LittleEndian.AppendUint64(wonBody, 101)
	wonBody = append(wonBody, 80, 1)
	won, err := ParseLootRollWon(wonBody)
	if err != nil || won.WinnerGUID != 101 || won.ItemID != 19019 {
		t.Fatalf("won = %+v, %v", won, err)
	}
}

func TestTradeStatusAndExtended(t *testing.T) {
	begin, err := ParseTradeStatus(append(u32buf(TradeStatusBegin), binary.LittleEndian.AppendUint64(nil, 101)...))
	if err != nil || begin.TraderGUID != 101 {
		t.Fatalf("begin = %+v, %v", begin, err)
	}

	body := []byte{1}
	body = append(body, u32buf(42, 7, 7, 2500, 0)...)
	for slot := 0; slot < 7; slot++ {
		body = append(body, byte(slot))
		if slot == 0 {
			body = append(body, u32buf(19019, 600, 1, 0)...)
			body = binary.LittleEndian.AppendUint64(body, 0)
			body = append(body, u32buf(0, 0, 0, 0)...)
			body = binary.LittleEndian.AppendUint64(body, 101)
			body = append(body, u32buf(0, 0, 0xffffffff, 0, 100, 90)...)
		} else {
			body = append(body, make([]byte, 72)...)
		}
	}
	if len(body) != 532 {
		t.Fatalf("extended fixture length = %d", len(body))
	}
	view, err := ParseTradeExtended(body)
	if err != nil || !view.TraderData || view.TradeID != 42 || view.Money != 2500 || len(view.Items) != 1 || view.Items[0].ItemID != 19019 || view.Items[0].RandomProperty != -1 {
		t.Fatalf("extended = %+v, %v", view, err)
	}
}

func TestFriendStatusOfflineHasNoOptionalFields(t *testing.T) {
	body := append([]byte{FriendAddedOffline}, binary.LittleEndian.AppendUint64(nil, 101)...)
	body = append(body, "note\x00"...)
	status, err := ParseFriendStatus(body)
	if err != nil || status.Note != "note" || status.Status != 0 {
		t.Fatalf("offline add = %+v, %v", status, err)
	}
	onlineBody := append([]byte{FriendOnline}, binary.LittleEndian.AppendUint64(nil, 101)...)
	onlineBody = append(onlineBody, 1)
	onlineBody = append(onlineBody, u32buf(309, 12, 1)...)
	online, err := ParseFriendStatus(onlineBody)
	if err != nil || online.Level != 12 {
		t.Fatalf("online status = %+v, %v", online, err)
	}
}

func TestGroupListLFGFlagIsDistinctFromBattlegroundFlag(t *testing.T) {
	body := []byte{0x08, 0, 0, 0, 2}
	body = append(body, u32buf(123)...)
	body = binary.LittleEndian.AppendUint64(body, 501)
	body = append(body, u32buf(9, 0)...)
	body = binary.LittleEndian.AppendUint64(body, 101)
	group, err := ParseGroupList(body)
	if err != nil || group.LFGState != 2 || group.LFGDungeon != 123 {
		t.Fatalf("LFG group = %+v, %v", group, err)
	}
}

func TestCooperationParsersRejectTruncation(t *testing.T) {
	if _, err := ParseContactList(make([]byte, 7)); err == nil {
		t.Fatal("short contact list accepted")
	}
	if _, err := ParseGroupList(make([]byte, 15)); err == nil {
		t.Fatal("short group list accepted")
	}
	if _, err := ParseChatMessage(make([]byte, 20)); err == nil {
		t.Fatal("short chat accepted")
	}
	if _, err := ParseLootStartRoll(make([]byte, 36)); err == nil {
		t.Fatal("short roll accepted")
	}
	if _, err := ParseTradeExtended(make([]byte, 531)); err == nil {
		t.Fatal("short trade accepted")
	}
}
