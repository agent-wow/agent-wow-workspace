
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
©
module.protosense.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"3
Vector3
x (Rx
y (Ry
z (Rz"Ü
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
money (Rmoney"£
Skill
id (Rid
name (	Rname
category (Rcategory
value (Rvalue
max (Rmax
step (Rstep

profession (R
profession"›
SelfResponse)
unit (2.sense.module.v1.UnitRunit
map_id (RmapId
in_world (RinWorld.
skills (2.sense.module.v1.SkillRskills"@
SkillsResponse.
skills (2.sense.module.v1.SkillRskills"=
NearbyRequest
radius (Rradius
limit (Rlimit"^
NearbyResponse+
units (2.sense.module.v1.UnitRunits
known_units (R
knownUnits"{
TargetResponse

has_target (R	hasTarget
target_guid (R
targetGuid)
unit (2.sense.module.v1.UnitRunit")
SelectTargetRequest
guid (Rguid"&
QueryNameRequest
guid (Rguid"¼
StatusResponse
packets (Rpackets!
parse_errors (RparseErrors
known_units (R
knownUnits!
name_queries (RnameQueries)
creature_queries (RcreatureQueries"0
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