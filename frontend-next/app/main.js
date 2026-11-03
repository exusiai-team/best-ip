/* ============================================================================
   页面主控：数据来源 → 筛选 → 排序 → 渲染 → 导出
   ----------------------------------------------------------------------------
   数据有三个来源，行对象只有一个形状（app/records.js）：
   - 在线部署（site-config.js 的 mode === "gateway"）：本站 Worker 上最近一次扫描的产物；
   - `?job=<任务 ID>`：本机 loopback API 上一次真实扫描的导出；
   - 默认：app/data.js 的合成示例，零网络请求。
   筛选与排序都在内存里对整份数据跑：几十到几百行，不值得为它上虚拟列表。
   ========================================================================== */

import { NODES, SNAPSHOT_META } from "./data.js";
import { IPQS_TYPE_LABELS, STATUS_LABELS } from "./labels.js";
import { resolveTarget, fetchExport } from "./api.js";
import { fetchLatestScan } from "./gateway.js";
import { toRows, toSnapshotMeta } from "./records.js";
import {
  SORTABLE_COLUMNS, applyScoreStyles, columnTitle, createColGroup, createHeadRows, createRow,
  defaultDirection, sortValue, staticGridHtml, syncSortIndicators,
} from "./render.js";
import { exportSnapshot } from "./snapshot.js";
import { initScanUi } from "./scan-ui.js";

/* -------------------------------------------------------------- 元素 --- */
// 页面是源码直上线的：必需节点缺一个就在启动前统一说明，而不是在某个 addEventListener 上白屏。
const REQUIRED_IDS = [
  "query", "statusFilter", "filterClean", "filterNative", "sortSelect", "grid", "gridColumns",
  "gridHead", "rows", "emptyState", "resultCount", "clearFilters", "runStats",
];
const $ = Object.fromEntries(REQUIRED_IDS.map((id) => [id, document.getElementById(id)]));
const optional = {
  toast: document.getElementById("toast"),
  exportMhtml: document.getElementById("exportMhtml"),
  exportHtml: document.getElementById("exportHtml"),
};
const missingIds = REQUIRED_IDS.filter((id) => !$[id]);

const DEFAULT_SORT = { sortKey: "ipqs", sortDirection: "desc" };

/* -------------------------------------------------------------- 状态 --- */
const state = {
  query: "",
  status: "all",
  clean: false,
  native: false,
  ...DEFAULT_SORT,
};

const dataset = {
  nodes: NODES,
  // 只有示例路径的 meta 是 SNAPSHOT_META；真实数据由 toSnapshotMeta() 现造，未到位时为 null。
  meta: SNAPSHOT_META,
  index: buildSearchIndex(NODES),
  version: 0,
  emptyCopy: null,
};

function setNodes(nodes) {
  dataset.nodes = nodes;
  dataset.index = buildSearchIndex(nodes);
  dataset.version += 1;
}

