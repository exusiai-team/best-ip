/* ============================================================================
   表格状态 —— 逐列筛选的语法、多列排序、以及把这两样写进地址栏
   ----------------------------------------------------------------------------
   页面上的表格要像电子表格一样用：点列名排序（再点反向、第三下取消，Shift 叠加次级排序），
   每一列下面直接输入就筛。这里只放纯函数，不碰 DOM——行对象进、行对象出。

   筛选语法（每一列都一样，写在输入框的 title 里）：
   - 文本列：包含即命中，不区分大小写；空格分隔的多个词须**同时**命中；`a|b` 任一命中；
             `!词` 表示**不含**这个词。
   - 分数列：`80` 即 ≥80；`>80`、`>=80`、`<60`、`<=60`、`=80`、`60-90`（闭区间）；
             `-1` / `=-1` 只看「受限 / 无数据」。写不成分数的输入不生效（不清空整表）。

   地址栏：#sort=ipure:desc,coffee:desc&status=success&q=…&f.geo=日本
   用 hash 而不是 query：?job= / ?api= 已经属于数据来源（app/api.js），两者互不干扰；
   而且 hash 变化不触发导航，replaceState 也不会在历史里堆一长串记录。
   ========================================================================== */

const LOCALE = "zh-CN";
const collator = new Intl.Collator(LOCALE, { numeric: true, sensitivity: "base" });

export const DEFAULT_SORTS = Object.freeze([{ key: "coffee", dir: "desc" }]);
export const STATUSES = ["all", "success", "partial", "failed"];

/* ------------------------------------------------------------ 文本筛选 --- */
function normalize(value) {
  return String(value ?? "").toLocaleLowerCase(LOCALE);
}

/** 解析成「每一组至少命中一个」的合取式；返回 null 表示这条输入不构成筛选。 */
export function textMatcher(input) {
  const terms = String(input || "").trim().split(/\s+/u).filter(Boolean);
  if (!terms.length) return null;
  const clauses = terms.map((term) => {
    const negate = term.startsWith("!") && term.length > 1;
    const options = (negate ? term.slice(1) : term).split("|").map(normalize).filter(Boolean);
    return { negate, options };
  }).filter((clause) => clause.options.length);
  if (!clauses.length) return null;
  return (haystack) => {
    const text = normalize(haystack);
    return clauses.every(({ negate, options }) => options.some((option) => text.includes(option)) !== negate);
  };
}

/* ------------------------------------------------------------ 分数筛选 --- */
const NUMBER = String.raw`-?\d+(?:\.\d+)?`;
const COMPARE = new RegExp(String.raw`^(>=|<=|>|<|=|≥|≤)?\s*(${NUMBER})$`, "u");
const RANGE = new RegExp(String.raw`^(${NUMBER})\s*(?:-|~|～|到)\s*(${NUMBER})$`, "u");

export function numericMatcher(input) {
  const text = String(input || "").trim().replace(/\s+/gu, "");
  if (!text) return null;
  const range = text.match(RANGE);
  if (range) {
    const low = Math.min(Number(range[1]), Number(range[2]));
    const high = Math.max(Number(range[1]), Number(range[2]));
    return (value) => Number.isFinite(value) && value !== -1 && value >= low && value <= high;
  }
  const compare = text.match(COMPARE);
  if (!compare) return null;
  const target = Number(compare[2]);
  // -1 不是分数，是「受限 / 无数据」：只做相等判断，也不让 `>-1` 之类把它算进分数区间。
  if (target === -1) return (value) => value === -1;
  const op = compare[1] || ">=";
  return (value) => {
    if (!Number.isFinite(value) || value === -1) return false;
    switch (op) {
      case ">": return value > target;
      case "<": return value < target;
      case "<=": case "≤": return value <= target;
      case "=": return value === target;
      default: return value >= target;
    }
  };
}

/** 把一组列筛选编译成一个谓词；无效输入（如分数列写了字母）直接跳过。 */
export function compileFilters(filters, columns) {
  const tests = [];
  for (const [key, raw] of Object.entries(filters)) {
    const column = columns.get(key);
    if (!column?.filter || !String(raw ?? "").trim()) continue;
    if (column.filter === "enum") {
      tests.push((row) => column.filterValue(row) === raw);
    } else if (column.filter === "num") {
      const match = numericMatcher(raw);
      if (match) tests.push((row) => match(column.filterValue(row)));
    } else {
      const match = textMatcher(raw);
      if (match) tests.push((row) => match(column.filterValue(row)));
    }
  }
  return (row) => tests.every((test) => test(row));
}

/** 这条输入有没有真正生效：分数列里写了解析不了的东西，界面要标出来而不是假装在筛。 */
export function filterIsValid(column, raw) {
  if (!String(raw ?? "").trim()) return true;
  if (column?.filter === "num") return numericMatcher(raw) !== null;
  return true;
}

