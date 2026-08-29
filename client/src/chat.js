// 患者端页面逻辑（原 chat.html 内联脚本外置为 ES 模块）。
// 通过 import 复用 shared/ 下的公共能力，避免与 review.js 重复实现 $/esc/apiFetch 等。
import { auth } from "./shared/state.js";
import { $, esc } from "./shared/dom.js";
import { absUrl, apiFetch, rememberAccount, getAccounts } from "./shared/api.js";
import { TOOL_NAME, VITAL_NAME } from "./shared/constants.js";
import { registerActions } from "./shared/csp-events.js";

const SERVICES = {
  home: { title: "健康咨询", desc: "描述您的症状或需求，我们将为您安排对应的就医服务。",
    chips: ["我最近总是头疼，有点担心", "我想预约下周的门诊", "帮我看看我的化验单"] },
  triage: { title: "智能分诊", desc: "说明您的不适，系统为您推荐合适的就诊科室；也可直接从下方科室导航选择。",
    chips: ["我头痛伴恶心，挂什么科？", "老人胸闷气短挂哪个科？", "小孩反复发烧怎么办？"] },
  booking: { title: "预约挂号", desc: "选择下方号源即可自动预约（有名额即锁定）；如需医保结算将由医生确认。",
    chips: ["挂神经内科今天上午的号", "预约心内科明天下午并办医保结算", "帮我锁定呼吸内科的号"] },
  hospital: { title: "院内服务", desc: "就诊流程、门诊时间、医保报销、检查化验须知、院区交通停车、体检与互联网医院复诊等院内事务，点击快速咨询。",
    chips: ["门诊几点开门？", "医保怎么报销？", "抽血要空腹吗？", "去医院怎么停车？", "体检前要注意什么？", "互联网医院怎么用？"] },
  intake: { title: "报告解读", desc: "系统已拉取您的检验报告（来自检验系统），可点开查看并申请解读。",
    chips: ["帮我解读最近的化验报告", "我的血常规报告正常吗？", "解读一下肝功能化验单"] },
  followup: { title: "健康随访", desc: "系统已加载您的体征与随访提醒，可继续与助手沟通记录随访计划。",
    chips: ["记录我的血压随访，今天 145/95", "提醒我下周复查血糖", "查看我的慢病随访计划"] },
  examflow: { title: "体检详细流程单", desc: "主诊医生为您开具的检查项目与对应位置，请按流程顺序逐项完成。" },
  emergency: { title: "急症求助", desc: "出现急危重症时，系统将立即转接急诊并联系医护人员。",
    chips: ["我胸口剧痛喘不上气", "突然半边身体麻木说不出话", "家里老人晕倒了"] },
};

let curSvc = "home", streaming = false, lastSlots = [];

/* ====== 多会话隔离：每个服务模块独立的 thread_id + 消息列表 ====== */
let sessionMsgs = {};   // { svc: [ {role, who, text}, ... ] }
let sessionBanner = {}; // { svc: payload | null }  审核状态卡也按模块存
let sessionOkCard = {}; // { svc: text | null }       成功卡也按模块存
let hydrated = {};      // { svc: true } 标记本模块是否已从服务端拉取过历史，避免重复拉取
let needConsent = false;

function getTid(svc) { return "thr-" + auth.user + "-" + svc; }

function setSvc(svc) {
  if (svc === curSvc) return;  // 同模块不重复切换

  // 1) 保存当前模块的消息和状态卡
  saveSession(curSvc);

  // 2) 切换到新模块
  curSvc = svc;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.svc === svc));
  $("svcTitle").textContent = SERVICES[svc].title;
  $("svcDesc").textContent = SERVICES[svc].desc;

  // 体检流程为「报表视图」：隐藏聊天输入框，专注展示流程单
  const isFlow = svc === "examflow";
  setChatVisible(!isFlow);

  // 3) 更新快捷短语
  const c = $("chips");
  c.innerHTML = "";
  if (!isFlow && SERVICES[svc].chips) {
    SERVICES[svc].chips.forEach((t) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = t;
      b.onclick = () => { $("msg").value = t; send(); };
      c.appendChild(b);
    });
  }

  // 4) 清空聊天区 + 加载目标模块历史消息
  restoreSession(svc);

  // 5) 加载数据面板（全局档案跨模块共享）
  loadServiceData(svc);

  // 6) 从服务端拉取本线程历史对话，刷新后重渲染
  hydrate(svc);
}

