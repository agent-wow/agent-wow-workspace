package wow

import (
	"encoding/binary"
	"errors"
	"math"
)

// Movement flags (WotLK MovementFlags).
const (
	MovementFlagForward    uint32 = 0x00000001
	MovementFlagBackward   uint32 = 0x00000002
	MovementFlagStrafeLeft uint32 = 0x00000004
	MovementFlagStrafeRgt  uint32 = 0x00000008
	MovementFlagTurnLeft   uint32 = 0x00000010
	MovementFlagTurnRight  uint32 = 0x00000020
	MovementFlagWalking    uint32 = 0x00000100
)

// MoveState is the flat subset of MovementInfo this workspace emits: no
// transport, pitch, jumping, or spline data.
type MoveState struct {
	Flags     uint32
	Flags2    uint16
	Timestamp uint32
	Position  Position
	FallTime  uint32
}

// EncodeMovement builds an MSG_MOVE_* body: packed GUID followed by
// movement info.
func EncodeMovement(guid GUID, state MoveState) []byte {
	out := packedGUID(nil, guid)
	out = binary.LittleEndian.AppendUint32(out, state.Flags)
	out = binary.LittleEndian.AppendUint16(out, state.Flags2)
	out = binary.LittleEndian.AppendUint32(out, state.Timestamp)
	out = appendPosition(out, state.Position)
	out = binary.LittleEndian.AppendUint32(out, state.FallTime)
	return out
}

// ParseMovement decodes an MSG_MOVE_* body. Unknown movement flags still
// consume their documented tail, keeping the position usable.
func ParseMovement(body []byte) (GUID, uint32, Position, error) {
	r := &reader{b: body}
	guid, err := r.packedGUID()
	if err != nil {
		return 0, 0, Position{}, err
	}
	pos, flags, err := readMovementInfo(r)
	if err != nil {
		return 0, 0, Position{}, err
	}
	return guid, flags, pos, nil
}

// ParseTeleportAck decodes the server's same-map teleport order. Despite the
// opcode name, this direction carries packed GUID, order counter, MovementInfo.
func ParseTeleportAck(body []byte) (GUID, uint32, Position, error) {
	r := &reader{b: body}
	guid, err := r.packedGUID()
	if err != nil {
		return 0, 0, Position{}, err
	}
	counter, err := r.u32()
	if err != nil {
		return 0, 0, Position{}, err
	}
	pos, _, err := readMovementInfo(r)
	if err != nil {
		return 0, 0, Position{}, err
	}
	return guid, counter, pos, nil
}

// EncodeTeleportAck builds the client's reply: packed GUID, the server's order
// counter, and the current client clock. AzerothCore's reader calls the counter
// "flags" and does not interpret it.
func EncodeTeleportAck(guid GUID, counter, timestamp uint32) []byte {
	out := packedGUID(nil, guid)
	out = binary.LittleEndian.AppendUint32(out, counter)
	return binary.LittleEndian.AppendUint32(out, timestamp)
}

// MonsterMove is the part of SMSG_MONSTER_MOVE that sense needs: which unit
// moved and where it is going.
type MonsterMove struct {
	GUID        GUID
	Start       Position
	Destination Position
	Duration    uint32 // milliseconds
	Stopped     bool
}

// ParseMonsterMove decodes the flat 3.3.5a wire layout:
//
//	packed GUID, uint8 unk, float x/y/z, uint32 splineId, uint8 type,
//	[guid/float/spot when facing], uint32 splineFlags, uint32 duration,
//	uint32 pointCount, one full Vector3 destination, packed deltas...
//
// Only the destination matters here. A stop packet reports the start position.
func ParseMonsterMove(body []byte) (MonsterMove, error) {
	r := &reader{b: body}
	guid, err := r.packedGUID()
	if err != nil {
		return MonsterMove{}, err
	}
	if _, err := r.u8(); err != nil { // unknown, always 0 in practice
		return MonsterMove{}, err
	}
	x, err := r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	y, err := r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	z, err := r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	if _, err := r.u32(); err != nil { // spline id
		return MonsterMove{}, err
	}
	moveType, err := r.u8()
	if err != nil {
		return MonsterMove{}, err
	}
	move := MonsterMove{GUID: guid, Start: Position{X: x, Y: y, Z: z}}
	if moveType == 1 { // MONSTER_MOVE_STOP has no spline body
		move.Destination = move.Start
		move.Stopped = true
		return move, nil
	}
	switch moveType {
	case 3: // facing target: raw 64-bit GUID
		if _, err := r.u64(); err != nil {
			return MonsterMove{}, err
		}
	case 4: // facing angle
		if _, err := r.f32(); err != nil {
			return MonsterMove{}, err
		}
	case 2: // facing spot: one Vector3
		if _, err := r.take(12); err != nil {
			return MonsterMove{}, err
		}
	}
	if _, err := r.u32(); err != nil { // spline flags
		return MonsterMove{}, err
	}
	duration, err := r.u32()
	if err != nil {
		return MonsterMove{}, err
	}
	move.Duration = duration
	count, err := r.u32()
	if err != nil {
		return MonsterMove{}, err
	}
	if count == 0 || count > 256 {
		return MonsterMove{}, errors.New("monster move: implausible point count")
	}
	// AzerothCore writes the destination as the only full Vector3 before a
	// run of 4-byte packed deltas, regardless of their order along the path.
	x, err = r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	y, err = r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	z, err = r.f32()
	if err != nil {
		return MonsterMove{}, err
	}
	move.Destination = Position{X: x, Y: y, Z: z}
	return move, nil
}

// ForwardPosition returns the point yards ahead of pos along its orientation.
// The server works in atan2(dy, dx) space, so orientation 0 points along +X
// and the forward direction is (cos o, sin o). Verified against a live
// 3.3.5a server: swings only land when the character's facing is set this way.
func ForwardPosition(pos Position, yards float64) Position {
	return Position{
		X: float32(float64(pos.X) + yards*math.Cos(float64(pos.O))),
		Y: float32(float64(pos.Y) + yards*math.Sin(float64(pos.O))),
		Z: pos.Z,
		O: pos.O,
	}
}

// NormalizeOrientation folds an angle into [-pi, pi).
func NormalizeOrientation(o float64) float64 {
	for o >= math.Pi {
		o -= 2 * math.Pi
	}
	for o < -math.Pi {
		o += 2 * math.Pi
	}
	return o
}

// EncodePackedGUID returns the compact GUID form: mask byte then the nonzero
// bytes, least significant first.
func EncodePackedGUID(guid GUID) []byte {
	return packedGUID(nil, guid)
}

func packedGUID(out []byte, guid GUID) []byte {
	var mask byte
	for i := 0; i < 8; i++ {
		if byte(uint64(guid)>>(8*i)) != 0 {
			mask |= 1 << i
		}
	}
	out = append(out, mask)
	for i := 0; i < 8; i++ {
		if v := byte(uint64(guid) >> (8 * i)); v != 0 {
			out = append(out, v)
		}
	}
	return out
}

func appendPosition(out []byte, p Position) []byte {
	out = binary.LittleEndian.AppendUint32(out, math.Float32bits(p.X))
	out = binary.LittleEndian.AppendUint32(out, math.Float32bits(p.Y))
	out = binary.LittleEndian.AppendUint32(out, math.Float32bits(p.Z))
	out = binary.LittleEndian.AppendUint32(out, math.Float32bits(p.O))
	return out
}
