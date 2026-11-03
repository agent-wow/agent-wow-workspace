package wow

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"math"
	"testing"
)

func appendFloat(b []byte, v float32) []byte {
	return binary.LittleEndian.AppendUint32(b, math.Float32bits(v))
}

// selfCreate builds a minimal player create block shaped like the reference
// fixtures in wrathplus: count, kind, packed GUID, TypeID, movement block, fields.
func selfCreate() []byte {
	var b []byte
	b = binary.LittleEndian.AppendUint32(b, 1) // block count
	b = append(b, UpdateCreate2)
	b = append(b, 0x0f, 0x04, 0x03, 0x02, 0x01) // packed GUID 0x01020304
	b = append(b, byte(ObjectTypePlayer))
	b = binary.LittleEndian.AppendUint16(b, UpdateFlagSelf|UpdateFlagLiving|UpdateFlagLowGUID)
	b = binary.LittleEndian.AppendUint32(b, 0) // movement flags
	b = binary.LittleEndian.AppendUint16(b, 0) // movement flags2
	b = binary.LittleEndian.AppendUint32(b, 55)
	b = appendFloat(b, 1)
	b = appendFloat(b, 2)
	b = appendFloat(b, 3)
	b = appendFloat(b, 4)
	b = binary.LittleEndian.AppendUint32(b, 0) // fall time
	for _, speed := range []float32{2.5, 7, 4.5, 4.72, 2.5, 7, 4.5, math.Pi, math.Pi} {
		b = appendFloat(b, speed)
	}
	b = binary.LittleEndian.AppendUint32(b, 0x2f) // low GUID

	values := map[uint32]uint32{
		0:    0x01020304,
		1:    0,
		0x18: 100,
		0x20: 100,
		0x36: 1,
		0x43: 49,
		0x96: 0x08,
	}
	const blocks = 5
	masks := make([]uint32, blocks)
	ordered := []uint32{0, 1, 0x18, 0x20, 0x36, 0x43, 0x96}
	for _, index := range ordered {
		masks[index/32] |= 1 << (index % 32)
	}
	b = append(b, blocks)
	for _, mask := range masks {
		b = binary.LittleEndian.AppendUint32(b, mask)
	}
	for _, index := range ordered {
		b = binary.LittleEndian.AppendUint32(b, values[index])
	}
	return b
}

func TestParseUpdateObjectPlayerCreate(t *testing.T) {
	u, err := ParseUpdateObject(selfCreate())
	if err != nil {
		t.Fatal(err)
	}
	if len(u.Objects) != 1 {
		t.Fatalf("objects = %d, want 1", len(u.Objects))
	}
	obj := u.Objects[0]
	if obj.GUID != 0x01020304 || obj.Type != ObjectTypePlayer {
		t.Fatalf("guid/type = %x/%v", obj.GUID, obj.Type)
	}
	if !obj.HasPos || obj.Position.Distance2D(Position{X: 1, Y: 2}) > 1e-6 {
		t.Fatalf("position = %v (hasPos=%v)", obj.Position, obj.HasPos)
	}
	if got, _ := obj.Fields.Uint32(UnitFieldHealth); got != 100 {
		t.Fatalf("health = %d", got)
	}
	if got, _ := obj.Fields.Uint32(UnitFieldLevel); got != 1 {
		t.Fatalf("level = %d", got)
	}
}

func TestParseCompressedUpdateObject(t *testing.T) {
	raw := selfCreate()
	var compressed bytes.Buffer
	zw := zlib.NewWriter(&compressed)
	if _, err := zw.Write(raw); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	body := binary.LittleEndian.AppendUint32(nil, uint32(len(raw)))
	body = append(body, compressed.Bytes()...)
	u, err := ParseCompressedUpdateObject(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(u.Objects) != 1 {
		t.Fatalf("objects = %d, want 1", len(u.Objects))
	}
}

func TestParseUpdateObjectRejectsTruncated(t *testing.T) {
	body := selfCreate()
	if _, err := ParseUpdateObject(body[:len(body)-3]); err == nil {
		t.Fatal("expected truncation error")
	}
}
