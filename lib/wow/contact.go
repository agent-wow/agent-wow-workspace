package wow

import "encoding/binary"

const SocialFlagFriend uint32 = 0x1

const (
	FriendOnline       uint8 = 0x02
	FriendOffline      uint8 = 0x03
	FriendRemoved      uint8 = 0x05
	FriendAddedOnline  uint8 = 0x06
	FriendAddedOffline uint8 = 0x07
)

// EncodeContactList builds CMSG_CONTACT_LIST for the requested social flags.
func EncodeContactList(flags uint32) []byte {
	return binary.LittleEndian.AppendUint32(nil, flags)
}

// EncodeAddFriend builds CMSG_ADD_FRIEND: friend name followed by note.
func EncodeAddFriend(name, note string) []byte {
	out := append([]byte(nil), name...)
	out = append(out, 0)
	out = append(out, note...)
	return append(out, 0)
}

// Contact is one row in SMSG_CONTACT_LIST.
type Contact struct {
	GUID   GUID
	Flags  uint32
	Note   string
	Status uint8
	Area   uint32
	Level  uint32
	Class  uint32
}

func ParseContactList(body []byte) ([]Contact, error) {
	r := &reader{b: body}
	if _, err := r.u32(); err != nil { // requested flags
		return nil, err
	}
	count, err := r.u32()
	if err != nil {
		return nil, err
	}
	out := make([]Contact, 0, count)
	for i := uint32(0); i < count; i++ {
		var c Contact
		guid, err := r.u64()
		if err != nil {
			return nil, err
		}
		c.GUID = GUID(guid)
		if c.Flags, err = r.u32(); err != nil {
			return nil, err
		}
		if c.Note, err = r.cstring(); err != nil {
			return nil, err
		}
		if c.Flags&SocialFlagFriend != 0 {
			if c.Status, err = r.u8(); err != nil {
				return nil, err
			}
			if c.Status != 0 {
				if c.Area, err = r.u32(); err != nil {
					return nil, err
				}
				if c.Level, err = r.u32(); err != nil {
					return nil, err
				}
				if c.Class, err = r.u32(); err != nil {
					return nil, err
				}
			}
		}
		out = append(out, c)
	}
	return out, nil
}

// FriendStatus is the server-authoritative result of a social-list update.
type FriendStatus struct {
	Result uint8
	GUID   GUID
	Note   string
	Status uint8
	Area   uint32
	Level  uint32
	Class  uint32
}

func ParseFriendStatus(body []byte) (FriendStatus, error) {
	r := &reader{b: body}
	var out FriendStatus
	var err error
	if out.Result, err = r.u8(); err != nil {
		return out, err
	}
	guid, err := r.u64()
	if err != nil {
		return out, err
	}
	out.GUID = GUID(guid)
	// FRIEND_ADDED_OFFLINE=7 and FRIEND_ADDED_ONLINE=6 carry a note.
	if out.Result == FriendAddedOffline || out.Result == FriendAddedOnline {
		if out.Note, err = r.cstring(); err != nil {
			return out, err
		}
	}
	// FRIEND_ADDED_ONLINE=6 and FRIEND_ONLINE=2 carry presence details.
	if out.Result == FriendAddedOnline || out.Result == FriendOnline {
		if out.Status, err = r.u8(); err != nil {
			return out, err
		}
		if out.Area, err = r.u32(); err != nil {
			return out, err
		}
		if out.Level, err = r.u32(); err != nil {
			return out, err
		}
		if out.Class, err = r.u32(); err != nil {
			return out, err
		}
	}
	return out, nil
}

// EncodeWho builds an exact-name CMSG_WHO query with no optional filters.
func EncodeWho(name string, minLevel, maxLevel uint32) []byte {
	out := binary.LittleEndian.AppendUint32(nil, minLevel)
	out = binary.LittleEndian.AppendUint32(out, maxLevel)
	out = append(out, name...)
	out = append(out, 0, 0)                         // player-name terminator and empty guild name
	out = binary.LittleEndian.AppendUint32(out, 0)  // race mask
	out = binary.LittleEndian.AppendUint32(out, 0)  // class mask
	out = binary.LittleEndian.AppendUint32(out, 0)  // zones
	return binary.LittleEndian.AppendUint32(out, 0) // search strings
}

type WhoPlayer struct {
	Name   string
	Guild  string
	Level  uint32
	Class  uint32
	Race   uint32
	Gender uint8
	Zone   uint32
}

func ParseWho(body []byte) ([]WhoPlayer, error) {
	r := &reader{b: body}
	count, err := r.u32() // displayed rows; the next field is total matches
	if err != nil {
		return nil, err
	}
	if _, err = r.u32(); err != nil {
		return nil, err
	}
	out := make([]WhoPlayer, 0, count)
	for i := uint32(0); i < count; i++ {
		var p WhoPlayer
		if p.Name, err = r.cstring(); err != nil {
			return nil, err
		}
		if p.Guild, err = r.cstring(); err != nil {
			return nil, err
		}
		if p.Level, err = r.u32(); err != nil {
			return nil, err
		}
		if p.Class, err = r.u32(); err != nil {
			return nil, err
		}
		if p.Race, err = r.u32(); err != nil {
			return nil, err
		}
		if p.Gender, err = r.u8(); err != nil {
			return nil, err
		}
		if p.Zone, err = r.u32(); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, nil
}
