
ä
google/protobuf/any.protogoogle.protobuf"6
Any
type_url (	RtypeUrl
value (RvalueBv
com.google.protobufBAnyProtoPZ,google.golang.org/protobuf/types/known/anypb¢GPBªGoogle.Protobuf.WellKnownTypesbproto3
¾
google/protobuf/empty.protogoogle.protobuf"
EmptyB}
com.google.protobufB
EmptyProtoPZ.google.golang.org/protobuf/types/known/emptypbø¢GPBªGoogle.Protobuf.WellKnownTypesbproto3
§
api/module/v1/session.protoagentwow.module.v1google/protobuf/any.protogoogle/protobuf/empty.proto"?
WorldPacket
opcode (Ropcode
payload (Rpayload"E
SendPacketRequest
opcode (Ropcode
payload (Rpayload"5
ClockResponse$
client_time_ms (RclientTimeMs"u
InvokeModuleRequest
module (	Rmodule
method (	Rmethod.
request (2.google.protobuf.AnyRrequest"D
InvokeModuleResponse,
result (2.google.protobuf.AnyRresult2€
SessionK

SendPacket%.agentwow.module.v1.SendPacketRequest.google.protobuf.EmptyE
GetClock.google.protobuf.Empty!.agentwow.module.v1.ClockResponsea
InvokeModule'.agentwow.module.v1.InvokeModuleRequest(.agentwow.module.v1.InvokeModuleResponseB5Z3github.com/agent-wow/agent-wow/pkg/modules/v1;modv1bproto3
Ì#
module.protosense.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"3
Vector3
x (Rx
y (Ry
z (Rz"¼
Unit
guid (Rguid
name (	Rname
type (Rtype4
position (2.sense.module.v1.Vector3Rposition 
orientation (Rorientation!
has_position (RhasPosition
health (Rhealth

max_health (R	maxHealth
level	 (Rlevel
class_id
 (RclassId
race_id (RraceId
target_guid (R
targetGuid
is_self (RisSelf
entry (Rentry
xp (Rxp"
next_level_xp (RnextLevelXp
flags (Rflags!
player_flags (RplayerFlags
dead (Rdead
faction (Rfaction
	in_combat (RinCombat
money (Rmoney

power_type (R	powerType
power (Rpower
	max_power (RmaxPower

display_id (R	displayId#
dynamic_flags (RdynamicFlags
	npc_flags (RnpcFlags+
auras (2.sense.module.v1.AuraRauras"ê
Aura
slot (Rslot
spell_id (RspellId
flags (Rflags!
caster_level (RcasterLevel
stacks (Rstacks
caster_guid (R
casterGuid

has_caster (R	hasCaster&
max_duration_ms (RmaxDurationMs
duration_ms	 (R
durationMs!
has_duration
 (RhasDuration
positive (Rpositive
negative (Rnegative"i
Cooldown
spell_id (RspellId
cooldown_ms (R
cooldownMs!
remaining_ms (RremainingMs"ê

GameObject
guid (Rguid
entry (Rentry
type_id (RtypeId

display_id (R	displayId
flags (Rflags
state (Rstate#
anim_progress (RanimProgress
level (Rlevel
faction	 (Rfaction4
position
 (2.sense.module.v1.Vector3Rposition 
orientation (Rorientation!
has_position (RhasPosition"£
Skill
id (Rid
name (	Rname
category (Rcategory
value (Rvalue
max (Rmax
step (Rstep

profession (R
profession"Þ
SelfResponse)
unit (2.sense.module.v1.UnitRunit
map_id (RmapId
in_world (RinWorld.
skills (2.sense.module.v1.SkillRskills
epoch (Repoch
ready (Rready
map_changes (R
mapChanges
has_map (RhasMap+
auras	 (2.sense.module.v1.AuraRauras7
	cooldowns
 (2.sense.module.v1.CooldownR	cooldowns!
combo_points (RcomboPoints*
combo_target_guid (RcomboTargetGuid
guid (Rguid
name (	Rname"@
SkillsResponse.
skills (2.sense.module.v1.SkillRskills"o
NearbyRequest
radius (Rradius
limit (Rlimit0
include_game_objects (RincludeGameObjects"Ì
NearbyResponse+
units (2.sense.module.v1.UnitRunits
known_units (R
knownUnits>
game_objects (2.sense.module.v1.GameObjectRgameObjects,
known_game_objects (RknownGameObjects"¹
TargetResponse

has_target (R	hasTarget
target_guid (R
targetGuid)
unit (2.sense.module.v1.UnitRunit<
game_object (2.sense.module.v1.GameObjectR
gameObject")
SelectTargetRequest
guid (Rguid"&
QueryNameRequest
guid (Rguid"—
StatusResponse
packets (Rpackets!
parse_errors (RparseErrors
known_units (R
knownUnits!
name_queries (RnameQueries)
creature_queries (RcreatureQueries
epoch (Repoch
ready (Rready
map_id (RmapId
map_changes	 (R
mapChanges!
aura_updates
 (RauraUpdates)
cooldown_packets (RcooldownPackets'
cooldown_events (RcooldownEvents"0
MonsterMoveSamples
payloads (	Rpayloads"7
	FieldSlot
index (Rindex
value (Rvalue"=
	FieldDump0
slots (2.sense.module.v1.FieldSlotRslots2–
Module@
GetSelf.google.protobuf.Empty.sense.module.v1.SelfResponseD
	GetSkills.google.protobuf.Empty.sense.module.v1.SkillsResponseL
	GetNearby.sense.module.v1.NearbyRequest.sense.module.v1.NearbyResponseD
	GetTarget.google.protobuf.Empty.sense.module.v1.TargetResponseL
SelectTarget$.sense.module.v1.SelectTargetRequest.google.protobuf.Empty=
ClearTarget.google.protobuf.Empty.google.protobuf.EmptyF
	QueryName!.sense.module.v1.QueryNameRequest.google.protobuf.EmptyA
Status.google.protobuf.Empty.sense.module.v1.StatusResponseP
DebugMonsterMoves.google.protobuf.Empty#.sense.module.v1.MonsterMoveSamplesA
DebugFields.google.protobuf.Empty.sense.module.v1.FieldDumpC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB;Z9github.com/doumoman/agent-wow-mods/sense/src/api;modulev1bproto3