function buildSearchIndex(nodes) {
  return new Map(nodes.map((row) => [row, [
    row.node,
    row.type,
    row.exit_ip,
    row.country,
    row.city,
    row.isp,
    row.asn ? `AS${row.asn}` : "",
    IPQS_TYPE_LABELS[row.ipqs_type]?.label,
    row.ipqs_native === true ? "原生" : row.ipqs_native === false ? "广播" : "",
    row.error,
    STATUS_LABELS[row.status],
  ].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN")]));
}

/* ------------------------------------------------------- 筛选 + 排序 --- */
function filtersActive() {
  return Boolean(state.query.trim()) || state.status !== "all" || state.clean || state.native;
}

function matches(row, needle) {
  if (state.status !== "all" && row.status !== state.status) return false;
  if (state.clean && row.ipqs_type !== "residential" && row.ipqs_type !== "mobile") return false;
  if (state.native && row.ipqs_native !== true) return false;
  return !needle || dataset.index.get(row).includes(needle);
}

const collator = new Intl.Collator("zh-CN", { numeric: true });

function compare(a, b) {
  const sign = state.sortDirection === "asc" ? 1 : -1;
  const left = sortValue(a, state.sortKey);
  const right = sortValue(b, state.sortKey);
  // 没有值的行永远沉底，而不是在升序时冒充「最低分」。
  if (left === null || right === null) {
    if (left !== right) return left === null ? 1 : -1;
  } else if (left !== right) {
    return sign * (typeof left === "string" ? collator.compare(left, right) : left - right);
  }
  return collator.compare(a.node || "", b.node || "");
}

function visibleRows() {
  const needle = state.query.trim().toLocaleLowerCase("zh-CN");
  return dataset.nodes.filter((row) => matches(row, needle)).sort(compare);
}

/* -------------------------------------------------------------- 渲染 --- */
let countsCache = { version: -1, counts: null };

function statusCounts() {
  if (countsCache.version !== dataset.version) {
    const counts = { all: dataset.nodes.length, success: 0, partial: 0, failed: 0 };
    for (const row of dataset.nodes) if (row.status in counts) counts[row.status] += 1;
    countsCache = { version: dataset.version, counts };
  }
  return countsCache.counts;
}

function metaLabel() {
  if (!dataset.meta) return null;
  const prefix = dataset.meta === SNAPSHOT_META ? "示例生成于" : "快照生成于";
  return `${prefix} ${dataset.meta.generatedAt}`;
}

function renderStats() {
  const counts = statusCounts();
  const parts = [
    [`${counts.all}`, " 个节点"],
    [`${counts.success}`, " 完整"],
    [`${counts.partial}`, " 部分"],
    [`${counts.failed}`, " 失败"],
  ].map(([value, suffix]) => {
    const b = document.createElement("b");
    b.textContent = value;
    const span = document.createElement("span");
    span.append(b, suffix);
    return span;
  });
  const meta = metaLabel();
  if (meta) parts.push(Object.assign(document.createElement("span"), { className: "muted", textContent: meta }));
  $.runStats.replaceChildren(...parts);
  $.statusFilter.querySelectorAll("[data-count]").forEach((badge) => {
    badge.textContent = String(counts[badge.dataset.count] ?? 0);
  });
}

// 渲染指纹：数据版本 + 全部视图条件。没变就跳过整表重建，滚动位置、焦点与复制反馈都不丢。
let lastRenderKey = null;
let lastVisibleRows = [];

function renderKey() {
  return [dataset.version, state.query, state.status, state.clean, state.native, state.sortKey, state.sortDirection].join("|");
}

function render() {
  const key = renderKey();
  if (key !== lastRenderKey) {
    lastRenderKey = key;
    lastVisibleRows = visibleRows();
    // 前三名只在「按分数降序」时才名副其实。
    const ranked = state.sortDirection === "desc" && SORTABLE_COLUMNS.find((c) => c.key === state.sortKey)?.sort === "score";
    const fragment = document.createDocumentFragment();
    lastVisibleRows.forEach((row, index) => {
      const top = ranked && index < 3 && sortValue(row, state.sortKey) !== null;
      fragment.append(createRow(row, { position: index + 1, top }, document));
    });
    $.rows.replaceChildren(fragment);
    // 顺序契约：色带必须在元素进 DOM 之后由 CSSOM 写入。
    applyScoreStyles($.rows);
    globalThis.BestIpCopy?.attach($.rows);
    syncSortIndicators($.gridHead, state);
    $.sortSelect.value = `${state.sortKey}:${state.sortDirection}`;
  }

  const total = dataset.nodes.length;
  const shown = lastVisibleRows.length;
  $.emptyState.hidden = shown > 0;
  applyEmptyCopy(total > 0 ? null : dataset.emptyCopy);
  $.resultCount.textContent = shown === total ? `共 ${total} 个节点` : `显示 ${shown} / ${total} 个节点`;
  $.clearFilters.hidden = !filtersActive();
  syncExportAvailability();
}

let frame = null;
function scheduleRender() {
  if (frame === null) frame = requestAnimationFrame(() => { frame = null; render(); });
}

function flushRender() {
  if (frame === null) return;
  cancelAnimationFrame(frame);
  frame = null;
  render();
}

/* ------------------------------------------------------------ 空状态 --- */
const EMPTY_NODES = {
  title: $.emptyState?.querySelector(".empty-title"),
  desc: $.emptyState?.querySelector(".empty-desc"),
};
const EMPTY_DEFAULT = {
  title: EMPTY_NODES.title?.textContent.trim() || "没有匹配的节点",
  desc: EMPTY_NODES.desc?.textContent.trim() || "",
};

/** 有数据却筛空时用默认文案；没有数据时显示加载中 / 加载失败的那一句。 */
function applyEmptyCopy(copy) {
  const { title, desc } = copy || EMPTY_DEFAULT;
  if (EMPTY_NODES.title) EMPTY_NODES.title.textContent = title;
  if (EMPTY_NODES.desc) EMPTY_NODES.desc.textContent = desc;
}

/* -------------------------------------------------------------- 提示 --- */
let toastTimer = null;
function toast(message) {
  if (!optional.toast) return;
  optional.toast.textContent = message;
  optional.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { optional.toast.hidden = true; }, 5200);
}

globalThis.addEventListener("pagehide", () => {
  if (frame !== null) cancelAnimationFrame(frame);
  frame = null;
  clearTimeout(toastTimer);
});

/* ---------------------------------------------------------- 真实数据 --- */
const REAL_ORIGIN = {
  gateway: "来自本站 Worker 上最近一次有结果的扫描产物，只存在内存里",
  local: "来自本机 loopback API 上的一次真实扫描导出，只存在内存里",
};

function markRealSource(origin) {
  const badge = document.querySelector(".tag-demo");
  if (!badge) return;
  badge.textContent = "真实扫描";
  badge.classList.add("tag-live");
  badge.title = `${dataset.meta?.source || "真实扫描"}：${REAL_ORIGIN[origin]}`;
}

/** 真实数据到位前先渲染「空表 + 状态文案」：绝不先亮一屏合成数据再被顶掉。 */
function showPending(origin, title, desc) {
  setNodes([]);
  dataset.meta = null;
  dataset.emptyCopy = { title, desc };
  markRealSource(origin);
  renderStats();
  render();
}

function showPayload(payload, origin, emptyTitle, emptyDesc) {
  setNodes(toRows(payload));
  dataset.meta = toSnapshotMeta(payload);
  dataset.emptyCopy = { title: emptyTitle, desc: emptyDesc };
  markRealSource(origin);
  renderStats();
  render();
}

function failGateway(failure) {
  const title = typeof failure === "object" && failure?.title ? failure.title : "没有读到真实扫描结果";
  const message = (typeof failure === "string" ? failure : failure?.message) || "网关没有说明原因";
  showPending("gateway", title, message);
  toast(`未显示真实扫描结果：${message}`);
}

function failLocal(message) {
  showPending("local", "真实扫描结果没有加载成功", `${message}。想换成别的 API 地址可以给页面加上 ?api=<本机 origin>。`);
  toast(`真实扫描加载失败：${message}`);
}

async function loadLocal(target) {
  showPending("local", "正在读取真实扫描结果…", `任务 ${target.jobId} · ${target.apiBase}`);
  try {
    const payload = await fetchExport(target.apiBase, target.jobId);
    showPayload(payload, "local", "这次扫描没有任何节点记录", "任务本身是完成的，但导出里的 results 是空的。");
  } catch (error) {
    failLocal(error?.message || String(error));
  }
}

async function loadGateway() {
  showPending("gateway", "正在读取最近一次真实扫描…", "结果来自本站 Worker 上的扫描网关；页面只读已经发布的结果。");
  try {
    const payload = await fetchLatestScan();
    showPayload(payload, "gateway", "最近一次扫描没有任何节点记录", "那次运行是完成的，但产物里的 results 是空的。");
  } catch (error) {
    failGateway(error);
  }
}

/* -------------------------------------------------------------- 导出 --- */
const EXPORT_TITLES = new Map(
  [optional.exportMhtml, optional.exportHtml].filter(Boolean).map((button) => [button, button.title]),
);

function syncExportAvailability() {
  const empty = dataset.nodes.length === 0;
  for (const [button, title] of EXPORT_TITLES) {
    button.disabled = empty;
    button.title = empty ? "还没有可导出的扫描结果" : title;
  }
}

function viewLabel() {
  const status = $.statusFilter.querySelector("input:checked")?.closest("label")?.querySelector("span")?.firstChild?.textContent || "全部";
  return [status, state.clean && "住宅 / 移动", state.native && "原生"].filter(Boolean).join(" + ");
}

async function runExport(format, button) {
  if (dataset.nodes.length === 0) return;
  // 搜索输入当下就写进 state，表格要等下一帧：先落帧，正文、行数、摘要才取自同一时刻。
  flushRender();
  const counts = statusCounts();
  button.disabled = true;
  try {
    const artifact = await exportSnapshot(format, {
      rowsHtml: staticGridHtml($.grid),
      total: dataset.nodes.length,
      shown: lastVisibleRows.length,
      state: {
        query: state.query.trim(),
        statusLabel: viewLabel(),
        sortLabel: $.sortSelect.selectedOptions[0]?.textContent.trim() || "",
        source: dataset.meta?.source || "",
        generatedAt: dataset.meta?.generatedAt || "时间未知",
        exportedAt: new Date().toLocaleString("zh-CN", { hour12: false }),
        success: counts.success,
        partial: counts.partial,
        failed: counts.failed,
      },
    });
    const warning = format === "html" && artifact.scriptWarning ? "；但快照内未内联复制脚本，复制按钮不可用" : "";
    const size = artifact.bytes < 1024 ? `${artifact.bytes} B` : `${(artifact.bytes / 1024).toFixed(1)} KB`;
    toast(`已导出 ${artifact.filename}（${size}）${warning}`);
  } catch (error) {
    toast(`导出失败：${error?.message || String(error)}`);
  } finally {
    syncExportAvailability();
  }
}

/* -------------------------------------------------------------- 接线 --- */
function buildSortOptions() {
  const options = SORTABLE_COLUMNS.flatMap((column) => {
    const title = columnTitle(column.key);
    const [first, second] = column.sort === "text" ? [["asc", "A→Z"], ["desc", "Z→A"]] : [["desc", "高→低"], ["asc", "低→高"]];
    return [first, second].map(([direction, hint]) => new Option(`${title} ${hint}`, `${column.key}:${direction}`));
  });
  $.sortSelect.replaceChildren(...options);
}

function setSort(sortKey, sortDirection) {
  state.sortKey = sortKey;
  state.sortDirection = sortDirection;
  scheduleRender();
}

function clearFilters() {
  $.query.value = "";
  $.statusFilter.querySelector('input[value="all"]').checked = true;
  $.filterClean.checked = false;
  $.filterNative.checked = false;
  readControls();
  scheduleRender();
}

/** 刷新 / 后退时浏览器会恢复表单值，所以视图条件一律从控件读，而不是假定默认值。 */
function readControls() {
  state.query = $.query.value;
  state.status = $.statusFilter.querySelector("input:checked")?.value || "all";
  state.clean = $.filterClean.checked;
  state.native = $.filterNative.checked;
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/u.test(target.tagName));
}

function wire() {
  for (const control of [$.query, $.statusFilter, $.filterClean, $.filterNative]) {
    control.addEventListener(control === $.query ? "input" : "change", () => { readControls(); scheduleRender(); });
  }
  $.sortSelect.addEventListener("change", () => {
    const [key, direction] = $.sortSelect.value.split(":");
    setSort(key, direction);
  });
  $.gridHead.addEventListener("click", (event) => {
    const button = event.target.closest("button.sort");
    if (!button) return;
    const key = button.dataset.sort;
    const flipped = state.sortDirection === "asc" ? "desc" : "asc";
    setSort(key, key === state.sortKey ? flipped : defaultDirection(key));
  });
  $.clearFilters.addEventListener("click", clearFilters);

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !isTyping(event.target)) {
      event.preventDefault();
      $.query.focus();
      $.query.select();
    } else if (event.key === "Escape" && event.target === $.query) {
      if ($.query.value) {
        $.query.value = "";
        readControls();
        scheduleRender();
      } else {
        $.query.blur();
      }
    }
  });

  optional.exportMhtml?.addEventListener("click", () => runExport("mhtml", optional.exportMhtml));
  optional.exportHtml?.addEventListener("click", () => runExport("html", optional.exportHtml));
}

