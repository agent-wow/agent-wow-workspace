package wow

import "testing"

func TestDistance2D(t *testing.T) {
	a := Position{X: 0, Y: 0}
	b := Position{X: 3, Y: 4}
	if got := a.Distance2D(b); got < 4.99 || got > 5.01 {
		t.Fatalf("Distance2D = %v, want 5", got)
	}
}
