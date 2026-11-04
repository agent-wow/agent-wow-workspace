package wow

import (
	"encoding/binary"
	"errors"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func makeTerrainFile(flags uint32, gridHeight, maxHeight float32, encode func([]byte, int), holes []uint16) []byte {
	valueCount := 0
	valueSize := 0
	if flags&terrainHeightNone == 0 {
		valueCount = terrainV9Size + terrainV8Size
		switch {
		case flags&terrainHeightUint16 != 0:
			valueSize = 2
		case flags&terrainHeightUint8 != 0:
			valueSize = 1
		default:
			valueSize = 4
		}
	}
	heightSize := 16 + valueCount*valueSize
	holesSize := len(holes) * 2
	data := make([]byte, 44+heightSize+holesSize)
	copy(data, "MAPS")
	binary.LittleEndian.PutUint32(data[4:8], 9)
	binary.LittleEndian.PutUint32(data[20:24], 44)
	binary.LittleEndian.PutUint32(data[24:28], uint32(heightSize))
	copy(data[44:48], "MHGT")
	binary.LittleEndian.PutUint32(data[48:52], flags)
	binary.LittleEndian.PutUint32(data[52:56], math.Float32bits(gridHeight))
	binary.LittleEndian.PutUint32(data[56:60], math.Float32bits(maxHeight))
	for i := 0; i < valueCount; i++ {
		encode(data[60+i*valueSize:60+(i+1)*valueSize], i)
	}
	if holesSize != 0 {
		binary.LittleEndian.PutUint32(data[36:40], uint32(44+heightSize))
		binary.LittleEndian.PutUint32(data[40:44], uint32(holesSize))
		for i, hole := range holes {
			offset := 44 + heightSize + i*2
			binary.LittleEndian.PutUint16(data[offset:offset+2], hole)
		}
	}
	return data
}

func floatHeightValues(v9, v8 []float32) func([]byte, int) {
	return func(dst []byte, i int) {
		var value float32
		if i < terrainV9Size {
			value = v9[i]
		} else {
			value = v8[i-terrainV9Size]
		}
		binary.LittleEndian.PutUint32(dst, math.Float32bits(value))
	}
}

func TestMapTerrainFlatHeight(t *testing.T) {
	dir := t.TempDir()
	data := makeTerrainFile(terrainHeightNone, 82.5, 0, nil, nil)
	if err := os.WriteFile(filepath.Join(dir, "0003232.map"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	terrain := NewMapTerrain(dir)
	height, err := terrain.GroundHeight(0, 0, 0)
	if err != nil || height != 82.5 {
		t.Fatalf("height = %v, %v; want 82.5", height, err)
	}
}

func TestTerrainFloatTriangleInterpolation(t *testing.T) {
	v9 := make([]float32, terrainV9Size)
	v8 := make([]float32, terrainV8Size)
	v9[0], v9[129], v9[1], v9[130] = 10, 20, 30, 40
	v8[0] = 25 // h5 is twice the center value: 50.
	data := makeTerrainFile(0, 0, 0, floatHeightValues(v9, v8), nil)
	tile, err := parseTerrainTile(data)
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name       string
		x, y       float32
		wantHeight float32
	}{
		{"corner", 0, 0, 10},
		{"first triangle", -2.0833333, -1.0416666, 20},
		{"fourth triangle", -2.0833333, -2.0833333, 25},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			height, err := tile.height(test.x, test.y)
			if err != nil || math.Abs(float64(height-test.wantHeight)) > 0.001 {
				t.Fatalf("height = %v, %v; want %v", height, err, test.wantHeight)
			}
		})
	}
}

func TestTerrainIntegerHeightDecoding(t *testing.T) {
	tests := []struct {
		name      string
		flags     uint32
		raw       uint16
		divisor   float32
		wantValue float32
	}{
		{"uint16", terrainHeightUint16, 100, 65535, 100},
		{"uint8", terrainHeightUint8, 100, 255, 100},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			encode := func(dst []byte, i int) {
				value := test.raw
				if i >= terrainV9Size {
					value /= 2 // Keep h5 equal to the four corner heights.
				}
				if test.flags == terrainHeightUint8 {
					dst[0] = byte(value)
				} else {
					binary.LittleEndian.PutUint16(dst, value)
				}
			}
			data := makeTerrainFile(test.flags, 10, 20, encode, nil)
			tile, err := parseTerrainTile(data)
			if err != nil {
				t.Fatal(err)
			}
			height, err := tile.height(0, 0)
			want := 10 + test.wantValue*(10/test.divisor)
			if err != nil || math.Abs(float64(height-want)) > 0.0001 {
				t.Fatalf("height = %v, %v; want %v", height, err, want)
			}
		})
	}
}

func TestTerrainRejectsHolesAndMissingTiles(t *testing.T) {
	holes := make([]uint16, 256)
	holes[0] = 1
	data := makeTerrainFile(terrainHeightNone, 50, 0, nil, holes)
	tile, err := parseTerrainTile(data)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tile.height(0, 0); !errors.Is(err, ErrTerrainUnavailable) {
		t.Fatalf("hole error = %v, want ErrTerrainUnavailable", err)
	}
	terrain := NewMapTerrain(t.TempDir())
	if _, err := terrain.GroundHeight(0, 0, 0); !errors.Is(err, ErrTerrainUnavailable) {
		t.Fatalf("missing tile error = %v, want ErrTerrainUnavailable", err)
	}
}

func TestTerrainRejectsUnsupportedVersion(t *testing.T) {
	data := makeTerrainFile(terrainHeightNone, 50, 0, nil, nil)
	binary.LittleEndian.PutUint32(data[4:8], 8)
	if _, err := parseTerrainTile(data); err == nil {
		t.Fatal("expected unsupported version error")
	}
}

func TestRealTerrainV20(t *testing.T) {
	dir := os.Getenv("AGENT_WOW_MAPS_DIR")
	if dir == "" {
		t.Skip("set AGENT_WOW_MAPS_DIR to validate the extracted v20 terrain")
	}
	tests := []struct {
		name       string
		mapID      uint32
		x, y, want float32
	}{
		{"Northshire login", 0, -8949.95, -132.49, 83.53},
		{"Valley of Trials", 1, -629.052, -4228.06, 38.2334},
		{"Durotar flat approach", 1, -343.19672, -4320.674, 58.43043},
		{"Durotar flat approach two yards", 1, -345.09937, -4320.0576, 58.43043},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			height, err := NewMapTerrain(dir).GroundHeight(test.mapID, test.x, test.y)
			if err != nil {
				t.Fatal(err)
			}
			if math.Abs(float64(height-test.want)) > 0.25 {
				t.Fatalf("terrain height = %.4f, want live-measured %.4f", height, test.want)
			}
			t.Logf("terrain height = %.4f (live measurement %.4f)", height, test.want)
		})
	}
}
