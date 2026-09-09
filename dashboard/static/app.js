"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const escapeHtml = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const statusText = {
  healthy: "正常",
  running: "运行中",
  active: "进行中",
  pending: "待执行",
  queued: "已入队",
  completed: "已完成",
  failed: "失败",
  failure: "失败",
  error: "故障",
  offline: "离线",
  warning: "需注意",
  waiting: "等待",
  blocked: "被阻塞",
  uncertain: "结果不确定",
  unknown: "未知",
  idle: "空闲",
  processing: "已认领",
  sent: "已送达",
  sending: "发送中",
  success: "通过",
  degraded: "部分异常",
  in_progress: "检查中",
  cancelled: "已取消",
  skipped: "已跳过",
  draft: "草稿",
  open: "开放",
  closed: "已关闭",
  ok: "已读取",
  accepted: "已接单",
  requested: "已登记",
  implementing: "实现中",
  candidate_tested: "候选验证通过",
  candidate_implemented: "候选代码已生成",
  capability_active: "已启用",
  resumed: "原任务已完成",
};
const effectText = {
  maintain_project: "修改与验证",
  inspect_project: "检查项目",
  run_project_command: "执行命令",
  push_engineering_branch: "推送分支",
  open_or_update_draft_pr: "创建 Draft PR",
};
const labels = {
  overview: "总览",
  tasks: "任务与进展",
  conversation: "对话与交付",
  capabilities: "能力与授权",
  github: "GitHub",
  napcat: "QQ 连接",
  events: "事件记录",
  settings: "配置",
};
let operations = null,
  latestStatus = null,
  settings = null,
  refreshing = false,
  toastTimer = null;
let githubPolicy = null,
  growthPolicy = null,
  growthSnapshot = null;
const pill = (s) =>
  '<span class="pill ' +
  (Object.hasOwn(statusText, s) ? s : "unknown") +
  '">' +
  escapeHtml(statusText[s] || s || "未知") +
  "</span>";
const empty = (text) =>
  '<div class="empty-state">' + escapeHtml(text) + "</div>";
