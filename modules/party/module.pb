
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

module.protoparty.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"¤
Contact
guid (Rguid
flags (Rflags
note (	Rnote
status (Rstatus
area (Rarea
level (Rlevel
class_id (RclassId"¦
	WhoPlayer
name (	Rname
guild (	Rguild
level (Rlevel
class_id (RclassId
race (Rrace
gender (Rgender
zone (Rzone"
Invite
	invite_id (RinviteId
inviter (	Rinviter
received_at (R
receivedAt

expires_at (R	expiresAt"®
GroupMember
name (	Rname
guid (Rguid
status (Rstatus
subgroup (Rsubgroup
flags (Rflags
roles (Rroles
is_self (RisSelf"À
ChatMessage
epoch (Repoch
sequence (Rsequence
sender_guid (R
senderGuid#
receiver_guid (RreceiverGuid
message (	Rmessage
received_at (R
receivedAt"š

PartyState
epoch (Repoch
revision (Rrevision4
contacts (2.party.module.v1.ContactRcontacts,
who (2.party.module.v1.WhoPlayerRwho>
pending_invite (2.party.module.v1.InviteRpendingInvite
in_group (RinGroup

group_guid (R	groupGuid
leader_guid (R
leaderGuid6
members	 (2.party.module.v1.GroupMemberRmembers
loot_method
 (R
lootMethod,
master_looter_guid (RmasterLooterGuid%
loot_threshold (RlootThreshold-
dungeon_difficulty (RdungeonDifficulty8
messages (2.party.module.v1.ChatMessageRmessages!
parse_errors (RparseErrors!
sent_actions (RsentActions"!
NameRequest
name (	Rname":
AddFriendRequest
name (	Rname
note (	Rnote"Z

WhoRequest
name (	Rname
	min_level (RminLevel
	max_level (RmaxLevel"_
InviteResponse
	invite_id (RinviteId
inviter (	Rinviter
accept (Raccept"j
LootMethodRequest
method (Rmethod
master_guid (R
masterGuid
	threshold (R	threshold"'
ChatRequest
message (	Rmessage2Ù
Module?
GetState.google.protobuf.Empty.party.module.v1.PartyStateF
RequestContacts.google.protobuf.Empty.party.module.v1.PartyStateK
	AddFriend!.party.module.v1.AddFriendRequest.party.module.v1.PartyState?
Who.party.module.v1.WhoRequest.party.module.v1.PartyStateC
Invite.party.module.v1.NameRequest.party.module.v1.PartyStateM
RespondInvite.party.module.v1.InviteResponse.party.module.v1.PartyStateP
SetLootMethod".party.module.v1.LootMethodRequest.party.module.v1.PartyStateA

LeaveParty.google.protobuf.Empty.party.module.v1.PartyStateJ
SendPartyChat.party.module.v1.ChatRequest.party.module.v1.PartyStateC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB;Z9github.com/doumoman/agent-wow-mods/party/src/api;modulev1bproto3