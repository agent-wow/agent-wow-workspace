package service

import (
	"context"
	"errors"
	"math"
	"testing"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/doumoman/agent-wow-mods/move/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func navigationCliff(mapID uint32, x, y float32) (float32, error) {
	if x >= 14 && x <= 18 && y >= 12 && y <= 28 {
		return 60, nil
	}
	return 30, nil
}

func TestPlanPathDetoursAroundCliff(t *testing.T) {
	s := newWithTerrain(nil, terrainFunc(navigationCliff))
	start := wow.Position{X: 10, Y: 20, Z: 30}
	// A bogus remote Z must not affect the grounded goal or local path.
	goal := wow.Position{X: 30, Y: 20, Z: 999}
	path, err := s.planPath(context.Background(), 0, start, goal)
	if err != nil {
		t.Fatal(err)
	}
	if len(path) < 3 {
		t.Fatalf("path = %+v, want a detour", path)
	}
	last := path[len(path)-1]
	if last.X != goal.X || last.Y != goal.Y || last.Z != 30 {
		t.Fatalf("path goal = %+v, want grounded %+v", last, goal)
	}
	for index := 1; index < len(path); index++ {
		a, b := path[index-1], path[index]
		if math.Abs(float64(b.Z-a.Z)) > maxGroundCorrection {
			t.Fatalf("unsafe path edge %+v -> %+v", a, b)
		}
		mid := lerpPosition(a, b, 0.5)
		z, err := navigationCliff(0, mid.X, mid.Y)
		if err != nil || math.Abs(float64(z-a.Z)) > navigationHeightDelta ||
			math.Abs(float64(b.Z-z)) > navigationHeightDelta {
			t.Fatalf("unsafe path midpoint %+v -> %+v -> %+v", a, mid, b)
		}
	}
}

func TestPlanPathReturnsSafePartialProgress(t *testing.T) {
	s := newWithTerrain(nil, terrainFunc(flatTerrain))
	start := wow.Position{X: 0, Y: 0, Z: 30}
	path, err := s.planPath(context.Background(), 0, start, wow.Position{X: 2000, Y: 0, Z: 30})
	if err != nil {
		t.Fatal(err)
	}
	last := path[len(path)-1]
	if last.X <= start.X || last.X >= 2000 {
		t.Fatalf("partial path = %+v, want progress short of the distant goal", path)
	}
}

func TestPlanPathRejectsIsolatedStart(t *testing.T) {
	s := newWithTerrain(nil, terrainFunc(func(mapID uint32, x, y float32) (float32, error) {
		if x == 0 && y == 0 {
			return 30, nil
		}
		if x >= 100 {
			return 30, nil
		}
		return 60, nil
	}))
	_, err := s.planPath(context.Background(), 0,
		wow.Position{X: 0, Y: 0, Z: 30}, wow.Position{X: 100, Y: 0, Z: 30})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("path error = %v, want FailedPrecondition", err)
	}
}

func TestPlanPathPropagatesContextCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	s := newWithTerrain(nil, terrainFunc(flatTerrain))
	_, err := s.planPath(ctx, 0,
		wow.Position{X: 0, Y: 0, Z: 30}, wow.Position{X: 100, Y: 0, Z: 30})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("path error = %v, want context cancellation", err)
	}
}

func TestNavigateWalksOnlyAShortSafeDetour(t *testing.T) {
	t.Setenv("AGENT_WOW_CHARACTER_GUID", "42")
	session := &recordingSession{firstSent: make(chan struct{}, 1)}
	s := newWithTerrain(session, terrainFunc(navigationCliff))
	if _, err := s.OnPacket(t.Context(), &modv1.WorldPacket{
		Opcode: opcode.SMSGLoginVerifyWorld, Payload: worldPayload(0, 10, 20, 30, 0),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetRunSpeed(t.Context(), &modulev1.RunSpeedRequest{YardsPerSecond: 20}); err != nil {
		t.Fatal(err)
	}
	response, err := s.Navigate(t.Context(), &modulev1.MoveToRequest{X: 30, Y: 20, Z: 999})
	if err != nil {
		t.Fatal(err)
	}
	pos := response.GetPosition()
	distance := math.Hypot(float64(pos.X-10), float64(pos.Y-20))
	if distance <= 0 || distance > navigationTravel+0.01 || pos.Z != 30 {
		t.Fatalf("navigated position = %+v, distance %.2f", pos, distance)
	}
}