function when(v) {
  if (!v) return "无记录";
  const d = new Date(typeof v === "number" ? v * 1000 : v);
  return Number.isNaN(d.getTime())
    ? "时间未知"
    : d.toLocaleString("zh-CN", {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("visible"), 4500);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      "X-Hikari-Action": "dashboard",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务返回了无法读取的响应");
  }
  if (!response.ok)
    throw new Error(data.detail || "请求失败（" + response.status + "）");
  return data;
}
function navigate(page) {
  if (!labels[page]) page = "overview";
  $$(".nav-item").forEach((x) =>
    x.classList.toggle("active", x.dataset.page === page),
  );
  $$(".page").forEach((x) =>
    x.classList.toggle("active", x.id === "page-" + page),
  );
  $("#breadcrumb").textContent = "工作空间 / " + labels[page];
  history.replaceState(null, "", "#" + page);
  if (page === "settings" && !settings) loadSettings();
  if (page === "settings" && !githubPolicy) loadGithubPolicy();
  if (page === "settings" && !growthPolicy) loadGrowthPolicy();
  if (page === "capabilities") refreshGrowth();
  if (page === "github") refreshGithub();
  if (page === "events") refreshEvents();
}
function steps(goal) {
  return (
    '<div class="steps">' +
    (goal.steps || [])
      .map(
        (s, i) =>
          '<div class="step ' +
          (s.status === "completed"
            ? "done"
            : i === goal.current_step_index
              ? "current"
              : "") +
          '"><small>0' +
          (i + 1) +
          "</small><b>" +
          escapeHtml(effectText[s.effect] || s.effect) +
          "</b><small>" +
          escapeHtml(
            goal.status !== "active" &&
              ["pending", "queued", "running"].includes(s.status)
              ? "已停止 · 原记录 " + (statusText[s.status] || s.status)
              : statusText[s.status] || s.status,
          ) +
          "</small></div>",
      )
      .join("") +
    "</div>"
  );
}
function taskContent(goal) {
  return (
    '<h3 class="focus-title">' +
    escapeHtml(goal.goal) +
    '</h3><div class="focus-meta">' +
    pill(goal.status) +
    "<span>" +
    escapeHtml(when(goal.updated_at)) +
    "</span></div>" +
    steps(goal)
  );
}
function renderTasks() {
  if (!operations) return;
  $("#task-source-list").innerHTML = (operations.tasks || [])
    .map(
      (task) =>
        '<details class="panel task-card"><summary><div><h3>' +
        escapeHtml(task.intent?.goal || task.turn?.text || "来源请求") +
        '</h3><span class="small">' +
        escapeHtml(task.intent?.kind) +
        " · " +
        escapeHtml(when(task.created_at)) +
        "</span></div>" +
        pill(task.status) +
        '</summary><div class="task-details"><p class="small">来源凭据 ' +
        escapeHtml(task.source_ref) +
        '</p><p class="small">' +
        escapeHtml(
          task.evidence?.goal_id ||
            task.evidence?.request_id ||
            task.evidence?.turn_id ||
            "",
        ) +
        "</p><pre>" +
        escapeHtml(
          JSON.stringify(
            {
              constraints: task.intent?.constraints,
              acceptance: task.intent?.acceptance_criteria,
              evidence: task.evidence,
            },
            null,
            2,
          ),
        ) +
        "</pre></div></details>",
    )
    .join("");
  const filter = $("#task-filter").value;
  const opened = new Set($$("details[open]").map((x) => x.dataset.id));
  const goals = (operations.goals || []).filter(
    (g) =>
      filter === "all" ||
      g.status === filter ||
      (filter === "blocked" && g.status === "failed"),
  );
  $("#goal-list").innerHTML = goals.length
    ? goals
        .map(
          (g) =>
            '<details class="panel task-card" data-id="' +
            escapeHtml(g.goal_id) +
            '" ' +
            (opened.has(g.goal_id) ? "open" : "") +
            "><summary><div><h3>" +
            escapeHtml(g.goal) +
            '</h3><span class="small">' +
            escapeHtml(when(g.updated_at)) +
            " · " +
            g.steps.length +
            " 个步骤</span></div>" +
            pill(g.status) +
            '</summary><div class="task-details">' +
            steps(g) +
            "<pre>" +
            escapeHtml(
              g.final_summary ||
                g.steps[g.current_step_index]?.result_message ||
                "等待下一步结果",
            ) +
            '</pre><span class="small">Goal ' +
            escapeHtml(g.goal_id) +
            "</span></div></details>",
        )
        .join("")
    : empty(
        operations.errors?.length
          ? "存在不可读取记录，请先检查诊断信息。"
          : "暂无符合条件的持久目标。",
      );
  $("#session-list").innerHTML =
    (operations.sessions || [])
      .map(
        (s) =>
          '<details class="panel task-card" data-id="' +
          escapeHtml(s.session_id) +
          '" ' +
          (opened.has(s.session_id) ? "open" : "") +
          "><summary><div><h3>" +
          escapeHtml(s.goal) +
          '</h3><span class="small">' +
          escapeHtml(when(s.updated_at)) +
          " · " +
          escapeHtml(s.branch || "尚未创建分支") +
          "</span></div>" +
          pill(s.status) +
          '</summary><div class="task-details"><pre>' +
          escapeHtml(s.summary || "暂无进展") +
          '</pre><div class="small">结果凭据：' +
          (s.result_verified
            ? "已读取 terminal result"
            : "尚无可确认的 terminal result") +
          "</div></div></details>",
      )
      .join("") || empty("尚无可读取的 Session。");
}
function renderOperations(data) {
  operations = data;
  const active = data.goals.filter((g) => g.status === "active");
  const uncertain =
    data.deliveries.filter((d) => d.state === "uncertain").length +
    data.claims.filter((c) => c.state === "uncertain").length;
  $("#task-badge").textContent = active.length;
  $("#metrics").innerHTML = [
    ["活动目标", active.length, "持久 Goal"],
    ["最近对话", data.receipts.length, "最近读取的回复凭据"],
    [
      "待交付",
      data.deliveries.filter((d) => ["pending", "sending"].includes(d.state))
        .length,
      "最近 60 条交付记录",
    ],
    [
      "需要核对",
      uncertain +
        data.errors.length +
        data.goals.filter((g) => ["failed", "blocked"].includes(g.status))
          .length,
      "失败目标、不确定结果与异常",
    ],
  ]
    .map(
      ([l, v, n]) =>
        '<div class="metric"><div class="metric-label">' +
        l +
        '</div><div class="metric-value">' +
        v +
        '</div><div class="metric-note">' +
        n +
        "</div></div>",
    )
    .join("");
  const focus = active[0] || data.goals[0];
  $("#focus-heading").textContent = active.length ? "当前进展" : "最近任务";
  $("#current-focus").className = "";
  $("#current-focus").innerHTML = focus
    ? taskContent(focus)
    : empty("当前没有可读取的持久目标。收到授权任务后，进展会出现在这里。");
  const attention = [
    ...data.errors.map((e) => ({
      title: "状态记录不可读取",
      detail: e.source + " · " + e.error,
    })),
    ...data.goals
      .filter((g) => ["failed", "blocked"].includes(g.status))
      .slice(0, 3)
      .map((g) => ({
        title: g.goal,
        detail: g.final_summary || "需要检查任务结果",
      })),
    ...data.claims
      .filter((c) => c.state === "uncertain")
      .slice(0, 2)
      .map((c) => ({ title: "请求结果不确定", detail: c.request_id })),
  ];
  $("#attention-list").className = "";
  $("#attention-list").innerHTML = attention.length
    ? attention
        .map(
          (a) =>
            '<div class="attention-item">' +
            escapeHtml(a.title) +
            "<small>" +
            escapeHtml(a.detail) +
            "</small></div>",
        )
        .join("")
    : empty("暂未发现需要你处理的记录。");
  renderTasks();
  $("#conversation-list").innerHTML =
    data.receipts
      .slice(0, 25)
      .map(
        (r) =>
          '<article class="conversation"><small>' +
          escapeHtml(r.channel + " / " + r.conversation_id) +
          " · " +
          escapeHtml(when(r.created_at)) +
          '</small><div class="bubble">' +
          escapeHtml(r.user_text) +
          '</div><div class="bubble reply">' +
          escapeHtml(r.reply_text) +
          "</div></article>",
      )
      .join("") || empty("尚无回复凭据。");
  $("#delivery-list").innerHTML =
    data.claims
      .filter((c) => c.state !== "completed")
      .map(
        (c) =>
          '<div class="remote-row">' +
          pill(c.state) +
          "<small>请求 " +
          escapeHtml(c.request_id) +
          "</small></div>",
      )
      .join("") +
      data.deliveries
        .map(
          (d) =>
            '<div class="remote-row">' +
            pill(d.state) +
            " <span>" +
            escapeHtml(d.channel) +
            "</span><small>" +
            escapeHtml(d.delivery_id) +
            "<br>" +
            escapeHtml(d.last_error || when(d.updated_at)) +
            "</small></div>",
        )
        .join("") || empty("暂无需要展示的交付记录。");
  $("#capability-list").innerHTML = data.capabilities
    .map(
      (c) =>
        '<article class="panel"><div class="capability-name">' +
        escapeHtml(c.key) +
        '</div><p class="small">' +
        escapeHtml(c.scope) +
        '</p><div class="capability-flags"><span class="pill ' +
        (c.available ? "healthy" : "unknown") +
        '">' +
        (c.available ? "已实现" : "未实现") +
        '</span><span class="pill ' +
        (c.delegated === null
          ? "unknown"
          : c.delegated
            ? "healthy"
            : "blocked") +
        '">' +
        (c.delegated === null
          ? "当前授权未知"
          : c.delegated
            ? "已委托"
            : "需授权") +
        '</span><span class="pill ' +
        (c.runtime_ready ? "healthy" : "unknown") +
        '">' +
        (c.runtime_ready === null
          ? "运行条件未知"
          : c.runtime_ready
            ? "Worker 在线"
            : "运行条件未满足") +
        "</span></div></article>",
    )
    .join("");
  renderComponents();
}
function renderComponents() {
  if (!latestStatus) return;
  let components = [...(latestStatus.components || [])];
  if (operations) {
    components.push(operations.worker, operations.model);
    if (operations.conversation) components.push(operations.conversation);
  }
  $("#component-cards").innerHTML = components
    .map(
      (c) =>
        '<article class="component-card"><div class="card-head"><h3>' +
        escapeHtml(c.label) +
        "</h3>" +
        pill(c.status) +
        '</div><div class="phase">' +
        escapeHtml(c.phase || c.model || statusText[c.status] || "未知") +
        "</div><p>" +
        escapeHtml(c.message) +
        "</p></article>",
    )
    .join("");
}
function renderNapcat() {
  const c = latestStatus?.components?.find((x) => x.id === "napcat");
  if (!c) return;
  const d = c.details || {};
  const truth = (v) => (v === true ? "是" : v === false ? "否" : "未知");
  $("#napcat-details").innerHTML = [
    ["NapCat 状态", statusText[c.status] || c.status],
    ["QQ 已登录", truth(d.qq_logged_in)],
    ["OneBot 监听端口可达", truth(d.onebot_listener_reachable)],
    ["OneBot 已连接", truth(d.onebot_connected)],
    ["当前阶段", c.phase],
  ]
    .map(
      ([k, v]) =>
        "<div><dt>" +
        escapeHtml(k) +
        "</dt><dd>" +
        escapeHtml(v) +
        "</dd></div>",
    )
    .join("");
  const qr = $("#qr-container");
  if (d.qq_logged_in) {
    qr.textContent = "QQ 已登录";
  } else if (d.qrcode_url) {
    if (!qr.querySelector("img"))
      qr.innerHTML = '<img src="/api/napcat/qrcode" alt="QQ 登录二维码">';
  } else {
    qr.textContent = "等待登录二维码";
  }
  $("#qr-message").textContent = c.last_error || c.message || "";
}
function eventRows(events) {
  return (
    events
      .map(
        (e) =>
          '<div class="event-row"><div class="event-source">' +
          escapeHtml(e.source) +
          '</div><div class="event-summary">' +
          escapeHtml(e.summary) +
          "</div></div>",
      )
      .join("") || empty("暂无记录。")
  );
}
async function refreshEvents() {
  try {
    const data = await api("/api/events?limit=80");
    $("#event-list").innerHTML = eventRows(data.events || []);
  } catch (e) {
    toast(e.message);
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const results = await Promise.allSettled([
      api("/api/status"),
      api("/api/operations"),
    ]);
    const failures = results.filter((r) => r.status === "rejected");
    if (results[0].status === "fulfilled") {
      latestStatus = results[0].value;
      $("#overall-label").outerHTML =
        '<span id="overall-label" class="pill ' +
        (statusText[latestStatus.overall] ? latestStatus.overall : "unknown") +
        '">' +
        escapeHtml(statusText[latestStatus.overall] || "未知") +
        "</span>";
      $("#recent-errors").innerHTML = eventRows(
        latestStatus.recent_errors || [],
      );
      renderNapcat();
    }
    if (results[1].status === "fulfilled") renderOperations(results[1].value);
    renderComponents();
    $("#connection-banner").classList.toggle("hidden", !failures.length);
    $("#connection-banner").textContent =
      "部分状态更新失败；页面可能保留旧数据。" +
      failures.map((r) => r.reason.message).join("；");
    if (!failures.length)
      $("#last-refresh").textContent = "更新于 " + when(Date.now() / 1000);
    $("#footer-time").textContent = new Date().toLocaleDateString("zh-CN");
  } finally {
    refreshing = false;
  }
}
async function refreshGithub() {
  try {
    const data = await api("/api/github");
    $("#github-summary").innerHTML =
      '<div class="notice">' +
      escapeHtml(data.repository || "GitHub") +
      " · " +
      escapeHtml(data.message || "已读取远端状态") +
      "</div>";
    $("#github-prs").innerHTML =
      (data.pull_requests || [])
        .map(
          (p) =>
            '<div class="remote-row"><a href="' +
            escapeHtml(p.url) +
            '" target="_blank" rel="noopener noreferrer">#' +
            p.number +
            " " +
            escapeHtml(p.title) +
            "</a><small>" +
            escapeHtml(p.head + " → " + p.base) +
            "</small>" +
            pill(p.draft ? "draft" : p.state) +
            '<button class="text-button gate-button" data-pr-gates="' +
            p.number +
            '">查看合并条件</button>' +
            "</div>",
        )
        .join("") || empty("暂无可读取的 PR。");
    $("#github-runs").innerHTML =
      (data.runs || [])
        .map(
          (r) =>
            '<div class="remote-row"><a href="' +
            escapeHtml(r.url) +
            '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(r.name) +
            "</a><small>" +
            escapeHtml(r.branch) +
            " · " +
            escapeHtml(when(r.updated_at)) +
            "</small>" +
            pill(r.conclusion || r.status) +
            "</div>",
        )
        .join("") || empty("暂无可读取的 Actions。");
  } catch (e) {
    $("#github-summary").innerHTML =
      '<div class="notice">远端状态不可用：' + escapeHtml(e.message) + "</div>";
  }
}
async function loadSettings() {
  try {
    settings = await api("/api/settings");
    const groups = {
      conversation: "对话模型",
      engineering: "工程能力",
      qq: "QQ 与访问范围",
      presence: "主动提醒",
      github: "GitHub",
    };
    $("#settings-fields").innerHTML = Object.entries(groups)
      .map(([g, label]) => {
        const fields = settings.fields.filter((f) => f.group === g);
        if (!fields.length) return "";
        return (
          '<section class="panel settings-group"><div class="panel-heading"><h2>' +
          label +
          '</h2></div><div class="settings-grid">' +
          fields
            .map((f) => {
              const choices = f.kind === "bool" ? ["false", "true"] : f.choices;
              const attrs =
                ' name="' +
                escapeHtml(f.key) +
                '" data-original="' +
                escapeHtml(f.value || "") +
                '"';
              const control = choices?.length
                ? "<select" +
                  attrs +
                  ">" +
                  choices
                    .map(
                      (v) =>
                        '<option value="' +
                        escapeHtml(v) +
                        '" ' +
                        (v === f.value ? "selected" : "") +
                        ">" +
                        escapeHtml(
                          v === "true" ? "启用" : v === "false" ? "关闭" : v,
                        ) +
                        "</option>",
                    )
                    .join("") +
                  "</select>"
                : "<input" +
                  attrs +
                  ' type="' +
                  (f.kind === "secret" ? "password" : "text") +
                  '" value="' +
                  escapeHtml(f.value || "") +
                  '" autocomplete="off" placeholder="' +
                  (f.kind === "secret"
                    ? f.configured
                      ? "已设置 · 留空保持"
                      : "尚未设置"
                    : "") +
                  '">';
              return (
                '<label class="field"><span>' +
                escapeHtml(f.label) +
                "</span>" +
                control +
                "<small>" +
                escapeHtml(f.key) +
                (f.process_override ? " · 当前进程存在覆盖值" : "") +
                "</small></label>"
              );
            })
            .join("") +
          "</div></section>"
        );
      })
      .join("");
    $("#settings-path").textContent = settings.path;
    $("#settings-state").textContent = "编辑已保存的配置";
  } catch (e) {
    toast(e.message);
  }
}
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!settings) return;
  const changes = {};
  $$("#settings-form [name]").forEach((x) => {
    if (x.value !== x.dataset.original) changes[x.name] = x.value;
  });
  if (!Object.keys(changes).length) {
    toast("没有待保存的修改");
    return;
  }
  $("#save-settings").disabled = true;
  try {
    const data = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ revision: settings.revision, changes }),
    });
    await loadSettings();
    $("#settings-state").textContent = "已保存 · 等待受控重启";
    toast(data.message);
  } catch (e) {
    toast(e.message);
  } finally {
    $("#save-settings").disabled = false;
  }
});
async function napcatAction(path) {
  try {
    const data = await api(path, { method: "POST" });
    toast(data.message);
    $("#qr-container").textContent = "正在刷新…";
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}
$$(".nav-item").forEach((x) =>
  x.addEventListener("click", () => navigate(x.dataset.page)),
);
$$("[data-go]").forEach((x) =>
  x.addEventListener("click", () => navigate(x.dataset.go)),
);
$("#refresh-all").addEventListener("click", refresh);
$("#refresh-events").addEventListener("click", refreshEvents);
$("#refresh-github").addEventListener("click", refreshGithub);
$("#github-prs").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-pr-gates]");
  if (!button) return;
  button.disabled = true;
  $("#github-gates").innerHTML =
    '<div class="notice">正在核对远端最新提交与合并条件…</div>';
  try {
    const data = await api(
      "/api/github/prs/" +
        encodeURIComponent(button.dataset.prGates) +
        "/gates",
    );
    $("#github-gates").innerHTML =
      '<section class="panel"><div class="panel-heading"><h2>PR #' +
      data.number +
      " · 合并条件</h2>" +
      pill(data.ready ? "healthy" : "blocked") +
      '</div><p class="small">已检查提交 ' +
      escapeHtml(data.head_sha) +
      "</p>" +
      data.conditions
        .map(
          (c) =>
            '<div class="attention-item">' +
            pill(c.passed ? "success" : "blocked") +
            " " +
            escapeHtml(c.reason) +
            "</div>",
        )
        .join("") +
      "</section>";
  } catch (error) {
    $("#github-gates").innerHTML =
      '<div class="notice">合并条件无法确认：' +
      escapeHtml(error.message) +
      "</div>";
  } finally {
    button.disabled = false;
  }
});
$("#reload-settings").addEventListener("click", loadSettings);
$("#task-filter").addEventListener("change", renderTasks);
$("#refresh-now").addEventListener("click", () =>
  napcatAction("/api/napcat/qrcode/refresh"),
);
$("#restart-napcat").addEventListener("click", () => {
  if (confirm("重启当前 NapCat 实例会暂时中断 QQ 连接，确认继续？"))
    napcatAction("/api/napcat/restart");
});
navigate(location.hash.slice(1));
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);

