/* WoW console: account cards, character lists, session start/stop, character creation. */
const state = { accounts: [], colima: null };
const ui = {}; // per account: charsOpen, chars, charsError, logsOpen, logs, self, selfError, busy, form
let refreshing = false;
let creationOpts = null;
let modalAction = null; // pending confirm handler for the modal

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function uiOf(name) {
  if (!ui[name]) ui[name] = { form: {} };
  if (!ui[name].form) ui[name].form = {};
  return ui[name];
}

async function api(path, options) {
  const res = await fetch(path, options);
  return res.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

let toastTimer = null;
function toast(msg, isError) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 9000 : 5000);
}

// ------------------------------------------------------------------ data

async function loadState() {
  try {
    const data = await api("/api/state");
    state.accounts = data.accounts || [];
    state.colima = data.colima;
    renderColima();
    renderAll();
  } catch (e) {
    toast("无法连接后端: " + e.message, true);
  }
}

async function loadSelf(name) {
  const u = uiOf(name);
  try {
    const data = await api("/api/self?account=" + encodeURIComponent(name));
    if (data.error) {
      u.selfError = data.error;
    } else {
      u.self = data.self;
      u.selfError = null;
    }
  } catch (e) {
    u.selfError = e.message;
  }
  renderSelf(name);
}

async function refreshAll() {
  if (refreshing) return;
  refreshing = true;
  try {
    await loadState();
    await Promise.all(state.accounts.filter(a => a.online).map(a => loadSelf(a.name)));
  } finally {
    refreshing = false;
  }
}

// ------------------------------------------------------------------ render

function renderColima() {
  const el = document.getElementById("colima-badge");
  if (state.colima === null) return;
  el.textContent = state.colima ? "Colima 已连接" : "Colima 不可用";
  el.className = "badge " + (state.colima ? "online" : "combat");
}

function renderAll(force) {
  const main = document.getElementById("accounts");
  const active = document.activeElement;
  const editing = active && main.contains(active) &&
    (active.tagName === "INPUT" || active.tagName === "TEXTAREA" ||
     active.tagName === "SELECT" || active.isContentEditable);

  // The 6s poll must not swap the DOM out from under a form field: that is
  // what made the character-name input lose focus. Explicit UI actions pass
  // force=true and get their focus/caret restored below.
  if (editing && !force) return;

  const focusId = editing ? active.id : "";
  let selStart = null, selEnd = null, selDir = null;
  if (editing && typeof active.selectionStart === "number") {
    selStart = active.selectionStart;
    selEnd = active.selectionEnd;
    selDir = active.selectionDirection;
  }

  main.innerHTML = state.accounts.map(cardHTML).join("") ||
    '<div class="hint">没有发现账号，先用 scripts/account.sh 添加。</div>';

  if (focusId) {
    const el = document.getElementById(focusId);
    if (el) {
      el.focus({ preventScroll: true });
      if (selStart !== null && typeof el.setSelectionRange === "function") {
        try {
          el.setSelectionRange(selStart, selEnd, selDir || "none");
        } catch (e) { /* select elements do not support selection ranges */ }
      }
    }
  }
}

