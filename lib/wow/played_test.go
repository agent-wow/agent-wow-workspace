package wow

import (
	"encoding/binary"
	"reflect"
	"testing"
)

func TestPlayedTimeCodec(t *testing.T) {
	if got := EncodePlayedTime(); !reflect.DeepEqual(got, []byte{0}) {
		t.Fatalf("request = %x", got)
	}

	body := binary.LittleEndian.AppendUint32(nil, 123456)
	body = binary.LittleEndian.AppendUint32(body, 7890)
	body = append(body, 1)
	got, err := ParsePlayedTime(body)
	if err != nil {
		t.Fatal(err)
	}
	if got.Total != 123456 || got.Level != 7890 || !got.Trigger {
		t.Fatalf("played time = %+v", got)
	}

	// Two counters without the trigger byte are still usable.
	got, err = ParsePlayedTime(body[:8])
	if err != nil {
		t.Fatal(err)
	}
	if got.Total != 123456 || got.Level != 7890 || got.Trigger {
		t.Fatalf("short played time = %+v", got)
	}

	if _, err := ParsePlayedTime(body[:6]); err == nil {
		t.Fatal("truncated payload did not error")
	}
}
