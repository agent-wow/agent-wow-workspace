
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
è
module.protocharacter.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"3
TrainerRequest!
trainer_guid (RtrainerGuid"O
BuySpellRequest!
trainer_guid (RtrainerGuid
spell_id (RspellId"Ê
TrainerSpell
spell_id (RspellId
usable (Rusable

money_cost (R	moneyCost2
profession_point_cost (RprofessionPointCost%
required_level (RrequiredLevel.
required_skill_line (RrequiredSkillLine.
required_skill_rank (RrequiredSkillRank-
required_abilities (RrequiredAbilities"X
TalentLearnRequest
	talent_id (RtalentId%
requested_rank (RrequestedRank"=

TalentRank
	talent_id (RtalentId
rank (Rrank"v
TalentGroup
index (Rindex
active (Ractive9
talents (2.character.module.v1.TalentRankRtalents"*
GameObjectUseRequest
guid (Rguid"Õ
CharacterState!
trainer_guid (RtrainerGuid!
trainer_type (RtrainerType
greeting (	RgreetingH
trainer_spells (2!.character.module.v1.TrainerSpellRtrainerSpells!
trainer_open (RtrainerOpen)
last_buy_spell_id (RlastBuySpellId,
last_buy_succeeded (RlastBuySucceeded&
last_buy_result (RlastBuyResult
has_talents	 (R
hasTalents,
free_talent_points
 (RfreeTalentPoints.
active_talent_group (RactiveTalentGroupE
talent_groups (2 .character.module.v1.TalentGroupRtalentGroups1
last_game_object_guid (RlastGameObjectGuid(
game_object_uses (RgameObjectUses

last_error (	R	lastError1
last_buy_result_known (RlastBuyResultKnown"‰
StatusResponse
packets (Rpackets!
parse_errors (RparseErrors#
trainer_lists (RtrainerLists!
trainer_buys (RtrainerBuys)
talent_snapshots (RtalentSnapshots(
game_object_uses (RgameObjectUses

last_error (	R	lastError2Ë
ModuleW
OpenTrainer#.character.module.v1.TrainerRequest#.character.module.v1.CharacterStateU
BuySpell$.character.module.v1.BuySpellRequest#.character.module.v1.CharacterState[
LearnTalent'.character.module.v1.TalentLearnRequest#.character.module.v1.CharacterState_
UseGameObject).character.module.v1.GameObjectUseRequest#.character.module.v1.CharacterStateG
GetState.google.protobuf.Empty#.character.module.v1.CharacterStateE
Status.google.protobuf.Empty#.character.module.v1.StatusResponseC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB?Z=github.com/doumoman/agent-wow-mods/character/src/api;modulev1bproto3