function cardHTML(a) {
  const name = a.name;
  const u = uiOf(name);
  const human = a.agentAllowed === false;
  const statusBadge = a.online
    ? '<span class="badge online">在线</span>'
    : '<span class="badge offline">离线</span>';
  const humanBadge = human ? '<span class="badge muted">人用账号 · 手动</span>' : "";
  const busy = u.busy ? "disabled" : "";

  let actions;
  if (a.online) {
    actions = `
      <button class="btn danger" ${busy} onclick="stopAccount('${name}')">下线</button>
      <button class="btn" ${busy} onclick="loadSelf('${name}')">刷新状态</button>`;
  } else {
    actions = `
      <button class="btn primary" ${busy} onclick="toggleChars('${name}')">
        ${u.charsOpen ? "收起角色列表" : "选择角色上线"}</button>
      <button class="btn" ${busy} onclick="toggleCreate('${name}')">
        ${u.createOpen ? "收起创建" : "创建角色"}</button>`;
  }

  return `
  <div class="card" id="card-${name}">
    <div class="card-head">
      <div>
        <div class="account-name">${esc(name)}</div>
        <div class="account-meta">${esc((a.username || "").toUpperCase())}${a.vmDir ? " · " + esc(a.vmDir) : ""}</div>
      </div>
      <div class="badges">
        <span class="badge port">:${a.port}</span>
        ${statusBadge}
        ${humanBadge}
      </div>
    </div>
    <div id="self-${name}">${a.online ? selfHTML(a) : ""}</div>
    ${!a.online && !u.charsOpen ? `<div class="hint">${human
      ? "人用账号：手动上线、建号、删号；掉线不会自动重连。"
      : "未在线。从角色列表选一个角色上线。"}</div>` : ""}
    <div class="card-actions">
      ${actions}
      <button class="btn ghost" ${busy} onclick="toggleLogs('${name}')">${u.logsOpen ? "收起日志" : "日志"}</button>
      ${u.busy ? '<span class="hint">操作中…</span>' : ""}
    </div>
    <div id="chars-${name}">${charsPanelHTML(a)}</div>
    <div id="create-${name}">${createPanelHTML(a)}</div>
    <div id="logs-${name}">${logsPanelHTML(a)}</div>
    <div class="card-footer">
      <button class="btn ghost small" ${busy} onclick="loginAccount('${name}')">重新登录</button>
      ${u.charsOpen ? `<button class="btn ghost small" ${busy} onclick="loadChars('${name}', true)">刷新角色</button>` : ""}
    </div>
  </div>`;
}

