// 医护端页面逻辑（原 review.html 内联脚本外置为 ES 模块）。
// 复用 shared/ 下的公共能力（$/esc/fmtTime/apiFetch/TOOL_NAME/事件委托）。
import { auth } from "./shared/state.js";
import { $, esc, fmtTime } from "./shared/dom.js";
import { absUrl, apiFetch, getAccounts } from "./shared/api.js";
import { TOOL_NAME } from "./shared/constants.js";
import { registerActions } from "./shared/csp-events.js";

const ACTION_META = {
  medicare_settle: { tag: "booking", label: "医保结算" },
  emergency_handoff: { tag: "emergency", label: "应急转诊" },
  tool_approval: { tag: "other", label: "医疗操作" },
};

function setTab(tab) {
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.tab === tab));
  $("tab-pending").style.display = tab === "pending" ? "block" : "none";
  $("tab-audit").style.display = tab === "audit" ? "block" : "none";
  $("tab-schedules").style.display = tab === "schedules" ? "block" : "none";
  $("tab-examorders").style.display = tab === "examorders" ? "block" : "none";
  $("tab-records").style.display = tab === "records" ? "block" : "none";
  if (tab === "audit") loadAudit();
  if (tab === "schedules") loadSchedules();
  if (tab === "examorders") {
    loadExamMeta();
    loadExamPatients().then(() => loadExamFlow());
  }
  if (tab === "records") {
    loadRecordPatients().then(() => loadRecord());
  }
}
document.querySelectorAll(".nav-item").forEach((n) => (n.onclick = () => setTab(n.dataset.tab)));

async function login() {
  const u = $("lu").value.trim(), p = $("lp").value;
  $("lerr").textContent = "";
  try {
    const r = await fetch(absUrl("/auth/login"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p, role: "doctor" }) });
    const d = await r.json();
    if (!r.ok) { $("lerr").textContent = d.detail || "登录失败"; return; }
    auth.token = "Bearer " + d.access_token;
    auth.user = u;
    $("loginMask").style.display = "none";
    $("userbox").style.display = "flex";
    $("uname").textContent = u;
    $("uava").textContent = u.slice(0, 1).toUpperCase();
    setTab("pending");
    loadPending();
    window._poll = setInterval(loadPending, 6000);
  } catch (e) { $("lerr").textContent = "网络异常，请稍后再试"; }
}
function logout() {
  auth.token = "";
  auth.user = "";
  if (window._poll) clearInterval(window._poll);
  location.reload();
}

async function loadPending() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/review/pending"), { headers: { Authorization: auth.token } });
    const d = await r.json();
    const list = d.pending || [];
    const badge = $("badge");
    badge.textContent = list.length;
    badge.classList.toggle("show", list.length > 0);
    $("countPill").textContent = list.length + " 项待处理";
    const host = $("pendingList");
    host.innerHTML = "";
    if (!list.length) {
      host.innerHTML = '<div class="empty"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg><div>暂无待审核事项</div></div>';
      return;
    }
    list.forEach((p) => host.appendChild(renderCard(p)));
  } catch (e) {}
}

function renderCard(p) {
  const pl = p.payload || {};
  const meta = ACTION_META[pl.action] || ACTION_META.tool_approval;
  const tools = (pl.tools || []).map((t) => TOOL_NAME[t] || t);
  const div = document.createElement("div");
  div.className = "card";
  div.innerHTML =
    '<div class="top"><span class="tag ' + meta.tag + '">' + meta.label + '申请</span>' +
      '<span class="meta">提交于 ' + esc(fmtTime(p.created_at)) + "</span></div>" +
    '<div class="who-line">患者 / 会话：<b>' + esc(p.thread_id) + "</b></div>" +
    '<div class="detail"><div class="lbl">系统将执行以下操作（需您确认）</div><div class="ops">' +
      opsHtml(p, meta.tag === "emergency") +
    "</div></div>" +
    '<div class="acts"><button class="btn ok" data-action="resolve" data-approval="' + esc(p.approval_id) + '" data-approve="1">批准执行</button>' +
      '<button class="btn no" data-action="resolve" data-approval="' + esc(p.approval_id) + '" data-approve="0">拒绝</button></div>';
  return div;
}

