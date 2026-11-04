// terrain_route finds a low-slope candidate corridor on read-only client maps.
// The output is route content only: every candidate still needs live validation.
package main

import (
	"container/heap"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"

	"github.com/doumoman/agent-wow-mods/wow"
)

type node struct{ x, y int }
type point struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	Z float64 `json:"z"`
}
type item struct {
	n     node
	score float64
	index int
}
type queue []*item

func (q queue) Len() int           { return len(q) }
func (q queue) Less(i, j int) bool { return q[i].score < q[j].score }
func (q queue) Swap(i, j int)      { q[i], q[j] = q[j], q[i]; q[i].index = i; q[j].index = j }
func (q *queue) Push(v any)        { *q = append(*q, v.(*item)) }
func (q *queue) Pop() any {
	old := *q
	v := old[len(old)-1]
	*q = old[:len(old)-1]
	return v
}

func main() {
	mapID := flag.Uint("map", 1, "map id")
	startX := flag.Float64("sx", 0, "start X")
	startY := flag.Float64("sy", 0, "start Y")
	goalX := flag.Float64("gx", 0, "goal X")
	goalY := flag.Float64("gy", 0, "goal Y")
	grid := flag.Float64("grid", 5, "pathfinding grid spacing")
	margin := flag.Float64("margin", 160, "search margin around the endpoints")
	dir := flag.String("maps", "../../data/client-data/terrain-v20/maps", "map tile directory")
	flag.Parse()
	if *grid < 2 || *margin < 0 {
		fmt.Fprintln(os.Stderr, "grid must be at least 2 and margin non-negative")
		os.Exit(2)
	}
	terrain := wow.NewMapTerrain(*dir)
	height := func(x, y float64) (float64, error) {
		z, err := terrain.GroundHeight(uint32(*mapID), float32(x), float32(y))
		return float64(z), err
	}
	start := node{int(math.Round(*startX / *grid)), int(math.Round(*startY / *grid))}
	goal := node{int(math.Round(*goalX / *grid)), int(math.Round(*goalY / *grid))}
	minX := int(math.Floor((math.Min(*startX, *goalX) - *margin) / *grid))
	maxX := int(math.Ceil((math.Max(*startX, *goalX) + *margin) / *grid))
	minY := int(math.Floor((math.Min(*startY, *goalY) - *margin) / *grid))
	maxY := int(math.Ceil((math.Max(*startY, *goalY) + *margin) / *grid))
	world := func(n node) (float64, float64) {
		return float64(n.x) * *grid, float64(n.y) * *grid
	}
	heights := map[node]float64{}
	var zAt func(node) (float64, bool)
	zAt = func(n node) (float64, bool) {
		if z, ok := heights[n]; ok {
			return z, true
		}
		x, y := world(n)
		z, err := height(x, y)
		if err != nil {
			return 0, false
		}
		heights[n] = z
		return z, true
	}
	walkable := func(a, b node) bool {
		ax, ay := world(a)
		bx, by := world(b)
		length := math.Hypot(bx-ax, by-ay)
		steps := int(math.Ceil(length / 2))
		px, py := ax, ay
		pz, ok := zAt(a)
		if !ok {
			return false
		}
		for i := 1; i <= steps; i++ {
			f := float64(i) / float64(steps)
			x := ax + (bx-ax)*f
			y := ay + (by-ay)*f
			z, err := height(x, y)
			if err != nil || math.Abs(z-pz) > 3.5 {
				return false
			}
			// A diagonal may not cut across a blocked corner.
			if i < steps && math.Hypot(x-px, y-py) > 2.1 {
				return false
			}
			px, py, pz = x, y, z
		}
		return true
	}
	distance := func(a, b node) float64 {
		return math.Hypot(float64(a.x-b.x), float64(a.y-b.y)) * *grid
	}
	open := &queue{}
	heap.Init(open)
	gScore := map[node]float64{start: 0}
	cameFrom := map[node]node{}
	heap.Push(open, &item{n: start, score: distance(start, goal)})
	closed := map[node]bool{}
	found := false
	for open.Len() > 0 {
		current := heap.Pop(open).(*item).n
		if closed[current] {
			continue
		}
		if current == goal {
			found = true
			break
		}
		closed[current] = true
		for dy := -1; dy <= 1; dy++ {
			for dx := -1; dx <= 1; dx++ {
				if dx == 0 && dy == 0 {
					continue
				}
				next := node{current.x + dx, current.y + dy}
				if next.x < minX || next.x > maxX || next.y < minY || next.y > maxY || closed[next] {
					continue
				}
				if !walkable(current, next) {
					continue
				}
				z0, _ := zAt(current)
				z1, _ := zAt(next)
				slope := math.Abs(z1-z0) / distance(current, next)
				tentative := gScore[current] + distance(current, next)*(1+2*slope*slope)
				old, seen := gScore[next]
				if !seen || tentative < old {
					gScore[next] = tentative
					cameFrom[next] = current
					heap.Push(open, &item{n: next, score: tentative + distance(next, goal)})
				}
			}
		}
	}
	if !found {
		fmt.Fprintln(os.Stderr, "no candidate corridor within the requested bounds")
		os.Exit(1)
	}
	path := []node{goal}
	for path[len(path)-1] != start {
		path = append(path, cameFrom[path[len(path)-1]])
	}
	for i, j := 0, len(path)-1; i < j; i, j = i+1, j-1 {
		path[i], path[j] = path[j], path[i]
	}
	// Greedy line-of-sight reduction keeps route files compact without crossing
	// a height discontinuity. Live movement remains the final authority.
	simplified := []node{path[0]}
	for i := 0; i < len(path)-1; {
		j := len(path) - 1
		for j > i+1 && !walkable(path[i], path[j]) {
			j--
		}
		simplified = append(simplified, path[j])
		i = j
	}
	points := make([]point, 0, len(simplified))
	for _, n := range simplified {
		x, y := world(n)
		z, _ := zAt(n)
		points = append(points, point{X: x, Y: y, Z: z})
	}
	// Report the exact requested endpoints separately; grid rounding is not
	// presented as an observed NPC position.
	out := map[string]any{
		"source":          "terrain-v20 low-slope candidate; live validation required",
		"requested_start": point{X: *startX, Y: *startY},
		"requested_goal":  point{X: *goalX, Y: *goalY},
		"waypoints":       points,
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
