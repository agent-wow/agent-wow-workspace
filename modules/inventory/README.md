# inventory

Server-backed equipment, bags, item templates and NPC item actions. The module
reports facts and sends bounded packets; the growth agent decides what to equip,
buy, sell or repair and reconciles every mutation from a later state snapshot.

Sparse `SMSG_UPDATE_OBJECT` fields are accumulated for the player, items and
containers. Unknown item entries are queried asynchronously with
`CMSG_ITEM_QUERY_SINGLE`; packet callbacks never call `SendPacket` directly.
World-entry packets clear object and vendor state, while static item-template
cache entries remain valid for the process lifetime.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `inventory.get_state` | — | `InventoryState` | Items, bags, buyback, money, free slots, vendor rows and failures |
| `inventory.list_inventory` | `vendorGuid` | `InventoryState` | Requests a vendor list; `vendorLoaded=false` until the reply arrives |
| `inventory.buy` | `vendorGuid`, `itemId`, `slot`, `count` | `InventoryState` | `slot` is the server's one-based vendor row |
| `inventory.sell` | `vendorGuid`, `itemGuid`, `count` | `InventoryState` | `count=0` sells the whole stack |
| `inventory.repair` | `vendorGuid`, `itemGuid`, `useGuildBank` | `InventoryState` | `itemGuid=0` repairs all; growth never uses guild funds |
| `inventory.equip` | `itemGuid`, `destinationSlot` | `InventoryState` | Equipment destination is 0..18 |
| `inventory.set_ammo` | `itemId` | `InventoryState` | Selects owned projectile item through `CMSG_SET_AMMO`; verify `selectedAmmoId` |
| `inventory.buyback` | `vendorGuid`, `slot` | `InventoryState` | Absolute buyback slot 74..85 |

A successful send is not proof of a purchase, sale, repair or equip. Sell and
repair do not provide a dedicated success reply suitable for commit-as-ack.
Callers must compare `revision`, item counts, equipment slots, money, durability,
buyback and `lastFailure` before confirming an intent. `templateKnown=false`
means stats, price, quality and eligibility are not safe decision inputs yet.

## Item facts

`Item` includes GUID, entry, template name/quality/class, inventory and equipment
slots, level/class/skill requirements, stats, armor, weapon DPS, prices, stack
count, bag/slot, container capacity, quest-item marker, stock and durability.
Bag 255 denotes player equipment, backpack and buyback positions; bag 19..22
denotes contents of the corresponding equipped container. Vendor stock `-1`
means unlimited.

## Subscribed packets

- Object create/update/out-of-range and destroy packets, including compressed
  updates and world-entry resets.
- `SMSG_ITEM_QUERY_SINGLE_RESPONSE` and `SMSG_LIST_INVENTORY`.
- `SMSG_BUY_FAILED`, `SMSG_SELL_ITEM` failure replies and
  `SMSG_INVENTORY_CHANGE_FAILURE`.

## Preconditions

The server enforces interaction range, ownership, money, stock, level, class,
skill, binding and equipment rules. This module validates only packet-shape
invariants such as nonzero GUIDs, positive buy counts and valid slot ranges.

## Verified

Local only: shared codec tests and module race tests cover sparse-field merging,
container/free-slot calculation, buyback prices, vendor rows, request layouts,
action validation and equipment-failure reporting. No inventory RPC has been
run in a live game session in this unit; live buy/sell/equip/full-bag recovery
remains an acceptance gate.