// 审批对象必须展示**完整参数**（如结算哪笔预约），否则医护只能盲批
function opsHtml(p, danger) {
  const calls = p.calls && p.calls.length ? p.calls : null;
  if (calls) {
    return calls.map(function (c) {
      const keys = c.args ? Object.keys(c.args) : [];
      const args = keys.length
        ? '<span style="display:block;font-size:11px;opacity:.75;margin-top:2px">' +
          esc(keys.map(function (k) { return k + "=" + c.args[k]; }).join("  ")) + "</span>"
        : "";
      return '<span class="op' + (danger ? " danger" : "") + '"><span class="dot"></span>' + esc(c.name) + args + "</span>";
    }).join("");
  }
  return (p.tools || []).map(function (t) {
    return '<span class="op' + (danger ? " danger" : "") + '"><span class="dot"></span>' + esc(t) + "</span>";
  }).join("");
}

async function resolve(aid, ok, btn) {
  if (!confirm(ok ? "确认批准并执行该申请？" : "确认拒绝该申请？")) return;
  btn.disabled = true;
  try {
    const r = await fetch(absUrl("/api/review/resolve"), { method: "POST", headers: { "Content-Type": "application/json", Authorization: auth.token },
      body: JSON.stringify({ approval_id: aid, decision: ok ? "approve" : "reject" }) });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || "处理失败"); btn.disabled = false; return; }
    loadPending();
    loadAudit();
  } catch (e) { alert("网络异常"); btn.disabled = false; }
}

async function loadAudit() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/audit"), { headers: { Authorization: auth.token } });
    const d = await r.json();
    const list = (d.audit || []).slice().reverse();
    const host = $("auditList");
    host.innerHTML = "";
    if (!list.length) { host.innerHTML = '<div class="empty">暂无记录</div>'; return; }
    list.forEach((a) => {
      const isResolve = a.action === "approval_resolve";
      const dec = a.decision && a.decision.approved;
      const cls = isResolve ? (dec ? "ok" : "no") : "";
      const pill = isResolve ? ('<span class="pill ' + (dec ? "ok" : "no") + '">' + (dec ? "已批准" : "已拒绝") + "</span>") : "";
      const detail = typeof a.detail === "string" ? a.detail : JSON.stringify(a.detail);
      const div = document.createElement("div");
      div.className = "rec " + cls;
      div.innerHTML = '<div class="t">' + esc(fmtTime(a.created_at)) + "</div>" +
        '<div class="h">' + esc(a.action === "approval_create" ? "提交审核" : "审核处理") + pill + "</div>" +
        '<div class="d">' + (a.actor ? "<b>" + esc(a.actor) + "</b> · " : "") + esc(detail) + "</div>";
      host.appendChild(div);
    });
  } catch (e) {}
}