function setChatVisible(v) {
  $("chat").style.display = v ? "flex" : "none";
  document.querySelector(".composer").style.display = v ? "flex" : "none";
  if ($("hint")) $("hint").style.display = v ? "block" : "none";
  $("switchTip").style.display = v ? "block" : "none";
}

/** 保存当前聊天区的 DOM 消息到 sessionMsgs[curSvc] */
function saveSession(svc) {
  const msgs = [];
  $("chat").querySelectorAll(".msg").forEach((m) => {
    const role = m.classList.contains("user") ? "user" : "bot";
    const whoEl = m.querySelector(".who");
    const who = whoEl ? whoEl.textContent : "";
    const txtEl = m.querySelector(".txt");
    const txt = txtEl ? txtEl.textContent : "";
    if (txt) msgs.push({ role, who, text: txt });
  });
  sessionMsgs[svc] = msgs;
  // 状态卡不存 DOM（每次从后端重新获取），只记标志
}

/** 从 sessionMsgs 恢复目标模块的消息到聊天区 */
function restoreSession(svc) {
  const chat = $("chat");
  chat.innerHTML = "";
  $("bannerHost").innerHTML = "";
  $("switchTip").textContent = "";

  const msgs = sessionMsgs[svc] || [];
  if (msgs.length === 0) {
    // 新会话：显示欢迎提示 + 欢迎语（唯一出口，避免重复）
    $("switchTip").textContent = "── 已开启新的 " + SERVICES[svc].title + " 会话 ──";
    addMsg("bot", SERVICES[svc].title, SERVICES[svc].desc);
  }
  msgs.forEach((m) => {
    addMsgToDOM(m.role, m.who, m.text);
  });

  // 恢复该模块的状态卡（如果有缓存）
  if (sessionBanner[svc]) showBannerDOM(sessionBanner[svc]);
  if (sessionOkCard[svc]) showOkCardDOM(sessionOkCard[svc]);
}

document.querySelectorAll(".nav-item").forEach((n) => (n.onclick = () => setSvc(n.dataset.svc)));

// 刷新/重进后从服务端拉取本线程历史对话，重渲染聊天框（避免内存清空即丢消息）
async function hydrate(svc) {
  if (!auth.token) return;
  if (hydrated[svc]) return;  // 已拉过（含欢迎语场景），不重复
  hydrated[svc] = true;
  try {
    const r = await apiFetch("/api/chat-history?thread_id=" + encodeURIComponent(getTid(svc)));
    if (!r.ok) return;
    const d = await r.json();
    const msgs = d.messages || [];
    if (!msgs.length) return;  // 服务端无历史，保留欢迎语
    // 用服务端历史覆盖（清空聊天区，含欢迎语占位）
    sessionMsgs[svc] = msgs.map((m) => ({ role: m.role, who: m.role === "bot" ? SERVICES[svc].title : "", text: m.text }));
    const chat = $("chat");
    chat.innerHTML = "";
    msgs.forEach((m) => addMsgToDOM(m.role, m.role === "bot" ? SERVICES[svc].title : "", m.text));
    chat.scrollTop = chat.scrollHeight;
  } catch (e) { /* 拉取失败不影响新对话 */ }
}

// -------- 真实业务数据加载（按服务模块，跨模块共享展示） --------
async function loadServiceData(svc) {
  const host = $("dataPanel");
  host.innerHTML = "";
  if (!auth.token) return;
  if (svc === "booking") return renderBooking(host);
  if (svc === "intake") return renderReports(host);
  if (svc === "followup") return renderFollowup(host);
  if (svc === "triage") return renderDepartments(host);
  if (svc === "examflow") return renderExamFlow(host);
}

