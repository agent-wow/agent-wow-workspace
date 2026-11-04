package wow

import (
	"encoding/binary"
	"errors"
	"strings"
)

// NameQueryResponse is the player/pet half of SMSG_NAME_QUERY_RESPONSE.
type NameQueryResponse struct {
	GUID   GUID
	Name   string
	Race   uint8
	Gender uint8
	Class  uint8
}

// EncodeNameQuery builds CMSG_NAME_QUERY: the full 64-bit GUID.
func EncodeNameQuery(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// EncodeSetSelection builds CMSG_SET_SELECTION: the full 64-bit GUID.
func EncodeSetSelection(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// ParseNameQueryResponse decodes a player/pet name reply. Creature names come
// from SMSG_CREATURE_QUERY_RESPONSE instead, so this returns ok=false when the
// server reports an unknown GUID.
func ParseNameQueryResponse(body []byte) (NameQueryResponse, bool, error) {
	r := &reader{b: body}
	guid, err := r.packedGUID()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	unknown, err := r.u8()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	if unknown != 0 {
		return NameQueryResponse{GUID: guid}, false, nil
	}
	name, err := r.cstring()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	if _, err := r.cstring(); err != nil { // realm name
		return NameQueryResponse{}, false, err
	}
	race, err := r.u8()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	gender, err := r.u8()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	class, err := r.u8()
	if err != nil {
		return NameQueryResponse{}, false, err
	}
	return NameQueryResponse{GUID: guid, Name: name, Race: race, Gender: gender, Class: class}, true, nil
}

func (r *reader) cstring() (string, error) {
	start := r.off
	for r.off < len(r.b) {
		if r.b[r.off] == 0 {
			s := string(r.b[start:r.off])
			r.off++
			return s, nil
		}
		r.off++
	}
	return "", errors.New("packet truncated: unterminated string")
}

// ParseGroupInvite returns the inviter's name from SMSG_GROUP_INVITE. The
// 3.3.5a body is: uint8 flag, NUL-terminated name, uint32 unk, uint8 count,
// uint32 unk. Only the name is needed to log who is inviting.
func ParseGroupInvite(body []byte) (string, error) {
	r := &reader{b: body}
	if _, err := r.u8(); err != nil {
		return "", err
	}
	name, err := r.cstring()
	if err != nil {
		return "", err
	}
	return TrimName(name), nil
}

// TrimName guards against pathological names in logs and responses.
func TrimName(name string) string {
	name = strings.TrimSpace(name)
	if len(name) > 64 {
		name = name[:64]
	}
	return name
}

// CreatureNameResponse is the UI-visible head of SMSG_CREATURE_QUERY_RESPONSE.
type CreatureNameResponse struct {
	Entry   uint32
	Name    string
	SubName string
	Unknown bool
}

// EncodeCreatureQuery builds CMSG_CREATURE_QUERY: template entry, then the
// full 64-bit GUID of the creature being queried.
func EncodeCreatureQuery(entry uint32, guid GUID) []byte {
	out := binary.LittleEndian.AppendUint32(nil, entry)
	return binary.LittleEndian.AppendUint64(out, uint64(guid))
}

// ParseCreatureQueryResponse decodes enough of SMSG_CREATURE_QUERY_RESPONSE to
// identify a creature by entry. The high bit of the entry marks an unknown
// template. Fields after the names are deliberately ignored.
func ParseCreatureQueryResponse(body []byte) (CreatureNameResponse, error) {
	r := &reader{b: body}
	raw, err := r.u32()
	if err != nil {
		return CreatureNameResponse{}, err
	}
	out := CreatureNameResponse{Entry: raw & 0x7fffffff}
	if raw&0x80000000 != 0 {
		out.Unknown = true
		return out, nil
	}
	name, err := r.cstring()
	if err != nil {
		return CreatureNameResponse{}, err
	}
	for i := 0; i < 3; i++ { // alternate names
		if _, err := r.cstring(); err != nil {
			return CreatureNameResponse{}, err
		}
	}
	sub, err := r.cstring()
	if err != nil {
		return CreatureNameResponse{}, err
	}
	out.Name = name
	out.SubName = sub
	return out, nil
}
