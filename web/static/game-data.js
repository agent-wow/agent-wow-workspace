/* Static WoW 3.3.5a ids -> names, for the character info panel. */

const RACE_NAMES = {
  1: "人类", 2: "兽人", 3: "矮人", 4: "暗夜精灵", 5: "亡灵",
  6: "牛头人", 7: "侏儒", 8: "巨魔", 11: "血精灵", 12: "德莱尼",
};

const CLASS_NAMES = {
  1: "战士", 2: "圣骑士", 3: "猎人", 4: "盗贼", 5: "牧师",
  6: "死亡骑士", 7: "萨满", 8: "法师", 9: "术士", 11: "德鲁伊",
};

// Map ids are the ones the server itself reports in the character list
// (e.g. Outland covers Silvermoon City / Eversong Woods as sub-zones).
const MAP_NAMES = {
  0: "东部王国", 1: "卡利姆多", 530: "外域", 571: "诺森德",
};

// Alliance races vs Horde races (WotLK).
const ALLIANCE_RACES = [1, 3, 4, 7, 11, 12];

// Skill line ids -> Chinese names.
//
// The client data this workspace reads only ships enUS strings, so the
// English names come from the module and this table is the Chinese overlay.
// It covers the professions and the gathering / life skills; anything else
// falls back to the English name.
const SKILL_NAMES = {
  // 职业
  164: "锻造", 165: "制皮", 171: "炼金术", 182: "草药学", 186: "采矿",
  197: "裁缝", 202: "工程学", 333: "附魔", 393: "剥皮", 755: "珠宝加工",
  773: "铭文",
  // 采集与生活
  129: "急救", 142: "生存", 155: "游泳", 185: "烹饪", 356: "钓鱼",
  762: "骑术", 148: "马骑术", 149: "狼骑术", 150: "虎骑术", 152: "山羊骑术",
  533: "迅猛龙骑术", 553: "陆行鸟操控", 554: "亡灵骑术", 713: "科多巴骑术",
  // 种族
  101: "矮人种族", 124: "牛头人种族", 125: "兽人种族", 126: "暗夜精灵种族",
  220: "亡灵种族", 733: "巨魔种族", 753: "侏儒种族", 754: "人类种族",
  756: "血精灵种族", 760: "德莱尼种族",
};

function raceName(id) { return RACE_NAMES[id] || ("种族 #" + id); }
function className(id) { return CLASS_NAMES[id] || ("职业 #" + id); }
function mapName(id) { return MAP_NAMES[id] || ("地图 #" + id); }
function skillName(id, fallback) { return SKILL_NAMES[id] || fallback || ("技能 #" + id); }
function factionName(raceId) {
  if (!raceId) return "-";
  return ALLIANCE_RACES.includes(raceId) ? "联盟" : "部落";
}

// Copper totals come over the wire as a uint64, so protobuf JSON renders
// them as a string.
function moneyText(copper) {
  const n = Number(copper || 0);
  const gold = Math.floor(n / 10000);
  const silver = Math.floor((n % 10000) / 100);
  const copperOnly = n % 100;
  const parts = [];
  if (gold) parts.push(`${gold} 金`);
  if (silver || gold) parts.push(`${silver} 银`);
  parts.push(`${copperOnly} 铜`);
  return parts.join(" ");
}