function boot() {
  $.gridColumns.replaceChildren(createColGroup(document));
  $.gridHead.replaceChildren(...createHeadRows(document));
  buildSortOptions();
  readControls();

  const target = resolveTarget({ search: globalThis.location?.search || "", config: globalThis.BEST_IP_CONFIG });
  if (target.mode === "gateway") {
    initScanUi({
      showPending: (title, desc) => showPending("gateway", title, desc),
      showPayload: (payload) => showPayload(payload, "gateway", "这次扫描没有任何节点记录", "那次运行是完成的，但产物里的 results 是空的。"),
      nodeCount: () => dataset.nodes.length,
      toast,
    });
  }

  // 顺序不能反：`?job=`（写了但为空）同时满足「没有 jobId」和「有 error」，先判 jobId 会把坏深链当成示例。
  if (target.error) {
    if (target.mode === "gateway") failGateway(target.error);
    else failLocal(target.error);
  } else if (target.mode === "gateway") {
    loadGateway();
  } else if (target.jobId) {
    loadLocal(target);
  } else {
    renderStats();
    render();
  }
}

if (missingIds.length) {
  const message = `页面缺少必需的 DOM 元素：${missingIds.map((id) => `#${id}`).join("、")}，界面无法渲染。`;
  console.error(message);
  (document.getElementById("resultCount") || document.body).textContent = message;
} else {
  wire();
  boot();
}
