/* ============================================================================
   扫描进度面板 —— 三段步骤条 + 节点进度条 + 逐节点的实时格子
   ----------------------------------------------------------------------------
   一次扫描要经过「启动（提交、排队、准备环境）→ 扫描节点 → 校验结果」。启动那几步用户既管不了、
   也不关心，只合成一格；面板的主体是扫描节点那一段：每个节点一格，实时显示它在做什么（等待 /
   启动代理 / 连接检测站 / 查询 IP 质量 / 重试）、用了多久，以及完整 / 部分 / 失败的终态与原因。
   读数来自 runner 约每秒一次的回报（见 worker/progress.js 与 backend/app/scan/reporter.py）。

   三条约定：
   - 读数只是过程：格子里没有出口 IP 与评分，表格仍然只认终态 artifact，面板上的东西不进表、不进导出；
   - 读不到读数（Worker 没有进度存储、runner 没报上来）时退回不定进度条，不编数字；
   - 只用 <progress> 与 data-* 属性表达状态，不写任何 style（生产 CSP 是 style-src 'self'）；
   - 过程是一次性的：扫描一完成，面板自己折成一行摘要（data-collapsed），版面还给下面的表格，
     要回看步骤条与逐节点格子再点「明细」展开。出错时不折——那时用户要看的正是这些。
   ========================================================================== */

// 细分的阶段：说明文字、阶段先后（不倒退）都按它算；步骤条上只画三格（STEP_OF）。
export const STAGES = [
  ["submit", "提交"],
  ["queue", "排队"],
  ["prepare", "准备环境"],
  ["scan", "扫描节点"],
  ["verify", "校验结果"],
];
export const STEPS = [
  ["start", "启动"],
  ["scan", "扫描节点"],
  ["verify", "校验结果"],
];
const STEP_OF = { submit: "start", queue: "start", prepare: "start", scan: "scan", verify: "verify" };

const STAGE_KEYS = STAGES.map(([key]) => key);
const STEP_KEYS = STEPS.map(([key]) => key);
// scan.yml 里真正跑扫描的那一步。之前的步骤都是准备环境，之后的是上传与清理。
export const SCAN_STEP_NAME = "Run production scan";
const PHASE_STAGE = { subscription: "prepare", scanning: "scan", packaging: "verify", done: "verify" };

// 每一段的那句话：说「正在做什么」，不说「加载中」。发起扫描与跟进扫描共用。
export const STAGE_TEXT = {
  submit: "正在提交订阅地址",
  queue: "启动中：排队等 GitHub 分配执行机",
  prepare: "启动中：准备环境、拉取订阅",
  scan: "正在逐个检测节点",
  verify: "节点检测完成：正在打包、上传并校验结果",
};
const MAX_COUNT = 100_000;
const MAX_NODES = 1000;

// 节点的几档（与 worker/progress.js 的 NODE_STATES 同一份）。
const NODE_TEXT = {
  wait: "等待中",
  retry: "准备重试",
  start: "启动代理",
  connect: "连接检测站",
  lookup: "查询 IP 质量",
  success: "完整",
  partial: "部分",
  failed: "失败",
};
const ACTIVE_STATES = new Set(["retry", "start", "connect", "lookup"]);
const FINAL_STATES = new Set(["success", "partial", "failed"]);

function count(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= MAX_COUNT ? value : null;
}

function text(value, max) {
  return typeof value === "string" && value.length >= 1 && value.length <= max ? value : null;
}

function normalizeNode(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const name = text(raw.n, 80);
  if (name === null || !Object.hasOwn(NODE_TEXT, raw.s)) return null;
  const attempt = Number.isSafeInteger(raw.a) && raw.a >= 0 && raw.a <= 10 ? raw.a : null;
  const elapsed = Number.isSafeInteger(raw.ms) && raw.ms >= 0 && raw.ms <= 3_600_000 ? raw.ms : null;
  if (attempt === null || elapsed === null) return null;
  return { n: name, t: text(raw.t, 24) || "", s: raw.s, a: attempt, ms: elapsed, r: text(raw.r, 60) };
}

/** 逐节点读数：个数必须等于总数、终态个数必须与计数一致，否则整份不用（格子退回不画）。 */
function normalizeNodes(raw, counts) {
  if (!Array.isArray(raw) || raw.length !== counts.total || raw.length > MAX_NODES) return null;
  const nodes = raw.map(normalizeNode);
  if (nodes.includes(null)) return null;
  for (const state of FINAL_STATES) {
    if (nodes.filter((node) => node.s === state).length !== counts[state]) return null;
  }
  return nodes;
}

/** Worker 交回的进度读数 → 自洽的计数（与可选的逐节点读数）；计数不合规就当没有，不猜。 */
export function normalizeProgress(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (!Object.hasOwn(PHASE_STAGE, raw.phase)) return null;
  const total = count(raw.total);
  const completed = count(raw.completed);
  const success = count(raw.success);
  const partial = count(raw.partial);
  const failed = count(raw.failed);
  if ([total, completed, success, partial, failed].includes(null)) return null;
  if (completed > total || success + partial + failed !== completed) return null;
  const progress = { phase: raw.phase, total, completed, success, partial, failed };
  const nodes = normalizeNodes(raw.nodes, progress);
  return nodes ? { ...progress, nodes } : progress;
}

