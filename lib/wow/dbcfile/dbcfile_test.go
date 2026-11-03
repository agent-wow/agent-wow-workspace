package dbcfile

import (
	"encoding/binary"
	"testing"
)

func TestParseFile(t *testing.T) {
	// Two records, two fields each, one shared string block.
	var b []byte
	b = append(b, "WDBC"...)
	b = binary.LittleEndian.AppendUint32(b, 2)  // record count
	b = binary.LittleEndian.AppendUint32(b, 2)  // field count
	b = binary.LittleEndian.AppendUint32(b, 8)  // record size
	b = binary.LittleEndian.AppendUint32(b, 12) // string block size
	b = binary.LittleEndian.AppendUint32(b, 7)  // record 0, field 0
	b = binary.LittleEndian.AppendUint32(b, 1)  // record 0, field 1 (string offset)
	b = binary.LittleEndian.AppendUint32(b, 9)  // record 1, field 0
	b = binary.LittleEndian.AppendUint32(b, 0)  // record 1, field 1 (empty string)
	b = append(b, "\x00Alpha\x00Beta\x00"...)

	file, err := ParseFile(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if file.Len() != 2 || file.Fields() != 2 {
		t.Fatalf("shape: %d records, %d fields", file.Len(), file.Fields())
	}
	if got := file.Uint32(1, 0); got != 9 {
		t.Fatalf("record 1 field 0: %d", got)
	}
	if got := file.String(0, 1); got != "Alpha" {
		t.Fatalf("record 0 field 1: %q", got)
	}
	if got := file.String(1, 1); got != "" {
		t.Fatalf("record 1 field 1: %q", got)
	}
}

func TestParseFileRejectsBadInput(t *testing.T) {
	if _, err := ParseFile([]byte("nope")); err == nil {
		t.Fatal("expected an error for a short file")
	}
	bad := append([]byte("XXXX"), make([]byte, 16)...)
	if _, err := ParseFile(bad); err == nil {
		t.Fatal("expected an error for a bad magic")
	}
}
