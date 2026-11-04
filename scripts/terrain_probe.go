// terrain_probe samples the same read-only AzerothCore map terrain used by move.
// Run from modules/move: go run ../../scripts/terrain_probe.go -x -347.6 -y -4317.2
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/doumoman/agent-wow-mods/wow"
)

func main() {
	mapID := flag.Uint("map", 1, "map id")
	x := flag.Float64("x", 0, "world X")
	y := flag.Float64("y", 0, "world Y")
	radius := flag.Float64("radius", 0, "optional sample radius in yards")
	step := flag.Float64("step", 5, "grid spacing in yards")
	dir := flag.String("maps", "../../data/client-data/terrain-v20/maps", "map tile directory")
	flag.Parse()
	if *step <= 0 || *radius < 0 {
		fmt.Fprintln(os.Stderr, "step must be positive and radius non-negative")
		os.Exit(2)
	}
	terrain := wow.NewMapTerrain(*dir)
	sample := func(px, py float64) {
		z, err := terrain.GroundHeight(uint32(*mapID), float32(px), float32(py))
		if err != nil {
			fmt.Printf("%.2f,%.2f,error,%v\n", px, py, err)
			return
		}
		fmt.Printf("%.2f,%.2f,%.4f\n", px, py, z)
	}
	if *radius == 0 {
		sample(*x, *y)
		return
	}
	for dx := -*radius; dx <= *radius; dx += *step {
		for dy := -*radius; dy <= *radius; dy += *step {
			sample(*x+dx, *y+dy)
		}
	}
}
