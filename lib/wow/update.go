package wow

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
)

// Update kinds used by SMSG_UPDATE_OBJECT blocks.
const (
	UpdateValues      uint8 = 0
	UpdateMovement    uint8 = 1
	UpdateCreate      uint8 = 2
	UpdateCreate2     uint8 = 3
	UpdateOutOfRange  uint8 = 4
	UpdateNearObjects uint8 = 5
)

// Update flag bits on a movement block (WotLK uses a uint16).
const (
	UpdateFlagSelf          uint16 = 0x0001
	UpdateFlagTransport     uint16 = 0x0002
	UpdateFlagHasTarget     uint16 = 0x0004
	UpdateFlagUnknown       uint16 = 0x0008
	UpdateFlagLowGUID       uint16 = 0x0010
	UpdateFlagLiving        uint16 = 0x0020
	UpdateFlagStationaryPos uint16 = 0x0040
	UpdateFlagVehicle       uint16 = 0x0080
	UpdateFlagPosition      uint16 = 0x0100
	UpdateFlagRotation      uint16 = 0x0200
)

// Movement flag bits needed to walk a movement-info record.
const (
	MovementFlagOnTransport   uint32 = 0x00000200
	MovementFlagFalling       uint32 = 0x00001000
	MovementFlagSwimming      uint32 = 0x00200000
	MovementFlagFlying        uint32 = 0x02000000
	MovementFlagSplineElev    uint32 = 0x04000000
	MovementFlagSplineEnabled uint32 = 0x08000000

	MovementFlag2AlwaysAllowPitching uint16 = 0x0020
	MovementFlag2Interpolated        uint16 = 0x0400
)

// ObjectType is the TypeID carried by create blocks.
type ObjectType uint8

const (
	ObjectTypeObject        ObjectType = 0
	ObjectTypeItem          ObjectType = 1
	ObjectTypeContainer     ObjectType = 2
	ObjectTypeUnit          ObjectType = 3
	ObjectTypePlayer        ObjectType = 4
	ObjectTypeGameObject    ObjectType = 5
	ObjectTypeDynamicObject ObjectType = 6
	ObjectTypeCorpse        ObjectType = 7
)

func (t ObjectType) String() string {
	switch t {
	case ObjectTypeObject:
		return "object"
	case ObjectTypeItem:
		return "item"
	case ObjectTypeContainer:
		return "container"
	case ObjectTypeUnit:
		return "unit"
	case ObjectTypePlayer:
		return "player"
	case ObjectTypeGameObject:
		return "gameobject"
	case ObjectTypeDynamicObject:
		return "dynamicobject"
	case ObjectTypeCorpse:
		return "corpse"
	default:
		return fmt.Sprintf("type(%d)", uint8(t))
	}
}

// Update field indices used by this workspace (see AzerothCore UpdateFields.h).
const (
	ObjectFieldGUID    uint32 = 0x0000
	ObjectFieldEntry   uint32 = 0x0003
	UnitFieldTarget    uint32 = 0x0012
	UnitFieldBytes0    uint32 = 0x0017
	UnitFieldHealth    uint32 = 0x0018
	UnitFieldPower1    uint32 = 0x0019
	UnitFieldMaxHealth uint32 = 0x0020
	UnitFieldLevel     uint32 = 0x0036
	UnitFieldFaction   uint32 = 0x0037
	UnitFieldFlags     uint32 = 0x003B

	// UNIT_END is 0x94 in build 12340.
	PlayerFlags       uint32 = 0x0096
	PlayerXP          uint32 = 0x027A
	PlayerNextLevelXP uint32 = 0x027B
	// PlayerSkillInfo is PLAYER_SKILL_INFO_1_1, the block the server sends
	// to the player itself. It holds the skill lines in groups of three
	// fields; see skills.go for the layout.
	PlayerSkillInfo uint32 = 0x027C
	// PlayerCoinage is PLAYER_FIELD_COINAGE, a single uint32 holding the
	// character's copper total. It sits after PLAYER_REST_STATE_EXPERIENCE,
	// far from the XP fields; the skill-info block (0x027C..) starts right
	// after XP and is two-short pairs, not money.
	PlayerCoinage uint32 = 0x0492
)

