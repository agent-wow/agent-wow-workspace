package wow

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sync"
)

const (
	terrainGridSize   = 533.3333333333333
	terrainResolution = 128
	terrainV9Size     = 129 * 129
	terrainV8Size     = 128 * 128

	terrainHeightNone   = 0x0001
	terrainHeightUint16 = 0x0002
	terrainHeightUint8  = 0x0004
)

// ErrTerrainUnavailable means that no reliable terrain height exists for a
// world position. Callers should stop rather than invent a Z coordinate.
var ErrTerrainUnavailable = errors.New("terrain height unavailable")

// MapTerrain reads AzerothCore map-format v9 tiles and caches decoded height
// grids. The coordinate transform and triangle interpolation match
// AzerothCore's GridTerrainData, so clients sample the same outdoor terrain as
// the server. It does not include VMap objects such as bridges and buildings.
type MapTerrain struct {
	dir string

	mu    sync.RWMutex
	tiles map[terrainTileKey]terrainTileResult
}

type terrainTileKey struct {
	mapID uint32
	x     int
	y     int
}

type terrainTileResult struct {
	tile *terrainTile
	err  error
}

type terrainTile struct {
	gridHeight float32
	v9         []float32
	v8         []float32
	holes      []uint16
}

func NewMapTerrain(dir string) *MapTerrain {
	return &MapTerrain{dir: dir, tiles: make(map[terrainTileKey]terrainTileResult)}
}

// GroundHeight returns the outdoor terrain height at a world position.
func (t *MapTerrain) GroundHeight(mapID uint32, x, y float32) (float32, error) {
	if t == nil || t.dir == "" || mapID > 999 || !finiteCoordinate(x) || !finiteCoordinate(y) {
		return 0, fmt.Errorf("%w: invalid terrain source or coordinates", ErrTerrainUnavailable)
	}
	gx := int(32 - float64(x)/terrainGridSize)
	gy := int(32 - float64(y)/terrainGridSize)
	if gx < 0 || gx >= 64 || gy < 0 || gy >= 64 {
		return 0, fmt.Errorf("%w: coordinates outside map %d", ErrTerrainUnavailable, mapID)
	}
	tile, err := t.loadTile(terrainTileKey{mapID: mapID, x: gx, y: gy})
	if err != nil {
		return 0, err
	}
	return tile.height(x, y)
}

func finiteCoordinate(v float32) bool {
	return !math.IsNaN(float64(v)) && !math.IsInf(float64(v), 0)
}

func (t *MapTerrain) loadTile(key terrainTileKey) (*terrainTile, error) {
	t.mu.RLock()
	result, ok := t.tiles[key]
	t.mu.RUnlock()
	if ok {
		return result.tile, result.err
	}

	t.mu.Lock()
	defer t.mu.Unlock()
	if result, ok = t.tiles[key]; ok {
		return result.tile, result.err
	}
	name := fmt.Sprintf("%03d%02d%02d.map", key.mapID, key.x, key.y)
	path := filepath.Join(t.dir, name)
	data, err := os.ReadFile(path)
	if err == nil {
		result.tile, err = parseTerrainTile(data)
	}
	if err != nil {
		err = fmt.Errorf("%w: %s: %v", ErrTerrainUnavailable, path, err)
	}
	result.err = err
	t.tiles[key] = result
	return result.tile, result.err
}

