package service

import (
	"encoding/binary"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
)

func tradeOpenPacket() *modv1.WorldPacket {
	return &modv1.WorldPacket{Opcode: opcode.SMSGTradeStatus, Payload: binary.LittleEndian.AppendUint32(nil, wow.TradeStatusOpen)}
}

func tradeExtendedPacket(trader bool, revision uint32) *modv1.WorldPacket {
	flag := byte(0)
	if trader {
		flag = 1
	}
	body := []byte{flag}
	body = binary.LittleEndian.AppendUint32(body, 0)
	body = binary.LittleEndian.AppendUint32(body, 7)
	body = binary.LittleEndian.AppendUint32(body, 7)
	body = binary.LittleEndian.AppendUint32(body, revision)
	body = binary.LittleEndian.AppendUint32(body, 0)
	for slot := 0; slot < 7; slot++ {
		body = append(body, byte(slot))
		if slot == 0 {
			body = binary.LittleEndian.AppendUint32(body, 19019)
			body = append(body, make([]byte, 68)...)
		} else {
			body = append(body, make([]byte, 72)...)
		}
	}
	return &modv1.WorldPacket{Opcode: opcode.SMSGTradeStatusExtended, Payload: body}
}

func openTestTrade(t *testing.T, s *Service) {
	t.Helper()
	_, _ = s.OnPacket(t.Context(), tradeOpenPacket())
	_, _ = s.OnPacket(t.Context(), tradeExtendedPacket(false, 0))
	_, _ = s.OnPacket(t.Context(), tradeExtendedPacket(true, 0))
}

func TestTradeInitiateWaitsForServerState(t *testing.T) {
	session := inventorySession{sent: make(chan *modv1.SendPacketRequest, 1)}
	s := New(session)
	if _, err := s.Trade(t.Context(), &modulev1.TradeRequest{Action: "initiate", TargetGuid: 2}); err != nil {
		t.Fatal(err)
	}
	trade := s.snapshot().GetTrade()
	if trade.GetPending() || trade.GetActive() || trade.GetTraderGuid() != 0 {
		t.Fatalf("initiate invented server trade state: %+v", trade)
	}
	if (<-session.sent).GetOpcode() != opcode.CMSGInitiateTrade {
		t.Fatal("initiate packet was not sent")
	}
}

func TestTradePendingCanBeCancelled(t *testing.T) {
	session := inventorySession{sent: make(chan *modv1.SendPacketRequest, 1)}
	s := New(session)
	body := binary.LittleEndian.AppendUint32(nil, wow.TradeStatusBegin)
	body = binary.LittleEndian.AppendUint64(body, 2)
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGTradeStatus, Payload: body})
	if !s.snapshot().GetTrade().GetPending() {
		t.Fatal("server pending trade was not recorded")
	}
	if _, err := s.Trade(t.Context(), &modulev1.TradeRequest{Action: "cancel"}); err != nil {
		t.Fatal(err)
	}
	if (<-session.sent).GetOpcode() != opcode.CMSGCancelTrade {
		t.Fatal("pending trade cancel was not sent")
	}
}

func TestTradeAcceptRequiresUnchangedRevisionAndIsSingleUse(t *testing.T) {
	session := inventorySession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	openTestTrade(t, s)
	trade := s.snapshot().GetTrade()
	if !trade.GetActive() || trade.GetRevision() == 0 {
		t.Fatalf("trade not open: %+v", trade)
	}
	stale := &modulev1.TradeRequest{Action: "accept", ExpectedRevision: trade.GetRevision() + 1}
	if _, err := s.Trade(t.Context(), stale); err == nil {
		t.Fatal("accept succeeded with stale revision")
	}
	request := &modulev1.TradeRequest{Action: "accept", ExpectedRevision: trade.GetRevision()}
	if _, err := s.Trade(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Trade(t.Context(), request); err == nil {
		t.Fatal("same accept dispatched twice")
	}
	if len(session.sent) != 1 || (<-session.sent).GetOpcode() != opcode.CMSGAcceptTrade {
		t.Fatal("accept packet was not sent exactly once")
	}

	_, _ = s.OnPacket(t.Context(), tradeExtendedPacket(true, 100))
	changed := s.snapshot().GetTrade()
	if changed.GetOwnAccepted() || changed.GetTraderAccepted() || changed.GetRevision() <= trade.GetRevision() {
		t.Fatalf("offer change did not invalidate accept: %+v", changed)
	}
}

func TestTradeSetItemReconfirmsRawInventoryIdentity(t *testing.T) {
	session := inventorySession{sent: make(chan *modv1.SendPacketRequest, 4)}
	s := New(session)
	s.selfGUID = 1
	s.objects[1] = &trackedObject{typ: wow.ObjectTypePlayer, fields: wow.UpdateFields{
		wow.PlayerFieldPackSlot1: 300, wow.PlayerFieldPackSlot1 + 1: 0,
	}}
	s.objects[300] = &trackedObject{typ: wow.ObjectTypeItem, fields: wow.UpdateFields{wow.ObjectFieldEntry: 19019}}
	openTestTrade(t, s)
	request := &modulev1.TradeRequest{Action: "set_item", TradeSlot: 0, Bag: 255, Slot: 23, ItemGuid: 301}
	if _, err := s.Trade(t.Context(), request); err == nil {
		t.Fatal("stale bag/slot identity accepted")
	}
	if len(session.sent) != 0 {
		t.Fatal("stale item was offered")
	}
	request.ItemGuid = 300
	if _, err := s.Trade(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	packet := <-session.sent
	if packet.GetOpcode() != opcode.CMSGSetTradeItem || len(packet.GetPayload()) != 3 {
		t.Fatalf("set item = %v", packet)
	}
}

func TestTradeCompleteIsTerminalUntilNewTrade(t *testing.T) {
	s := New(nil)
	openTestTrade(t, s)
	body := binary.LittleEndian.AppendUint32(nil, wow.TradeStatusComplete)
	_, _ = s.OnPacket(t.Context(), &modv1.WorldPacket{Opcode: opcode.SMSGTradeStatus, Payload: body})
	trade := s.snapshot().GetTrade()
	if trade.GetActive() || !trade.GetComplete() || !trade.GetOwnAccepted() || !trade.GetTraderAccepted() {
		t.Fatalf("complete trade = %+v", trade)
	}
	if _, err := s.Trade(t.Context(), &modulev1.TradeRequest{Action: "accept", ExpectedRevision: trade.GetRevision()}); err == nil {
		t.Fatal("completed trade accepted another action")
	}
}
