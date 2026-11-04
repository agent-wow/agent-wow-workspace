
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
·"
module.protoinventory.module.v1api/module/v1/session.protogoogle/protobuf/empty.proto"¬
Item
guid (Rguid
item_id (RitemId
name (	Rname
quality (Rquality
class_id (RclassId
subclass (Rsubclass%
inventory_type (RinventoryType%
equipment_slot (RequipmentSlot%
required_level	 (RrequiredLevel.
required_class_mask
 (RrequiredClassMask%
required_skill (RrequiredSkill.
required_skill_rank (RrequiredSkillRank:
stats (2$.inventory.module.v1.Item.StatsEntryRstats
armor (Rarmor
dps (Rdps

sell_price (R	sellPrice
	buy_price (RbuyPrice
count (Rcount
bag (Rbag
slot (Rslot'
container_slots (RcontainerSlots
equipped (Requipped

quest_item (R	questItem
stock (Rstock

durability (R
durability%
max_durability (RmaxDurability%
template_known (RtemplateKnown
bonding (Rbonding8

StatsEntry
key (	Rkey
value (Rvalue:8"´
Failure
	operation (	R	operation
result (Rresult
	item_guid (RitemGuid
item_id (RitemId%
required_level (RrequiredLevel
detail (	Rdetail"š
InventoryState/
items (2.inventory.module.v1.ItemRitems<
vendor_items (2.inventory.module.v1.ItemRvendorItems3
buyback (2.inventory.module.v1.ItemRbuyback
money (Rmoney

free_slots (R	freeSlots
vendor_guid (R
vendorGuid?
last_failure (2.inventory.module.v1.FailureRlastFailure#
failure_count (RfailureCount!
item_queries	 (RitemQueries!
parse_errors
 (RparseErrors#
vendor_loaded (RvendorLoaded
revision (Rrevision5
trade (2.inventory.module.v1.TradeStateRtrade@
item_templates (2.inventory.module.v1.ItemRitemTemplates(
selected_ammo_id (RselectedAmmoId"0
VendorRequest
vendor_guid (R
vendorGuid"$
	ItemQuery
item_id (RitemId"&
AmmoRequest
item_id (RitemId"p

BuyRequest
vendor_guid (R
vendorGuid
item_id (RitemId
slot (Rslot
count (Rcount"a
SellRequest
vendor_guid (R
vendorGuid
	item_guid (RitemGuid
count (Rcount"s
RepairRequest
vendor_guid (R
vendorGuid
	item_guid (RitemGuid$
use_guild_bank (RuseGuildBank"V
EquipRequest
	item_guid (RitemGuid)
destination_slot (RdestinationSlot"E
BuybackRequest
vendor_guid (R
vendorGuid
slot (Rslot"w
	TradeItem
slot (Rslot
item_id (RitemId
count (Rcount'
random_property (RrandomProperty" 

TradeState
trade_id (RtradeId
revision (Rrevision
pending (Rpending
active (Ractive
trader_guid (R
traderGuid!
own_accepted (RownAccepted'
trader_accepted (RtraderAccepted
complete (Rcomplete
	cancelled	 (R	cancelled
last_status
 (R
lastStatus!
close_result (RcloseResult;
	own_items (2.inventory.module.v1.TradeItemRownItemsA
trader_items (2.inventory.module.v1.TradeItemRtraderItems
	own_money (RownMoney!
trader_money (RtraderMoney"‰
TradeRequest
action (	Raction
trade_id (RtradeId+
expected_revision (RexpectedRevision
target_guid (R
targetGuid

trade_slot (R	tradeSlot
bag (Rbag
slot (Rslot
	item_guid (RitemGuid
copper	 (Rcopper2ú
ModuleG
GetState.google.protobuf.Empty#.inventory.module.v1.InventoryStateX
ListInventory".inventory.module.v1.VendorRequest#.inventory.module.v1.InventoryStateK
Buy.inventory.module.v1.BuyRequest#.inventory.module.v1.InventoryStateM
Sell .inventory.module.v1.SellRequest#.inventory.module.v1.InventoryStateQ
Repair".inventory.module.v1.RepairRequest#.inventory.module.v1.InventoryStateO
Equip!.inventory.module.v1.EquipRequest#.inventory.module.v1.InventoryStateP
SetAmmo .inventory.module.v1.AmmoRequest#.inventory.module.v1.InventoryStateS
Buyback#.inventory.module.v1.BuybackRequest#.inventory.module.v1.InventoryStateO
Trade!.inventory.module.v1.TradeRequest#.inventory.module.v1.InventoryStateP
	QueryItem.inventory.module.v1.ItemQuery#.inventory.module.v1.InventoryStateC
OnPacket.agentwow.module.v1.WorldPacket.google.protobuf.EmptyB?Z=github.com/doumoman/agent-wow-mods/inventory/src/api;modulev1bproto3