// 体检详细流程单（医生开具的检查项目 + 对应楼宇位置）
async function renderExamFlow(host) {
  host.innerHTML = '<div class="panel-loading">正在加载您的体检详细流程…</div>';
  try {
    const d = await apiFetch("/api/patient/exam-flow").then((x) => x.json());
    const steps = d.steps || [];
    let html = '<div class="panel"><div class="panel-h"><span>体检详细流程单</span>' +
      '<span class="panel-sub">' + esc(d.patient) + " · 由主诊医生开具</span></div>";
    if (!steps.length) {
      html += '<div class="empty">暂无检查流程。医生面诊后会为您开具验血、彩超、CT 等检查项目，并标注每项的楼宇位置。</div></div>';
      host.innerHTML = html; return;
    }
    // 概览统计
    html += '<div class="flow-summary">' +
      '<div class="flow-stat">共 <b>' + d.total + "</b> 项</div>" +
      '<div class="flow-stat">已完成 <b>' + d.done + "</b> 项</div>" +
      '<div class="flow-stat">待完成 <b>' + (d.total - d.done) + "</b> 项</div></div>";
    // 路径指引：按位置聚合，提示先去哪栋楼
    const locOrder = [];
    const locMap = {};
    steps.forEach((s) => { if (!locMap[s.location]) { locMap[s.location] = true; locOrder.push(s.location); } });
    html += '<div class="flow-path">🧭 <b>就诊路径指引：</b>请按流程顺序完成，涉及楼宇：' +
      locOrder.map((l) => "「" + l + "」").join(" → ") + "，建议携带医保卡与过往报告。</div>";
    // 流程时间线
    html += '<div class="flow-list">';
    steps.forEach((s, i) => {
      const done = s.status === "DONE";
      html += '<div class="flow-step' + (done ? " done" : "") + '">' +
        '<div class="flow-rail"><div class="flow-dot">' + (done ? "✓" : (i + 1)) + '</div><div class="flow-line"></div></div>' +
        '<div class="flow-body">' +
        '<div class="flow-top"><span class="flow-name">' + esc(s.name) + "</span>" +
        '<span class="flow-badge ' + (done ? "done" : "todo") + '">' + (done ? "已完成" : "待完成") + "</span></div>" +
        '<div class="flow-loc">📍 ' + esc(s.location) + "</div>" +
        (s.note ? '<div class="flow-note">备注：' + esc(s.note) + "</div>" : "") +
        (s.done_at ? '<div class="flow-meta">完成时间：' + esc(s.done_at.replace("T", " ").slice(0, 16)) + "</div>" : "") +
        "</div></div>";
    });
    html += "</div></div>";
    host.innerHTML = html;
  } catch (e) { host.innerHTML = '<div class="panel-loading">流程加载失败，请稍后重试。</div>'; }
}

async function renderBooking(host) {
  host.innerHTML = '<div class="panel-loading">正在加载可约号源…</div>';
  try {
    const r = await apiFetch("/api/appointments/available");
    const d = await r.json();
    lastSlots = d.slots || [];
    let html = '<div class="panel"><div class="panel-h"><span>今日可约号源</span>' +
      '<span class="panel-sub">' + esc(d.date) + " · 有名额即可自动预约</span></div>";
    if (!lastSlots.length) {
      html += '<div class="empty">今日号源已约满，请明日再来或现场挂号。</div></div>';
    } else {
      html += '<div class="slot-grid">';
      lastSlots.forEach((s, i) => {
        const full = s.remaining <= 0;
        html += '<div class="slot-card' + (full ? " disabled" : "") + '" ' + (full ? "" : ('data-i="' + i + '"')) + ">" +
          '<div class="slot-top"><b>' + esc(s.department) + '</b><span class="period">' + (s.period === "AM" ? "上午" : "下午") + "</span></div>" +
          '<div class="slot-doc">' + esc(s.doctor) + " · " + esc(s.title) + "</div>" +
          '<div class="slot-foot"><span class="remain ' + (full ? "full" : "") + '">剩余 ' + s.remaining + " 号</span>" +
          (full ? '<span class="tag-full">已满</span>' : '<span class="book-btn">预约 ›</span>') + "</div></div>";
      });
      html += "</div></div>";
    }
    host.innerHTML = html;
    host.querySelectorAll(".slot-card[data-i]").forEach((c) => {
      c.onclick = () => {
        const s = lastSlots[+c.dataset.i];
        if (!s) return;
        const dept = s.department, d = s.date, period = s.period === "AM" ? "上午" : "下午";
        $("msg").value = "我要预约 " + dept + " " + d + " " + period + " 的号，请锁定号源";
        send();
      };
    });
  } catch (e) { host.innerHTML = '<div class="panel-loading">号源加载失败，请稍后重试。</div>'; }
}