function selfHTML(a) {
  const u = uiOf(a.name);
  const self = u.self;
  if (!self || !self.unit) {
    const msg = u.selfError ? "正在进入世界…" : "读取角色状态…";
    return `<div class="hint">${msg}</div>`;
  }
  const unit = self.unit;
  const hpPct = unit.maxHealth ? Math.round(unit.health / unit.maxHealth * 100) : 0;
  const xpPct = unit.nextLevelXp ? Math.round(unit.xp / unit.nextLevelXp * 100) : 0;
  const pos = unit.position || {};
  const dead = unit.dead || (unit.playerFlags & 0x10) !== 0;
  const mapId = self.mapId || 0;
  const info = [
    ["等级", unit.level],
    ["种族", raceName(unit.raceId)],
    ["职业", className(unit.classId)],
    ["阵营", factionName(unit.raceId)],
    ["地图", mapName(mapId)],
    ["经验", `${xpPct}%`],
    ["金钱", moneyText(unit.money)],
  ];
  const professions = (self.skills || []).filter(s => s.profession);
  const skillsHTML = professions.length
    ? professions.map(s => `<span class="badge skill">${esc(skillName(s.id, s.name))}
        <b>${s.value || 0}/${s.max || 0}</b></span>`).join("")
    : '<span class="hint">尚未学习专业</span>';
  return `
    <div class="self-name">${esc(unit.name)}
      <span class="badge level">Lv ${unit.level}</span>
      ${unit.inCombat ? '<span class="badge combat">战斗中</span>' : ""}
      ${dead ? '<span class="badge combat">死亡</span>' : ""}
      ${self.inWorld === false ? '<span class="badge muted">未在世界中</span>' : ""}
    </div>
    <div class="info-grid">
      ${info.map(([k, v]) => `<div><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join("")}
    </div>
    <div class="skills"><span class="k">专业</span>${skillsHTML}</div>
    <div class="bar-row">
      <span class="bar-label">HP</span>
      <div class="bar"><div class="bar-fill hp ${hpPct < 35 ? "low" : ""}" style="width:${hpPct}%"></div></div>
      <span class="bar-value">${unit.health} / ${unit.maxHealth}</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">XP</span>
      <div class="bar"><div class="bar-fill xp" style="width:${xpPct}%"></div></div>
      <span class="bar-value">${unit.xp} / ${unit.nextLevelXp}</span>
    </div>
    <div class="coords">x ${Number(pos.x).toFixed(1)} · y ${Number(pos.y).toFixed(1)} · z ${Number(pos.z).toFixed(1)} · 朝向 ${Number(unit.orientation || 0).toFixed(2)} rad</div>`;
}

function renderSelf(name) {
  const a = state.accounts.find(x => x.name === name);
  const el = document.getElementById("self-" + name);
  if (a && el) el.innerHTML = a.online ? selfHTML(a) : "";
}

function charsPanelHTML(a) {
  const u = uiOf(a.name);
  if (!u.charsOpen) return "";
  if (a.online) return '<div class="hint">账号在线。先点「下线」再删除角色。</div>';
  if (u.loadingChars) return '<div class="loading">正在读取角色列表…</div>';
  if (u.charsError) {
    return `<div class="error-box">${esc(u.charsError)}</div>
      <div class="hint">会话失效时点「重新登录」恢复（会让该账号在线角色掉线）。</div>`;
  }
  const chars = u.chars || [];
  if (!chars.length) return '<div class="hint">该账号没有角色。</div>';
  const rows = chars.map(c => `
    <tr>
      <td><div class="char-name">${esc(c.name)}</div>
        <div class="char-sub">${esc(c.race)} · ${esc(c.class)}</div></td>
      <td class="num">${c.level}</td>
      <td class="char-sub">${esc(c.zone)}</td>
      <td class="num act"><button class="btn small primary" ${u.busy ? "disabled" : ""}
        onclick="startChar('${a.name}', '${esc(c.name)}')">上线</button></td>
      <td class="num act"><button class="btn small danger" ${u.busy ? "disabled" : ""}
        onclick="askDelete('${a.name}', '${esc(c.name)}', ${c.level})">删除</button></td>
    </tr>`).join("");
  return `<table class="chars-table">
    <thead><tr><th>角色</th><th style="text-align:right">等级</th><th>所在区域</th><th></th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function logsPanelHTML(a) {
  const u = uiOf(a.name);
  if (!u.logsOpen) return "";
  return `<div class="log-box">${esc(u.logs || "读取中…")}</div>`;
}

// ------------------------------------------------------------------ create

