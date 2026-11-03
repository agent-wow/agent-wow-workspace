
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
 
module.protoloot.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"9
LootItem
item_id (RitemId
count (Rcount"Ü
	LootState
opened (Ropened
items (Ritems
money (Rmoney
errors (Rerrors
	auto_loot (RautoLoot

last_error (	R	lastError7

last_items (2.loot.module.v1.LootItemR	lastItems"!
LootRequest
guid (Rguid"+
AutoLootRequest
enabled (Renabled2—
Module=
GetState.google.protobuf.Empty.loot.module.v1.LootState>
Loot.loot.module.v1.LootRequest.loot.module.v1.LootStateI
SetAutoLoot.loot.module.v1.AutoLootRequest.loot.module.v1.LootStateC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB:Z8github.com/doumoman/agent-wow-mods/loot/src/api;modulev1bproto3