// Selected flag bits used by the modules.
const (
	UnitFlagInCombat uint32 = 0x00080000
	PlayerFlagGhost  uint32 = 0x00000010
)

// UpdateFields is a sparse map of field index to value.
type UpdateFields map[uint32]uint32

// Uint32 returns a single field value.
func (f UpdateFields) Uint32(index uint32) (uint32, bool) {
	v, ok := f[index]
	return v, ok
}

// Uint64 joins a long field stored in two consecutive slots.
func (f UpdateFields) Uint64(index uint32) (uint64, bool) {
	low, okLow := f[index]
	high, okHigh := f[index+1]
	if !okLow || !okHigh {
		return 0, false
	}
	return uint64(low) | uint64(high)<<32, true
}

// ObjectUpdate is one decoded block of SMSG_UPDATE_OBJECT.
type ObjectUpdate struct {
	Kind      uint8
	GUID      GUID
	Type      ObjectType
	Position  Position
	HasPos    bool
	Target    GUID
	HasTarget bool
	Fields    UpdateFields
}

// Update is a decoded SMSG_UPDATE_OBJECT body. Out-of-range and near-object
// blocks are lifted out because they only carry GUIDs.
type Update struct {
	Objects    []ObjectUpdate
	OutOfRange []GUID
	Near       []GUID
}

const (
	maxUpdateBlocks = 65535
	maxSplineNodes  = 4096
	maxDecompressed = 8 << 20
)

// ParseCompressedUpdateObject decodes SMSG_COMPRESSED_UPDATE_OBJECT: a little
// endian uint32 decoded size followed by a zlib stream.
func ParseCompressedUpdateObject(body []byte) (*Update, error) {
	if len(body) < 4 {
		return nil, errors.New("compressed update: missing size")
	}
	declared := binary.LittleEndian.Uint32(body)
	if declared > maxDecompressed {
		return nil, fmt.Errorf("compressed update: declared size %d exceeds %d", declared, maxDecompressed)
	}
	zr, err := zlib.NewReader(bytes.NewReader(body[4:]))
	if err != nil {
		return nil, fmt.Errorf("compressed update: %w", err)
	}
	defer zr.Close()
	raw, err := io.ReadAll(io.LimitReader(zr, maxDecompressed+1))
	if err != nil {
		return nil, fmt.Errorf("compressed update: %w", err)
	}
	if len(raw) > maxDecompressed {
		return nil, errors.New("compressed update: decompressed data too large")
	}
	return ParseUpdateObject(raw)
}

