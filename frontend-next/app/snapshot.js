/* ============================================================================
   离线快照导出 —— 「导出按钮」的全部实现
   ----------------------------------------------------------------------------
   产物有两种，内容同源：
   1. .mhtml —— MIME multipart/related 单文件。HTML 部件用 <link href="./styles.css">
      引样式，样式表作为独立部件靠 Content-Location 命中同一个 URL，这正是 MHTML 的
      标准关联机制（Chrome 自己保存网页就是这个形状）。CSS 不在正文里，所以正文可以
      做到零外链。
   2. .html —— 自包含单文件，CSS 直接内联进 <style>，不需要任何子资源。
      MHTML 是被逐步冷落的老格式（Firefox 早已不支持），必须有这条兜底。

   两种产物只有两处不同，都由 format 决定：
   - 样式：.mhtml 用 <link> + 独立 CSS 部件；.html 把 CSS 内联进 <style>。
   - 脚本：**只有 .html 内联 copy.js**。.mhtml 一律不内联——Chrome 把 file:// 打开的
     .mhtml 放进沙箱 frame 且不设 allow-scripts，那段约 6.9KB 的代码永远不会执行
     （实测日志：Blocked script execution … the document's frame is sandboxed and the
     'allow-scripts' permission is not set），内联进去只是给每个快照白塞一份死代码。
     .mhtml 的复制路径是纯 CSS：styles.css 里 `.ip { user-select: all }`（本轮核对时在
     styles.css:429；该文件的样式仍在被编辑，行号会漂，所以按选择器找）让单击出口 IP
     即全选，零 JS 也成立，所以不内联脚本并没有让 .mhtml 少掉任何功能。
     按钮本身在 .mhtml 里确实仍然按不动，那句免责说明必须留着（见 buildSnapshotDocument
     的 scriptNote）。

   两条共同要求：
   - 快照不含任何 <a href> 外链、不含 <meta> CSP。后者很关键：.html 版本靠内联 <script>
     挂复制行为，一旦写上 CSP 就会把自己的脚本拦掉。
   ========================================================================== */

import { buildMhtml, utf8ToBytes } from "../export/mhtml.js";
import { DEFAULT_TIMEOUT_MS, fetchWithTimeout, isTimeoutError } from "./net.js";
import { sourceCreditsHtml } from "./render.js";

const STYLESHEET_URL = new URL("../styles.css", import.meta.url);
const COPY_SCRIPT_URL = new URL("./copy.js", import.meta.url);

/* ---------------------------------------------- 文件内常量：品牌区 --- */
// 这段标记是线上页品牌区（index.html:24-32 的 .brand 块）的手抄副本：<svg> 的参数与
// 两条 <path d> 与 index.html:26-28 **逐字相同**，<h1> 文案也相同。
// 快照 import 不了 index.html（它是 HTML 模板，不是模块），所以只能留这一份字面副本：
// **改一边必须改另一边**，否则离线快照和线上页的品牌会长得不一样——图标参数漂移是
// 肉眼最晚发现的那类不一致。
// 唯一刻意不同的一处是角标文案：线上页写「示例数据」或真实来源，快照固定写「离线快照」，
// 因为快照要离开站点后还能自证是什么。
// 首行的 8 个空格是烘焙进常量的缩进：替换处顶格插入，产物字节与抽出前完全一致。
const BRAND_BLOCK = `        <div class="brand">
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 17.5 8.5 11l4 4L21 6"></path><path d="M15.5 6H21v5.5"></path>
            </svg>
          </span>
          <h1>节点质量榜</h1>
          <span class="tag-demo">离线快照</span>
        </div>`;

/* ------------------------------------------------------- 读取可内联的资源 --- */

/**
 * 超时和「读不到」必须分开说：前者对应的动作是重试，后者对应的是「换个方式打开页面」。
 * 混成一句，用户就只能在两种动作里瞎猜。
 */
function isTimeoutFailure(error) {
  // net.js 有两条路径：AbortSignal.timeout() 抛 name === "TimeoutError" 的 DOMException；
  // 退化路径是 AbortController.abort(new Error(`请求超过 ${ms} 毫秒未返回`))，抛出来的是
  // 那个 Error 本身（name 是 "Error"），所以还得看它自带的文案。
  return isTimeoutError(error) || /毫秒未返回/u.test(String(error?.message ?? ""));
}

