package wow

// EncodePlayedTime builds CMSG_PLAYED_TIME. The server reads a single byte
// ("trigger script event"); zero asks for the counters without echoing them to
// the chat frame.
func EncodePlayedTime() []byte {
	return []byte{0}
}

// PlayedTime is the server's own account of how long the character has been
// played, the same numbers the client's /played command prints.
type PlayedTime struct {
	// Total is the character's cumulative played time in seconds.
	Total uint32
	// Level is the time in seconds spent at the current level.
	Level uint32
	// Trigger is the client-supplied flag echoed back by the server.
	Trigger bool
}

// ParsePlayedTime decodes SMSG_PLAYED_TIME: total seconds, level seconds and
// the trigger byte. The trigger is optional so a short reply still yields the
// two counters that matter.
func ParsePlayedTime(body []byte) (PlayedTime, error) {
	r := &reader{b: body}
	total, err := r.u32()
	if err != nil {
		return PlayedTime{}, err
	}
	level, err := r.u32()
	if err != nil {
		return PlayedTime{}, err
	}
	result := PlayedTime{Total: total, Level: level}
	if len(body) > 8 {
		trigger, err := r.u8()
		if err != nil {
			return PlayedTime{}, err
		}
		result.Trigger = trigger != 0
	}
	return result, nil
}
