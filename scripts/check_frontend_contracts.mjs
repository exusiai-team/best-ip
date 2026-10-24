/* ============================================================================
   前端契约门禁（frontend-next）
   运行：node scripts/check_frontend_contracts.mjs（由 npm run check 串起）
   ----------------------------------------------------------------------------
   这里守的四条约定原先只写在设计记录与源码注释里，靠人记住：

     a. frontend-next 源码里不得出现**标记形式**的 `style=` 属性；
     b. frontend-next/index.html 的 <thead>…</thead> 区间内零控件（表头只由 app/render.js 生成）；
     c. frontend-next/index.html 不得有内联 <script>（只允许带 src 的）；
     d. frontend-next/app/ 下除 app/net.js 外不得出现裸 `fetch(`。

   为什么 a 必须有：生产 CSP 挂在每个 text/html 响应上（worker/responses.js:36-39），
   `style-src 'self'` 让标记里的 style 属性被浏览器**静默丢弃**——不报错、不警告，只退回
   样式表兜底色，所以「线上分数看着有色」证明不了写法，只能由源码断言证明。
   本地 dev 现在也发同一串 CSP（scripts/dev.py 从 worker/responses.js 里读出唯一真源），
   于是这类写法在本地同样失效，不再有「本地看着正常、线上没颜色」的分叉。

   职责边界（刻意保持分工，别把别的检查塞进来）：
   - 语法：scripts/check_js.mjs（node --check）
   - 合同：本脚本
   - 序列化往返：frontend-next/tools/mhtml-roundtrip.mjs

   失败输出格式：文件:行号 — 违反了哪条 — 怎么修；任何一条命中即以非 0 退出。
   ========================================================================== */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const FRONTEND = "frontend-next";
const INDEX_HTML = `${FRONTEND}/index.html`;

const failures = [];

function fail({ contract, file, line, fix, excerpt = "" }) {
  failures.push({ contract, file, line, fix, excerpt });
}

function read(relativePath) {
  return readFileSync(join(ROOT, relativePath), "utf8");
}

/* ------------------------------------------------------------ 注释抹平 --- */

/**
 * 把注释内容替换成空格（不跨行、不动换行符，所以行号与列号都不变），
 * 让后面的正则在代码上匹配：注释里举例写 `style="x"` 不该让门禁变红。
 *
 * 这是**有意保守**的近似——不确定的地方宁可当注释（漏报），也不要误报：
 *   - `//` 只有出现在行首、或者前面是空白时才算行注释，所以字符串里的
 *     `https://…` 不会被当成注释起点；
 *   - `/*` 开头后找不到配对的结束标记时，整段按代码处理（例如 "app/*.js" 这种 glob 字符串）；
 *   - 不解析字符串状态与模板串里的 ${} 嵌套。最坏结果是漏掉一处匹配，
 *     不会凭空造出一处违规（宁可窄也不要宽）。
 */
function maskComments(source, { html = false } = {}) {
  const chars = Array.from(source);
  const blank = (from, to) => {
    for (let index = from; index < to && index < chars.length; index += 1) {
      if (chars[index] !== "\n") chars[index] = " ";
    }
  };
  const find = (from, needle) => {
    for (let index = from; index <= chars.length - needle.length; index += 1) {
      if (chars[index] !== needle[0]) continue;
      let matched = true;
      for (let offset = 1; offset < needle.length; offset += 1) {
        if (chars[index + offset] !== needle[offset]) { matched = false; break; }
      }
      if (matched) return index;
    }
    return -1;
  };

  if (html) {
    let index = 0;
    while (index < chars.length) {
      const start = find(index, ["<", "!", "-", "-"]);
      if (start < 0) break;
      const end = find(start + 4, ["-", "-", ">"]);
      if (end < 0) break; // 未闭合：按代码处理，不吞掉后面所有内容
      blank(start, end + 3);
      index = end + 3;
    }
    return chars.join("");
  }

  let index = 0;
  while (index < chars.length) {
    const char = chars[index];
    const previous = index === 0 ? "\n" : chars[index - 1];
    if (char === "/" && chars[index + 1] === "/" && /\s/u.test(previous)) {
      let end = index;
      while (end < chars.length && chars[end] !== "\n") end += 1;
      blank(index, end);
      index = end;
      continue;
    }
    if (char === "/" && chars[index + 1] === "*") {
      const end = find(index + 2, ["*", "/"]);
      if (end >= 0) {
        blank(index, end + 2);
        index = end + 2;
        continue;
      }
    }
    index += 1;
  }
  return chars.join("");
}

/* ------------------------------------------------------------ 定位辅助 --- */

// 索引 → 行号/原文行。masked 与 raw 逐字符对齐（注释被替换成等长空格），所以索引通用。
function lineOf(masked, index) {
  let line = 1;
  for (let cursor = 0; cursor < index; cursor += 1) if (masked[cursor] === "\n") line += 1;
  return line;
}