// ParseUpdateObject decodes an SMSG_UPDATE_OBJECT body.
func ParseUpdateObject(body []byte) (*Update, error) {
	r := &reader{b: body}
	count, err := r.u32()
	if err != nil {
		return nil, err
	}
	if count > maxUpdateBlocks {
		return nil, fmt.Errorf("update object: block count %d exceeds %d", count, maxUpdateBlocks)
	}
	u := &Update{Objects: make([]ObjectUpdate, 0, count)}
	for i := uint32(0); i < count; i++ {
		kind, err := r.u8()
		if err != nil {
			return nil, err
		}
		switch kind {
		case UpdateValues:
			guid, err := r.packedGUID()
			if err != nil {
				return nil, err
			}
			fields, err := readFields(r)
			if err != nil {
				return nil, err
			}
			u.Objects = append(u.Objects, ObjectUpdate{Kind: kind, GUID: guid, Fields: fields})
		case UpdateMovement:
			guid, err := r.packedGUID()
			if err != nil {
				return nil, err
			}
			block, err := readMovementBlock(r)
			if err != nil {
				return nil, err
			}
			u.Objects = append(u.Objects, ObjectUpdate{
				Kind: kind, GUID: guid, Position: block.pos, HasPos: block.hasPos,
				Target: block.target, HasTarget: block.hasTarget,
			})
		case UpdateCreate, UpdateCreate2:
			guid, err := r.packedGUID()
			if err != nil {
				return nil, err
			}
			rawType, err := r.u8()
			if err != nil {
				return nil, err
			}
			if rawType > uint8(ObjectTypeCorpse) {
				return nil, fmt.Errorf("update object: unknown object type %d", rawType)
			}
			block, err := readMovementBlock(r)
			if err != nil {
				return nil, err
			}
			fields, err := readFields(r)
			if err != nil {
				return nil, err
			}
			u.Objects = append(u.Objects, ObjectUpdate{
				Kind: kind, GUID: guid, Type: ObjectType(rawType),
				Position: block.pos, HasPos: block.hasPos,
				Target: block.target, HasTarget: block.hasTarget,
				Fields: fields,
			})
		case UpdateOutOfRange, UpdateNearObjects:
			n, err := r.u32()
			if err != nil {
				return nil, err
			}
			if n > maxUpdateBlocks {
				return nil, fmt.Errorf("update object: guid count %d exceeds %d", n, maxUpdateBlocks)
			}
			guids := make([]GUID, 0, n)
			for j := uint32(0); j < n; j++ {
				guid, err := r.packedGUID()
				if err != nil {
					return nil, err
				}
				guids = append(guids, guid)
			}
			if kind == UpdateOutOfRange {
				u.OutOfRange = append(u.OutOfRange, guids...)
			} else {
				u.Near = append(u.Near, guids...)
			}
		default:
			return nil, fmt.Errorf("update object: unknown update kind %d", kind)
		}
	}
	return u, nil
}

// ParseLoginVerifyWorld decodes SMSG_LOGIN_VERIFY_WORLD: map id followed by the
// character's world position and orientation.
func ParseLoginVerifyWorld(body []byte) (uint32, Position, error) {
	if len(body) < 20 {
		return 0, Position{}, fmt.Errorf("login verify world: body length %d", len(body))
	}
	r := &reader{b: body}
	mapID, err := r.u32()
	if err != nil {
		return 0, Position{}, err
	}
	pos, err := readPosition(r)
	if err != nil {
		return 0, Position{}, err
	}
	return mapID, pos, nil
}

// ParseDestroyObject decodes SMSG_DESTROY_OBJECT enough to get its GUID.
func ParseDestroyObject(body []byte) (GUID, error) {
	if len(body) < 8 {
		return 0, errors.New("destroy object: body too short")
	}
	return GUID(binary.LittleEndian.Uint64(body)), nil
}

type movementBlock struct {
	pos       Position
	hasPos    bool
	target    GUID
	hasTarget bool
}