/* ------------------------------------------------------------------ 排序 --- */
/** IP 排序键：IPv4 按四段数值、IPv6 排在 IPv4 之后按展开后的十六进制。 */
function ipKey(value) {
  if (!value) return null;
  if (!value.includes(":")) {
    return `4.${value.split(".").map((part) => part.padStart(3, "0")).join(".")}`;
  }
  const [head, tail = ""] = value.split("::");
  const left = head ? head.split(":") : [];
  const right = tail ? tail.split(":") : [];
  const groups = value.includes("::") ? [...left, ...Array(8 - left.length - right.length).fill("0"), ...right] : left;
  return `6.${groups.map((group) => group.padStart(4, "0")).join(":")}`;
}

function compareValues(left, right, type) {
  if (type === "num" || type === "enum") return left - right;
  if (type === "ip") return left < right ? -1 : left > right ? 1 : 0;
  return collator.compare(left, right);
}

/**
 * 多列排序：按 sorts 的先后逐列比较；某列两边都没值（或相等）才看下一列。
 * 没值永远沉底，不论升降序——升序时把「没分」排在最前面等于说它分数最低，那是谎话。
 * 全部相等时按节点名、再按原始顺序（orderOf 给出订阅里的位次），保证同一份数据每次排出来都一样。
 */
export function comparator(sorts, columns, orderOf) {
  const plan = sorts
    .map(({ key, dir }) => ({ column: columns.get(key), sign: dir === "asc" ? 1 : -1 }))
    .filter(({ column }) => column?.sort);
  const keyCache = new Map();
  const valueOf = (row, column) => {
    let cached = keyCache.get(row);
    if (!cached) { cached = new Map(); keyCache.set(row, cached); }
    if (!cached.has(column.key)) {
      const raw = column.sortValue(row);
      cached.set(column.key, raw === null || raw === undefined || raw === "" ? null : column.sort === "ip" ? ipKey(raw) : raw);
    }
    return cached.get(column.key);
  };
  return (a, b) => {
    for (const { column, sign } of plan) {
      const left = valueOf(a, column);
      const right = valueOf(b, column);
      if (left === null && right === null) continue;
      if (left === null) return 1;
      if (right === null) return -1;
      const order = compareValues(left, right, column.sort);
      if (order !== 0) return sign * order;
    }
    return collator.compare(String(a.node ?? ""), String(b.node ?? "")) || orderOf(a) - orderOf(b);
  };
}

/**
 * 点表头的行为：
 * - 普通点击：只按这一列排；同一列再点依次「默认方向 → 反向 → 取消」；
 * - Shift+点击：在现有排序后面追加这一列（已在其中则同样按三态循环），用来做次级排序。
 * 分数列第一下是降序（高分在前），文本列第一下是升序（A→Z）。
 */
export function nextSorts(sorts, column, additive) {
  const first = column.sort === "num" ? "desc" : "asc";
  const current = sorts.find((sort) => sort.key === column.key);
  const cycled = !current ? first : current.dir === first ? (first === "asc" ? "desc" : "asc") : null;
  if (additive) {
    const rest = sorts.filter((sort) => sort.key !== column.key);
    if (!cycled) return rest;
    return current
      ? sorts.map((sort) => (sort.key === column.key ? { key: column.key, dir: cycled } : sort))
      : [...rest, { key: column.key, dir: cycled }];
  }
  // 普通点击一个「次级」排序列：直接让它成为唯一的排序，从默认方向开始。
  if (current && sorts.length > 1) return [{ key: column.key, dir: current.dir }];
  return cycled ? [{ key: column.key, dir: cycled }] : [];
}

/* ------------------------------------------------------------- 地址栏 --- */
export function encodeHash(state) {
  const params = new URLSearchParams();
  const sameAsDefault = state.sorts.length === DEFAULT_SORTS.length
    && state.sorts.every((sort, index) => sort.key === DEFAULT_SORTS[index].key && sort.dir === DEFAULT_SORTS[index].dir);
  if (!sameAsDefault) params.set("sort", state.sorts.map((sort) => `${sort.key}:${sort.dir}`).join(",") || "none");
  if (state.status !== "all") params.set("status", state.status);
  if (state.query.trim()) params.set("q", state.query.trim());
  for (const [key, value] of Object.entries(state.filters)) {
    if (String(value ?? "").trim()) params.set(`f.${key}`, String(value).trim());
  }
  const text = params.toString();
  return text ? `#${text}` : "";
}

/** 地址栏里的东西是用户可编辑的输入：列名不认识、方向写错的一律丢掉，不让它进 state。 */
export function decodeHash(hash, columns) {
  const params = new URLSearchParams(String(hash || "").replace(/^#/u, ""));
  const result = { sorts: null, status: null, query: null, filters: {} };
  const sort = params.get("sort");
  if (sort === "none") result.sorts = [];
  else if (sort) {
    const sorts = sort.split(",").map((part) => {
      const [key, dir] = part.split(":");
      return columns.get(key)?.sort && (dir === "asc" || dir === "desc") ? { key, dir } : null;
    }).filter(Boolean);
    if (sorts.length) result.sorts = sorts;
  }
  const status = params.get("status");
  if (STATUSES.includes(status)) result.status = status;
  if (params.has("q")) result.query = params.get("q");
  for (const [name, value] of params) {
    if (!name.startsWith("f.")) continue;
    const column = columns.get(name.slice(2));
    if (!column?.filter) continue;
    if (column.filter === "enum" && !column.options.includes(value)) continue;
    result.filters[column.key] = value;
  }
  return result;
}
