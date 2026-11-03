// Package events owns the bounded, non-destructive event ring shared by the
// gameplay modules.
//
// Poll never consumes events and never advances a consumer position. Each
// event carries a monotonically increasing cursor; a poll with
// after_cursor=N returns only events with a greater cursor. When the ring
// overflows, the evicted event's domain is counted in Dropped, and the count
// stays visible across polls so a consumer that crashed before persisting the
// signal can still force a domain re-read.
package events

import (
	"encoding/json"
	"sync"
)

// Event is one decoded server event. Payload is opaque to the ring.
type Event struct {
	Cursor  uint64          `json:"cursor"`
	ID      string          `json:"id"`
	Domain  string          `json:"domain"`
	Seq     uint64          `json:"seq"`
	Payload json.RawMessage `json:"payload,omitempty"`
}

// PollResult is a non-destructive snapshot of the ring after a cursor.
type PollResult struct {
	Events     []Event           `json:"events"`
	NextCursor uint64            `json:"next_cursor"`
	Dropped    map[string]uint64 `json:"dropped"`
	DroppedAny bool              `json:"dropped_any"`
	Epoch      uint64            `json:"epoch"`
}

// Ring is a concurrency-safe bounded event ring.
type Ring struct {
	mu       sync.Mutex
	capacity int
	epoch    uint64
	cursor   uint64
	events   []Event
	dropped  map[string]uint64
}

// NewRing creates a ring with a bounded capacity. A non-positive capacity is
// treated as one so overflow accounting remains well defined.
func NewRing(capacity int, epoch uint64) *Ring {
	if capacity < 1 {
		capacity = 1
	}
	return &Ring{
		capacity: capacity,
		epoch:    epoch,
		dropped:  make(map[string]uint64),
	}
}

// Push appends an event and returns its cursor.
func (r *Ring) Push(domain, id string, payload json.RawMessage) uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.cursor++
	event := Event{
		Cursor:  r.cursor,
		ID:      id,
		Domain:  domain,
		Seq:     r.cursor,
		Payload: payload,
	}
	if len(r.events) >= r.capacity {
		evicted := r.events[0]
		r.events = r.events[1:]
		r.dropped[evicted.Domain]++
	}
	r.events = append(r.events, event)
	return event.Cursor
}

// Poll returns events with Cursor > afterCursor without removing them.
func (r *Ring) Poll(afterCursor uint64, max int) PollResult {
	r.mu.Lock()
	defer r.mu.Unlock()

	result := PollResult{
		Events:     []Event{},
		NextCursor: afterCursor,
		Dropped:    make(map[string]uint64, len(r.dropped)),
		Epoch:      r.epoch,
	}
	for domain, count := range r.dropped {
		result.Dropped[domain] = count
		if count > 0 {
			result.DroppedAny = true
		}
	}
	if max <= 0 {
		return result
	}

	start := -1
	for i := range r.events {
		if r.events[i].Cursor > afterCursor {
			start = i
			break
		}
	}
	if start < 0 {
		// Everything the ring still has was already seen. If the caller's
		// cursor is behind the production cursor (events were evicted), jump
		// over the gap; Dropped tells it which domains need a full re-read.
		if afterCursor < r.cursor {
			result.NextCursor = r.cursor
		}
		return result
	}

	for i := start; i < len(r.events) && len(result.Events) < max; i++ {
		result.Events = append(result.Events, r.events[i])
	}
	if len(result.Events) > 0 {
		result.NextCursor = result.Events[len(result.Events)-1].Cursor
	}
	return result
}

// Cursor returns the most recently assigned event cursor.
func (r *Ring) Cursor() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.cursor
}

// Epoch returns the current session epoch.
func (r *Ring) Epoch() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.epoch
}

// Dropped returns the cumulative per-domain drop counts.
func (r *Ring) Dropped() map[string]uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]uint64, len(r.dropped))
	for domain, count := range r.dropped {
		out[domain] = count
	}
	return out
}

// Reset starts a new session epoch with an empty ring and cursor zero.
func (r *Ring) Reset(epoch uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.epoch = epoch
	r.cursor = 0
	r.events = nil
	r.dropped = make(map[string]uint64)
}
