package dbcfile

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// File is a parsed WDBC table: a fixed set of uint32 fields per record plus a
// shared string block. String fields hold a byte offset into that block; zero
// means an empty string.
//
// The layout is the one every 3.3.5a client uses:
//
//	offset 0    char[4]  "WDBC"
//	offset 4    uint32    record count
//	offset 8    uint32    field count
//	offset 12   uint32    record size in bytes (field count * 4)
//	offset 16   uint32    string block size
//	offset 20   records
//	             string block
type File struct {
	fields  int
	records [][]uint32
	strings []byte
}

// ParseFile decodes a WDBC file.
func ParseFile(data []byte) (*File, error) {
	if len(data) < 20 {
		return nil, errors.New("dbc: file shorter than the header")
	}
	if string(data[:4]) != "WDBC" {
		return nil, fmt.Errorf("dbc: unexpected magic %q", data[:4])
	}
	count := binary.LittleEndian.Uint32(data[4:8])
	fields := binary.LittleEndian.Uint32(data[8:12])
	recordSize := binary.LittleEndian.Uint32(data[12:16])
	stringSize := binary.LittleEndian.Uint32(data[16:20])
	if fields == 0 || recordSize != fields*4 {
		return nil, fmt.Errorf("dbc: record size %d does not match %d fields", recordSize, fields)
	}
	body := uint64(count) * uint64(recordSize)
	end := 20 + body
	if uint64(len(data)) < end+uint64(stringSize) {
		return nil, errors.New("dbc: truncated file")
	}
	f := &File{
		fields:  int(fields),
		records: make([][]uint32, count),
		strings: data[end : end+uint64(stringSize)],
	}
	for i := range f.records {
		off := 20 + uint64(i)*uint64(recordSize)
		rec := make([]uint32, fields)
		for j := range rec {
			rec[j] = binary.LittleEndian.Uint32(data[off+uint64(j)*4:])
		}
		f.records[i] = rec
	}
	return f, nil
}

// Len returns the number of records.
func (f *File) Len() int { return len(f.records) }

// Fields returns the number of fields per record.
func (f *File) Fields() int { return f.fields }

// Record returns the raw fields of one record, or nil when the index is out
// of range.
func (f *File) Record(i int) []uint32 {
	if i < 0 || i >= len(f.records) {
		return nil
	}
	return f.records[i]
}

// Uint32 returns one numeric field.
func (f *File) Uint32(record, field int) uint32 {
	rec := f.Record(record)
	if rec == nil || field < 0 || field >= len(rec) {
		return 0
	}
	return rec[field]
}

// String resolves a string field. The field holds an offset into the string
// block; an offset of zero is an empty string.
func (f *File) String(record, field int) string {
	off := f.Uint32(record, field)
	if off == 0 || int(off) >= len(f.strings) {
		return ""
	}
	rest := f.strings[off:]
	for i, b := range rest {
		if b == 0 {
			return string(rest[:i])
		}
	}
	return string(rest)
}
