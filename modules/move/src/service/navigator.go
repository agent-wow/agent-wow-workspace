package service

import (
	"container/heap"
	"context"
	"math"

	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/move/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	navigationGrid        = 4.0
	navigationTravel      = 12.0
	navigationHeightDelta = 6.0
	maxNavigationNodes    = 30000
	maxNavigationRadius   = 256
)

type navigationNode struct {
	x int
	y int
}

type navigationItem struct {
	node navigationNode
	g    float64
	f    float64
}

type navigationQueue []navigationItem

func (q navigationQueue) Len() int { return len(q) }
func (q navigationQueue) Less(i, j int) bool {
	if q[i].f == q[j].f {
		return q[i].g > q[j].g
	}
	return q[i].f < q[j].f
}
func (q navigationQueue) Swap(i, j int) { q[i], q[j] = q[j], q[i] }
func (q *navigationQueue) Push(value any) {
	*q = append(*q, value.(navigationItem))
}
func (q *navigationQueue) Pop() any {
	old := *q
	value := old[len(old)-1]
	*q = old[:len(old)-1]
	return value
}

// Navigate follows a terrain-safe path for at most twelve yards. The caller
// retains route strategy and can reissue it as the world changes. Unlike
// MoveTo, the destination Z is only a hint: the goal is grounded before path
// planning, so a distant terrace height cannot poison every local segment.
func (s *Service) Navigate(ctx context.Context, req *modulev1.MoveToRequest) (*modulev1.PositionResponse, error) {
	if !finite(req.GetX()) || !finite(req.GetY()) || !finite(req.GetZ()) {
		return nil, status.Error(codes.InvalidArgument, "coordinates must be finite")
	}

	s.moveMu.Lock()
	defer s.moveMu.Unlock()
	s.mu.RLock()
	start := s.pos
	hasPos := s.hasPos
	epoch := s.worldEpoch
	mapID := s.mapID
	hasMap := s.hasMap
	s.mu.RUnlock()
	if !hasPos || !hasMap {
		return nil, status.Error(codes.FailedPrecondition, "position and map unknown: wait for the character to enter the world")
	}

	goal := wow.Position{X: req.GetX(), Y: req.GetY(), Z: req.GetZ(), O: start.O}
	path, err := s.planPath(ctx, mapID, start, goal)
	if err != nil {
		return nil, err
	}

	traveled := 0.0
	var response *modulev1.PositionResponse
	for _, end := range path[1:] {
		distance := math.Hypot(float64(end.X-start.X), float64(end.Y-start.Y))
		if traveled+distance > navigationTravel+0.001 {
			break
		}
		actual, speed, currentEpoch, currentMap, worldCh, err := s.startMove()
		if err != nil {
			return nil, err
		}
		if currentEpoch != epoch || currentMap != mapID {
			s.endMove()
			return nil, status.Error(codes.Aborted, "world changed during navigation")
		}
		dx := float64(end.X - actual.X)
		dy := float64(end.Y - actual.Y)
		end.O = float32(wow.NormalizeOrientation(math.Atan2(dy, dx)))
		actual.O = end.O
		if err := s.sendForEpoch(ctx, epoch, opcode.MSGMoveSetFacing, 0, actual); err != nil {
			s.endMove()
			return nil, err
		}
		response, err = s.walk(ctx, epoch, mapID, worldCh, actual, end, wow.MovementFlagForward, opcode.MSGMoveStartForward, speed)
		if err != nil {
			return nil, err
		}
		start = end
		traveled += distance
	}
	if response == nil {
		return &modulev1.PositionResponse{Position: toProto(start)}, nil
	}
	return response, nil
}