async function fetchText(url) {
  let response;
  try {
    // 一律走 net.js 的超时契约，不自造 timeoutSignal：这个 fetch 挂住的话，导出按钮会
    // 永久停在 disabled 且没有任何提示。超时之后 collectStylesheet 的 catch 会把
    // 「怎么才能导出」写进给用户看的错误里。
    response = await fetchWithTimeout(globalThis.fetch, url, { cache: "no-store" });
  } catch (error) {
    if (isTimeoutFailure(error)) {
      throw new Error(`${url} 读取超时：${Math.round(DEFAULT_TIMEOUT_MS / 1000)} 秒内没有返回`);
    }
    throw new Error(`${url} 读取失败：${error?.message || error}`);
  }
  if (!response.ok) throw new Error(`${url} 读取失败：HTTP ${response.status}`);
  return response.text();
}

// fetch 在 file:// 打开的页面上会被浏览器拒绝（不透明来源），此时退回到 CSSOM：
// 已加载的样式表规则可以逐条序列化。代价是丢注释与格式，换来「从本地文件也能导出」。
function stylesheetFromCssom() {
  const chunks = [];
  for (const sheet of Array.from(document.styleSheets)) {
    let rules = null;
    try {
      rules = sheet.cssRules;
    } catch (error) {
      continue; // 跨源样式表读不到规则，跳过而不是让整次导出失败
    }
    if (!rules) continue;
    for (const rule of Array.from(rules)) chunks.push(rule.cssText);
  }
  return chunks.join("\n");
}

async function collectStylesheet() {
  try {
    return { css: await fetchText(STYLESHEET_URL), from: "fetch" };
  } catch (error) {
    const css = stylesheetFromCssom();
    // 两条路都走不通只在一种情况下发生：页面是 file:// 直接打开的。
    // 浏览器把每个本地文件当独立的不透明来源，fetch 和 cssRules 都会拒绝。
    // 这不是能靠代码绕开的事，所以直接把「怎么才能导出」写进错误里。
    if (!css) {
      throw new Error(
        `浏览器不允许页面读取同目录的 styles.css（${error.message}）。`
        + "请通过本地静态服务打开本页后再导出，例如：uv run --no-dev python -m http.server 5180 --directory frontend-next",
      );
    }
    return { css, from: "cssom" };
  }
}

async function collectCopyScript() {
  try {
    return { script: await fetchText(COPY_SCRIPT_URL), from: "fetch" };
  } catch (error) {
    // 不抛：.html 快照本身仍然可用，只是复制按钮会失效。由调用方把这件事告诉用户，
    // 而不是悄悄给出一个「看着能用其实按不动」的文件。
    return { script: "", from: "missing", reason: error.message };
  }
}

/* --------------------------------------------------------- 拼装快照文档 --- */

// 文本与属性共用一套转义。顺序有硬要求：`&` 必须第一个替换，否则后面替换出来的
// `&#39;` 之类实体会被自己的 `&` 规则再转一遍（双转义）。
// 单引号在 HTML 文本里本来合法，一起转是因为产物会被转发、被别的工具再加工：任何
// 下游用单引号写属性的地方都不该被我们送去的 `'` 打破，而转义它的成本是零。
function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * @param {object} options
 * @param {"mhtml"|"html"} options.format 决定脚本怎么处理：mhtml 不内联 copy.js
 *        （浏览器沙箱拦掉，见文件头），改为写清「单击 IP 全选」这条零 JS 路径；
 *        html 照旧内联，那里的复制按钮真的能用。
 * @param {"linked"|"inlined"} options.styleMode linked=给 MHTML（<link> + 独立部件），
 *        inlined=给 .html 兜底（<style> 内联）
 * @param {string} options.css
 * @param {string} options.rowsHtml 整张表的实时 outerHTML（含表头与色带的 CSSOM 结果）
 * @param {string} options.copyScript copy.js 原文；为空则快照内复制不可用
 * @param {object} options.summary 顶部静态摘要用到的文案。title 是**未转义原文**，
 *        由本函数负责转义一次（见 buildSummary 的注释）。
 */
