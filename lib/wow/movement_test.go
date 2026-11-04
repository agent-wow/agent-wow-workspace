package wow

import (
	"encoding/hex"
	"math"
	"testing"
)

func TestEncodeParseMovement(t *testing.T) {
	guid := GUID(0xF130000000002A00)
	pos := Position{X: 1, Y: 2, Z: 3, O: 0.5}
	body := EncodeMovement(guid, MoveState{
		Flags:     MovementFlagForward,
		Timestamp: 1234,
		Position:  pos,
	})
	gotGUID, flags, gotPos, err := ParseMovement(body)
	if err != nil {
		t.Fatal(err)
	}
	if gotGUID != guid || flags != MovementFlagForward {
		t.Fatalf("guid/flags = %x/%x", gotGUID, flags)
	}
	if gotPos.Distance2D(pos) > 1e-6 || gotPos.O != pos.O {
		t.Fatalf("position = %v, want %v", gotPos, pos)
	}
}

func TestTeleportAckRoundTrip(t *testing.T) {
	guid := GUID(0xF130000000002A00)
	pos := Position{X: 10, Y: 20, Z: 30, O: 1.5}
	prefix := packedGUID(nil, guid)
	movement := EncodeMovement(guid, MoveState{Timestamp: 1234, Position: pos})
	body := append(prefix, 7, 0, 0, 0)
	body = append(body, movement[len(prefix):]...)
	gotGUID, counter, gotPos, err := ParseTeleportAck(body)
	if err != nil {
		t.Fatal(err)
	}
	if gotGUID != guid || counter != 7 || gotPos != pos {
		t.Fatalf("teleport = %x/%d/%+v", gotGUID, counter, gotPos)
	}
	ack := EncodeTeleportAck(guid, counter, 0xaabbccdd)
	want := append(packedGUID(nil, guid), 7, 0, 0, 0, 0xdd, 0xcc, 0xbb, 0xaa)
	if string(ack) != string(want) {
		t.Fatalf("ack = %x, want %x", ack, want)
	}
}

func TestForwardPosition(t *testing.T) {
	// Orientation is atan2(dy, dx): 0 points along +X, pi/2 along +Y.
	east := Position{X: 10, Y: 10, Z: 5, O: 0}
	got := ForwardPosition(east, 7)
	if math.Abs(float64(got.X-17)) > 1e-5 || math.Abs(float64(got.Y-10)) > 1e-5 {
		t.Fatalf("east = %v", got)
	}
	north := Position{X: 10, Y: 10, Z: 5, O: math.Pi / 2}
	got = ForwardPosition(north, 7)
	if math.Abs(float64(got.X-10)) > 1e-5 || math.Abs(float64(got.Y-17)) > 1e-5 {
		t.Fatalf("north = %v", got)
	}
	if got.Z != 5 {
		t.Fatalf("z changed: %v", got.Z)
	}
}

func TestParseMonsterMove(t *testing.T) {
	// Captured from the live 3.3.5a server: a five-point wolf run.
	body := mustHex(t, "df3eb40d348030f100efc00cc68c29a2c310a99642a74057ef0000100000731d00000500000068030dc671ce9dc3500d9442ed4f40fffc17000009e03f0017a07f00")
	move, err := ParseMonsterMove(body)
	if err != nil {
		t.Fatal(err)
	}
	if move.GUID != GUID(0xf1300080340db43e) {
		t.Fatalf("guid = %x", move.GUID)
	}
	if move.Duration == 0 {
		t.Fatal("duration was not parsed")
	}
	if math.Abs(float64(move.Destination.X)-(-9024.8516)) > 0.01 ||
		math.Abs(float64(move.Destination.Y)-(-315.6128)) > 0.01 ||
		math.Abs(float64(move.Destination.Z)-74.0260) > 0.01 {
		t.Fatalf("destination = %v", move.Destination)
	}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
