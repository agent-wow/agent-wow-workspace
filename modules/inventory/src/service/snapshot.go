package service

import (
	"sort"

	modulev1 "github.com/doumoman/agent-wow-mods/inventory/src/api"
	"github.com/doumoman/agent-wow-mods/wow"
)

func (s *Service) snapshot() *modulev1.InventoryState {
	s.mu.RLock()
	defer s.mu.RUnlock()

	state := &modulev1.InventoryState{
		VendorGuid: uint64(s.vendor), VendorLoaded: s.vendorLoaded,
		FailureCount: s.failureCount, ItemQueries: s.itemQueries,
		ParseErrors: s.parseErrors, Revision: s.revision,
		Trade: s.tradeSnapshotLocked(),
	}
	if s.lastFailure != nil {
		failure := *s.lastFailure
		state.LastFailure = &failure
	}
	var selfFields wow.UpdateFields
	if self := s.objects[s.selfGUID]; self != nil {
		selfFields = self.fields
		if money, ok := selfFields.Uint32(wow.PlayerCoinage); ok {
			state.Money = money
		}
	}

	playerSlots := wow.DecodePlayerSlots(selfFields)
	containers := make(map[wow.GUID]wow.PlayerSlot)
	capacity := 16
	used := 0
	for _, slot := range playerSlots {
		if slot.Slot >= 74 {
			item := s.itemLocked(slot.GUID, slot.Bag, slot.Slot, false)
			if price, ok := wow.BuybackPrice(selfFields, slot.Slot); ok {
				item.BuyPrice = int64(price)
			}
			state.Buyback = append(state.Buyback, item)
			continue
		}
		if slot.Slot >= 19 && slot.Slot <= 22 {
			containers[slot.GUID] = slot
			if container := s.objects[slot.GUID]; container != nil {
				slots, ok := container.fields.Uint32(wow.ContainerFieldNumSlots)
				if !ok || slots == 0 {
					slots = s.templateLocked(container.fields).ContainerSlots
				}
				capacity += int(slots)
			}
		}
		if slot.Slot >= 23 && slot.Slot <= 38 {
			used++
		}
		state.Items = append(state.Items, s.itemLocked(slot.GUID, slot.Bag, slot.Slot, slot.Slot <= 22))
	}
	for containerGUID, containerSlot := range containers {
		container := s.objects[containerGUID]
		if container == nil {
			continue
		}
		for slot, itemGUID := range wow.DecodeContainerSlots(container.fields) {
			used++
			state.Items = append(state.Items, s.itemLocked(itemGUID, containerSlot.Slot, slot, false))
		}
	}
	if used < capacity {
		state.FreeSlots = uint32(capacity - used)
	}

	state.VendorItems = make([]*modulev1.Item, 0, len(s.vendorItems))
	for _, row := range s.vendorItems {
		item := s.templateItemLocked(row.ItemID)
		item.Slot = row.Slot
		item.Bag = uint32(wow.InventorySlotBag0)
		item.Stock = row.Stock
		item.Count = row.BuyCount
		item.BuyPrice = int64(row.Price)
		state.VendorItems = append(state.VendorItems, item)
	}
	sort.Slice(state.Items, func(i, j int) bool {
		a, b := state.Items[i], state.Items[j]
		if a.GetBag() != b.GetBag() {
			return a.GetBag() < b.GetBag()
		}
		if a.GetSlot() != b.GetSlot() {
			return a.GetSlot() < b.GetSlot()
		}
		return a.GetGuid() < b.GetGuid()
	})
	sort.Slice(state.Buyback, func(i, j int) bool { return state.Buyback[i].GetSlot() < state.Buyback[j].GetSlot() })
	for itemID := range s.templates {
		state.ItemTemplates = append(state.ItemTemplates, s.templateItemLocked(itemID))
	}
	sort.Slice(state.ItemTemplates, func(i, j int) bool { return state.ItemTemplates[i].GetItemId() < state.ItemTemplates[j].GetItemId() })
	return state
}

func (s *Service) itemLocked(guid wow.GUID, bag, slot uint8, equipped bool) *modulev1.Item {
	item := &modulev1.Item{
		Guid: uint64(guid), Bag: uint32(bag), Slot: uint32(slot), Count: 1,
		Equipped: equipped, EquipmentSlot: -1, Stock: -1,
	}
	object := s.objects[guid]
	if object == nil {
		return item
	}
	if entry, ok := object.fields.Uint32(wow.ObjectFieldEntry); ok {
		item.ItemId = entry
	}
	if count, ok := object.fields.Uint32(wow.ItemFieldStackCount); ok && count > 0 {
		item.Count = count
	}
	template := s.templateLocked(object.fields)
	s.applyTemplateLocked(item, template)
	if durability, ok := object.fields.Uint32(wow.ItemFieldDurability); ok {
		item.Durability = durability
	}
	if maxDurability, ok := object.fields.Uint32(wow.ItemFieldMaxDurability); ok {
		item.MaxDurability = maxDurability
	}
	if slots, ok := object.fields.Uint32(wow.ContainerFieldNumSlots); ok {
		item.ContainerSlots = slots
	}
	return item
}

func (s *Service) templateItemLocked(itemID uint32) *modulev1.Item {
	item := &modulev1.Item{ItemId: itemID, Count: 1, EquipmentSlot: -1, Stock: -1}
	s.applyTemplateLocked(item, s.templates[itemID])
	return item
}

func (s *Service) templateLocked(fields wow.UpdateFields) wow.ItemTemplate {
	entry, _ := fields.Uint32(wow.ObjectFieldEntry)
	return s.templates[entry]
}

func (s *Service) applyTemplateLocked(item *modulev1.Item, template wow.ItemTemplate) {
	if template.ItemID == 0 || template.Unknown {
		return
	}
	item.TemplateKnown = true
	item.Name = template.Name
	item.Quality = template.Quality
	item.ClassId = template.ClassID
	item.Subclass = template.Subclass
	item.InventoryType = template.InventoryType
	item.RequiredLevel = template.RequiredLevel
	item.RequiredClassMask = template.AllowableClass
	item.RequiredSkill = template.RequiredSkill
	item.RequiredSkillRank = template.RequiredSkillRank
	item.Armor = template.Armor
	item.Dps = template.DPS
	item.SellPrice = template.SellPrice
	item.BuyPrice = int64(template.BuyPrice)
	item.ContainerSlots = template.ContainerSlots
	item.QuestItem = template.ClassID == 12 || template.StartQuest != 0
	item.Bonding = template.Bonding
	item.MaxDurability = template.MaxDurability
	item.Stats = make(map[string]int32, len(template.Stats))
	for _, stat := range template.Stats {
		item.Stats[stat.Name] += stat.Value
	}
	if template.EquipmentSlot != nil {
		item.EquipmentSlot = int32(*template.EquipmentSlot)
	}
}