export function buildSnapshotDocument(options) {
  const { format, styleMode, css, rowsHtml, copyScript, summary } = options;

  const styleBlock = styleMode === "inlined"
    ? `<style>\n${css}\n</style>`
    : `<link rel="stylesheet" href="./styles.css">`;

  // Chrome 把 file:// 打开的 .mhtml 放进**沙箱 frame 且不设 allow-scripts**，
  // 实测日志：Blocked script execution … the document's frame is sandboxed and the
  // 'allow-scripts' permission is not set。也就是说 mhtml 版本里 BestIpCopy 永远不会被
  // 绑定，复制按钮点了没反应——这是浏览器对 MHTML 的处置，不是绑定逻辑写错了。
  // 所以 mhtml 版本要额外写清一条不依赖脚本的复制路径（.ip 的 user-select: all
  // 让单击即全选），并指向脚本可用的 .html 版本。.html 版本脚本正常，不加这段说明。
  // 这段文案是为「按钮确实按不动」而留的，不内联脚本之后它**必须原样保留**。
  const scriptNote = format === "mhtml"
    ? "<p class=\"snapshot-note\">浏览器会把 .mhtml 里的脚本置于沙箱、不执行，"
      + "因此本文件的复制按钮不可用：<b>单击出口 IP 即全选</b>，再按 Ctrl/⌘+C 复制；"
      + "要按钮可用请打开同批导出的 .html 版本。</p>"
    : "";
  // 快照的头部是静态摘要而不是控件：离线快照没有可操作的对象，
  // 摆一排按不动的输入框只会让人以为下载坏了。这里改为把「当时的视图条件」写清楚。
  const header = `
    <header class="topbar topbar-static">
      <div class="topbar-head">
${BRAND_BLOCK}
        <p class="run-stats">${summary.statsHtml}</p>
      </div>
      <p class="source-credits">${sourceCreditsHtml({ withLinks: false })}</p>
      <div class="topbar-controls">
        <p class="snapshot-state">${summary.stateHtml}</p>
        ${scriptNote}
      </div>
    </header>`;

  // 内联门只有这一处：mhtml 永远不内联，即使上游把 copy.js 原文递进来也丢掉
  // （那份脚本在 .mhtml 里不会执行，内联只是白占体积，还让人误以为按钮能用）。
  const inlineScript = format === "mhtml" ? "" : copyScript;
  const scriptBlock = inlineScript
    ? `\n<script>\n${inlineScript}\n</script>\n<script>\nBestIpCopy.attach(document);\n</script>`
    : "\n<!-- 快照内未内联 copy.js，复制按钮不可用 -->";

  // rowsHtml 是 grid.outerHTML：浏览器把 29 行序列化在**一行 84KB** 里，人审阅时没法看。
  // 只在 </tr> 之后补一个换行：<td> 内部一字不动，<thead>/<table> 语义不受影响
  // （表格里行与行之间的空白字符按 HTML 解析规则被忽略），渲染结果完全不变。
  const tableHtml = String(rowsHtml).replaceAll("</tr>", "</tr>\n");

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(summary.title)}</title>
${styleBlock}
</head>
<body>
<div class="shell">
${header}
<p class="result-count">${summary.countHtml}</p>
<main class="table-wrap">${tableHtml}</main>
</div>${scriptBlock}
</body>
</html>
`;
}

/* ------------------------------------------------------------- 下载产物 --- */

function timestampSlug(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  // 立刻 revoke 会让部分浏览器来不及读取；延迟释放，代价只是多占一会儿内存。
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

/* --------------------------------------------------------------- 对外接口 --- */

/**
 * 把「当时的视图条件」拼成快照顶部的静态摘要。
 *
 * 数字字段（total / success / partial / failed / shown）全部走 escapeHtml：拼装层能
 * 自己保证的只有「拼进去的东西不会是标记」，不再依赖上游 main.js「这几个字段一定是
 * number」的类型纪律——那份纪律没有任何地方强制它，也不该由这里替上游背。
 *
 * title 里存的是**未转义原文**（来源、生成时间），由 buildSnapshotDocument 写 <title>
 * 时统一转义一次。这里再转一次就是双转义：来源含 `&` 时会写成 `&amp;amp;`，用户在
 * 文件管理器/标签页上看到的就是一个坏标题。
 */
export function buildSummary({ total, shown, state }) {
  // 快照会被归档、被转发，所以真实扫描的来源要跟着文件走：标题与摘要各写一次，
  // 否则一份真实结果和一个设计示例在文件列表里长得一模一样。
  // 「示例数据」不加前缀——它本来就是设计示例，不必在自己的标题里反复声明。
  const source = typeof state.source === "string" ? state.source.trim() : "";
  const realSource = source && source !== "示例数据" ? source : "";
  const sourceTag = realSource ? `${realSource} · ` : "";
  const sourceHtml = realSource
    ? `来源：<b>${escapeHtml(realSource)}</b> <span class="sep">·</span> `
    : "";
  return {
    title: `${sourceTag}节点质量榜 · 离线快照 · ${state.generatedAt}`,
    statsHtml: `<b>${escapeHtml(total)}</b> 个节点 <span class="sep">·</span> 完整 <b>${escapeHtml(state.success)}</b> <span class="sep">·</span> 部分 <b>${escapeHtml(state.partial)}</b> <span class="sep">·</span> 失败 <b>${escapeHtml(state.failed)}</b>`,
    stateHtml: `${sourceHtml}筛选：<b>${escapeHtml(state.query || "无")}</b> <span class="sep">·</span> 状态：<b>${escapeHtml(state.statusLabel)}</b> <span class="sep">·</span> 排序：<b>${escapeHtml(state.sortLabel)}</b> <span class="sep">·</span> 视图内 <b>${escapeHtml(shown)}</b> / ${escapeHtml(total)} 个节点 <span class="sep">·</span> 导出于 <b>${escapeHtml(state.exportedAt)}</b>`,
    countHtml: `当前视图 <b>${escapeHtml(shown)}</b> 个节点，共 ${escapeHtml(total)} 个`,
  };
}

const FORMATS = {
  mhtml: { mime: "multipart/related", extension: "mhtml" },
  html: { mime: "text/html;charset=utf-8", extension: "html" },
};

/**
 * 把拼好的 HTML 交成最终的下载字节：.mhtml 多一层 MIME 封装（HTML 部件 + CSS 部件），
 * .html 就是它的 UTF-8 字节本身。
 *
 * 为什么单独导出：往返测试要在 Node 下断言「快照 → .mhtml 字节 → QP 解码逐字节等于
 * 原文」，而 exportSnapshot 本体依赖浏览器（CSSOM 回退、Blob 下载、location）在 Node
 * 里跑不起来。这里不碰 DOM、不读全局、参数全部显式传入，所以导出流程和测试共用同一条
 * 组装路径，而不是测试自己再抄一份（抄一份就等于测试只验证了抄件）。
 */
export function serializeSnapshot({ format, html, css, snapshotUrl, subject, date }) {
  if (format !== "mhtml") return utf8ToBytes(html);
  return buildMhtml({
    snapshotUrl,
    subject,
    date,
    parts: [
      {
        contentType: "text/html; charset=utf-8",
        contentLocation: snapshotUrl,
        encoding: "quoted-printable",
        bytes: utf8ToBytes(html),
      },
      {
        contentType: "text/css; charset=utf-8",
        contentLocation: new URL("styles.css", snapshotUrl).href,
        encoding: "quoted-printable",
        bytes: utf8ToBytes(css),
      },
    ],
  });
}

// 不用裸 location：在非窗口环境（Node、Worker）里它是**未声明的标识符**，读它就是
// ReferenceError；globalThis.location 至多拿到 undefined，还能退到 document.baseURI。
// 两者都没有就明确报错——否则后面 new URL("styles.css", "") 抛出的 TypeError 看不出成因。
function currentDocumentUrl() {
  const href = globalThis.location?.href || globalThis.document?.baseURI || "";
  if (!href) {
    throw new Error("拿不到当前页面地址（location 与 document.baseURI 都不可用），无法为快照部件生成 Content-Location");
  }
  return href.split("#")[0].split("?")[0];
}

/**
 * 导出当前视图。
 * @returns {Promise<{filename:string, bytes:number, styleSource:string, scriptSource:string}>}
 */
export async function exportSnapshot(format, context) {
  const spec = FORMATS[format];
  if (!spec) throw new Error(`未知的导出格式：${format}`);

  const { rowsHtml, total, shown, state } = context;
  // copyScript 对 .mhtml 是白读一次：不内联这件事的决策在 buildSnapshotDocument 里
  // （那里是唯一的门），这里照常读是为了把 scriptSource / scriptWarning 如实回给调用方，
  // 而不是在这一层替调用方判断哪种格式需不需要它。
  const [stylesheet, copy] = await Promise.all([collectStylesheet(), collectCopyScript()]);
  const summary = buildSummary({ total, shown, state });

  const now = new Date();
  // 文件名要能自证来源：「真实扫描」与设计示例的产物只差时间戳的话，在下载列表里根本分不开
  // （标题里的来源要打开才看得见）。示例路径不加后缀，产物名与上一轮完全一致。
  const sourceTag = typeof state.source === "string" && state.source.trim() && state.source.trim() !== "示例数据" ? "real-" : "";
  const filename = `best-ip-snapshot-${sourceTag}${timestampSlug(now)}.${spec.extension}`;
  const documentUrl = currentDocumentUrl();

  // 两种格式共用同一条组装路径：只有 styleMode 与 CSS 来源不同，脚本内联由
  // buildSnapshotDocument 按 format 决定，字节封装交给 serializeSnapshot。
  const isMhtml = format === "mhtml";
  const html = buildSnapshotDocument({
    format,
    styleMode: isMhtml ? "linked" : "inlined",
    css: isMhtml ? "" : stylesheet.css,
    rowsHtml,
    copyScript: copy.script,
    summary,
  });
  const bytes = serializeSnapshot({
    format,
    html,
    css: stylesheet.css,
    snapshotUrl: documentUrl,
    subject: summary.title,
    date: now,
  });

  download(new Blob([bytes], { type: spec.mime }), filename);
  return {
    filename,
    bytes: bytes.byteLength,
    styleSource: stylesheet.from,
    scriptSource: copy.from,
    scriptWarning: copy.reason || "",
  };
}
