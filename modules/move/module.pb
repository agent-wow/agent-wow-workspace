
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
Ü
module.protomove.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"V
Position
x (Rx
y (Ry
z (Rz 
orientation (Rorientation"`
PositionResponse4
position (2.move.module.v1.PositionRposition
moving (Rmoving"#
MoveRequest
yards (Ryards"'
TurnRequest
radians (Rradians"1
TurnToRequest 
orientation (Rorientation"9
MoveToRequest
x (Rx
y (Ry
z (Rz";
RunSpeedRequest(
yards_per_second (RyardsPerSecond"Û
StatusResponse4
position (2.move.module.v1.PositionRposition!
has_position (RhasPosition
moving (Rmoving
	run_speed (RrunSpeed
packets (Rpackets!
parse_errors (RparseErrors2¬
ModuleG
GetPosition.google.protobuf.Empty .move.module.v1.PositionResponseH
Forward.move.module.v1.MoveRequest .move.module.v1.PositionResponseI
Backward.move.module.v1.MoveRequest .move.module.v1.PositionResponseI
TurnLeft.move.module.v1.TurnRequest .move.module.v1.PositionResponseJ
	TurnRight.move.module.v1.TurnRequest .move.module.v1.PositionResponseI
TurnTo.move.module.v1.TurnToRequest .move.module.v1.PositionResponseI
MoveTo.move.module.v1.MoveToRequest .move.module.v1.PositionResponse@
Stop.google.protobuf.Empty .move.module.v1.PositionResponseN
SetRunSpeed.move.module.v1.RunSpeedRequest.move.module.v1.StatusResponse@
Status.google.protobuf.Empty.move.module.v1.StatusResponseC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB:Z8github.com/doumoman/agent-wow-mods/move/src/api;modulev1bproto3