const lines = (text) =>
  text
    .split(/\r?\n/)
    .map((v) => v.trim())
    .filter(Boolean);
async function loadGithubPolicy() {
  try {
    githubPolicy = await api("/api/operator/github-policy");
    const name =
      $("#policy-repository").value.trim() ||
      Object.keys(githubPolicy.document.repositories)[0] ||
      settings?.fields.find((f) => f.key === "HIKARI_GITHUB_REPOSITORY")
        ?.value ||
      "";
    $("#policy-repository").value = name;
    displayGithubPolicy(name);
  } catch (error) {
    toast(error.message);
  }
}
function displayGithubPolicy(name) {
  const policy = githubPolicy?.document.repositories[name] || {};
  $("#policy-merge").value = String(policy.auto_merge === true);
  $("#policy-bases").value = (policy.allowed_bases || []).join("\n");
  $("#policy-checks").value = (policy.required_checks || []).join("\n");
  $("#policy-physical").value = String(policy.require_physical_gate !== false);
  $("#policy-method").value = policy.method || "squash";
  $("#policy-workflows").value = Object.entries(policy.rerun_workflows || {})
    .map(([path, sha]) => path + " = " + sha)
    .join("\n");
  $("#github-policy-state").textContent = githubPolicy?.configured
    ? "已读取操作人策略"
    : "尚未配置，自动合并关闭";
}
async function loadGrowthPolicy() {
  try {
    growthPolicy = await api("/api/operator/growth-policy");
    $("#growth-auto").value = String(
      growthPolicy.document.auto_activate_pure_recipes,
    );
    const available = growthPolicy.available_services;
    const names = Array.isArray(available)
      ? available
      : Object.keys(available || {});
    $("#growth-services").innerHTML = names
      .map(
        (name) =>
          '<label><input type="checkbox" name="growth-service" value="' +
          escapeHtml(name) +
          '" ' +
          (growthPolicy.document.allowed_services.includes(name)
            ? "checked"
            : "") +
          "> " +
          escapeHtml(name) +
          "</label>",
      )
      .join("");
    $("#growth-policy-state").textContent = growthPolicy.configured
      ? "已读取操作人策略"
      : "默认不自动启用候选能力";
  } catch (error) {
    toast(error.message);
  }
}
async function refreshGrowth() {
  try {
    growthSnapshot = await api("/api/operator/capabilities");
    $("#growth-requests").innerHTML =
      (growthSnapshot.requests || [])
        .map(
          (item) =>
            '<section class="panel"><div class="panel-heading"><h2>' +
            escapeHtml(item.capability_id) +
            " · v" +
            item.version +
            "</h2>" +
            pill(item.status === "active" ? "capability_active" : item.status) +
            '</div><p class="small">' +
            escapeHtml(item.intent || "") +
            '</p><p class="small">实现类型 ' +
            escapeHtml(item.implementation_kind) +
            " · " +
            escapeHtml(when(item.updated_at)) +
            '</p><p class="small">版本摘要 ' +
            escapeHtml(item.candidate_digest || "尚未生成") +
            '</p><details class="task-details"><summary>查看验证证据</summary><pre>' +
            escapeHtml(JSON.stringify(item.evidence, null, 2)) +
            "</pre></details>" +
            (item.activatable
              ? '<button class="button secondary activate-capability" data-request="' +
                escapeHtml(item.request_id) +
                '" data-digest="' +
                escapeHtml(item.candidate_digest) +
                '">启用这个已验证版本</button>'
              : "") +
            "</section>",
        )
        .join("") ||
      empty(
        "还没有能力增长请求。需要新能力时，可以在私聊中描述目标和验收示例。",
      );
    if (growthSnapshot.errors?.length)
      $("#growth-requests").insertAdjacentHTML(
        "afterbegin",
        '<div class="notice">' +
          escapeHtml(growthSnapshot.errors.map((e) => e.error).join("；")) +
          "</div>",
      );
  } catch (error) {
    $("#growth-requests").innerHTML = empty(error.message);
  }
}
$("#reload-github-policy").addEventListener("click", loadGithubPolicy);
$("#reload-growth-policy").addEventListener("click", loadGrowthPolicy);
$("#refresh-growth").addEventListener("click", refreshGrowth);
$("#policy-repository").addEventListener("change", () =>
  displayGithubPolicy($("#policy-repository").value.trim()),
);
$("#github-policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!githubPolicy) return;
  const repository = $("#policy-repository").value.trim();
  const workflows = {};
  try {
    for (const line of lines($("#policy-workflows").value)) {
      const index = line.indexOf("=");
      if (index < 0) throw new Error("工作流每行需要 路径 = 完整 blob SHA");
      workflows[line.slice(0, index).trim()] = line.slice(index + 1).trim();
    }
    const document = structuredClone(githubPolicy.document);
    document.repositories[repository] = {
      auto_merge: $("#policy-merge").value === "true",
      allowed_bases: lines($("#policy-bases").value),
      required_checks: lines($("#policy-checks").value),
      require_physical_gate: $("#policy-physical").value === "true",
      method: $("#policy-method").value,
      rerun_workflows: workflows,
    };
    githubPolicy = await api("/api/operator/github-policy", {
      method: "PUT",
      body: JSON.stringify({ document, revision: githubPolicy.revision }),
    });
    $("#github-policy-state").textContent = "策略已保存，后续操作将按此验证";
    toast("GitHub 授权策略已保存，没有执行任何合并");
  } catch (error) {
    toast(error.message);
  }
});
$("#growth-policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!growthPolicy) return;
  try {
    const document = {
      version: 1,
      auto_activate_pure_recipes: $("#growth-auto").value === "true",
      allowed_services: $$("#growth-services input:checked").map(
        (x) => x.value,
      ),
    };
    growthPolicy = await api("/api/operator/growth-policy", {
      method: "PUT",
      body: JSON.stringify({ document, revision: growthPolicy.revision }),
    });
    $("#growth-policy-state").textContent = "已保存，由运行中的续跑器应用";
    toast("能力授权策略已保存");
  } catch (error) {
    toast(error.message);
  }
});
$("#growth-requests").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-request]");
  if (!button) return;
  if (
    !confirm(
      "启用此摘要对应的已验证纯文本能力？原请求会在运行中的续跑器内继续。",
    )
  )
    return;
  button.disabled = true;
  try {
    await api(
      "/api/operator/capabilities/" +
        encodeURIComponent(button.dataset.request) +
        "/activate",
      {
        method: "POST",
        body: JSON.stringify({ digest: button.dataset.digest }),
      },
    );
    toast("指定版本已启用");
    await refreshGrowth();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
});