// -------- 开检查单（体检详细流程） --------
let _examLoc = {};
async function loadExamMeta() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/exam-types"), { headers: { Authorization: auth.token } });
    const d = await r.json();
    _examLoc = d.locations || {};
    const dl = $("examTypes");
    dl.innerHTML = "";
    (d.types || []).forEach((t) => { const o = document.createElement("option"); o.value = t; dl.appendChild(o); });
  } catch (e) {}
}
function resolveLocJS(name) {
  name = (name || "").trim();
  if (!name) return "";
  if (_examLoc[name]) return _examLoc[name];
  for (const k in _examLoc) { if (k.indexOf(name) >= 0 || name.indexOf(k) >= 0) return _examLoc[k]; }
  return "";
}
async function loadExamPatients() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/doctor/patients"), { headers: { Authorization: auth.token } });
    const d = await r.json();
    const sel = $("examPatient");
    sel.innerHTML = "";
    (d || []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.username;
      o.textContent = p.username + (p.full_name && p.full_name !== p.username ? ("（" + p.full_name + "）") : "");
      sel.appendChild(o);
    });
  } catch (e) {}
}
async function loadExamFlow() {
  if (!auth.token) return;
  const pat = $("examPatient").value;
  if (!pat) { $("examFlowHost").innerHTML = '<div class="empty">请先在上方选择患者。</div>'; return; }
  try {
    const r = await fetch(absUrl("/api/doctor/exam-orders?patient=") + encodeURIComponent(pat), { headers: { Authorization: auth.token } });
    const d = await r.json();
    const host = $("examFlowHost");
    host.innerHTML = "";
    if (!d.steps || !d.steps.length) {
      host.innerHTML = '<div class="empty">该患者暂无检查流程，请在下方新增检查项并提交。</div>';
      return;
    }
    const total = d.steps.length, done = d.steps.filter((s) => s.status === "DONE").length;
    let html = '<div class="flow-summary"><div class="flow-stat">共 <b>' + total + '</b> 项</div>' +
      '<div class="flow-stat">已完成 <b>' + done + '</b> 项</div>' +
      '<div class="flow-stat">待完成 <b>' + (total - done) + '</b> 项</div></div>';
    html += '<div class="flow-list">';
    d.steps.forEach((s, i) => {
      const isDone = s.status === "DONE";
      html += '<div class="flow-step' + (isDone ? " done" : "") + '"><div class="flow-rail"><div class="flow-dot">' + (isDone ? "✓" : (i + 1)) + '</div><div class="flow-line"></div></div>' +
        '<div class="flow-body"><div class="flow-top"><span class="flow-name">' + esc(s.step_name) + '</span>' +
        '<span class="flow-badge ' + (isDone ? "done" : "todo") + '">' + (isDone ? "已完成" : "待完成") + '</span></div>' +
        '<div class="flow-loc">📍 ' + esc(s.location) + '</div>' +
        (s.note ? '<div class="flow-note">备注：' + esc(s.note) + '</div>' : '') +
        '<div class="ops" style="margin-top:10px"><button class="op-btn ' + (isDone ? "undo" : "ok") + '" data-action="toggle-step" data-step-id="' + s.id + '" data-done="' + (isDone ? 0 : 1) + '">' + (isDone ? "撤销完成" : "标记完成") + '</button></div>' +
        '</div></div>';
    });
    html += "</div>";
    host.innerHTML = html;
  } catch (e) {}
}
function addExamRow() {
  const host = $("examRows");
  const row = document.createElement("div");
  row.className = "exam-row";
  row.innerHTML = '<input class="er-name" list="examTypes" placeholder="检查项目，如 验血 / 彩超 / CT" data-action-input="auto-loc">' +
    '<input class="er-loc" placeholder="楼宇位置（自动）" readonly>' +
    '<input class="er-note" placeholder="备注 / 注意事项（可选）">' +
    '<button class="er-del" title="删除" data-action="remove-parent">✕</button>';
  host.appendChild(row);
}
function autoLoc(inp) {
  const row = inp.parentNode;
  const loc = row.querySelector(".er-loc");
  const v = resolveLocJS(inp.value);
  loc.value = v;
  loc.classList.toggle("editable", !!v);
}
async function submitExamOrder() {
  const pat = $("examPatient").value;
  if (!pat) { alert("请先选择患者"); return; }
  const rows = [...$("examRows").querySelectorAll(".exam-row")];
  const steps = [];
  rows.forEach((r) => {
    const name = r.querySelector(".er-name").value.trim();
    if (!name) return;
    const loc = r.querySelector(".er-loc").value.trim();
    const note = r.querySelector(".er-note").value.trim();
    steps.push({ name, location: loc, note });
  });
  if (!steps.length) { alert("请至少填写一项检查项目"); return; }
  try {
    const r = await fetch(absUrl("/api/doctor/exam-orders"), { method: "POST", headers: { "Content-Type": "application/json", Authorization: auth.token },
      body: JSON.stringify({ patient_username: pat, steps }) });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || "提交失败"); return; }
    $("examRows").innerHTML = "";
    addExamRow();
    loadExamFlow();
  } catch (e) { alert("网络异常"); }
}
async function toggleStep(id, done, btn) {
  btn.disabled = true;
  try {
    const r = await fetch(absUrl("/api/doctor/exam-steps/") + id, { method: "PUT", headers: { "Content-Type": "application/json", Authorization: auth.token },
      body: JSON.stringify({ status: done ? "DONE" : "PENDING" }) });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || "操作失败"); btn.disabled = false; return; }
    loadExamFlow();
  } catch (e) { alert("网络异常"); btn.disabled = false; }
}

setTab("pending");
let _schDate = new Date().toISOString().slice(0, 10);