func readMovementBlock(r *reader) (movementBlock, error) {
	var out movementBlock
	flags, err := r.u16()
	if err != nil {
		return out, err
	}
	switch {
	case flags&UpdateFlagLiving != 0:
		pos, moveFlags, err := readMovementInfo(r)
		if err != nil {
			return out, err
		}
		// Nine speeds: walk, run, run-back, swim, swim-back, flight, flight-back,
		// turn rate, pitch rate.
		for i := 0; i < 9; i++ {
			if _, err := r.f32(); err != nil {
				return out, err
			}
		}
		if moveFlags&MovementFlagSplineEnabled != 0 {
			if err := readCreateSpline(r); err != nil {
				return out, err
			}
		}
		out.pos, out.hasPos = pos, true
	case flags&UpdateFlagPosition != 0:
		if _, err := r.packedGUID(); err != nil { // transport
			return out, err
		}
		x, err := r.f32()
		if err != nil {
			return out, err
		}
		y, err := r.f32()
		if err != nil {
			return out, err
		}
		z, err := r.f32()
		if err != nil {
			return out, err
		}
		if _, err := r.f32(); err != nil { // transport offset x
			return out, err
		}
		if _, err := r.f32(); err != nil { // transport offset y
			return out, err
		}
		if _, err := r.f32(); err != nil { // transport offset z
			return out, err
		}
		o, err := r.f32()
		if err != nil {
			return out, err
		}
		if _, err := r.f32(); err != nil { // corpse orientation
			return out, err
		}
		out.pos, out.hasPos = Position{X: x, Y: y, Z: z, O: o}, true
	case flags&UpdateFlagStationaryPos != 0:
		pos, err := readPosition(r)
		if err != nil {
			return out, err
		}
		out.pos, out.hasPos = pos, true
	}

	if flags&UpdateFlagUnknown != 0 {
		if _, err := r.u32(); err != nil {
			return out, err
		}
	}
	if flags&UpdateFlagLowGUID != 0 {
		if _, err := r.u32(); err != nil {
			return out, err
		}
	}
	if flags&UpdateFlagHasTarget != 0 {
		guid, err := r.packedGUID()
		if err != nil {
			return out, err
		}
		out.target, out.hasTarget = guid, guid != 0
	}
	if flags&UpdateFlagTransport != 0 {
		if _, err := r.u32(); err != nil {
			return out, err
		}
	}
	if flags&UpdateFlagVehicle != 0 {
		if _, err := r.u32(); err != nil {
			return out, err
		}
		if _, err := r.f32(); err != nil {
			return out, err
		}
	}
	if flags&UpdateFlagRotation != 0 {
		if _, err := r.i64(); err != nil {
			return out, err
		}
	}
	return out, nil
}

func readMovementInfo(r *reader) (Position, uint32, error) {
	flags, err := r.u32()
	if err != nil {
		return Position{}, 0, err
	}
	flags2, err := r.u16()
	if err != nil {
		return Position{}, 0, err
	}
	if _, err := r.u32(); err != nil { // timestamp
		return Position{}, 0, err
	}
	pos, err := readPosition(r)
	if err != nil {
		return Position{}, 0, err
	}
	if flags&MovementFlagOnTransport != 0 {
		if _, err := r.packedGUID(); err != nil {
			return Position{}, 0, err
		}
		if _, err := readPosition(r); err != nil {
			return Position{}, 0, err
		}
		if _, err := r.u32(); err != nil { // transport time
			return Position{}, 0, err
		}
		if _, err := r.u8(); err != nil { // seat
			return Position{}, 0, err
		}
		if flags2&MovementFlag2Interpolated != 0 {
			if _, err := r.u32(); err != nil {
				return Position{}, 0, err
			}
		}
	}
	if flags&(MovementFlagSwimming|MovementFlagFlying) != 0 ||
		flags2&MovementFlag2AlwaysAllowPitching != 0 {
		if _, err := r.f32(); err != nil { // pitch
			return Position{}, 0, err
		}
	}
	if _, err := r.u32(); err != nil { // fall time
		return Position{}, 0, err
	}
	if flags&MovementFlagFalling != 0 {
		for i := 0; i < 4; i++ { // jump info
			if _, err := r.f32(); err != nil {
				return Position{}, 0, err
			}
		}
	}
	if flags&MovementFlagSplineElev != 0 {
		if _, err := r.f32(); err != nil { // spline elevation
			return Position{}, 0, err
		}
	}
	return pos, flags, nil
}