async function renderReports(host) {
  host.innerHTML = '<div class="panel-loading">正在加载您的检验报告…</div>';
  try {
    const list = await apiFetch("/api/reports").then((x) => x.json());
    let html = '<div class="panel"><div class="panel-h"><span>我的检验报告</span>' +
      '<span class="panel-sub">数据来自检验系统（LIS）</span></div>';
    if (!list.length) { html += '<div class="empty">暂无检验报告记录。</div></div>'; }
    else {
      html += '<div class="report-list">';
      list.forEach((x) => {
        const item = x.item || "";
        const result = x.result || "";
        const ref = x.ref_range || "—";
        const date = x.report_date || "";
        const abnormal = x.abnormal ? "1" : "0";
        html += '<div class="report-card' + (x.abnormal ? " abnormal" : "") + '">' +
          '<div class="rc-top"><b>' + esc(item) + "</b>" + (x.abnormal ? '<span class="badge-abn">异常</span>' : '<span class="badge-ok">正常</span>') + "</div>" +
          '<div class="rc-val">' + esc(result) + "</div>" +
          '<div class="rc-meta">参考 ' + esc(ref) + " · " + esc(date) + "</div>" +
          '<button class="link-btn" data-ask="report" data-item="' + esc(item) + '" data-result="' + esc(result) + '" data-ref="' + esc(ref) + '" data-date="' + esc(date) + '" data-abnormal="' + abnormal + '">请解读这份报告 ›</button></div>';
      });
      html += "</div></div>";
    }
    host.innerHTML = html;
    host.querySelectorAll('button[data-ask="report"]').forEach((b) => {
      b.onclick = () => askReport(b.dataset.item, b.dataset.result, b.dataset.ref, b.dataset.date, b.dataset.abnormal === "1");
    });
  } catch (e) { host.innerHTML = '<div class="panel-loading">报告加载失败。</div>'; }
}
function askReport(item, result, refRange, date, abnormal) {
  if (!item) { return; }
  // 自动切换到「报告解读」模块，确保解读对话落在 intake 上下文
  setSvc("intake");
  const status = abnormal ? "异常（超出参考范围）" : "正常";
  const prompt = "请帮我解读我的【" + item + "】检验报告："
    + "结果 " + result + "，"
    + "参考范围 " + refRange + "，"
    + "报告日期 " + date + "，"
    + "状态 " + status + "。"
    + "请说明这个指标的临床意义、本次结果的含义、日常注意事项，以及是否需要复诊或进一步评估；如需就诊请推荐科室。";
  $("msg").value = prompt;
  send();
}

async function renderFollowup(host) {
  host.innerHTML = '<div class="panel-loading">正在加载健康档案…</div>';
  try {
    const [v, r] = await Promise.all([
      apiFetch("/api/vitals").then((x) => x.json()),
      apiFetch("/api/reminders").then((x) => x.json()),
    ]);
    let html = '<div class="panel"><div class="panel-h"><span>我的健康档案</span>' +
      '<span class="panel-sub">最近体征与随访提醒</span></div>';
    html += '<div class="vital-grid">';
    if (!v.length) html += '<div class="empty">暂无体征记录。</div>';
    v.forEach((x) => {
      html += '<div class="vital-card"><div class="v-name">' + (VITAL_NAME[x.type] || x.type) + "</div>" +
        '<div class="v-val">' + esc(x.value) + '<span class="v-unit">' + esc(x.unit || "") + "</span></div>" +
        '<div class="v-time">' + esc(x.measured_at || "") + "</div></div>";
    });
    html += "</div>";
    html += '<div class="rem-h">随访提醒</div>';
    if (!r.length) html += '<div class="empty">暂无随访提醒。</div>';
    else {
      html += '<div class="rem-list">';
      r.forEach((x) => {
        html += '<div class="rem-item"><div class="rem-dot"></div><div class="rem-body"><div>' + esc(x.content) + "</div>" +
          '<div class="rem-meta">' + esc(x.remind_at || "—") + " · " + (x.status === "DONE" ? "已完成" : "待执行") + "</div></div></div>";
      });
      html += "</div>";
    }
    html += "</div>";
    host.innerHTML = html;
  } catch (e) { host.innerHTML = '<div class="panel-loading">健康档案加载失败。</div>'; }
}

async function renderDepartments(host) {
  try {
    const list = await apiFetch("/api/departments").then((x) => x.json());
    let html = '<div class="panel"><div class="panel-h"><span>科室导航</span>' +
      '<span class="panel-sub">按症状选择对应科室</span></div><div class="dept-list">';
    list.forEach((d) => {
      const name = d.name || "";
      html += '<div class="dept-card"><b>' + esc(name) + "</b><span>" + esc(d.description || "") + "</span>" +
        '<button class="link-btn" data-ask="dept" data-item="' + esc(name) + '">智能分诊 ›</button></div>';
    });
    html += "</div></div>";
    host.innerHTML = html;
    host.querySelectorAll('button[data-ask="dept"]').forEach((b) => b.onclick = () => askDept(b.dataset.item));
  } catch (e) { host.innerHTML = ""; }
}
function askDept(name) { $("msg").value = "我应该挂" + name + "吗？请帮我分诊"; send(); }

