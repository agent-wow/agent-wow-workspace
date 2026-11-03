package wow

import "encoding/binary"

const (
	ChatMsgParty uint8  = 2
	LangAddon    uint32 = 0xffffffff
)

func EncodeChat(chatType uint8, language uint32, message string) []byte {
	out := binary.LittleEndian.AppendUint32(nil, uint32(chatType))
	out = binary.LittleEndian.AppendUint32(out, language)
	out = append(out, message...)
	return append(out, 0)
}

type ChatMessage struct {
	Type         uint8
	Language     uint32
	SenderGUID   GUID
	Flags        uint32
	ReceiverGUID GUID
	Message      string
	Tag          uint8
}

// ParseChatMessage decodes player-to-player SMSG_MESSAGECHAT rows. Creature,
// channel and achievement variants have additional fields and are rejected by
// callers that only subscribe to party chat.
func ParseChatMessage(body []byte) (ChatMessage, error) {
	r := &reader{b: body}
	var out ChatMessage
	var err error
	if out.Type, err = r.u8(); err != nil {
		return out, err
	}
	if out.Language, err = r.u32(); err != nil {
		return out, err
	}
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.SenderGUID = GUID(guid)
	if out.Flags, err = r.u32(); err != nil {
		return out, err
	}
	guid, err = r.u64()
	if err != nil {
		return out, err
	}
	out.ReceiverGUID = GUID(guid)
	length, err := r.u32()
	if err != nil {
		return out, err
	}
	if length == 0 {
		return out, errTruncated
	}
	raw, err := r.take(int(length))
	if err != nil {
		return out, err
	}
	if raw[len(raw)-1] != 0 {
		return out, errTruncated
	}
	out.Message = string(raw[:len(raw)-1])
	out.Tag, err = r.u8()
	return out, err
}
