async function api(url, body) {
  const opt = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(url, opt);
  if (resp.status === 401) {
    location.href = "/login";
    return { ok: false, message: "未登录" };
  }
  let data = {};
  try {
    data = await resp.json();
  } catch (e) {
    data = { ok: false, message: "服务端返回异常（HTTP " + resp.status + "）" };
  }
  return data;
}

async function getJSON(url) {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (resp.status === 401) {
    location.href = "/login";
    return { ok: false };
  }
  try {
    return await resp.json();
  } catch (e) {
    return { ok: false, message: "服务端返回异常" };
  }
}

// ---------------------------------------------------------------- 积分余额
function paintCredit(item) {
  const box = document.querySelector('.acct-stats[data-account="' + item.id + '"]');
  if (!box) return;
  const bal = box.querySelector(".js-balance");
  const today = box.querySelector(".js-today");
  const age = box.querySelector(".js-age");
  if (bal) {
    bal.textContent = item.balance;
    bal.classList.toggle("zero", !item.balance);
  }
  if (today && typeof item.today === "number") {
    // 今日新增 = 官方余额差（余额 − 当日开盘）。非数字（接口没给）时**保持原值**，
    // 绝不拼出 "+null" —— 这正是之前的 bug：单账号刷新时硬塞了 today: null。
    today.textContent = "+" + item.today;
    today.classList.toggle("ok", item.today > 0);
    today.classList.toggle("zero", !item.today);
    today.title =
      (item.today_source || "官方余额差") +
      (typeof item.task_today === "number" ? " · 任务累计 +" + item.task_today : "");
  }
  if (age) {
    age.textContent = item.age_text || "本地统计";
    age.classList.toggle("live", !!item.age_text);
  }
  const cell = box.firstElementChild;
  if (cell) cell.title = item.kind + (item.age_text ? " · " + item.age_text + "更新" : "");
}

async function syncCredits(force) {
  const data = await getJSON("/api/credits" + (force ? "?force=1" : ""));
  if (!data || !data.ok) return;
  (data.accounts || []).forEach(paintCredit);
  // 总览的余额按平台拆成两块；有 data-credit-total 的旧位置仍然兜底更新
  const byPlatform = { workbuddy: 0, qoder: 0 };
  (data.accounts || []).forEach((a) => {
    if (a.supported && byPlatform[a.platform] !== undefined) byPlatform[a.platform] += a.balance || 0;
  });
  const wb = document.querySelector("[data-credit-workbuddy]");
  if (wb) wb.textContent = byPlatform.workbuddy;
  const qd = document.querySelector("[data-credit-qoder]");
  if (qd) qd.textContent = byPlatform.qoder;
  const total = document.querySelector("[data-credit-total]");
  if (total) total.textContent = data.total;
}

