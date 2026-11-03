/* ============================================================================
   表格渲染：列定义、两级表头、行、分数着色
   ----------------------------------------------------------------------------
   COLUMNS 是列的唯一真值：colgroup 宽度、两级表头、可排序列、卡片模式下每格的
   data-label 都由它生成。列按数据来源分组（网络出口 / 1ms IPQS / Coffee / IPure），
   每个数字都能在表头上找到出处。

   CSP 约束：生产是 style-src 'self'，标记里的 style 属性会被静默丢弃。分数色带只能
   等元素进 DOM 之后用 CSSOM 写入（applyScoreStyles）。
   ========================================================================== */

import { IPQS_DEDUCTION_LABELS, IPQS_TYPE_LABELS, SCENARIO_LABELS, STATUS_LABELS } from "./labels.js";
import { comparableScore, ipureScoreInlineStyle } from "./score-color.js";

const GROUPS = {
  net: "网络出口",
  ipqs: "1ms IPQS",
  ipure: "IPure",
};

// width 单位 px；不写 width 的「节点」列吃掉剩余宽度。sort: "text" 首次点击升序，"score" 首次降序。
// title 是脱离表头分组时的完整列名（卡片模式、排序下拉、按钮提示）：单独一个「评分」说不清是谁的分。
export const COLUMNS = [
  { key: "rank", label: "#", width: 44 },
  { key: "node", label: "节点", sort: "text" },
  { key: "ip", label: "出口 IP", group: "net", width: 162 },
  { key: "geo", label: "国家和地区", group: "net", width: 100, sort: "text" },
  { key: "isp", label: "服务商 / ASN", group: "net", width: 160 },
  { key: "ipqsType", label: "类型", title: "1ms 类型", group: "ipqs", width: 62 },
  { key: "ipqsNative", label: "原生", title: "1ms 原生性", group: "ipqs", width: 62 },
  { key: "ipqs", label: "评分", title: "1ms 评分", short: "1ms", group: "ipqs", width: 74, align: "center", sort: "score" },
  { key: "coffee", label: "Coffee", short: "Coffee", width: 72, align: "center", sort: "score" },
  { key: "ipure", label: "总分", title: "IPure 总分", short: "IPure", group: "ipure", width: 64, align: "center", sort: "score" },
  ...SCENARIO_LABELS.map(([id, label]) => ({
    key: `scn:${id}`, label, title: `IPure ${label}`, short: label, group: "ipure", width: 62, align: "center", sort: "score", scenario: id,
  })),
];

const COLUMN_BY_KEY = new Map(COLUMNS.map((column) => [column.key, column]));
// 来源分组的第一列：表头与单元格都在这里画一条竖分隔线，把「谁给的分」在视觉上分开。
const GROUP_STARTS = new Set(COLUMNS.filter((column, index) => index > 1 && column.group !== COLUMNS[index - 1].group).map((column) => column.key));
export const SORTABLE_COLUMNS = COLUMNS.filter((column) => column.sort);
// 失败行从「出口 IP」到最后一列合成一格写失败原因：那些列对它全都没有值。
const ABSENT_SPAN = COLUMNS.length - 2;

export function columnTitle(key) {
  const column = COLUMN_BY_KEY.get(key);
  return column ? column.title ?? column.label : key;
}

/* ----------------------------------------------------------------- 排序 --- */
export function defaultDirection(key) {
  return COLUMN_BY_KEY.get(key)?.sort === "text" ? "asc" : "desc";
}

/** 排序取值：文本列返回字符串，分数列返回数字；null 表示「没有可比的值」，永远沉底。 */
export function sortValue(row, key) {
  switch (key) {
    case "node": return row.node || null;
    case "geo": return row.country || null;
    case "ipqs": return row.ipqs_score;
    case "coffee": return row.coffee_score;
    case "ipure": return comparableScore(row.score);
    default: {
      const scenario = COLUMN_BY_KEY.get(key)?.scenario;
      return scenario ? comparableScore(row.ipure_scores?.[scenario]) : null;
    }
  }
}

/* ----------------------------------------------------------------- 工具 --- */
function el(document, tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}

function chip(document, label, tone, title) {
  const node = el(document, "span", `chip chip-${tone}`, label);
  if (title) node.title = title;
  return node;
}