function rank(stage) {
  const index = STAGE_KEYS.indexOf(stage);
  return index < 0 ? 0 : index;
}

/** 两段里较后的那一段：晚到的旧读数不能把步骤条往回拨。 */
export function laterStage(left, right) {
  if (!STAGE_KEYS.includes(left)) return right;
  if (!STAGE_KEYS.includes(right)) return left;
  return rank(left) >= rank(right) ? left : right;
}

/** 进度读数自己说到了哪一段（没有读数时为 null）。 */
export function stageOfProgress(progress) {
  return progress ? PHASE_STAGE[progress.phase] || null : null;
}

/** 从 Actions 的步骤列表看走到了哪一段：扫描那一步之前是准备，之后是校验。 */
function stageFromSteps(jobs) {
  const list = Array.isArray(jobs) ? jobs : [];
  const job = list.find((item) => item?.status === "in_progress") || list[0] || null;
  const steps = Array.isArray(job?.steps) ? job.steps : [];
  const scanIndex = steps.findIndex((step) => step?.name === SCAN_STEP_NAME);
  if (scanIndex < 0) return "prepare";
  const current = steps.findIndex((step) => step?.status === "in_progress");
  if (current >= 0) {
    if (current < scanIndex) return "prepare";
    return current === scanIndex ? "scan" : "verify";
  }
  return steps[scanIndex]?.status === "completed" ? "verify" : "prepare";
}

/**
 * 运行状态 → 细分阶段。runner 的进度读数比步骤名更准（它知道订阅下完没有、节点扫完没有），
 * 两者取较后的那一段。
 */
export function stageOf({ runStatus, jobs = [], progress = null }) {
  if (!runStatus || runStatus === "queued") return "queue";
  if (runStatus === "completed") return "verify";
  const fromSteps = stageFromSteps(jobs);
  const fromProgress = stageOfProgress(progress);
  return fromProgress ? laterStage(fromProgress, fromSteps) : fromSteps;
}