// -------- 多账号本地记忆 --------
function renderAccountSwitcher() {
  // 切换按钮上标注已记住的账号数量
  const n = Object.keys(getAccounts()).length;
  const btn = document.querySelector(".acct-wrap .btn-ghost");
  if (btn) btn.textContent = (n > 1 ? "切换(" + n + ") " : "切换 ") + "▾";
}

// -------- 登录 / 注册 / 切换 --------
function enterApp(u, token, refresh) {
  auth.token = token; auth.user = u;
  auth.refresh = refresh || "";
  rememberAccount(u, auth.token, auth.refresh);
  if (auth.refresh) localStorage.setItem("med_refresh", auth.refresh); else localStorage.removeItem("med_refresh");
  $("loginMask").style.display = "none";
  $("userbox").style.display = "flex";
  $("uname").textContent = u;
  $("uava").textContent = u.slice(0, 1).toUpperCase();
  renderAccountSwitcher();
  // 初始化当前模块为全新会话
  sessionMsgs = {};
  sessionBanner = {};
  sessionOkCard = {};
  restoreSession(curSvc);  // 新会话自动展示欢迎语（唯一出口）
  loadServiceData(curSvc);
  hydrate(curSvc);  // 刷新后从服务端恢复历史对话
  checkConsent();          // Tier-0：按需弹出知情同意书
}

async function login() {
  const u = $("lu").value.trim(), p = $("lp").value;
  $("lerr").textContent = "";
  try {
    const r = await fetch(absUrl("/auth/login"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p, role: "patient" }) });
    const d = await r.json();
    if (!r.ok) { $("lerr").textContent = d.detail || "登录失败"; return; }
    enterApp(u, "Bearer " + d.access_token, d.refresh_token);
  } catch (e) { $("lerr").textContent = "网络异常，请稍后再试"; }
}

function showRegister() { $("loginForm").style.display = "none"; $("regForm").style.display = "block"; $("lerr").textContent = ""; $("rerr").textContent = ""; }
function showLogin() { $("regForm").style.display = "none"; $("loginForm").style.display = "block"; $("rerr").textContent = ""; }

async function register() {
  const u = $("ru").value.trim(), p = $("rp").value, p2 = $("rp2").value;
  $("rerr").textContent = "";
  if (!/^[A-Za-z0-9_]{3,32}$/.test(u)) { $("rerr").textContent = "用户名需 3-32 位字母/数字/下划线"; return; }
  if (p.length < 6) { $("rerr").textContent = "密码至少 6 位"; return; }
  if (p !== p2) { $("rerr").textContent = "两次密码不一致"; return; }
  try {
    const r = await fetch(absUrl("/auth/register"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p, role: "patient" }) });
    const d = await r.json();
    if (!r.ok) { $("rerr").textContent = d.detail || "注册失败"; return; }
    // 注册成功自动登录
    const r2 = await fetch(absUrl("/auth/login"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p, role: "patient" }) });
    const d2 = await r2.json();
    if (!r2.ok) { $("rerr").textContent = "注册成功，请返回登录页手动登录"; return; }
    enterApp(u, "Bearer " + d2.access_token, d2.refresh_token);
  } catch (e) { $("rerr").textContent = "网络异常，请稍后重试"; }
}

function openLogin() {
  $("loginMask").style.display = "flex";
  $("lerr").textContent = ""; $("rerr").textContent = "";
  // 自动填入已记住的第一个账号，方便切换
  const acc = getAccounts();
  const names = Object.keys(acc);
  if (names.length && !auth.user) { $("lu").value = names[0]; }
}

function toggleAcctMenu(e) {
  e.stopPropagation();
  const m = $("acctMenu");
  if (m.style.display === "block") { m.style.display = "none"; return; }
  const acc = getAccounts();
  m.innerHTML = "";
  Object.keys(acc).forEach((u) => {
    const it = document.createElement("div");
    it.className = "acct-item" + (u === auth.user ? " active" : "");
    it.textContent = u + (u === auth.user ? "（当前）" : "");
    it.onclick = () => switchAccount(u);
    m.appendChild(it);
  });
  const add = document.createElement("div");
  add.className = "acct-item add";
  add.textContent = "＋ 登录其他账号";
  add.onclick = () => { m.style.display = "none"; openLogin(); };
  m.appendChild(add);
  m.style.display = "block";
}
document.addEventListener("click", () => { const m = $("acctMenu"); if (m) m.style.display = "none"; });

