package events

import (
	"encoding/json"
	"sync"
	"testing"
)

func payload(value string) json.RawMessage {
	data, err := json.Marshal(map[string]string{"v": value})
	if err != nil {
		panic(err)
	}
	return data
}

func TestPollIsNonDestructive(t *testing.T) {
	ring := NewRing(8, 1)
	ring.Push("sense", "e1", payload("a"))
	ring.Push("sense", "e2", payload("b"))

	first := ring.Poll(0, 10)
	if len(first.Events) != 2 {
		t.Fatalf("first poll = %d events, want 2", len(first.Events))
	}
	if first.NextCursor != 2 {
		t.Fatalf("next cursor = %d, want 2", first.NextCursor)
	}

	second := ring.Poll(0, 10)
	if len(second.Events) != 2 {
		t.Fatalf("second poll = %d events, want 2", len(second.Events))
	}
	for i := range first.Events {
		if first.Events[i].ID != second.Events[i].ID {
			t.Fatalf("poll consumed events: %q != %q",
				first.Events[i].ID, second.Events[i].ID)
		}
	}
}

func TestCursorOrderingAndPaging(t *testing.T) {
	ring := NewRing(8, 1)
	for _, id := range []string{"e1", "e2", "e3", "e4", "e5"} {
		ring.Push("sense", id, payload(id))
	}

	page := ring.Poll(2, 2)
	if len(page.Events) != 2 {
		t.Fatalf("page size = %d, want 2", len(page.Events))
	}
	if page.Events[0].Cursor != 3 || page.Events[1].Cursor != 4 {
		t.Fatalf("cursors = %d,%d, want 3,4",
			page.Events[0].Cursor, page.Events[1].Cursor)
	}
	if page.NextCursor != 4 {
		t.Fatalf("next cursor = %d, want 4", page.NextCursor)
	}

	next := ring.Poll(page.NextCursor, 2)
	if len(next.Events) != 1 || next.Events[0].ID != "e5" {
		t.Fatalf("next page = %#v, want e5", next.Events)
	}
	if next.NextCursor != 5 {
		t.Fatalf("next cursor = %d, want 5", next.NextCursor)
	}
}

func TestOverflowReportsPerDomainDroppedCounts(t *testing.T) {
	ring := NewRing(2, 1)
	ring.Push("sense", "s1", payload("s1"))
	ring.Push("sense", "s2", payload("s2"))
	ring.Push("move", "m1", payload("m1")) // evicts s1

	result := ring.Poll(0, 10)
	if len(result.Events) != 2 {
		t.Fatalf("events = %d, want 2 (bounded ring)", len(result.Events))
	}
	if result.Events[0].ID != "s2" || result.Events[1].ID != "m1" {
		t.Fatalf("events = %q,%q, want s2,m1",
			result.Events[0].ID, result.Events[1].ID)
	}
	if result.Dropped["sense"] != 1 {
		t.Fatalf("sense dropped = %d, want 1", result.Dropped["sense"])
	}

	// Poll does not clear the cumulative drop signal: a consumer that crashed
	// before persisting the signal must observe it again.
	again := ring.Poll(0, 10)
	if again.Dropped["sense"] != 1 {
		t.Fatalf("repeated drop signal = %d, want 1", again.Dropped["sense"])
	}
}

func TestOverflowTracksEachDomain(t *testing.T) {
	ring := NewRing(2, 1)
	ring.Push("sense", "s1", payload("s1"))
	ring.Push("sense", "s2", payload("s2"))
	ring.Push("move", "m1", payload("m1"))   // drop sense
	ring.Push("combat", "c1", payload("c1")) // drop sense

	result := ring.Poll(0, 10)
	if result.Dropped["sense"] != 2 {
		t.Fatalf("sense dropped = %d, want 2", result.Dropped["sense"])
	}
	if result.Dropped["move"] != 0 {
		t.Fatalf("move dropped = %d, want 0", result.Dropped["move"])
	}
}

func TestEpochResetClearsRingAndCursor(t *testing.T) {
	ring := NewRing(4, 1)
	ring.Push("sense", "e1", payload("e1"))
	if got := ring.Poll(0, 10); len(got.Events) != 1 {
		t.Fatalf("pre-reset events = %d, want 1", len(got.Events))
	}

	ring.Reset(2)
	result := ring.Poll(0, 10)
	if ring.Epoch() != 2 {
		t.Fatalf("epoch = %d, want 2", ring.Epoch())
	}
	if len(result.Events) != 0 {
		t.Fatalf("events after reset = %d, want 0", len(result.Events))
	}
	if ring.Cursor() != 0 {
		t.Fatalf("cursor after reset = %d, want 0", ring.Cursor())
	}
}

func TestConcurrentPushAndPoll(t *testing.T) {
	ring := NewRing(64, 1)
	var wg sync.WaitGroup
	for writer := 0; writer < 4; writer++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				ring.Push("sense", "e", payload("x"))
				ring.Poll(0, 8)
			}
		}()
	}
	wg.Wait()
	if ring.Cursor() != 400 {
		t.Fatalf("cursor = %d, want 400", ring.Cursor())
	}
}
