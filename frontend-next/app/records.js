/* ============================================================================
   导出记录 → 行对象：真实数据与渲染器之间唯一的适配层
   ----------------------------------------------------------------------------
   输入是 backend 导出的 results[i]，输出是与 app/data.js 示例同形的行对象。
   三条规则：
   1. 三态布尔的 null 是「没测到」，不是「否」——ipqs_native 原样透传 true/false/null。
   2. IPure 的 -1 是「该地区受限」哨兵，null 才是「无数据」，两者不能混。
   3. 非预期类型一律归一成 null，渲染器只区分「有值 / 无值」两条路。
   ========================================================================== */

import { IPQS_TYPE_LABELS, SCENARIO_LABELS } from "./labels.js";

const STATUSES = new Set(["success", "partial", "failed"]);
const IPQS_GRADES = new Set(["A", "B", "C", "D", "F"]);

// 失败可能卡在这几步上；其余探针（ping / 端口 / 关联域名）失败不代表流程卡住。
const STEP_LABELS = [
  ["page", "Coffee 页面"],
  ["trace", "出口 IP 确认"],
  ["lookup", "归属查询"],
  ["ipure", "IPure 评分"],
  ["ipqs", "1ms 评分"],
];

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function text(value) {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function integer(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : null;
}

function bool(value) {
  return value === true || value === false ? value : null;
}

/** 0..100 的整数才是分数；allowRestricted 时 -1 作为 IPure 受限哨兵透传，其余一律 null。 */
function score(value, { allowRestricted = false } = {}) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  if (!Number.isInteger(number)) return null;
  if (allowRestricted && number === -1) return -1;
  return number >= 0 && number <= 100 ? number : null;
}

/**
 * location 是后端拼的展示串，只有两种形态能无歧义拆开：单个词（只有国家），
 * 或前后两半相同（`Hong Kong Hong Kong`）。其余形态宁可留空也不猜。
 */
function pairFromLocation(value) {
  const words = text(value)?.split(/\s+/u) ?? [];
  if (words.length === 1) return { country: words[0], city: null };
  const half = words.length / 2;
  if (words.length && Number.isInteger(half)) {
    const head = words.slice(0, half).join(" ");
    if (head === words.slice(half).join(" ")) return { country: head, city: head };
  }
  return { country: null, city: null };
}

function geoOf(record) {
  const lookup = record?.requests?.lookup?.data;
  const coffee = isObject(record?.coffee?.lookup?.data) ? record.coffee.lookup.data : record?.coffee?.lookup;
  for (const source of [lookup, coffee]) {
    if (!isObject(source)) continue;
    const pair = { country: text(source.country), city: text(source.city) };
    if (pair.country || pair.city) return pair;
  }
  return pairFromLocation(record?.location);
}

function errorStepOf(record) {
  const requests = isObject(record?.requests) ? record.requests : {};
  const stuck = STEP_LABELS.find(([key]) => isObject(requests[key]) && requests[key].ok !== true);
  return stuck ? stuck[1] : null;
}

function scenariosOf(source) {
  if (!isObject(source)) return null;
  return Object.fromEntries(SCENARIO_LABELS.map(([key]) => [key, score(source[key], { allowRestricted: true })]));
}

/** 1ms 的扣分明细与注册地只在请求证据里：记录顶层只有拍平后的五个字段。 */
function ipqsEvidenceOf(record) {
  const data = record?.requests?.ipqs?.data;
  if (!isObject(data)) return { deductions: [], country: null, registeredCountry: null };
  const deductions = Array.isArray(data.deductions)
    ? data.deductions
      .filter((item) => isObject(item) && text(item.key) && Number.isFinite(item.points) && item.points > 0)
      .map((item) => ({ key: item.key, points: Math.round(item.points) }))
    : [];
  return { deductions, country: text(data.country), registeredCountry: text(data.registered_country) };
}

function toRow(record) {
  const status = STATUSES.has(record?.status) ? record.status : "failed";
  const live = status !== "failed";
  const geo = geoOf(record);
  const asn = integer(record?.asn);
  const ipure = live ? score(record?.score, { allowRestricted: true }) : null;
  const ipqsScore = live ? score(record?.ipqs_score) : null;
  const ipqsType = live && Object.hasOwn(IPQS_TYPE_LABELS, record?.ipqs_type) ? record.ipqs_type : null;
  const grade = text(record?.ipqs_grade);
  const evidence = live ? ipqsEvidenceOf(record) : ipqsEvidenceOf(null);

  return {
    node: text(record?.node),
    type: text(record?.type),
    status,
    // 失败行即便残留出口 IP 也不显示，否则「没连上」会被伪装成可用节点。
    exit_ip: live ? text(record?.exit_ip) : null,
    country: geo.country,
    city: geo.city,
    isp: text(record?.isp),
    asn: asn !== null && asn > 0 ? asn : null,
    ipqs_score: ipqsScore,
    ipqs_grade: ipqsScore !== null && IPQS_GRADES.has(grade) ? grade : null,
    ipqs_type: ipqsType,
    ipqs_native: live ? bool(record?.ipqs_native) : null,
    ipqs_country: evidence.country,
    ipqs_registered_country: evidence.registeredCountry,
    ipqs_deductions: evidence.deductions,
    ipqs_report_url: live ? text(record?.ipqs_report_url) : null,
    coffee_score: live ? score(record?.coffee_score) : null,
    score: ipure,
    ipure_scores: live ? scenariosOf(record?.ipure_scores) : null,
    ipure_report_url: live ? text(record?.ipure_report_url) : null,
    error: text(record?.error) || text(record?.transport_error),
    error_step: errorStepOf(record),
  };
}

function byNodeIndex(results) {
  return results
    .map((record, index) => ({ record, index, key: integer(record?.node_index) }))
    .sort((left, right) => (left.key ?? Infinity) - (right.key ?? Infinity) || left.index - right.index)
    .map((entry) => entry.record);
}

/** 把一份导出 payload 映射成行对象数组；畸形载荷直接抛错，不默默造出「失败节点」。 */
export function toRows(exportPayload) {
  const results = exportPayload?.results;
  if (!Array.isArray(results)) throw new Error("导出结果缺少 results 列表");
  if (!results.every(isObject)) throw new Error("导出结果里有不是对象的记录项");
  return byNodeIndex(results).map(toRow);
}

function formatLocalTimestamp(value) {
  const date = new Date(text(value) ?? "");
  if (Number.isNaN(date.getTime())) return null;
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 与 data.js 的 SNAPSHOT_META 同形；时间取任务完成时刻而不是打开页面的时刻。 */
export function toSnapshotMeta(exportPayload) {
  const jobId = text(exportPayload?.id) || "";
  return {
    generatedAt: formatLocalTimestamp(exportPayload?.finished_at)
      || formatLocalTimestamp(exportPayload?.created_at)
      || "时间未知",
    source: jobId ? `真实扫描 · ${jobId.slice(0, 8)}` : "真实扫描",
  };
}
