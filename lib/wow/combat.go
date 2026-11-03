package wow

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
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

// SpellCooldown is one (spell, duration) pair from SMSG_SPELL_COOLDOWN.
type SpellCooldown struct {
	SpellID    uint32
	CooldownMS uint32
}

const maxCooldownPairs = 4096

// ParseSpellCooldown decodes SMSG_SPELL_COOLDOWN. AzerothCore's
// Unit::BuildCooldownPacket writes the caster GUID unpacked, a flag byte and
// then (spell id, cooldown ms) pairs to the end of the packet; there is no
// count field in this build. The cooldown is the duration the server applied,
// not a remaining time at receipt.
func ParseSpellCooldown(body []byte) (GUID, uint8, []SpellCooldown, error) {
	r := &reader{b: body}
	guid, err := r.u64()
	if err != nil {
		return 0, 0, nil, fmt.Errorf("spell cooldown: %w", err)
	}
	flags, err := r.u8()
	if err != nil {
		return 0, 0, nil, fmt.Errorf("spell cooldown: %w", err)
	}
	remaining := len(body) - r.off
	if remaining%8 != 0 {
		return 0, 0, nil, fmt.Errorf("spell cooldown: %d trailing bytes", remaining)
	}
	count := remaining / 8
	if count > maxCooldownPairs {
		return 0, 0, nil, fmt.Errorf("spell cooldown: %d pairs exceeds %d", count, maxCooldownPairs)
	}
	out := make([]SpellCooldown, 0, count)
	for i := 0; i < count; i++ {
		spell, err := r.u32()
		if err != nil {
			return 0, 0, nil, fmt.Errorf("spell cooldown: %w", err)
		}
		cooldown, err := r.u32()
		if err != nil {
			return 0, 0, nil, fmt.Errorf("spell cooldown: %w", err)
		}
		out = append(out, SpellCooldown{SpellID: spell, CooldownMS: cooldown})
	}
	return GUID(guid), flags, out, nil
}

// CooldownEvent is SMSG_COOLDOWN_EVENT: the spell whose cooldown timer the
// server just activated, and the caster.
type CooldownEvent struct {
	SpellID uint32
	GUID    GUID
}

// ParseCooldownEvent decodes SMSG_COOLDOWN_EVENT.
func ParseCooldownEvent(body []byte) (CooldownEvent, error) {
	if len(body) < 12 {
		return CooldownEvent{}, fmt.Errorf("cooldown event: body length %d", len(body))
	}
	return CooldownEvent{
		SpellID: binary.LittleEndian.Uint32(body[0:4]),
		GUID:    GUID(binary.LittleEndian.Uint64(body[4:12])),
	}, nil
}

// Aura flag bits from AuraApplication::BuildUpdatePacket.
const (
	AuraFlagCaster   uint8 = 0x08
	AuraFlagPositive uint8 = 0x10
	AuraFlagDuration uint8 = 0x20
	AuraFlagNegative uint8 = 0x80
)

// Aura is one visible aura slot. SpellID zero removes the slot.
type Aura struct {
	Slot          uint8
	SpellID       uint32
	Flags         uint8
	CasterLevel   uint8
	Stacks        uint8
	Caster        GUID
	HasCaster     bool
	MaxDurationMS uint32
	DurationMS    uint32
	HasDuration   bool
}

// Positive reports the client's positive-aura flag.
func (a Aura) Positive() bool { return a.Flags&AuraFlagPositive != 0 }

// Negative reports the client's negative-aura flag.
func (a Aura) Negative() bool { return a.Flags&AuraFlagNegative != 0 }

// AuraUpdate is SMSG_AURA_UPDATE(_ALL): the target GUID followed by slots.
type AuraUpdate struct {
	Target GUID
	Auras  []Aura
}

const maxAurasPerUpdate = 256