async function loadSchedules() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/admin/schedules?date=") + _schDate, { headers: { Authorization: auth.token } });
    const d = await r.json();
    const list = d.schedules || [];
    const host = $("scheduleList");
    host.innerHTML = "";
    if (!list.length) {
      host.innerHTML = '<div class="empty"><div>该日期暂无排班数据</div></div>';
      return;
    }
    // 日期选择栏
    const dateBar = document.createElement("div");
    dateBar.className = "sch-date-bar";
    dateBar.innerHTML = '<label style="font-size:13px;color:var(--muted)">查看日期：</label>' +
      '<input type="date" value="' + esc(_schDate) + '" data-action-change="sch-date">';
    host.appendChild(dateBar);
    // 按科室分组
    const grid = document.createElement("div");
    grid.className = "sch-grid";
    list.forEach((sch) => {
      const remaining = sch.remaining;
      const cls = remaining <= 0 ? "danger" : remaining <= 5 ? "warn" : "ok";
      const card = document.createElement("div");
      card.className = "sch-card";
      card.id = "sch-" + sch.id;
      card.innerHTML =
        '<div class="sch-head">' +
          '<span class="sch-dept">' + esc(sch.department) + '</span>' +
          '<span class="sch-period">' + (sch.period === "AM" ? "上午" : "下午") + '</span>' +
        '</div>' +
        '<div class="sch-doctor">' + esc(sch.doctor) + ' <span class="sch-title">' + esc(sch.title) + '</span></div>' +
        '<div class="sch-row"><span class="sch-label">已预约</span><span class="sch-num">' + sch.booked_slots + ' 号</span></div>' +
        '<div class="sch-row"><span class="sch-label">剩余</span><span class="sch-num ' + cls + '" id="rem-' + sch.id + '">' + remaining + ' 号</span></div>' +
        '<div class="sch-row">' +
          '<label class="sch-label">总名额</label>' +
          '<input class="sch-input" type="number" min="' + sch.booked_slots + '" max="100" value="' + sch.total_slots + '" id="inp-' + sch.id + '">' +
          '<button class="sch-btn" data-action="save-schedule" data-sch-id="' + sch.id + '">保存</button>' +
          '<span class="sch-saved" id="saved-' + sch.id + '">✓ 已同步</span>' +
        '</div>';
      grid.appendChild(card);
    });
    host.appendChild(grid);
  } catch (e) {}
}

async function saveSchedule(sid, btn) {
  const inp = $("inp-" + sid);
  const total = parseInt(inp.value);
  if (isNaN(total) || total < 0) { alert("请输入有效数字"); return; }
  btn.disabled = true;
  try {
    const r = await fetch(absUrl("/api/admin/schedules"), { method: "PUT", headers: { "Content-Type": "application/json", Authorization: auth.token },
      body: JSON.stringify({ updates: [{ id: sid, total_slots: total }] }) });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || "保存失败"); btn.disabled = false; return; }
    // 更新剩余显示
    const card = $("sch-" + sid);
    const booked = card.querySelector(".sch-num"); // 第一个 sch-num 是已预约
    const bookedNum = parseInt(booked.textContent) || 0;
    const rem = Math.max(0, total - bookedNum);
    const remEl = $("rem-" + sid);
    remEl.textContent = rem + " 号";
    remEl.className = "sch-num " + (rem <= 0 ? "danger" : rem <= 5 ? "warn" : "ok");
    // 显示已同步提示
    $("saved-" + sid).style.display = "inline";
    setTimeout(() => ($("saved-" + sid).style.display = "none"), 2000);
    btn.disabled = false;
  } catch (e) { alert("网络异常"); btn.disabled = false; }
}