func parseTerrainTile(data []byte) (*terrainTile, error) {
	const fileHeaderSize = 44
	if len(data) < fileHeaderSize || string(data[:4]) != "MAPS" {
		return nil, errors.New("invalid map file header")
	}
	if version := binary.LittleEndian.Uint32(data[4:8]); version != 9 {
		return nil, fmt.Errorf("unsupported map version %d", version)
	}
	heightOffset := binary.LittleEndian.Uint32(data[20:24])
	heightSize := binary.LittleEndian.Uint32(data[24:28])
	if heightOffset == 0 || heightSize < 16 {
		return nil, errors.New("map tile has no height data")
	}
	heightData, err := terrainSection(data, heightOffset, heightSize)
	if err != nil {
		return nil, fmt.Errorf("invalid height section: %w", err)
	}
	if string(heightData[:4]) != "MHGT" {
		return nil, errors.New("invalid height section magic")
	}

	flags := binary.LittleEndian.Uint32(heightData[4:8])
	tile := &terrainTile{
		gridHeight: math.Float32frombits(binary.LittleEndian.Uint32(heightData[8:12])),
	}
	if !finiteCoordinate(tile.gridHeight) {
		return nil, errors.New("invalid grid height")
	}
	if flags&terrainHeightNone == 0 {
		var multiplier float32
		var valueSize int
		switch {
		case flags&terrainHeightUint16 != 0:
			maxHeight := math.Float32frombits(binary.LittleEndian.Uint32(heightData[12:16]))
			multiplier = (maxHeight - tile.gridHeight) / 65535
			valueSize = 2
		case flags&terrainHeightUint8 != 0:
			maxHeight := math.Float32frombits(binary.LittleEndian.Uint32(heightData[12:16]))
			multiplier = (maxHeight - tile.gridHeight) / 255
			valueSize = 1
		default:
			multiplier = 1
			valueSize = 4
		}
		values := heightData[16:]
		if len(values) < (terrainV9Size+terrainV8Size)*valueSize {
			return nil, errors.New("truncated height grid")
		}
		tile.v9 = make([]float32, terrainV9Size)
		tile.v8 = make([]float32, terrainV8Size)
		decode := func(offset int) float32 {
			switch valueSize {
			case 1:
				return tile.gridHeight + float32(values[offset])*multiplier
			case 2:
				return tile.gridHeight + float32(binary.LittleEndian.Uint16(values[offset:offset+2]))*multiplier
			default:
				return math.Float32frombits(binary.LittleEndian.Uint32(values[offset : offset+4]))
			}
		}
		for i := range tile.v9 {
			tile.v9[i] = decode(i * valueSize)
		}
		offset := terrainV9Size * valueSize
		for i := range tile.v8 {
			tile.v8[i] = decode(offset + i*valueSize)
		}
	}

	holesOffset := binary.LittleEndian.Uint32(data[36:40])
	holesSize := binary.LittleEndian.Uint32(data[40:44])
	if holesSize != 0 {
		holes, err := terrainSection(data, holesOffset, holesSize)
		if err != nil || len(holes) < 512 {
			return nil, errors.New("invalid holes section")
		}
		tile.holes = make([]uint16, 256)
		for i := range tile.holes {
			tile.holes[i] = binary.LittleEndian.Uint16(holes[i*2 : i*2+2])
		}
	}
	return tile, nil
}

func terrainSection(data []byte, offset, size uint32) ([]byte, error) {
	if uint64(offset)+uint64(size) > uint64(len(data)) {
		return nil, errors.New("section outside file")
	}
	return data[offset : offset+size], nil
}

func (t *terrainTile) height(worldX, worldY float32) (float32, error) {
	x := terrainResolution * (32 - float64(worldX)/terrainGridSize)
	y := terrainResolution * (32 - float64(worldY)/terrainGridSize)
	if x < 0 || y < 0 || x >= 64*terrainResolution || y >= 64*terrainResolution {
		return 0, fmt.Errorf("%w: height coordinates outside map", ErrTerrainUnavailable)
	}
	xi, yi := int(x), int(y)
	fx, fy := float32(x-float64(xi)), float32(y-float64(yi))
	xi &= terrainResolution - 1
	yi &= terrainResolution - 1
	if t.isHole(xi, yi) {
		return 0, fmt.Errorf("%w: position is inside a terrain hole", ErrTerrainUnavailable)
	}
	if t.v9 == nil {
		return t.gridHeight, nil
	}

	h1 := t.v9[xi*129+yi]
	h2 := t.v9[(xi+1)*129+yi]
	h3 := t.v9[xi*129+yi+1]
	h4 := t.v9[(xi+1)*129+yi+1]
	h5 := 2 * t.v8[xi*128+yi]
	var a, b, c float32
	if fx+fy < 1 {
		if fx > fy {
			a, b, c = h2-h1, h5-h1-h2, h1
		} else {
			a, b, c = h5-h1-h3, h3-h1, h1
		}
	} else if fx > fy {
		a, b, c = h2+h4-h5, h4-h2, h5-h4
	} else {
		a, b, c = h4-h3, h3+h4-h5, h5-h4
	}
	height := a*fx + b*fy + c
	if !finiteCoordinate(height) {
		return 0, fmt.Errorf("%w: invalid decoded height", ErrTerrainUnavailable)
	}
	return height, nil
}

func (t *terrainTile) isHole(row, col int) bool {
	if t.holes == nil {
		return false
	}
	holeTabsH := [4]uint16{0x1111, 0x2222, 0x4444, 0x8888}
	holeTabsV := [4]uint16{0x000F, 0x00F0, 0x0F00, 0xF000}
	cellRow, cellCol := row/8, col/8
	holeRow, holeCol := (row%8)/2, (col%8)/2
	hole := t.holes[cellRow*16+cellCol]
	return hole&holeTabsH[holeCol]&holeTabsV[holeRow] != 0
}