function switchAccount(u) {
  const acc = getAccounts();
  if (!acc[u]) { openLogin(); return; }
  $("acctMenu").style.display = "none";
  enterApp(u, acc[u].token, acc[u].refresh);
}

function logout() {
  const acc = getAccounts();
  delete acc[auth.user];
  localStorage.setItem("med_accounts", JSON.stringify(acc));
  auth.token = ""; auth.user = ""; auth.refresh = "";
  localStorage.removeItem("med_refresh");
  if (Object.keys(acc).length) { openLogin(); }
  else { location.reload(); }
}

// -------- 聊天 / SSE（操作当前模块的 thread_id 和消息列表） --------
function addMsg(role, who, text) {
  const el = addMsgToDOM(role, who, text);
  // 同时追加到当前模块的 session 记录
  if (!sessionMsgs[curSvc]) sessionMsgs[curSvc] = [];
  sessionMsgs[curSvc].push({ role, who, text });
  return el;
}

function addMsgToDOM(role, who, text) {
  const m = document.createElement("div");
  m.className = "msg " + role;
  const ava = document.createElement("div");
  ava.className = "ava";
  ava.textContent = role === "user" ? auth.user.slice(0, 1).toUpperCase() : "护";
  const b = document.createElement("div");
  b.className = "bubble";
  if (role === "bot") { const w = document.createElement("div"); w.className = "who"; w.textContent = who || "健康服务"; b.appendChild(w); }
  const t = document.createElement("div");
  t.className = "txt";
  t.textContent = text;
  b.appendChild(t);
  m.appendChild(ava);
  m.appendChild(b);
  $("chat").appendChild(m);
  $("chat").scrollTop = $("chat").scrollHeight;
  return t;
}

function showTyping() {
  const m = document.createElement("div");
  m.className = "msg bot";
  m.id = "typingMsg";
  m.innerHTML = '<div class="ava">护</div><div class="bubble"><div class="typing"><i></i><i></i><i></i></div></div>';
  $("chat").appendChild(m);
  $("chat").scrollTop = $("chat").scrollHeight;
}
function clearTyping() { const t = $("typingMsg"); if (t) t.remove(); }

// 免责声明脚注：渲染于每条助手回复气泡下方（Tier-0「每次显著免责声明」）
function appendDisclaimer(txtEl, text) {
  const bubble = txtEl && txtEl.parentElement;
  if (!bubble) return;
  let d = bubble.querySelector(".disclaimer-note");
  if (!d) { d = document.createElement("div"); d.className = "disclaimer-note"; bubble.appendChild(d); }
  d.textContent = text;
}

// 硬闸专用气泡：紧急救助（红）/ 服务范围说明（琥珀）
function addSpecialMsg(kind, who, text) {
  const m = document.createElement("div");
  m.className = "msg bot " + kind;
  const ava = document.createElement("div");
  ava.className = "ava";
  ava.textContent = "护";
  const b = document.createElement("div");
  b.className = "bubble";
  const w = document.createElement("div");
  w.className = "who";
  w.textContent = who;
  b.appendChild(w);
  const t = document.createElement("div");
  t.className = "txt";
  t.textContent = text;
  b.appendChild(t);
  m.appendChild(ava);
  m.appendChild(b);
  $("chat").appendChild(m);
  $("chat").scrollTop = $("chat").scrollHeight;
  if (!sessionMsgs[curSvc]) sessionMsgs[curSvc] = [];
  sessionMsgs[curSvc].push({ role: "bot", who, text });
  return t;
}
function addEmergencyMsg(text) { return addSpecialMsg("emergency", "🚨 紧急救助", text); }
function addScopeMsg(text) { return addSpecialMsg("scope", "服务范围说明", text); }

function showBanner(payload) {
  // 存到当前模块的 session 缓存
  sessionBanner[curSvc] = payload;
  showBannerDOM(payload);
}
function showBannerDOM(payload) {
  const host = $("bannerHost");
  host.innerHTML = "";
  const tools = (payload.tools || []).map((t) => TOOL_NAME[t] || t).join(" + ") || "相关操作";
  let kind = "";
  if (payload.action === "emergency_handoff") kind = "应急转诊";
  else if (payload.action === "medicare_settle") kind = "医保结算";
  else kind = "待审核操作";
  const b = document.createElement("div");
  b.className = "banner";
  b.innerHTML = '<svg class="bi icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>' +
    '<div>已提交<b>' + kind + '</b>申请，待医生审核确认后执行（' + esc(tools) + '）。<br/>审核编号 <span class="aid">' + esc(payload._aid || "") + '</span>，您可稍后在「健康咨询」中查看处理结果。</div>';
  host.appendChild(b);
}