// 时长写法：一小时以内给「分秒」。
export function formatElapsed(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 1000) return "不到 1 秒";
  const totalSeconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}小时${minutes}分`;
  if (minutes) return `${minutes}分${seconds}秒`;
  return `${seconds}秒`;
}

/** 「12 / 25 个节点 · 9 完整 · 2 部分 · 1 失败 · 6 检测中」：只说有的那几项，0 就不提。 */
export function countText(progress) {
  if (!progress || progress.total < 1) return "";
  const parts = [`${progress.completed} / ${progress.total} 个节点`];
  if (progress.success) parts.push(`${progress.success} 完整`);
  if (progress.partial) parts.push(`${progress.partial} 部分`);
  if (progress.failed) parts.push(`${progress.failed} 失败`);
  const active = Array.isArray(progress.nodes) ? progress.nodes.filter((node) => ACTIVE_STATES.has(node.s)).length : 0;
  if (active) parts.push(`${active} 检测中`);
  return parts.join(" · ");
}

/** 一格里的那句阶段说明。 */
export function nodeStageText(node) {
  const label = NODE_TEXT[node.s] || node.s;
  if (node.s === "retry") return node.r ? `准备重试 · 上次：${node.r}` : label;
  if (ACTIVE_STATES.has(node.s)) return node.a > 1 ? `重试中 · ${label}` : label;
  return node.r && FINAL_STATES.has(node.s) ? `${label} · ${node.r}` : label;
}

/** 一格里的用时：进行中的整秒往上走（extraMs 是读数到手后过去的时间），终态给一位小数。 */
export function nodeTimeText(node, extraMs = 0) {
  if (node.s === "wait") return "";
  if (FINAL_STATES.has(node.s)) return `${(node.ms / 1000).toFixed(1)}s`;
  return `${Math.floor((node.ms + Math.max(0, extraMs)) / 1000)}s`;
}

export function nodesAllFinal(nodes) {
  return Array.isArray(nodes) && nodes.length > 0 && nodes.every((node) => FINAL_STATES.has(node.s));
}

/** 逐节点格子：每个节点一个 <li>，按订阅顺序；读数更新时只改变了的那几格。 */
function createNodeGrid(doc, list) {
  let cells = [];
  let nodes = null;
  let receivedAt = 0;

  function cell() {
    const item = doc.createElement("li");
    const dot = doc.createElement("span");
    dot.className = "node-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = doc.createElement("span");
    name.className = "node-name";
    const time = doc.createElement("span");
    time.className = "node-time";
    const stage = doc.createElement("span");
    stage.className = "node-stage";
    item.append(dot, name, time, stage);
    return { item, name, time, stage, key: "" };
  }

  function paintTimes(now) {
    if (!nodes) return;
    const extra = receivedAt ? now - receivedAt : 0;
    nodes.forEach((node, index) => {
      const value = nodeTimeText(node, extra);
      if (cells[index].time.textContent !== value) cells[index].time.textContent = value;
    });
  }

  function update(next, at = Date.now()) {
    if (!Array.isArray(next) || !next.length) {
      list.hidden = true;
      return;
    }
    if (cells.length !== next.length) {
      cells = next.map(cell);
      list.replaceChildren(...cells.map((entry) => entry.item));
    }
    next.forEach((node, index) => {
      const entry = cells[index];
      const key = `${node.n}|${node.t}|${node.s}|${node.a}|${node.r || ""}`;
      if (entry.key === key) return;
      entry.key = key;
      entry.item.dataset.state = node.s;
      entry.item.title = node.t ? `${node.n}（${node.t}）` : node.n;
      entry.name.textContent = node.n;
      entry.stage.textContent = nodeStageText(node);
    });
    nodes = next;
    receivedAt = at;
    list.hidden = false;
    paintTimes(at);
  }

  function clear() {
    cells = [];
    nodes = null;
    receivedAt = 0;
    list.replaceChildren();
    list.hidden = true;
  }

  return { update, clear, tick: paintTimes };
}

/**
 * 绑定面板骨架（index.html 里的 #scanPanel）。缺任何一块都返回一个什么也不做的面板：
 * 进度面板是反馈层，少了它扫描照样能跑完、表格照样能填上。逐节点格子（#scanNodes）可选。
 */
export function createScanPanel(doc = globalThis.document) {
  const root = doc?.getElementById("scanPanel");
  const steps = root ? Array.from(root.querySelectorAll("[data-step]")) : [];
  const meter = doc?.getElementById("scanMeter");
  const counts = doc?.getElementById("scanCounts");
  const message = doc?.getElementById("scanProgress");
  const elapsed = doc?.getElementById("scanElapsed");
  const nodeList = doc?.getElementById("scanNodes");
  const toggle = doc?.getElementById("scanToggle");
  if (!root || steps.length !== STEPS.length || !meter || !counts || !message || !elapsed) {
    return { show() {}, hide() {}, tick() {}, get visible() { return false; } };
  }
  const grid = nodeList ? createNodeGrid(doc, nodeList) : { update() {}, clear() {}, tick() {} };

  let startedAt = 0;
  let finishedAt = 0;
  let lastState = "";

  // 折叠只在「完成」时有意义：别的状态下按钮藏起来、面板总是展开。
  function setCollapsed(collapsed) {
    root.dataset.collapsed = collapsed ? "true" : "false";
    if (!toggle) return;
    toggle.hidden = root.dataset.state !== "done";
    toggle.textContent = collapsed ? "明细" : "收起";
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  // 用 onclick 而不是 addEventListener：同一份骨架被绑定两次时，按钮也只翻一次。
  if (toggle) toggle.onclick = () => setCollapsed(root.dataset.collapsed !== "true");

  function tick(now = Date.now()) {
    elapsed.textContent = startedAt ? `已用时 ${formatElapsed((finishedAt || now) - startedAt)}` : "";
    grid.tick(now);
  }

  /**
   * @param {{stage:string, state?:"active"|"done"|"error", progress?:object|null,
   *          message?:string, startedAt?:number, receivedAt?:number}} view
   */
  function show(view) {
    const stage = STAGE_KEYS.includes(view.stage) ? view.stage : "submit";
    const state = view.state || "active";
    const current = STEP_KEYS.indexOf(STEP_OF[stage]);
    steps.forEach((item, index) => {
      let value = "todo";
      if (state === "done" || index < current) value = "done";
      else if (index === current) value = state === "error" ? "error" : "active";
      item.dataset.state = value;
      if (value === "active") item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    root.dataset.state = state;
    // 刚走进「完成」那一刻自动折起；之后同为完成的重绘不动它，用户手动展开的就保持展开。
    if (state !== lastState) setCollapsed(state === "done");
    lastState = state;

    const progress = view.progress || null;
    const known = Boolean(progress && progress.total > 0);
    // 扫描段且知道总数：确定进度；扫描段但还不知道：不定进度（不编数字）；完成：拉满；其余：空。
    if (state === "done") {
      meter.max = 1;
      meter.value = 1;
    } else if (known && (stage === "scan" || stage === "verify")) {
      meter.max = progress.total;
      meter.value = progress.completed;
    } else if (state === "active" && (stage === "scan" || stage === "prepare")) {
      meter.removeAttribute("value");
    } else {
      meter.max = 1;
      meter.value = 0;
    }
    counts.textContent = known ? countText(progress) : "";
    counts.hidden = !known;
    message.textContent = view.message || "";
    grid.update(progress?.nodes || null, Number.isFinite(view.receivedAt) && view.receivedAt > 0 ? view.receivedAt : Date.now());

    startedAt = Number.isFinite(view.startedAt) && view.startedAt > 0 ? view.startedAt : 0;
    finishedAt = state === "active" ? 0 : (finishedAt || Date.now());
    root.hidden = false;
    tick();
  }

  function hide() {
    root.hidden = true;
    lastState = "";
    startedAt = 0;
    finishedAt = 0;
    grid.clear();
  }

  return { show, hide, tick, get visible() { return !root.hidden; } };
}