async function refreshAllCredits(btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "查询中…"; }
  try {
    await syncCredits(true);
    toast("余额已刷新", "ok");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function refreshCredits(id, btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "查询中…"; }
  try {
    const r = await api("/api/accounts/" + id + "/credits");
    if (r.ok) {
      // 传真实口径：today 用官方余额差（接口给 today_delta），任务累计单独带
      paintCredit({
        id: id,
        balance: r.balance,
        today: typeof r.today_delta === "number" ? r.today_delta : undefined,
        task_today: r.task_today,
        today_source: "官方余额差",
        age_text: "刚刚",
        kind: "积分余额",
      });
      toast("实时余额：" + r.balance + " 分", "ok");
    } else {
      toast(r.message || "余额查询失败", "err");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

// ---------------------------------------------------------------- 扫码纳管
let _scanState = null;
let _scanTimer = null;

function _stopScan() {
  if (_scanTimer) { clearInterval(_scanTimer); _scanTimer = null; }
  _scanState = null;
}

async function startScan() {
  openModal("scan-modal");
  const box = document.getElementById("scan-box");
  const status = document.getElementById("scan-status");
  _stopScan();
  box.innerHTML = "";
  status.textContent = "正在获取二维码…";

  const r = await api("/api/auth/start");
  if (!r.ok) {
    status.textContent = r.message || "获取二维码失败";
    return;
  }
  box.innerHTML = r.qr || "";
  status.textContent = "等待手机扫码确认…";
  _scanState = r.state;

  let ticks = 0;
  _scanTimer = setInterval(async () => {
    const modal = document.getElementById("scan-modal");
    if (!modal || !modal.classList.contains("open")) { _stopScan(); return; }
    ticks += 1;
    if (ticks > 150) { _stopScan(); status.textContent = "二维码已过期，请点「刷新二维码」"; return; }

    const p = await getJSON("/api/auth/poll?state=" + encodeURIComponent(_scanState || ""));
    if (!p || !p.ok) return;
    if (p.status === "waiting") {
      status.textContent = "等待手机扫码确认…";
      return;
    }
    if (p.status === "ready") {
      _stopScan();
      status.textContent = (p.action || "已添加") + "：" + (p.name || "");
      toast((p.action || "已添加") + "「" + (p.name || "") + "」", "ok");
      setTimeout(() => location.reload(), 900);
      return;
    }
    if (p.status === "expired" || p.status === "invalid") {
      _stopScan();
      status.textContent = p.message || "二维码已失效，请刷新";
    }
  }, 2000);
}

function closeScan() {
  _stopScan();
  closeModal("scan-modal");
}

async function renewAccount(id, btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "续期中…"; }
  try {
    const r = await api("/api/accounts/" + id + "/renew");
    toast(r.message || (r.ok ? "已续期" : "续期失败"), r.ok ? "ok" : "err");
    if (r.ok && !r.skipped) setTimeout(() => location.reload(), 900);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

// ---------------------------------------------------------------- Qoder 设备码登录
let _qlSession = null;
let _qlTimer = null;

function _stopQlogin() {
  if (_qlTimer) { clearInterval(_qlTimer); _qlTimer = null; }
  _qlSession = null;
}

function openQlogin() { openModal("qlogin-modal"); }

function closeQlogin() {
  _stopQlogin();
  closeModal("qlogin-modal");
}

async function startQlogin() {
  const status = document.getElementById("ql-status");
  const realm = document.getElementById("ql-realm").value;
  const btn = document.getElementById("ql-go");
  _stopQlogin();
  document.getElementById("ql-step-link").style.display = "none";
  status.textContent = "正在获取授权链接…";
  btn.disabled = true;
  const r = await api("/api/qauth/start", { realm });
  btn.disabled = false;
  if (!r.ok) { status.textContent = r.message || "获取授权链接失败"; return; }

  _qlSession = r.session;
  document.getElementById("ql-url").href = r.url;
  document.getElementById("ql-qr").innerHTML = r.qr || "";
  document.getElementById("ql-step-link").style.display = "";
  status.textContent = "等待你在官方页完成登录…";

  let ticks = 0;
  _qlTimer = setInterval(async () => {
    const modal = document.getElementById("qlogin-modal");
    if (!modal || !modal.classList.contains("open")) { _stopQlogin(); return; }
    ticks += 1;
    if (ticks > 150) { _stopQlogin(); status.textContent = "授权链接已过期，请重新获取"; return; }

    const p = await getJSON("/api/qauth/poll?session=" + encodeURIComponent(_qlSession || ""));
    if (!p || !p.ok) return;
    if (p.status === "pending") return;
    if (p.status === "ready") {
      _stopQlogin();
      status.textContent = (p.action || "已添加") + "：" + (p.name || "");
      toast((p.action || "已添加") + "「" + (p.name || "") + "」", "ok");
      setTimeout(() => location.reload(), 900);
      return;
    }
    if (p.status === "expired" || p.status === "invalid") {
      _stopQlogin();
      status.textContent = p.message || "会话已过期";
    }
  }, 2000);
}

function toast(msg, type) {
  let box = document.getElementById("toast");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast " + (type || "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function callAndReload(url, body, okMsg) {
  const r = await api(url, body);
  toast(r.message || okMsg || (r.ok ? "已完成" : "操作失败"), r.ok ? "ok" : "err");
  if (r.ok) setTimeout(() => location.reload(), 600);
  return r;
}

function openModal(id) { document.getElementById(id).classList.add("open"); }
function closeModal(id) { document.getElementById(id).classList.remove("open"); }

document.addEventListener("click", (e) => {
  if (e.target.classList && e.target.classList.contains("overlay")) {
    e.target.classList.remove("open");
  }
});

function runTask(id) {
  const btn = event && event.target ? event.target.closest("button") : null;
  if (btn) { btn.disabled = true; btn.textContent = "执行中…"; }
  api("/api/tasks/" + id + "/run").then((r) => {
    toast(r.message, r.ok ? "ok" : "err");
    if (btn) { btn.disabled = false; btn.textContent = "立即执行"; }
  });
}

function toggleTask(id) { callAndReload("/api/tasks/" + id + "/toggle"); }
function toggleAccount(id) { callAndReload("/api/accounts/" + id + "/toggle"); }

async function testAccount(id) {
  toast("正在校验凭证…");
  const r = await api("/api/accounts/" + id + "/test");
  let msg = r.message || "";
  if (r.info) {
    msg += " · 连续签到 " + (r.info.streak_days ?? "-") + " 天";
  }
  toast(msg, r.ok ? "ok" : "err");
  if (r.ok) setTimeout(() => location.reload(), 800);
}

async function deleteAccount(id, name) {
  if (!confirm("确定删除账号「" + name + "」？该操作不可撤销。")) return;
  await callAndReload("/api/accounts/" + id + "/delete");
}

async function saveCron(id) {
  const input = document.getElementById("cron-" + id);
  await callAndReload("/api/tasks/" + id + "/cron", { cron: input.value.trim() });
}

function syncRealmRow() {
  const platform = document.getElementById("ac-platform");
  const row = document.getElementById("ac-realm-row");
  if (!platform || !row) return;
  row.style.display = platform.value === "qoder" ? "" : "none";
}

async function createAccount() {
  const platform = document.getElementById("ac-platform").value;
  const name = document.getElementById("ac-name").value.trim();
  const credential = document.getElementById("ac-cred").value.trim();
  const location = document.getElementById("ac-loc") ? document.getElementById("ac-loc").value.trim() : "";
  const autoDepart = document.getElementById("ac-auto") ? document.getElementById("ac-auto").checked : true;
  const realmEl = document.getElementById("ac-realm");
  const realm = realmEl && platform === "qoder" ? realmEl.value : undefined;
  if (!credential) { toast("请粘贴凭证 JSON", "err"); return; }
  const btn = document.getElementById("ac-submit");
  btn.disabled = true; btn.textContent = "校验中…";
  const r = await api("/api/accounts", {
    platform, name, credential, location, auto_depart: autoDepart, realm,
  });
  btn.disabled = false; btn.textContent = "添加并校验";
  toast(r.message, r.ok ? "ok" : "err");
  if (r.ok) setTimeout(() => location.reload(), 900);
}

async function saveAccountConfig(id) {
  const body = {};
  const nameEl = document.getElementById("an-" + id);
  if (nameEl) body.name = nameEl.value.trim();
  const locEl = document.getElementById("loc-" + id);
  if (locEl) body.location = locEl.value.trim();
  const autoEl = document.getElementById("auto-" + id);
  if (autoEl) body.auto_depart = autoEl.checked;
  const credEl = document.getElementById("cred-" + id);
  if (credEl && credEl.value.trim()) body.credential = credEl.value.trim();
  await callAndReload("/api/accounts/" + id + "/update", body);
}

async function saveNotify() {
  const body = {
    wecom_webhook: document.getElementById("wecom").value.trim(),
    generic_webhook: document.getElementById("generic").value.trim(),
    notify_enabled: document.getElementById("n-enabled").checked ? "1" : "0",
    notify_on_success: document.getElementById("n-success").checked ? "1" : "0",
  };
  await callAndReload("/api/settings", body);
}

async function testNotify() {
  toast("正在发送测试消息…");
  const r = await api("/api/settings/test");
  toast(r.message, r.ok ? "ok" : "err");
}

async function changePassword() {
  const oldPw = document.getElementById("pw-old").value;
  const newPw = document.getElementById("pw-new").value;
  const r = await api("/api/password", { old: oldPw, new: newPw });
  toast(r.message, r.ok ? "ok" : "err");
  if (r.ok) { document.getElementById("pw-old").value = ""; document.getElementById("pw-new").value = ""; }
}

function toggleLogDebug(on) {
  const box = document.getElementById("logbox");
  if (!box) return;
  box.classList.toggle("show-debug", !!on);
  if (on) box.scrollTop = box.scrollHeight;
}

function initLogStream(lastId) {
  const box = document.getElementById("logbox");
  if (!box) return;
  let cursor = lastId || 0;
  const atBottom = () => box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const es = new EventSource("/api/logs/stream?since=" + cursor);
  es.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    if (row.id <= cursor) return;
    cursor = row.id;
    const stick = atBottom();
    const div = document.createElement("div");
    div.className = "logline " + (row.level || "INFO");
    div.innerHTML =
      '<span class="t">' + row.time + '</span>' +
      '<span class="l">' + (row.level || "INFO") + '</span>' +
      '<span class="m"></span>';
    div.querySelector(".m").textContent = row.msg;
    box.appendChild(div);
    while (box.childElementCount > 800) box.removeChild(box.firstChild);
    if (stick) box.scrollTop = box.scrollHeight;
  };
  es.onerror = () => { /* 浏览器自动重连 */ };
  box.scrollTop = box.scrollHeight;
}