func readCreateSpline(r *reader) error {
	flags, err := r.u32()
	if err != nil {
		return err
	}
	switch {
	case flags&0x00008000 != 0: // final point
		if _, err := r.f32(); err != nil {
			return err
		}
		if _, err := r.f32(); err != nil {
			return err
		}
		if _, err := r.f32(); err != nil {
			return err
		}
	case flags&0x00010000 != 0: // final target
		if _, err := r.u64(); err != nil {
			return err
		}
	case flags&0x00020000 != 0: // final angle
		if _, err := r.f32(); err != nil {
			return err
		}
	}
	for i := 0; i < 3; i++ { // time passed, duration, id
		if _, err := r.u32(); err != nil {
			return err
		}
	}
	for i := 0; i < 3; i++ { // duration modifier, next duration modifier, vertical acceleration
		if _, err := r.f32(); err != nil {
			return err
		}
	}
	if _, err := r.u32(); err != nil { // effect start time
		return err
	}
	nodes, err := r.u32()
	if err != nil {
		return err
	}
	if nodes > maxSplineNodes {
		return fmt.Errorf("create spline: node count %d exceeds %d", nodes, maxSplineNodes)
	}
	for i := uint32(0); i < nodes; i++ {
		for j := 0; j < 3; j++ {
			if _, err := r.f32(); err != nil {
				return err
			}
		}
	}
	if _, err := r.u8(); err != nil { // mode
		return err
	}
	for i := 0; i < 3; i++ { // final destination
		if _, err := r.f32(); err != nil {
			return err
		}
	}
	return nil
}

func readPosition(r *reader) (Position, error) {
	x, err := r.f32()
	if err != nil {
		return Position{}, err
	}
	y, err := r.f32()
	if err != nil {
		return Position{}, err
	}
	z, err := r.f32()
	if err != nil {
		return Position{}, err
	}
	o, err := r.f32()
	if err != nil {
		return Position{}, err
	}
	return Position{X: x, Y: y, Z: z, O: o}, nil
}

func readFields(r *reader) (UpdateFields, error) {
	blocks, err := r.u8()
	if err != nil {
		return nil, err
	}
	masks := make([]uint32, blocks)
	for i := range masks {
		masks[i], err = r.u32()
		if err != nil {
			return nil, err
		}
	}
	fields := make(UpdateFields)
	for block, mask := range masks {
		for bit := 0; bit < 32; bit++ {
			if mask&(1<<bit) == 0 {
				continue
			}
			value, err := r.u32()
			if err != nil {
				return nil, err
			}
			index := uint32(block*32 + bit)
			fields[index] = value
		}
	}
	return fields, nil
}

// reader is a bounds-checked little-endian cursor.
type reader struct {
	b   []byte
	off int
}

var errTruncated = errors.New("packet truncated")

func (r *reader) take(n int) ([]byte, error) {
	if n < 0 || r.off+n > len(r.b) {
		return nil, errTruncated
	}
	part := r.b[r.off : r.off+n]
	r.off += n
	return part, nil
}

func (r *reader) u8() (uint8, error) {
	b, err := r.take(1)
	if err != nil {
		return 0, err
	}
	return b[0], nil
}

func (r *reader) u16() (uint16, error) {
	b, err := r.take(2)
	if err != nil {
		return 0, err
	}
	return binary.LittleEndian.Uint16(b), nil
}

func (r *reader) u32() (uint32, error) {
	b, err := r.take(4)
	if err != nil {
		return 0, err
	}
	return binary.LittleEndian.Uint32(b), nil
}

func (r *reader) u64() (uint64, error) {
	b, err := r.take(8)
	if err != nil {
		return 0, err
	}
	return binary.LittleEndian.Uint64(b), nil
}

func (r *reader) i64() (int64, error) {
	v, err := r.u64()
	return int64(v), err
}

func (r *reader) f32() (float32, error) {
	v, err := r.u32()
	if err != nil {
		return 0, err
	}
	return math.Float32frombits(v), nil
}

func (r *reader) packedGUID() (GUID, error) {
	mask, err := r.u8()
	if err != nil {
		return 0, err
	}
	var guid uint64
	for i := 0; i < 8; i++ {
		if mask&(1<<i) == 0 {
			continue
		}
		b, err := r.u8()
		if err != nil {
			return 0, err
		}
		guid |= uint64(b) << (8 * i)
	}
	return GUID(guid), nil
}