// ============ 患者病历可视化 ============
async function loadRecordPatients() {
  if (!auth.token) return;
  try {
    const r = await fetch(absUrl("/api/doctor/patients"), { headers: { Authorization: auth.token } });
    const d = await r.json();
    const sel = $("recPatient");
    sel.innerHTML = "";
    (d || []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.username;
      o.textContent = p.username + (p.full_name && p.full_name !== p.username ? ("（" + p.full_name + "）") : "");
      sel.appendChild(o);
    });
  } catch (e) {}
}
function _num(v) {
  const m = String(v == null ? "" : v).match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : NaN;
}
// 内联 SVG 折线趋势图（零依赖，严格 CSP 兼容）
function trendSvg(series) {
  const W = 660, H = 150, pad = 34, pl = 44, pr = 14, pt = 14, pb = 28;
  const xs = series.map((s, i) => Math.round(pl + (W - pl - pr) * (series.length === 1 ? 0.5 : i / (series.length - 1))));
  const vals = series.map((s) => s.value);
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }
  const ys = series.map((v) => Math.round(pt + (H - pt - pb) * (1 - (v - min) / (max - min))));
  const yTicks = [min, (min + max) / 2, max].map((t) => Math.round(t * 10) / 10);
  let grid = "";
  yTicks.forEach((t, i) => {
    const y = Math.round(pt + (H - pt - pb) * (1 - (t - min) / (max - min)));
    grid += '<line x1="' + pl + '" y1="' + y + '" x2="' + (W - pr) + '" y2="' + y + '" stroke="#eef2f6"/><text x="' + (pl - 8) + '" y="' + (y + 4) + '" text-anchor="end" font-size="10" fill="#94a3b8">' + t + "</text>";
  });
  let dots = "", line = "", labels = "";
  series.forEach((s, i) => {
    const c = s.abnormal ? "#dc2626" : "#0d9488";
    dots += '<circle cx="' + xs[i] + '" cy="' + ys[i] + '" r="4" fill="' + c + '" stroke="#fff" stroke-width="1.5"/>';
    line += (i ? " L" : "M") + xs[i] + " " + ys[i];
    if (series.length <= 8 || i === 0 || i === series.length - 1) {
      labels += '<text x="' + xs[i] + '" y="' + (H - 10) + '" text-anchor="middle" font-size="10" fill="#94a3b8">' + esc(s.label) + "</text>";
    }
  });
  return '<svg class="trend" viewBox="0 0 ' + W + " " + H + '" width="100%" preserveAspectRatio="xMidYMid meet" role="img">' +
    grid +
    '<path d="' + line + '" fill="none" stroke="#0d9488" stroke-width="2.2" stroke-linejoin="round"/>' +
    dots + labels + "</svg>";
}
function renderRecord(d) {
  const host = $("recordHost");
  host.innerHTML = "";
  const labs = d.lab_reports || [], vitals = d.vital_signs || [], cases = d.case_summaries || [], rems = d.reminders || [];
  const abnCount = labs.filter((l) => l.abnormal).length;
  // 概览
  let html = '<div class="rec-summary">' +
    '<div class="rec-chip"><b>' + labs.length + '</b><span>检验报告</span></div>' +
    '<div class="rec-chip' + (abnCount ? " danger" : "") + '"><b>' + abnCount + '</b><span>异常项</span></div>' +
    '<div class="rec-chip"><b>' + vitals.length + '</b><span>生命体征</span></div>' +
    '<div class="rec-chip"><b>' + cases.length + '</b><span>病例小结</span></div>' +
    '<div class="rec-chip"><b>' + rems.length + '</b><span>随访提醒</span></div>' +
    "</div>";
  // 检验报告
  html += '<div class="card"><div class="rec-sec-title">检验报告' + (labs.length ? '<span class="badge-n">' + labs.length + "</span>" : "") + "</div>";
  if (!labs.length) {
    html += '<div class="empty">该患者暂无检验报告</div>';
  } else {
    // 按项目分组，出现 ≥2 次数值项画趋势
    const byItem = {};
    labs.forEach((l) => { (byItem[l.item] = byItem[l.item] || []).push(l); });
    Object.keys(byItem).forEach((item) => {
      const arr = byItem[item];
      const numeric = arr.filter((l) => !isNaN(_num(l.result)) && l.report_date);
      if (numeric.length >= 2) {
        html += '<div class="lab-trend"><div class="lt-title">' + esc(item) + " 趋势（" + numeric.length + ' 次）</div>' +
          trendSvg(numeric.map((l) => ({ label: l.report_date, value: _num(l.result), abnormal: l.abnormal }))) + "</div>";
      }
    });
    html += '<table class="lab-table"><thead><tr><th>项目</th><th>结果</th><th>参考范围</th><th>日期</th></tr></thead><tbody>';
    labs.forEach((l) => {
      html += '<tr' + (l.abnormal ? ' class="abn"' : "") + '><td>' + esc(l.item) + "</td><td><b>" + esc(l.result) + "</b>" +
        (l.abnormal ? '<span class="abn-flag">异常</span>' : "") + "</td><td>" + esc(l.ref_range || "—") + "</td><td>" + esc(l.report_date || "—") + "</td></tr>";
    });
    html += "</tbody></table>";
  }
  html += "</div>";
  // 生命体征
  html += '<div class="card"><div class="rec-sec-title">生命体征' + (vitals.length ? '<span class="badge-n">' + vitals.length + "</span>" : "") + "</div>";
  if (!vitals.length) {
    html += '<div class="empty">暂无生命体征记录</div>';
  } else {
    html += '<div class="vital-grid">';
    vitals.forEach((v) => {
      html += '<div class="vital-card"><div class="vt-type">' + esc(v.type) + "</div>" +
        '<div class="vt-val">' + esc(v.value) + (v.unit ? '<small>' + esc(v.unit) + "</small>" : "") + "</div>" +
        '<div class="vt-time">' + (v.measured_at ? esc(v.measured_at.slice(0, 16)) : "—") + "</div></div>";
    });
    html += "</div>";
  }
  html += "</div>";
  // 病例小结（按类别分组）
  html += '<div class="card"><div class="rec-sec-title">病例小结 / 既往史' + (cases.length ? '<span class="badge-n">' + cases.length + "</span>" : "") + "</div>";
  if (!cases.length) {
    html += '<div class="empty">暂无病例小结</div>';
  } else {
    const byCat = {};
    cases.forEach((c) => { (byCat[c.category] = byCat[c.category] || []).push(c); });
    const CAT_LABEL = { general: "一般情况", history: "既往史", allergy: "过敏史", family: "家族史", lifestyle: "生活方式", present: "现病史" };
    Object.keys(byCat).forEach((cat) => {
      const catLabel = CAT_LABEL.hasOwnProperty(cat) ? CAT_LABEL[cat] : cat;
      html += '<div class="case-group"><h4>' + esc(catLabel) + "</h4>";
      byCat[cat].forEach((c) => {
        html += '<div class="case-item">' + esc(c.text) + (c.created_at ? '<span class="ci-time">记录于 ' + esc(c.created_at.slice(0, 16)) + "</span>" : "") + "</div>";
      });
      html += "</div>";
    });
  }
  html += "</div>";
  // 随访提醒
  html += '<div class="card"><div class="rec-sec-title">随访提醒' + (rems.length ? '<span class="badge-n">' + rems.length + "</span>" : "") + "</div>";
  if (!rems.length) {
    html += '<div class="empty">暂无随访提醒</div>';
  } else {
    rems.forEach((r) => {
      const done = r.status === "DONE" || r.status === "SENT" || r.status === "已完成";
      html += '<div class="rem-item' + (done ? " done" : "") + '"><span class="rem-dot"></span><span>' + esc(r.content) + "</span>" +
        (r.remind_at ? '<span class="rem-time">' + esc(r.remind_at.slice(0, 16)) + "</span>" : "") + "</div>";
    });
  }
  html += "</div>";
  host.innerHTML = html;
}
async function loadRecord() {
  if (!auth.token) return;
  const pat = $("recPatient").value;
  const host = $("recordHost");
  if (!pat) { host.innerHTML = '<div class="empty">请先在右上角选择患者。</div>'; return; }
  try {
    const r = await fetch(absUrl("/api/doctor/patient-record?patient=") + encodeURIComponent(pat), { headers: { Authorization: auth.token } });
    if (r.status === 404) { host.innerHTML = '<div class="empty">该患者不存在或无查看权限。</div>'; return; }
    const d = await r.json();
    renderRecord(d);
  } catch (e) { host.innerHTML = '<div class="empty">加载失败，请稍后重试。</div>'; }
}

// 交互动作注册表（供事件委托按 data-action 分发）
registerActions({
  "login": function () { login(); },
  "go-chat": function () { window.location = "/"; },
  "logout": function () { logout(); },
  "load-schedules": function () { loadSchedules(); },
  "load-exam-flow": function () { loadExamFlow(); },
  "add-exam-row": function () { addExamRow(); },
  "submit-exam-order": function () { submitExamOrder(); },
  "resolve": function (el) { resolve(el.getAttribute("data-approval"), el.getAttribute("data-approve") === "1", el); },
  "toggle-step": function (el) { toggleStep(Number(el.getAttribute("data-step-id")), Number(el.getAttribute("data-done")), el); },
  "save-schedule": function (el) { saveSchedule(Number(el.getAttribute("data-sch-id")), el); },
  "remove-parent": function (el) { el.parentNode.remove(); },
  "auto-loc": function (el) { autoLoc(el); },
  "load-record": function () { loadRecord(); },
  "sch-date": function (el) { _schDate = el.value; loadSchedules(); },
});
