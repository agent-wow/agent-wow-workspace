package wow

import (
	"fmt"
	"math"
)

// GUID is a WoW object identifier. The low bits encode the object type and
// the high bits identify the entry; modules treat it as opaque.
type GUID uint64

// Position is a world position in yards plus the facing angle in radians.
type Position struct {
	X float32
	Y float32
	Z float32
	O float32
}

// Distance2D returns the planar distance between two positions.
func (p Position) Distance2D(q Position) float64 {
	dx := float64(p.X - q.X)
	dy := float64(p.Y - q.Y)
	return math.Sqrt(dx*dx + dy*dy)
}

func (p Position) String() string {
	return fmt.Sprintf("(%.2f, %.2f, %.2f, o=%.2f)", p.X, p.Y, p.Z, p.O)
}
