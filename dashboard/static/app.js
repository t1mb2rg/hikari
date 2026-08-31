const statusText = {
  healthy: "正常",
  running: "运行中",
  waiting: "等待中",
  warning: "警告",
  error: "故障",
  offline: "离线",
  idle: "空闲",
};

const overallText = {
  healthy: "正常",
  degraded: "部分异常",
  offline: "Resident 离线",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let latestStatus = null;
let toastTimer = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
}

function formatTime(iso) {
  if (!iso) return "--";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function statusClass(status) {
  return `status-${status || "idle"}`;
}

function renderCards(components) {
  $("#component-cards").innerHTML = components.map((item) => `
    <article class="status-card">
      <div class="card-top">
        <span class="card-label">${escapeHtml(item.label)}</span>
        <span class="status-pill ${statusClass(item.status)}">${escapeHtml(statusText[item.status] || item.status)}</span>
      </div>
      <div class="card-phase">${escapeHtml(item.phase)}</div>
      <p class="card-message">${escapeHtml(item.message)}</p>
      ${item.blocking_on ? `<div class="card-blocker">正在等待：${escapeHtml(item.blocking_on)}</div>` : ""}
      <div class="muted" style="margin-top:10px">最后更新 ${escapeHtml(formatTime(item.updated_at))}</div>
    </article>
  `).join("");
}

function renderPipeline(components) {
  const pipeline = $("#pipeline");
  pipeline.innerHTML = components.map((item, index) => `
    ${index ? '<div class="pipe-arrow">›</div>' : ""}
    <div class="pipe-step ${statusClass(item.status)}">
      <div class="pipe-name">${escapeHtml(item.label)}</div>
      <div class="pipe-phase">${escapeHtml(item.phase)}</div>
    </div>
  `).join("");
}

function renderBlocker(blocker) {
  const target = $("#current-blocker");
  if (!blocker) {
    target.className = "empty-state";
    target.textContent = "当前没有明确阻塞点。";
    return;
  }
  target.className = "blocker-box";
  target.innerHTML = `
    <strong>${escapeHtml(blocker.component)} · ${escapeHtml(blocker.phase)}</strong>
    <div>正在等待：${escapeHtml(blocker.blocking_on || "未知")}</div>
    <div class="muted" style="margin-top:6px">${escapeHtml(blocker.message)}</div>
  `;
}

function eventRows(events, emptyText = "暂无记录") {
  if (!events?.length) return `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
  return events.map((event) => `
    <div class="event-row">
      <div class="event-source">${escapeHtml(event.source)}</div>
      <div class="event-summary">${escapeHtml(event.summary)}</div>
    </div>
  `).join("");
}

function renderNapCat(components) {
  const item = components.find((component) => component.id === "napcat");
  if (!item) return;
  const details = item.details || {};
  $("#napcat-state").textContent = statusText[item.status] || item.status;
  $("#qq-login-state").textContent = details.qq_logged_in ? "已登录" : "未登录";
  $("#onebot-state").textContent = details.onebot_connected ? "已连接" : "未连接";
  $("#napcat-phase").textContent = item.phase || "--";

  const qr = $("#qr-container");
  const message = $("#qr-message");
  const qrUrl = details.qrcode_url;
  if (details.qq_logged_in) {
    qr.innerHTML = '<div class="qr-placeholder">QQ 已登录</div>';
    message.textContent = "当前账号已经在线。";
  } else if (qrUrl) {
    qr.innerHTML = `<img src="${escapeHtml(qrUrl)}" alt="QQ 登录二维码" />`;
    message.textContent = "请使用手机 QQ 扫码并在手机上确认登录。";
  } else {
    qr.innerHTML = '<div class="qr-placeholder">暂未拿到登录二维码</div>';
    message.textContent = item.last_error || "等待 NapCat 生成二维码。";
  }
}

function renderStatus(data) {
  latestStatus = data;
  const components = data.components || [];
  renderCards(components);
  renderPipeline(components);
  renderBlocker(data.current_blocker);
  $("#recent-errors").innerHTML = eventRows(data.recent_errors, "最近没有异常");
  renderNapCat(components);

  const overall = data.overall || "degraded";
  $("#overall-label").textContent = overallText[overall] || overall;
  $("#overall-dot").className = `status-dot ${overall === "healthy" ? "status-healthy" : overall === "offline" ? "status-offline" : "status-warning"}`;
  $("#last-refresh").textContent = `更新 ${formatTime(data.generated_at)}`;
}

async function refreshStatus({ silent = true } = {}) {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderStatus(await response.json());
  } catch (error) {
    $("#overall-label").textContent = "面板后端不可用";
    $("#overall-dot").className = "status-dot status-error";
    if (!silent) showToast(`刷新失败：${error.message}`);
  }
}

async function refreshEvents({ silent = true } = {}) {
  try {
    const response = await fetch("/api/events?limit=80", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    $("#event-list").innerHTML = eventRows(data.events, "暂时没有事件");
  } catch (error) {
    if (!silent) showToast(`事件刷新失败：${error.message}`);
  }
}

async function restartNapCat() {
  const button = $("#restart-napcat");
  if (!window.confirm("确认重启 Hikari 使用的 NapCat 实例？")) return;
  button.disabled = true;
  try {
    const response = await fetch("/api/napcat/restart", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "重启失败");
    showToast(data.message || "已请求重启 NapCat");
    setTimeout(() => refreshStatus({ silent: false }), 2500);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

$$('.nav-item').forEach((button) => {
  button.addEventListener("click", () => {
    $$('.nav-item').forEach((item) => item.classList.toggle("active", item === button));
    $$('.page').forEach((page) => page.classList.toggle("active", page.id === `page-${button.dataset.page}`));
    if (button.dataset.page === "events") refreshEvents({ silent: true });
  });
});

$("#restart-napcat").addEventListener("click", restartNapCat);
$("#refresh-now").addEventListener("click", () => refreshStatus({ silent: false }));
$("#refresh-events").addEventListener("click", () => refreshEvents({ silent: false }));

refreshStatus({ silent: false });
refreshEvents({ silent: true });
setInterval(() => refreshStatus({ silent: true }), 2500);
setInterval(() => {
  if ($("#page-events").classList.contains("active")) refreshEvents({ silent: true });
}, 5000);
