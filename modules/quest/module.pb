
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
È
module.protoquest.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"\
QuestObjective
entry (Rentry
current (Rcurrent
required (Rrequired"õ
Quest
quest_id (RquestId
title (	Rtitle
level (Rlevel
icon (Ricon
flags (Rflags

repeatable (R
repeatable
complete (Rcomplete?

objectives (2.quest.module.v1.QuestObjectiveR
objectives"Å

QuestState
npc_guid (RnpcGuid*
menu (2.quest.module.v1.QuestRmenu.
active (2.quest.module.v1.QuestRactive
accepts (Raccepts
	completed (R	completed
errors (Rerrors

last_error (	R	lastError"
last_quest_id (RlastQuestId-
turning_in_quest_id	 (RturningInQuestId"#
GossipRequest
guid (Rguid"=
QuestRequest
guid (Rguid
quest_id (RquestId"a
TurnInRequest
guid (Rguid
quest_id (RquestId!
reward_index (RrewardIndex"š
StatusResponse
packets (Rpackets!
parse_errors (RparseErrors
gossips (Rgossips'
accept_requests (RacceptRequests'
reward_requests (RrewardRequests
turn_ins (RturnIns

last_error (	R	lastError%
reward_choices (RrewardChoices2¥
ModuleE
Gossip.quest.module.v1.GossipRequest.quest.module.v1.QuestStateD
Accept.quest.module.v1.QuestRequest.quest.module.v1.QuestStateE
TurnIn.quest.module.v1.TurnInRequest.quest.module.v1.QuestState?
GetState.google.protobuf.Empty.quest.module.v1.QuestStateA
Status.google.protobuf.Empty.quest.module.v1.StatusResponseC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB;Z9github.com/doumoman/agent-wow-mods/quest/src/api;modulev1bproto3