function showOkCard(text) {
  sessionOkCard[curSvc] = text;
  showOkCardDOM(text);
}
function showOkCardDOM(text) {
  const host = $("bannerHost");
  host.innerHTML = "";
  const b = document.createElement("div");
  b.className = "ok-card";
  b.innerHTML = '<svg class="oci icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>' +
    '<div><b>预约成功</b><div class="ok-detail">' + esc(text) + "</div></div>";
  host.appendChild(b);
}

async function send() {
  if (streaming) return;
  const inp = $("msg");
  const text = inp.value.trim();
  if (!text) return;
  if (!auth.token) { $("loginMask").style.display = "flex"; return; }
  if (needConsent) { showConsentModal(); return; }  // Tier-0：未同意则先拦截
  addMsg("user", "", text);
  inp.value = "";
  inp.style.height = "auto";
  streaming = true;
  $("sendBtn").disabled = true;
  showTyping();
  // 超时控制：90秒未完成自动中止，避免 deepseek 卡死时永久挂起
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 90000);
  try {
    // 使用当前模块的独立 thread_id
    const tid = getTid(curSvc);
    const resp = await apiFetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, thread_id: tid }), signal: ac.signal });
    clearTimeout(timer);
    // 状态码检查：非 200 时给出明确提示，避免 resp.body 为 null 导致 getReader() 崩溃
    if (!resp.ok) {
      const errBody = await resp.text().catch(() => "");
      clearTyping();
      if (resp.status === 401) {
        auth.token = null;
        localStorage.removeItem("med_refresh");
        $("loginMask").style.display = "flex";
        addMsg("bot", SERVICES[curSvc].title, "登录已过期，请重新登录。");
      } else if (resp.status >= 500) {
        addMsg("bot", SERVICES[curSvc].title, "服务器繁忙，请稍后重试。(" + resp.status + ")");
      } else {
        addMsg("bot", SERVICES[curSvc].title, "请求失败(" + resp.status + ")，请稍后重试。");
      }
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", aid = null, done = null, payload = null, botEl = null, pendingDisclaimer = "";
    clearTyping();
    const curTitle = SERVICES[curSvc].title;
    function ensureBotBubble() {
      if (!botEl) { botEl = addMsg("bot", curTitle, ""); if (pendingDisclaimer) { appendDisclaimer(botEl, pendingDisclaimer); pendingDisclaimer = ""; } }
      return botEl;
    }
    while (true) {
      const { done: nd, value } = await reader.read();
      if (nd) break;
      buf += dec.decode(value);
      const blocks = buf.split("\n\n");
      buf = blocks.pop();
      for (const blk of blocks) {
        if (!blk.startsWith("data:")) continue;
        let p;
        try { p = JSON.parse(blk.slice(5)); } catch (e) { continue; }
        if (p.type === "token") { ensureBotBubble().textContent += p.text; }
        else if (p.type === "emergency") { botEl = addEmergencyMsg(p.text); if (pendingDisclaimer) { appendDisclaimer(botEl, pendingDisclaimer); pendingDisclaimer = ""; } done = "emergency"; }
        else if (p.type === "scope") { botEl = addScopeMsg(p.text); if (pendingDisclaimer) { appendDisclaimer(botEl, pendingDisclaimer); pendingDisclaimer = ""; } done = "scope"; }
        else if (p.type === "consent_required") { needConsent = true; showConsentModal(); done = "consent"; }
        else if (p.type === "disclaimer") { pendingDisclaimer = p.text; if (botEl) { appendDisclaimer(botEl, p.text); pendingDisclaimer = ""; } }
        else if (p.type === "interrupt") { aid = p.approval_id; payload = p.payload; done = "human"; }
        else if (p.type === "done") done = p.turn;
        $("chat").scrollTop = $("chat").scrollHeight;
      }
    }
    if (aid && payload) { showBanner({ ...payload, _aid: aid }); }
    else if (!botEl && done && done !== "consent" && done !== "emergency" && done !== "scope") { const b = addMsg("bot", curTitle, ""); if (pendingDisclaimer) appendDisclaimer(b, pendingDisclaimer); b.textContent = "已为您处理，请问还有其他需要吗？"; }
    // 挂号自动成功（无 interrupt）→ 显示绿色成功卡
    else if (curSvc === "booking" && !aid && botEl && (botEl.textContent.includes("挂号") || botEl.textContent.includes("预约") || botEl.textContent.includes("锁定"))) {
      showOkCard(botEl.textContent);
    }
  } catch (e) {
    clearTimeout(timer);
    clearTyping();
    // 显示具体错误信息用于诊断（正式环境可收起）
    const detail = e.name + ": " + (e.message || "").substring(0, 200);
    console.error("[chat] send error:", e);
    if (e.name === "AbortError") {
      addMsg("bot", SERVICES[curSvc].title, "⏱ 响应超时（90秒），AI模型可能较慢。请稍后重试。\n[调试] " + detail);
    } else if (e.message && (e.message.includes("fetch") || e.message.includes("Failed") || e.message.includes("NetworkError"))) {
      addMsg("bot", SERVICES[curSvc].title, "🔌 网络连接失败，请检查网络/代理设置。\n[调试] " + detail);
    } else if (e.message && e.message.includes("Unexpected token")) {
      addMsg("bot", SERVICES[curSvc].title, "⚠ 服务端返回异常数据。\n[调试] " + detail);
    } else {
      addMsg("bot", SERVICES[curSvc].title, "❌ 服务暂时不可用\n[调试] " + detail);
    }
  } finally {
    streaming = false;
    $("sendBtn").disabled = false;
    if ($("chat").scrollHeight) $("chat").scrollTop = $("chat").scrollHeight;
  }
}
$("msg").addEventListener("input", function () { this.style.height = "auto"; this.style.height = Math.min(this.scrollHeight, 120) + "px"; });