function createPanelHTML(a) {
  const u = uiOf(a.name);
  if (!u.createOpen) return "";
  if (!creationOpts) return '<div class="loading">加载种族 / 职业选项…</div>';
  const f = u.form;
  const raceOpts = creationOpts.races.map(r =>
    `<option value="${esc(r.id)}" ${f.race === r.id ? "selected" : ""}>${esc(r.label)}</option>`).join("");
  const currentRace = creationOpts.races.find(r => r.id === f.race) || creationOpts.races[0];
  const classOpts = currentRace.classes.map(c => {
    const label = (creationOpts.classes.find(x => x.id === c) || {}).label || c;
    return `<option value="${esc(c)}" ${f.class === c ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
  const genderOpts = creationOpts.genders.map(g =>
    `<option value="${esc(g.id)}" ${f.gender === g.id ? "selected" : ""}>${esc(g.label)}</option>`).join("");

  return `
  <div class="create-form">
    <div class="form-row">
      <label>角色名</label>
      <input type="text" id="create-name-${a.name}" placeholder="留空则随机" maxlength="12"
        value="${esc(f.name || "")}"
        oninput="saveForm('${a.name}','name',this.value)">
    </div>
    <div class="form-row">
      <label>种族</label>
      <select id="create-race-${a.name}"
        onchange="saveForm('${a.name}','race',this.value); onRaceChange('${a.name}')">${raceOpts}</select>
    </div>
    <div class="form-row">
      <label>职业</label>
      <select id="create-class-${a.name}" onchange="saveForm('${a.name}','class',this.value)">${classOpts}</select>
    </div>
    <div class="form-row">
      <label>性别</label>
      <select id="create-gender-${a.name}" onchange="saveForm('${a.name}','gender',this.value)">
        <option value="" ${!f.gender ? "selected" : ""}>随机</option>
        ${genderOpts}
      </select>
    </div>
    <div class="form-row form-actions">
      <button class="btn primary" ${u.busy ? "disabled" : ""} onclick="createChar('${a.name}')">创建角色</button>
      <button class="btn ghost" onclick="toggleCreate('${a.name}')">取消</button>
      ${u.busy ? '<span class="hint">创建中…</span>' : ""}
    </div>
    <div class="hint">职业只列出该种族可选的；留空的项由服务端按 3.3.5a 规则随机。</div>
  </div>`;
}

function saveForm(name, field, value) {
  uiOf(name).form[field] = value;
}

function onRaceChange(name) {
  const u = uiOf(name);
  if (!creationOpts) return;
  const race = creationOpts.races.find(r => r.id === u.form.race);
  if (race && !race.classes.includes(u.form.class)) {
    u.form.class = race.classes[0];
  }
  renderAll(true);
}

async function toggleCreate(name) {
  const u = uiOf(name);
  u.createOpen = !u.createOpen;
  if (u.createOpen && !creationOpts) {
    renderAll();
    try {
      creationOpts = await api("/api/creation-options");
    } catch (e) {
      toast("加载创建选项失败: " + e.message, true);
    }
  }
  if (creationOpts && !u.form.race) u.form.race = creationOpts.races[0].id;
  renderAll();
}

async function createChar(name) {
  const u = uiOf(name);
  const f = u.form;
  if (!f.name && !confirm("未填写角色名，将由服务端随机生成。继续？")) return;
  u.busy = true;
  renderAll();
  try {
    const data = await post("/api/create", {
      account: name, name: f.name || "", race: f.race || "",
      class: f.class || "", gender: f.gender || "",
    });
    if (data.ok) {
      toast("角色创建成功：" + (data.output || "").split("\n").pop());
      u.createOpen = false;
      u.charsOpen = true;
      await loadChars(name, true);
    } else {
      toast(data.output || data.error || "创建失败", true);
    }
  } catch (e) {
    toast("创建失败: " + e.message, true);
  } finally {
    u.busy = false;
    renderAll();
  }
}

// ------------------------------------------------------------------ actions

// The modal is the single place a destructive action is confirmed. The
// confirm button stays disabled until the input reads exactly DELETE, so a
// stray Enter or a mis-click cannot delete a character.
function openModal(title, bodyHTML, confirmLabel, onConfirm) {
  const modal = document.getElementById("modal");
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHTML;
  const btn = document.getElementById("modal-confirm");
  btn.textContent = confirmLabel;
  btn.disabled = true;
  const input = document.getElementById("modal-input");
  input.value = "";
  input.classList.remove("ok");
  modalAction = onConfirm;
  modal.classList.remove("hidden");
  setTimeout(() => input.focus(), 30);
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  modalAction = null;
}

function onModalInput(value) {
  const input = document.getElementById("modal-input");
  const ok = value.trim() === "DELETE";
  input.classList.toggle("ok", ok);
  document.getElementById("modal-confirm").disabled = !ok;
}

function confirmModal() {
  const action = modalAction;
  closeModal();
  if (action) action();
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
});

function askDelete(account, character, level) {
  openModal(
    "删除角色",
    `即将永久删除账号 <b>${esc(account)}</b> 下的角色 <b>${esc(character)}</b>（Lv ${level}）。<br>
     <span class="warn">此操作不可撤销，角色与背包物品将永久消失。</span>`,
    "删除角色",
    () => deleteChar(account, character),
  );
}

async function deleteChar(account, character) {
  const u = uiOf(account);
  u.busy = true;
  renderAll();
  try {
    const data = await post("/api/delete", { account, name: character, confirm: "DELETE" });
    if (data.ok) {
      toast(`角色 ${character} 已删除`);
      u.chars = null;
      u.charsError = null;
      await loadChars(account, true);
    } else {
      toast(data.output || data.error || "删除失败", true);
    }
  } catch (e) {
    toast("删除失败: " + e.message, true);
  } finally {
    u.busy = false;
    renderAll();
  }
}

async function toggleChars(name) {
  const u = uiOf(name);
  u.charsOpen = !u.charsOpen;
  if (u.charsOpen && !u.chars && !u.charsError) {
    await loadChars(name);
  } else {
    renderAll();
  }
}

async function loadChars(name, refresh) {
  const u = uiOf(name);
  u.loadingChars = true;
  u.charsError = null;
  renderAll();
  try {
    const data = await api("/api/chars?account=" + encodeURIComponent(name) + (refresh ? "&refresh=1" : ""));
    if (data.error) {
      u.charsError = data.error;
      u.chars = null;
    } else {
      u.chars = data.chars || [];
    }
  } catch (e) {
    u.charsError = e.message;
  } finally {
    u.loadingChars = false;
    renderAll();
  }
}

async function startChar(name, character) {
  const u = uiOf(name);
  u.busy = true;
  renderAll();
  toast(`正在启动 ${name} / ${character}（同步工作区并进入世界，约需 10–30 秒）…`);
  try {
    const data = await post("/api/start", { account: name, character });
    if (data.ok) {
      toast(`已启动 ${character}，正在进入世界…`);
      u.charsOpen = false;
      u.self = null;
      await loadState();
      // The client needs a few seconds to enter the world; poll for it.
      setTimeout(() => loadSelf(name), 4000);
      setTimeout(() => loadSelf(name), 10000);
    } else {
      toast(data.error || data.output || "启动失败", true);
    }
  } catch (e) {
    toast("启动失败: " + e.message, true);
  } finally {
    u.busy = false;
    await loadState();
    loadSelf(name);
  }
}

async function stopAccount(name) {
  if (!confirm(`确定让 ${name} 的角色下线？`)) return;
  const u = uiOf(name);
  u.busy = true;
  renderAll();
  try {
    const data = await post("/api/stop", { account: name });
    if (data.ok) {
      toast(`${name} 已下线`);
      u.self = null;
    } else {
      toast(data.error || data.output || "下线失败", true);
    }
  } catch (e) {
    toast("下线失败: " + e.message, true);
  } finally {
    u.busy = false;
    await loadState();
  }
}

async function loginAccount(name) {
  if (!confirm(`重新登录 ${name} 会刷新会话密钥，该账号已在线的角色会掉线。确定继续？`)) return;
  const u = uiOf(name);
  u.busy = true;
  renderAll();
  try {
    const data = await post("/api/login", { account: name });
    toast(data.ok ? `${name} 登录成功` : (data.output || "登录失败"), !data.ok);
    if (data.ok) {
      u.chars = null;
      u.charsError = null;
      if (u.charsOpen) await loadChars(name, true);
    }
  } catch (e) {
    toast("登录失败: " + e.message, true);
  } finally {
    u.busy = false;
    renderAll();
  }
}

async function toggleLogs(name) {
  const u = uiOf(name);
  u.logsOpen = !u.logsOpen;
  renderAll();
  if (u.logsOpen) {
    try {
      const data = await api("/api/logs?account=" + encodeURIComponent(name) + "&lines=60");
      u.logs = data.logs || "(空)";
    } catch (e) {
      u.logs = "读取失败: " + e.message;
    }
    const el = document.getElementById("logs-" + name);
    if (el) el.innerHTML = logsPanelHTML(state.accounts.find(a => a.name === name) || { name });
  }
}

// ------------------------------------------------------------------ boot

refreshAll();
setInterval(refreshAll, 6000);
api("/api/creation-options").then(d => { creationOpts = d; renderAll(); }).catch(() => {});