// ParseAuraUpdate decodes SMSG_AURA_UPDATE and SMSG_AURA_UPDATE_ALL. Both use
// the same body: a packed target GUID, then repeated slot records. A caster
// GUID is present unless AFLAG_CASTER is set; max/remaining durations are
// present only with AFLAG_DURATION.
func ParseAuraUpdate(body []byte) (AuraUpdate, error) {
	r := &reader{b: body}
	target, err := r.packedGUID()
	if err != nil {
		return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
	}
	update := AuraUpdate{Target: target}
	for r.off < len(body) {
		if len(update.Auras) >= maxAurasPerUpdate {
			return AuraUpdate{}, fmt.Errorf("aura update: more than %d slots", maxAurasPerUpdate)
		}
		slot, err := r.u8()
		if err != nil {
			return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
		}
		spell, err := r.u32()
		if err != nil {
			return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
		}
		aura := Aura{Slot: slot, SpellID: spell}
		if spell == 0 {
			update.Auras = append(update.Auras, aura)
			continue
		}
		if aura.Flags, err = r.u8(); err != nil {
			return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
		}
		if aura.CasterLevel, err = r.u8(); err != nil {
			return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
		}
		if aura.Stacks, err = r.u8(); err != nil {
			return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
		}
		if aura.Flags&AuraFlagCaster == 0 {
			if aura.Caster, err = r.packedGUID(); err != nil {
				return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
			}
			aura.HasCaster = true
		}
		if aura.Flags&AuraFlagDuration != 0 {
			if aura.MaxDurationMS, err = r.u32(); err != nil {
				return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
			}
			if aura.DurationMS, err = r.u32(); err != nil {
				return AuraUpdate{}, fmt.Errorf("aura update: %w", err)
			}
			aura.HasDuration = true
		}
		update.Auras = append(update.Auras, aura)
	}
	return update, nil
}

// ComboPoints is SMSG_UPDATE_COMBO_POINTS: the rogue/druid combo target and
// the current point count. A zero target means no combo points are active.
type ComboPoints struct {
	Target GUID
	Points uint8
}

// ParseComboPoints decodes SMSG_UPDATE_COMBO_POINTS: packed target GUID, then
// one byte of points.
func ParseComboPoints(body []byte) (ComboPoints, error) {
	r := &reader{b: body}
	target, err := r.packedGUID()
	if err != nil {
		return ComboPoints{}, fmt.Errorf("combo points: %w", err)
	}
	points, err := r.u8()
	if err != nil {
		return ComboPoints{}, fmt.Errorf("combo points: %w", err)
	}
	return ComboPoints{Target: target, Points: points}, nil
}

// ParseCorpseReclaimDelay decodes SMSG_CORPSE_RECLAIM_DELAY: the remaining
// delay in milliseconds before the corpse may be reclaimed again.
func ParseCorpseReclaimDelay(body []byte) (uint32, error) {
	if len(body) < 4 {
		return 0, fmt.Errorf("corpse reclaim delay: body length %d", len(body))
	}
	return binary.LittleEndian.Uint32(body), nil
}

// DeathReleaseLocation is SMSG_DEATH_RELEASE_LOC. MapID -1 clears the marker.
type DeathReleaseLocation struct {
	MapID       uint32
	Position    Position
	HasLocation bool
}

// ParseDeathReleaseLoc decodes SMSG_DEATH_RELEASE_LOC: int32 map id followed by
// the graveyard/spirit-healer x, y, z. A map id of -1 clears the location.
func ParseDeathReleaseLoc(body []byte) (DeathReleaseLocation, error) {
	if len(body) < 16 {
		return DeathReleaseLocation{}, fmt.Errorf("death release location: body length %d", len(body))
	}
	mapID := int32(binary.LittleEndian.Uint32(body[0:4]))
	if mapID < 0 {
		return DeathReleaseLocation{}, nil
	}
	return DeathReleaseLocation{
		MapID: uint32(mapID),
		Position: Position{
			X: math.Float32frombits(binary.LittleEndian.Uint32(body[4:8])),
			Y: math.Float32frombits(binary.LittleEndian.Uint32(body[8:12])),
			Z: math.Float32frombits(binary.LittleEndian.Uint32(body[12:16])),
		},
		HasLocation: true,
	}, nil
}