setSvc("home");

// ==================== 知情同意（Tier-0 法律责任红线） ====================
const CONSENT_TEXT = (
  "一、服务性质\n" +
  "本服务是由人工智能辅助的「健康科普 / 智能分诊 / 就医引导」工具，并非医疗机构，" +
  "不提供疾病诊断、不出具处方、不替代医师面诊。\n\n" +
  "二、责任边界\n" +
  "AI 的回复可能存在偏差或不确定性，关键医疗决策（用药、手术、急症处置等）必须由具备资质的" +
  "执业医师作出。您理解并同意：因依赖本服务而产生的任何健康风险由本人承担。\n\n" +
  "三、数据处理\n" +
  "为提供服务与质量改进，您与系统的对话内容将被记录（含输入/输出文本、时间、链路追踪标识）。" +
  "我们按最小必要原则处理个人信息，并依据《个人信息保护法》等法规予以保护。\n\n" +
  "四、急症免责\n" +
  "当您描述胸痛、呼吸困难、大出血、昏迷、卒中（面瘫/肢体无力/言语不清）等危急征象时，" +
  "系统会直接提示您拨打 120 或前往最近急诊，而不会给出常规对话式建议。\n\n" +
  "点击「我已阅读并同意」，即表示您已知晓上述条款并自愿使用本服务。"
);

function checkConsent() {
  if (!auth.token) return;
  apiFetch("/api/consent/status").then((r) => r.json()).then((d) => {
    if (d.required) { needConsent = true; showConsentModal(); }
  }).catch(() => {});
}
function showConsentModal() {
  $("consentBody").textContent = CONSENT_TEXT;
  $("consentChk").checked = false;
  $("consentBtn").disabled = true;
  $("consentMask").style.display = "flex";
}
$("consentChk").onchange = () => { $("consentBtn").disabled = !$("consentChk").checked; };
async function submitConsent() {
  try {
    const r = await apiFetch("/api/consent", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ types: "service,scope,data", channel: "web" }) });
    if (!r.ok) { alert("同意提交失败，请重试"); return; }
    needConsent = false;
    $("consentMask").style.display = "none";
  } catch (e) { alert("网络异常，请重试"); }
}

// 交互动作注册表（供事件委托按 data-action 分发）
registerActions({
  "login": function () { login(); },
  "show-register": function () { showRegister(); },
  "register": function () { register(); },
  "show-login": function () { showLogin(); },
  "go-review": function () { window.location = "/review"; },
  "toggle-acct-menu": function (el, e) { toggleAcctMenu(e); },
  "logout": function () { logout(); },
  "send": function () { send(); },
  "submit-consent": function () { submitConsent(); },
});

// 刷新后若本地存有账号 token，静默重登，避免每次刷新都要求重新输入密码
(function autoResume() {
  try {
    const acc = getAccounts();
    const names = Object.keys(acc);
    if (names.length) {
      const u = names[0];
      const a = acc[u];
      if (a && a.token) { enterApp(u, a.token, a.refresh || ""); return; }
    }
  } catch (e) {}
  openLogin();
})();
