
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
š
module.protocombat.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"#
AttackRequest
guid (Rguid"I
CastRequest
spell_id (RspellId
target_guid (R
targetGuid"Ç
CombatState

has_target (R	hasTarget
target_guid (R
targetGuid
	attacking (R	attacking
	in_combat (RinCombat#
target_health (RtargetHealth*
target_max_health (RtargetMaxHealth
target_dead (R
targetDead
self_health (R
selfHealth&
self_max_health	 (RselfMaxHealth
	self_dead
 (RselfDead

self_level (R	selfLevel
self_xp (RselfXp+
self_next_level_xp (RselfNextLevelXp
kills (Rkills
deaths (Rdeaths
last_xp (RlastXp
last_victim (R
lastVictim

last_error (	R	lastError
refusals (Rrefusals
	level_ups (RlevelUps#
cast_failures (RcastFailures
	self_rage (RselfRage"W
SpiritHealerRequest
healer_guid (R
healerGuid
packed_guid (R
packedGuid"Ç
StatusResponse
packets (Rpackets!
parse_errors (RparseErrors#
attack_starts (RattackStarts!
attack_stops (RattackStops

last_error (	R	lastError'
gossip_messages (RgossipMessages4
spirit_healer_confirms (RspiritHealerConfirms2
corpse_reclaim_delays (RcorpseReclaimDelays2š
ModuleH
Attack.combat.module.v1.AttackRequest.combat.module.v1.CombatStateC

StopAttack.google.protobuf.Empty.combat.module.v1.CombatStateD
Cast.combat.module.v1.CastRequest.combat.module.v1.CombatStateF
ReleaseSpirit.google.protobuf.Empty.combat.module.v1.CombatStateF
ReclaimCorpse.google.protobuf.Empty.combat.module.v1.CombatState_
ResurrectAtSpiritHealer%.combat.module.v1.SpiritHealerRequest.combat.module.v1.CombatStateA
GetState.google.protobuf.Empty.combat.module.v1.CombatStateB
Status.google.protobuf.Empty .combat.module.v1.StatusResponseC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB<Z:github.com/doumoman/agent-wow-mods/combat/src/api;modulev1bproto3