package wow

import "encoding/binary"

// EncodeGameObjectUse builds CMSG_GAMEOBJ_USE. The server enforces distance
// and object-specific requirements before calling GameObject::Use.
func EncodeGameObjectUse(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}