// planPath uses bounded A* over the same outdoor heightfield used by movement
// heartbeats. If a distant goal is outside the search budget, it returns a
// safe partial path toward the closest explored node rather than inventing a
// straight segment through a cliff.
func (s *Service) planPath(ctx context.Context, mapID uint32, start, goal wow.Position) ([]wow.Position, error) {
	if s.terrain == nil {
		return nil, status.Error(codes.FailedPrecondition, "terrain sampler is not configured")
	}
	groundStart, err := s.sampleGround(mapID, start)
	if err != nil {
		return nil, err
	}
	groundGoal, err := s.sampleGround(mapID, goal)
	if err != nil {
		return nil, err
	}
	startNode := navigationNode{}
	goalNode := navigationNode{
		x: int(math.Round(float64(goal.X-start.X) / navigationGrid)),
		y: int(math.Round(float64(goal.Y-start.Y) / navigationGrid)),
	}
	if startNode == goalNode && math.Hypot(float64(goal.X-start.X), float64(goal.Y-start.Y)) < 0.05 {
		return []wow.Position{groundStart}, nil
	}

	positions := map[navigationNode]wow.Position{startNode: groundStart}
	scores := map[navigationNode]float64{startNode: 0}
	cameFrom := make(map[navigationNode]navigationNode)
	closed := make(map[navigationNode]bool)
	queue := &navigationQueue{}
	heap.Init(queue)
	heap.Push(queue, navigationItem{node: startNode, f: navigationHeuristic(startNode, goalNode)})
	best := startNode
	bestDistance := navigationHeuristic(startNode, goalNode)
	reached := false

	directions := [...]navigationNode{{1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}}
	for queue.Len() > 0 && len(closed) < maxNavigationNodes {
		if len(closed)%256 == 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			default:
			}
		}
		item := heap.Pop(queue).(navigationItem)
		current := item.node
		if closed[current] || item.g != scores[current] {
			continue
		}
		closed[current] = true
		distance := navigationHeuristic(current, goalNode)
		if distance < bestDistance {
			best, bestDistance = current, distance
		}
		if current == goalNode {
			best, reached = current, true
			break
		}

		for _, direction := range directions {
			next := navigationNode{x: current.x + direction.x, y: current.y + direction.y}
			if closed[next] || !navigationWithinBounds(next, goalNode) {
				continue
			}
			nextPosition, ok := s.navigationPosition(mapID, start, next, positions)
			if !ok || !s.navigationSegmentSafe(mapID, positions[current], nextPosition) {
				continue
			}
			step := navigationGrid * math.Hypot(float64(direction.x), float64(direction.y))
			step *= 1 + math.Abs(float64(nextPosition.Z-positions[current].Z))*0.05
			candidate := scores[current] + step
			old, seen := scores[next]
			if seen && candidate >= old {
				continue
			}
			cameFrom[next] = current
			scores[next] = candidate
			heap.Push(queue, navigationItem{
				node: next, g: candidate,
				f: candidate + navigationHeuristic(next, goalNode),
			})
		}
	}
	if best == startNode {
		return nil, status.Error(codes.FailedPrecondition, "no terrain-safe navigation segment toward destination")
	}

	nodes := []navigationNode{best}
	for nodes[len(nodes)-1] != startNode {
		parent, ok := cameFrom[nodes[len(nodes)-1]]
		if !ok {
			return nil, status.Errorf(codes.Internal, "navigation path is disconnected at %+v", nodes[len(nodes)-1])
		}
		nodes = append(nodes, parent)
	}
	path := make([]wow.Position, len(nodes))
	for index := range nodes {
		path[len(nodes)-1-index] = positions[nodes[index]]
	}
	if reached && s.navigationSegmentSafe(mapID, path[len(path)-1], groundGoal) {
		path = append(path, groundGoal)
	}
	return path, nil
}

func navigationWithinBounds(node, goal navigationNode) bool {
	limitX := min(max(abs(goal.x)+24, 24), maxNavigationRadius)
	limitY := min(max(abs(goal.y)+24, 24), maxNavigationRadius)
	return abs(node.x) <= limitX && abs(node.y) <= limitY
}

func navigationHeuristic(a, b navigationNode) float64 {
	return navigationGrid * math.Hypot(float64(a.x-b.x), float64(a.y-b.y))
}

func (s *Service) navigationPosition(mapID uint32, origin wow.Position, node navigationNode, cache map[navigationNode]wow.Position) (wow.Position, bool) {
	if pos, ok := cache[node]; ok {
		return pos, true
	}
	pos := wow.Position{
		X: origin.X + float32(float64(node.x)*navigationGrid),
		Y: origin.Y + float32(float64(node.y)*navigationGrid),
	}
	z, err := s.terrain.GroundHeight(mapID, pos.X, pos.Y)
	if err != nil {
		return pos, false
	}
	pos.Z = z
	cache[node] = pos
	return pos, true
}

func (s *Service) navigationSegmentSafe(mapID uint32, a, b wow.Position) bool {
	mid := lerpPosition(a, b, 0.5)
	midZ, err := s.terrain.GroundHeight(mapID, mid.X, mid.Y)
	if err != nil || math.Abs(float64(midZ-a.Z)) > navigationHeightDelta ||
		math.Abs(float64(b.Z-midZ)) > navigationHeightDelta ||
		math.Abs(float64(b.Z-a.Z)) > maxGroundCorrection {
		return false
	}
	return true
}

func abs(value int) int {
	if value < 0 {
		return -value
	}
	return value
}