function lineTextAt(raw, index) {
  const start = raw.lastIndexOf("\n", index - 1) + 1;
  const end = raw.indexOf("\n", index);
  return raw.slice(start, end < 0 ? raw.length : end);
}

/** 递归列出目录下所有 .js/.cjs/.mjs（new file 自动纳入检查，不靠手工登记）。 */
function listScripts(relativeDir) {
  const found = [];
  for (const entry of readdirSync(join(ROOT, relativeDir), { withFileTypes: true })) {
    const relativePath = `${relativeDir}/${entry.name}`;
    if (entry.isDirectory()) found.push(...listScripts(relativePath));
    else if (entry.isFile() && /\.(?:c?js|mjs)$/u.test(entry.name)) found.push(relativePath);
  }
  return found.sort();
}

/* ============================== 契约 a ==================================== */

// 标记里的 style 属性。三种写法都抓：style="…" / style='…' / style=${…}（模板串里拼出来）。
//
// 边界（宁窄勿宽，收窄到零误报）：
//   - 负向断言排掉前面是标识符字符、`.`、`$`、`-` 的情况，于是：
//       .style.width = …、element.style = …、.style.cssText = …  → 不算（CSSOM 赋值，
//       本地与线上唯一合法的着色通道，见 app/render.js 的 applyScoreStyles）
//       styleSheet / getComputedStyle / styleMode / styleSource → 后面根本没有 `=`，不匹配
//       data-style="…"                                          → 前面是 `-`，不匹配
//   - 赋值号后面必须紧跟引号或 ${：所以注释/文档里「不要写 style 属性」这类提及不匹配，
//     而 `style=width:1px`（无引号）这种不成形的写法也不在射程内（HTML 里合法但不合约定，
//     当前代码没有；要不要收进来是另一个决定）。
//   - 已知未覆盖：setAttribute("style", …) 单独成句时不匹配（当前代码没有这种写法，
//     app/ 下所有 setAttribute 都用于 aria-* / svg 属性）。
//   - 注释内容在扫描前已被抹平（见 maskComments）。
const STYLE_ATTRIBUTE = /(?<![\w$.-])style\s*=\s*(?=["'`]|\$\{)/gu;

const MARKUP_STYLE_FIX = "改成 CSSOM：元素进 DOM 后用 element.style.<prop> = … 或 "
  + "element.style.cssText = …（见 app/render.js 的 applyScoreStyles），样式配方写进 styles.css。"
  + "原因：生产 CSP 与本地 dev 现在都是 style-src 'self'（worker/responses.js:36-39），"
  + "标记里的 style 属性会被浏览器静默丢弃。";

function checkMarkupStyles() {
  const files = [INDEX_HTML, ...listScripts(`${FRONTEND}/app`), ...listScripts(`${FRONTEND}/export`), ...listScripts(`${FRONTEND}/tools`)];
  for (const file of files) {
    const raw = read(file);
    const masked = maskComments(raw, { html: file.endsWith(".html") });
    for (const match of masked.matchAll(STYLE_ATTRIBUTE)) {
      fail({
        contract: "标记里的 style 属性（CSP 会静默丢弃）",
        file,
        line: lineOf(masked, match.index),
        fix: MARKUP_STYLE_FIX,
        excerpt: lineTextAt(raw, match.index),
      });
    }
  }
  return files.length;
}

/* ============================== 契约 b ==================================== */

// 静态 HTML 的表头里零控件：表头（可点击排序的列名 + 逐列筛选行）只由 app/render.js 按
// COLUMNS 生成，index.html 里再手写一份控件就会和 COLUMNS 漂移（列宽、data-label、筛选取值
// 三处各说各的）。2026-10-08 起表头确实有控件了，但它们只能来自 render.js。
// 找不到 <thead> 时按通过处理并打印说明——那时这条断言无对象（如实报，不假装检查过）。
const THEAD_CONTROL = /<(?:input|select|button)\b/giu;

function checkHeadHasNoControls() {
  const raw = read(INDEX_HTML);
  const masked = maskComments(raw, { html: true });
  const open = masked.search(/<thead\b/iu);
  const close = open < 0 ? -1 : masked.indexOf("</thead>", open);
  if (open < 0 || close < 0) {
    // 表头不在这份 HTML 里 = 这条断言没有对象。如实打印 SKIP，不假装检查过，
    // 也不因为「找不到」就变红：结构搬进 JS 是另一件事，改的人自会看到这行提示。
    console.log(`SKIP 契约 b：${INDEX_HTML} 里找不到 <thead>…</thead> 区间（表头不在这份 HTML 里）`);
    return false;
  }
  for (const match of masked.slice(open, close).matchAll(THEAD_CONTROL)) {
    const index = open + match.index;
    fail({
      contract: "表头（<thead>）内出现控件",
      file: INDEX_HTML,
      line: lineOf(masked, index),
      fix: "表头控件改由 app/render.js 的 createHead() 按 COLUMNS 生成，index.html 的 <thead> 保持为空。",
      excerpt: lineTextAt(raw, index),
    });
  }
  return true;
}

/* ============================== 契约 c ==================================== */

// script-src 'self' 禁内联脚本：index.html 里每个 <script> 都必须带 src。
// 快照（app/snapshot.js 生成的离线文件）不受这条约束——它不带 CSP，是另一条通路。
const SCRIPT_TAG = /<script\b[^>]*>/giu;

function checkNoInlineScripts() {
  const raw = read(INDEX_HTML);
  const masked = maskComments(raw, { html: true });
  for (const match of masked.matchAll(SCRIPT_TAG)) {
    if (/\bsrc\s*=/iu.test(match[0])) continue;
    fail({
      contract: "index.html 出现内联 <script>",
      file: INDEX_HTML,
      line: lineOf(masked, match.index),
      fix: "把脚本移进 app/ 下的模块并用 <script type=\"module\" src=\"…\"> 引入；"
        + "生产 CSP 是 script-src 'self'（worker/responses.js:36-39），内联脚本会被拦掉。",
      excerpt: lineTextAt(raw, match.index),
    });
  }
}

/* ============================== 契约 d ==================================== */

// 裸 fetch(：出网请求必须走 app/net.js 的超时封装（原因见该文件头部，
// 「没有超时 → promise 永不 settle → 页面永久停在加载中」）。
//
// 匹配边界：
//   - (?<![\w$]) 排掉注入式的 fetchImpl(…)：app/ 里绝大多数调用点都是把 fetch 当参数注入
//     （fetchImpl、globalThis.fetch 传参），那不是裸调用，不许报；
//   - 必须紧跟 `(`，所以 net.js 的 fetchWithTimeout(…)、以及 fetchText(…) 这类名字不匹配；
//   - 反过来 `.fetch(` / `globalThis.fetch(` 会被抓到（前面不是标识符字符），
//     那同样是绕过封装的网络调用，应当红。
//   - 注释已抹平，文档里提到 `fetch(` 不会变红。
//   - 范围只到 frontend-next/app/（build/测试脚本不在契约里）。
//
// 没有例外：写这份门禁时 app/snapshot.js 的 fetchText() 还是裸 fetch(url, …)（只读同源的
// styles.css / copy.js，用来拼离线快照），当时按「按行内容登记一条带理由的例外」设计过；
// 落地前那个调用点已经改成 fetchWithTimeout(globalThis.fetch, …)，于是例外整条删掉——
// 门禁宁可窄，也不要为了兼容一处已经修好的写法留一个长期后门。若将来确实出现
// 「读了也白读、超时无意义」的同源静态子资源请求，再按那次的理由单独加例外。
const BARE_FETCH_SOURCE = "(?<![\\w$])fetch\\s*\\(";
const NET_MODULE = `${FRONTEND}/app/net.js`;

function checkNoBareFetch() {
  const files = listScripts(`${FRONTEND}/app`).filter((file) => file !== NET_MODULE);
  for (const file of files) {
    const raw = read(file);
    const masked = maskComments(raw);
    for (const match of masked.matchAll(new RegExp(BARE_FETCH_SOURCE, "gu"))) {
      fail({
        contract: "裸 fetch(（绕过 app/net.js 的超时封装）",
        file,
        line: lineOf(masked, match.index),
        fix: "改用 app/net.js 的 fetchWithTimeout(fetchImpl, input, init, ms)；"
          + "需要同时保留调用方取消权时用 timeoutPair() 拿信号并在 finally 里 release()。",
        excerpt: lineTextAt(raw, match.index),
      });
    }
  }
  return files.length;
}

/* ================================ 执行 ==================================== */

const styleFiles = checkMarkupStyles();
const headChecked = checkHeadHasNoControls();
checkNoInlineScripts();
const fetchFiles = checkNoBareFetch();

if (failures.length === 0) {
  console.log(`PASS 契约 a：${styleFiles} 个源码文件里没有标记形式的 style= 属性`);
  if (headChecked) console.log(`PASS 契约 b：${INDEX_HTML} 的 <thead> 区间内没有 input/select/button`);
  console.log(`PASS 契约 c：${INDEX_HTML} 的 <script> 全部带 src（无内联脚本）`);
  console.log(`PASS 契约 d：${fetchFiles} 个 app/ 模块（除 net.js）没有裸 fetch(`);
  console.log("\n全部前端契约通过");
  process.exit(0);
}

console.error(`\n${failures.length} 处前端契约违规：\n`);
for (const { contract, file, line, fix, excerpt } of failures) {
  console.error(`${file}:${line} — ${contract}`);
  if (excerpt.trim()) console.error(`      ${excerpt.trim()}`);
  console.error(`      怎么修：${fix}\n`);
}
process.exit(1);
