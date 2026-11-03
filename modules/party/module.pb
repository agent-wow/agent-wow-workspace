
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
ü
module.protoparty.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"„

PartyState
invites (Rinvites
accepts (Raccepts
auto_accept (R
autoAccept!
last_inviter (	RlastInviter"-
AutoAcceptRequest
enabled (Renabled2¥
Module?
GetState.google.protobuf.Empty.party.module.v1.PartyStateP
SetAutoAccept".party.module.v1.AutoAcceptRequest.party.module.v1.PartyStateC
AcceptInvite.google.protobuf.Empty.party.module.v1.PartyStateC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB;Z9github.com/doumoman/agent-wow-mods/party/src/api;modulev1bproto3