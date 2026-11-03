package wow

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// Combat and progression packet codecs for WotLK 3.3.5a (build 12340).
// Layouts follow AzerothCore's CombatHandler.cpp, Player::SendLogXPGain and
// Player::SendLevelupInfo.

// EncodeAttackSwing builds CMSG_ATTACKSWING: one unpacked 64-bit GUID.
func EncodeAttackSwing(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// EncodeAttackStop builds CMSG_ATTACKSTOP, which has no body.
func EncodeAttackStop() []byte {
	return nil
}

// EncodeRepopRequest builds CMSG_REPOP_REQUEST: one bool, CheckInstance.
// The server turns the corpse into a ghost at the nearest graveyard.
func EncodeRepopRequest() []byte {
	return []byte{0}
}

// EncodeReclaimCorpse builds CMSG_RECLAIM_CORPSE: the corpse GUID, which for a
// player is the player's own GUID, as one unpacked 64-bit value.
func EncodeReclaimCorpse(guid GUID) []byte {
	return binary.LittleEndian.AppendUint64(nil, uint64(guid))
}

// EncodeCastSpell builds CMSG_CAST_SPELL for a unit target. A zero target
// emits an empty target mask, which the server resolves against the caster.
func EncodeCastSpell(castCount uint8, spellID uint32, target GUID) []byte {
	out := []byte{castCount}
	out = binary.LittleEndian.AppendUint32(out, spellID)
	out = append(out, 0) // cast flags
	if target == 0 {
		return binary.LittleEndian.AppendUint32(out, 0)
	}
	const targetFlagUnit uint32 = 0x0002
	out = binary.LittleEndian.AppendUint32(out, targetFlagUnit)
	return packedGUID(out, target)
}

// AttackEvent is SMSG_ATTACKSTART.
type AttackEvent struct {
	Attacker GUID
	Victim   GUID
}

// ParseAttackStart decodes SMSG_ATTACKSTART: attacker and victim as full GUIDs.
func ParseAttackStart(body []byte) (AttackEvent, error) {
	if len(body) < 16 {
		return AttackEvent{}, errors.New("attack start: body too short")
	}
	return AttackEvent{
		Attacker: GUID(binary.LittleEndian.Uint64(body[0:8])),
		Victim:   GUID(binary.LittleEndian.Uint64(body[8:16])),
	}, nil
}

// AttackStopEvent is SMSG_ATTACKSTOP. Unlike attack start, this packet uses
// packed GUIDs.
type AttackStopEvent struct {
	Attacker GUID
	Victim   GUID
	NowDead  bool
}

// ParseAttackStop decodes SMSG_ATTACKSTOP.
func ParseAttackStop(body []byte) (AttackStopEvent, error) {
	r := &reader{b: body}
	attacker, err := r.packedGUID()
	if err != nil {
		return AttackStopEvent{}, err
	}
	victim, err := r.packedGUID()
	if err != nil {
		return AttackStopEvent{}, err
	}
	dead, err := r.u32()
	if err != nil {
		return AttackStopEvent{}, err
	}
	return AttackStopEvent{Attacker: attacker, Victim: victim, NowDead: dead != 0}, nil
}

// XPGain is SMSG_LOG_XPGAIN. Victim is zero for non-kill awards; only a kill
// carries the pre-rested base amount and group bonus.
type XPGain struct {
	Victim GUID
	Total  uint32
	Base   uint32
	Kill   bool
}

// ParseLogXPGain decodes SMSG_LOG_XPGAIN.
func ParseLogXPGain(body []byte) (XPGain, error) {
	r := &reader{b: body}
	victim, err := r.u64()
	if err != nil {
		return XPGain{}, err
	}
	total, err := r.u32()
	if err != nil {
		return XPGain{}, err
	}
	kind, err := r.u8()
	if err != nil {
		return XPGain{}, err
	}
	out := XPGain{Victim: GUID(victim), Total: total, Base: total, Kill: kind == 0}
	if out.Kill {
		base, err := r.u32()
		if err != nil {
			return XPGain{}, err
		}
		if _, err := r.f32(); err != nil { // group bonus
			return XPGain{}, err
		}
		out.Base = base
	}
	return out, nil
}

// ParseLevelUpInfo returns the new level from SMSG_LEVELUP_INFO. The rest of
// the body carries health, power and stat deltas that this workspace ignores.
func ParseLevelUpInfo(body []byte) (uint32, error) {
	if len(body) < 4 {
		return 0, errors.New("level up info: body too short")
	}
	return binary.LittleEndian.Uint32(body), nil
}

// CastFailure is SMSG_CAST_FAILED.
type CastFailure struct {
	CastCount uint8
	SpellID   uint32
	Result    uint8
}

// ParseCastFailed decodes SMSG_CAST_FAILED.
func ParseCastFailed(body []byte) (CastFailure, error) {
	if len(body) < 6 {
		return CastFailure{}, fmt.Errorf("cast failed: body length %d", len(body))
	}
	return CastFailure{
		CastCount: body[0],
		SpellID:   binary.LittleEndian.Uint32(body[1:5]),
		Result:    body[5],
	}, nil
}