function cell(document, key, className) {
  const td = el(document, "td", className);
  const column = COLUMN_BY_KEY.get(key);
  td.dataset.label = columnTitle(key);
  if (column?.short) td.dataset.short = column.short;
  if (column?.align) td.dataset.align = column.align;
  if (GROUP_STARTS.has(key)) td.classList.add("gs");
  return td;
}

function formatAsn(asn) {
  return Number.isInteger(asn) && asn > 0 ? `AS${asn}` : "";
}

/** 有报告地址时分数本身就是出处链接；快照导出时 staticGridHtml 会把它降成 span。 */
function scoreElement(document, href) {
  if (!href) return el(document, "span", "score");
  const link = el(document, "a", "score");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function svgIcon(document, { size, paths, strokeWidth = 2.2 }) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [name, value] of Object.entries({
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": strokeWidth, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  })) svg.setAttribute(name, String(value));
  for (const d of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

const ICONS = {
  copy: ["M9 9h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2z", "M5 15V5a2 2 0 0 1 2-2h8"],
  warn: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 8v5", "M12 16.5h.01"],
  sort: ["m8 9 4-4 4 4", "m16 15-4 4-4-4"],
};

/* ----------------------------------------------------------------- 表头 --- */
export function createColGroup(document) {
  const fragment = document.createDocumentFragment();
  for (const column of COLUMNS) {
    const col = el(document, "col", `col-${column.key.replace(":", "-")}`);
    if (column.width) col.style.width = `${column.width}px`;
    fragment.append(col);
  }
  return fragment;
}

function headCell(document, column, { rowSpan = 1 } = {}) {
  const th = el(document, "th", `col-${column.key.replace(":", "-")}`);
  th.scope = "col";
  if (GROUP_STARTS.has(column.key)) th.classList.add("gs");
  if (rowSpan > 1) th.rowSpan = rowSpan;
  if (column.align) th.dataset.align = column.align;
  if (!column.sort) {
    th.textContent = column.label;
    return th;
  }
  th.dataset.sortKey = column.key;
  const button = el(document, "button", "sort");
  button.type = "button";
  button.dataset.sort = column.key;
  button.title = `按${columnTitle(column.key)}排序`;
  button.append(el(document, "span", "sort-label", column.label), svgIcon(document, { size: 11, paths: ICONS.sort }));
  th.append(button);
  return th;
}

/** 两级表头：无分组的列跨两行，分组列在第一行合并出来源名、第二行写各自的列名。 */
export function createHeadRows(document) {
  const top = el(document, "tr", "head-groups");
  const bottom = el(document, "tr", "head-columns");
  for (let index = 0; index < COLUMNS.length;) {
    const column = COLUMNS[index];
    if (!column.group) {
      top.append(headCell(document, column, { rowSpan: 2 }));
      index += 1;
      continue;
    }
    let span = 0;
    while (COLUMNS[index + span]?.group === column.group) {
      bottom.append(headCell(document, COLUMNS[index + span]));
      span += 1;
    }
    const group = el(document, "th", `th-group gs th-group-${column.group}`, GROUPS[column.group]);
    group.scope = "colgroup";
    group.colSpan = span;
    top.append(group);
    index += span;
  }
  return [top, bottom];
}

/** 把当前排序同步到表头：aria-sort 给读屏，data-direction 给箭头样式。 */
export function syncSortIndicators(head, { sortKey, sortDirection }) {
  head.querySelectorAll("th[data-sort-key]").forEach((th) => {
    if (th.dataset.sortKey === sortKey) {
      th.setAttribute("aria-sort", sortDirection === "asc" ? "ascending" : "descending");
      th.dataset.direction = sortDirection;
    } else {
      th.removeAttribute("aria-sort");
      delete th.dataset.direction;
    }
  });
}

/* ----------------------------------------------------------------- 行 --- */
/** 左侧导轨：good = 1ms 判为住宅/移动且原生，mixed = 可用但有折损，bad = 失败。 */
export function verdictOf(row) {
  if (row.status === "failed") return "bad";
  const clean = row.ipqs_type === "residential" || row.ipqs_type === "mobile";
  return row.status === "success" && clean && row.ipqs_native === true ? "good" : "mixed";
}

function rankCell(document, position) {
  const td = cell(document, "rank", "rank");
  td.textContent = String(position);
  td.setAttribute("aria-label", `第 ${position} 位`);
  return td;
}

function identCell(document, row) {
  const td = cell(document, "node", "ident");
  const name = row.node || "未命名节点";
  td.append(el(document, "span", "node", name));
  const sub = el(document, "span", "ident-sub");
  if (row.type) sub.append(el(document, "span", "proto", row.type));
  if (row.status === "partial") sub.append(chip(document, STATUS_LABELS.partial, "warn", "部分数据源未返回结果"));
  if (row.status === "failed") sub.append(chip(document, STATUS_LABELS.failed, "bad"));
  td.append(sub);
  td.title = name;
  return td;
}

function ipCell(document, row) {
  const td = cell(document, "ip", "cell-ip");
  const code = el(document, "code", "ip", row.exit_ip);
  code.title = row.exit_ip;
  const copy = el(document, "button", "copy-btn");
  copy.type = "button";
  copy.dataset.copy = row.exit_ip;
  copy.title = `复制 ${row.exit_ip}`;
  copy.setAttribute("aria-label", `复制出口 IP ${row.exit_ip}`);
  copy.append(svgIcon(document, { size: 12, paths: ICONS.copy, strokeWidth: 2 }));
  td.append(code, copy);
  return td;
}

function geoCell(document, row) {
  const td = cell(document, "geo", "cell-geo");
  td.textContent = row.country || "未知";
  td.title = [row.country, row.city && row.city !== row.country ? row.city : null].filter(Boolean).join(" · ") || "归属地未知";
  return td;
}

function ispCell(document, row) {
  const td = cell(document, "isp", "cell-isp");
  const isp = el(document, "span", "isp", row.isp || "服务商未知");
  isp.title = row.isp || "服务商未知";
  td.append(isp);
  const asn = formatAsn(row.asn);
  if (asn) td.append(el(document, "span", "asn", asn));
  return td;
}

function ipqsTypeCell(document, row) {
  const td = cell(document, "ipqsType", "cell-tag");
  const type = IPQS_TYPE_LABELS[row.ipqs_type];
  td.append(type
    ? chip(document, type.label, type.tone, `1ms 多源共识：${type.label}`)
    : chip(document, "未知", "neutral", "1ms 没有给出接入类型"));
  return td;
}

function ipqsNativeCell(document, row) {
  const td = cell(document, "ipqsNative", "cell-tag");
  const registry = row.ipqs_registered_country;
  const located = row.ipqs_country;
  if (row.ipqs_native === true) {
    td.append(chip(document, "原生", "good", registry ? `注册地 ${registry} 与归属地一致` : "注册地与归属地一致"));
  } else if (row.ipqs_native === false) {
    td.append(chip(document, "广播", "warn", `注册地 ${registry || "?"}，归属地 ${located || "?"}`));
  } else {
    td.append(chip(document, "未知", "neutral", "1ms 无法比对注册地与归属地"));
  }
  return td;
}

function gradeTone(grade) {
  if (grade === "A" || grade === "B") return "good";
  if (grade === "C") return "warn";
  return grade ? "bad" : "na";
}

function ipqsScoreCell(document, row) {
  const td = cell(document, "ipqs", "cell-score");
  const pill = scoreElement(document, row.ipqs_report_url);
  if (row.ipqs_score === null) {
    pill.dataset.band = "na";
    pill.textContent = "—";
    pill.title = "1ms 未返回评分";
  } else {
    pill.dataset.band = gradeTone(row.ipqs_grade);
    pill.append(document.createTextNode(String(row.ipqs_score)));
    if (row.ipqs_grade) pill.append(el(document, "small", "grade", row.ipqs_grade));
    const deductions = row.ipqs_deductions.map((item) => `${IPQS_DEDUCTION_LABELS[item.key] || item.key} −${item.points}`);
    pill.title = [
      `1ms 综合评分 ${row.ipqs_score}${row.ipqs_grade ? ` · ${row.ipqs_grade} 级` : ""}（满分 100，按扣分计）`,
      deductions.length ? `扣分：${deductions.join("，")}` : "无扣分项",
      row.ipqs_report_url ? "点击查看 1ms 完整报告" : "",
    ].filter(Boolean).join("\n");
  }
  td.append(pill);
  return td;
}

function coffeeBand(value) {
  if (value === null) return "na";
  if (value >= 75) return "good";
  return value >= 45 ? "warn" : "bad";
}

function coffeeCell(document, row) {
  const td = cell(document, "coffee", "cell-score");
  const pill = el(document, "span", "score", row.coffee_score === null ? "—" : String(row.coffee_score));
  pill.dataset.band = coffeeBand(row.coffee_score);
  pill.title = row.coffee_score === null ? "Coffee 未返回评分" : `Coffee 评分 ${row.coffee_score}`;
  td.append(pill);
  return td;
}

/** IPure 分数通道串先挂在 data-ipure-style 上，进 DOM 后由 applyScoreStyles 写入。 */
function bandIpure(element, value) {
  const style = value === null ? "" : ipureScoreInlineStyle(value);
  element.dataset.band = style ? "band" : "na";
  if (style) element.dataset.ipureStyle = style;
}

function ipureTotalCell(document, row) {
  const td = cell(document, "ipure", "cell-score");
  const pill = scoreElement(document, row.ipure_report_url);
  bandIpure(pill, row.score);
  pill.textContent = row.score === null ? "—" : String(row.score);
  if (row.score === -1) pill.title = "IPure：该地区受限";
  else if (row.score === null) pill.title = "IPure 未返回纯净度总分";
  else pill.title = `IPure 纯净度总分 ${row.score}${row.ipure_report_url ? "\n点击查看 IPure 报告" : ""}`;
  td.append(pill);
  return td;
}

function scenarioCell(document, row, column) {
  const td = cell(document, column.key, "cell-scn");
  const value = row.ipure_scores?.[column.scenario] ?? null;
  const item = el(document, "span", "scn", value === null ? "—" : String(value));
  bandIpure(item, value);
  if (value === null) item.title = `${column.label}：无数据`;
  else if (value === -1) item.title = `${column.label}：该地区受限`;
  else item.title = `IPure ${column.label}场景 ${value}`;
  td.append(item);
  return td;
}

function absentCell(document, row) {
  const td = el(document, "td", "cell-absent gs");
  td.dataset.label = "失败原因";
  td.colSpan = ABSENT_SPAN;
  const line = el(document, "span", "failure");
  line.append(svgIcon(document, { size: 13, paths: ICONS.warn }), document.createTextNode(row.error || "连接失败"));
  if (row.error_step) line.append(el(document, "span", "failure-step", `卡在：${row.error_step}`));
  td.append(line);
  if (row.isp) {
    const asn = formatAsn(row.asn);
    td.append(el(document, "span", "failure-isp", `配置服务商 ${row.isp}${asn ? ` · ${asn}` : ""}`));
  }
  return td;
}

export function createRow(row, { position, top }, document) {
  const tr = el(document, "tr", "row");
  tr.dataset.status = row.status;
  tr.dataset.verdict = verdictOf(row);
  if (top) tr.dataset.tier = "top";
  tr.append(rankCell(document, position), identCell(document, row));
  if (!row.exit_ip) {
    tr.append(absentCell(document, row));
    return tr;
  }
  tr.append(
    ipCell(document, row),
    geoCell(document, row),
    ispCell(document, row),
    ipqsTypeCell(document, row),
    ipqsNativeCell(document, row),
    ipqsScoreCell(document, row),
    coffeeCell(document, row),
    ipureTotalCell(document, row),
    ...COLUMNS.filter((column) => column.scenario).map((column) => scenarioCell(document, row, column)),
  );
  return tr;
}

/** 必须在元素进 DOM 之后调用：CSSOM 赋值不受 style-src 'self' 限制，标记里的 style 属性会被拦。 */
export function applyScoreStyles(root) {
  root.querySelectorAll("[data-ipure-style]").forEach((element) => {
    element.style.cssText = element.dataset.ipureStyle;
  });
}

/**
 * 快照用的静态表格：排序按钮降成表头文字，出处链接降成普通药丸。
 * 离线快照没有可操作的对象，也约定不含任何外链。
 */
export function staticGridHtml(grid) {
  const clone = grid.cloneNode(true);
  clone.querySelectorAll("button.sort").forEach((button) => {
    button.replaceWith(button.querySelector(".sort-label")?.textContent || button.textContent);
  });
  clone.querySelectorAll("a.score").forEach((link) => {
    const span = link.ownerDocument.createElement("span");
    for (const { name, value } of Array.from(link.attributes)) {
      if (!["href", "target", "rel"].includes(name)) span.setAttribute(name, value);
    }
    span.append(...Array.from(link.childNodes));
    link.replaceWith(span);
  });
  return clone.outerHTML;
}
