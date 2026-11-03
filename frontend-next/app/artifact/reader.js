/* ============================================================================
   产物读取器：解 zip，核对 status.json 声明的身份与 result.json 的 SHA-256，再核对
   manifest、计数与逐节点摘要彼此一致。
   ----------------------------------------------------------------------------
   节点记录的字段级规则（取值域、代理证据等）只在 runner 上校验一次
   （backend/app/results/validation.py，发布前 build_artifact 必过）。这里不再抄一份：
   原先两边各维护一套 schema，每加一个字段都要「两侧校验器同步」。
   ========================================================================== */
const EOCD = 0x06054b50;
const CENTRAL = 0x02014b50;
const LOCAL = 0x04034b50;
const MAX_COMPRESSED = 50 * 1024 * 1024;
const MIN_ZIP_BYTES = 22;
const MAX_UNCOMPRESSED = 100 * 1024 * 1024;
const ALLOWED_FILES = new Set(["status.json", "result.json"]);
const TEXT_DECODER = new TextDecoder();
const CRC32_TABLE = Uint32Array.from({ length: 256 }, (_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit += 1) {
    value = (value >>> 1) ^ (0xedb88320 & -(value & 1));
  }
  return value >>> 0;
});
const SUMMARY_KEYS = [
  "node_index", "node", "type", "selected_proxy", "status", "error",
  "transport_error", "attempt_count", "retry_count", "attempt_errors", "exit_ip",
  "cidr", "rdns", "ai_verdict", "location", "isp", "score", "ipure_scores",
  "ipure_report_url", "is_residential", "is_datacenter", "is_native", "native_status", "native_detail", "is_bogon",
  "bogon_status", "bogon_reason", "rpki_status", "asn_kind", "asn_kind_display",
  "abuse_level", "honeypot_status", "traffic_profile", "company_type", "is_vpn",
  "is_proxy", "is_tor", "is_crawler", "is_abuser", "security_status", "threat_tags",
  "asn", "as_org", "global_ping", "port_scan", "ping_check", "gpt_check",
  "related_domains", "elapsed_ms", "started_at", "finished_at", "completeness",
  "proxy_evidence",
];

function u16(view, offset) { return view.getUint16(offset, true); }
function u32(view, offset) { return view.getUint32(offset, true); }

// “信封不完整”与“信封完整但内容被拒”要分开：前者是传输层少给了字节（空 body、被截断），
// 换一次就好；后者是结果本身有问题，重取多少次都一样。
function truncated(message) {
  return Object.assign(new Error(message), { code: "artifact_truncated" });
}

function findEnd(view) {
  const start = Math.max(0, view.byteLength - 65_557);
  for (let offset = view.byteLength - 22; offset >= start; offset -= 1) {
    if (offset + 4 <= view.byteLength && u32(view, offset) === EOCD) return offset;
  }
  throw truncated("artifact ZIP 缺少结束目录");
}

function safeName(name) {
  return Boolean(
    name &&
    ALLOWED_FILES.has(name) &&
    !name.startsWith("/") &&
    !name.includes("\\") &&
    !name.split("/").includes(".."),
  );
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc = (crc >>> 8) ^ CRC32_TABLE[(crc ^ byte) & 0xff];
  }
  return (crc ^ 0xffffffff) >>> 0;
}

async function inflateRaw(bytes) {
  if (typeof DecompressionStream !== "function") {
    throw new Error("当前浏览器不支持 ZIP deflate 解压");
  }
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function readZip(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength > MAX_COMPRESSED) {
    throw new Error("artifact ZIP 大小无效");
  }
  // 空 body 与坏归档原来共用「缺少结束目录」这一条报错，看不出是谁的问题；这里把字节数直接写出来。
  if (buffer.byteLength < MIN_ZIP_BYTES) {
    throw truncated(`artifact ZIP 内容不完整：只收到 ${buffer.byteLength} 字节，至少应有 ${MIN_ZIP_BYTES} 字节`);
  }
  const view = new DataView(buffer);
  const end = findEnd(view);
  const entryCount = u16(view, end + 10);
  const directorySize = u32(view, end + 12);
  const directoryOffset = u32(view, end + 16);
  if (!entryCount || entryCount > 32 || directoryOffset + directorySize > buffer.byteLength) {
    throw new Error("artifact ZIP 目录无效");
  }
  const files = new Map();
  let offset = directoryOffset;
  let totalUncompressed = 0;
  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > buffer.byteLength || u32(view, offset) !== CENTRAL) {
      throw new Error("artifact ZIP 中央目录无效");
    }
    const flags = u16(view, offset + 8);
    const method = u16(view, offset + 10);
    const crc = u32(view, offset + 16);
    const compressedSize = u32(view, offset + 20);
    const uncompressedSize = u32(view, offset + 24);
    const nameLength = u16(view, offset + 28);
    const extraLength = u16(view, offset + 30);
    const commentLength = u16(view, offset + 32);
    const localOffset = u32(view, offset + 42);
    const nameBytes = new Uint8Array(buffer, offset + 46, nameLength);
    const name = TEXT_DECODER.decode(nameBytes);
    if (!safeName(name) || files.has(name) || (flags & 1) !== 0) {
      throw new Error("artifact ZIP 含有不允许的文件");
    }
    if (uncompressedSize > MAX_UNCOMPRESSED || compressedSize > MAX_COMPRESSED) {
      throw new Error("artifact ZIP 文件大小无效");
    }
    totalUncompressed += uncompressedSize;
    if (totalUncompressed > MAX_UNCOMPRESSED) throw new Error("artifact ZIP 解压后超出限制");
    if (localOffset + 30 > buffer.byteLength || u32(view, localOffset) !== LOCAL) {
      throw new Error("artifact ZIP 本地文件头无效");
    }
    const localNameLength = u16(view, localOffset + 26);
    const localExtraLength = u16(view, localOffset + 28);
    const dataOffset = localOffset + 30 + localNameLength + localExtraLength;
    if (dataOffset + compressedSize > buffer.byteLength) throw new Error("artifact ZIP 数据越界");
    const compressed = new Uint8Array(buffer, dataOffset, compressedSize);
    let content;
    if (method === 0) content = new Uint8Array(compressed);
    else if (method === 8) content = await inflateRaw(compressed);
    else throw new Error("artifact ZIP 使用不支持的压缩算法");
    if (content.byteLength !== uncompressedSize || crc32(content) !== crc) {
      throw new Error("artifact ZIP CRC 或大小校验失败");
    }
    files.set(name, content);
    offset += 46 + nameLength + extraLength + commentLength;
  }
  if (files.size !== ALLOWED_FILES.size) throw new Error("artifact 缺少结果文件");
  return files;
}

function parseJson(files, name) {
  const bytes = files.get(name);
  if (!bytes) throw new Error(`artifact 缺少 ${name}`);
  try {
    return JSON.parse(TEXT_DECODER.decode(bytes));
  } catch {
    throw new Error(`${name} 不是有效 JSON`);
  }
}

function sameJson(left, right) {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) &&
      left.length === right.length && left.every((value, index) => sameJson(value, right[index]));
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return leftKeys.length === rightKeys.length &&
    leftKeys.every((key) => Object.hasOwn(right, key) && sameJson(left[key], right[key]));
}

function summary(record) {
  return Object.fromEntries(SUMMARY_KEYS.map((key) => [key, record[key] ?? null]));
}

async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

export async function readArtifact(buffer, identity) {
  const files = await readZip(buffer);
  const status = parseJson(files, "status.json");
  const resultBytes = files.get("result.json");
  const result = parseJson(files, "result.json");
  if (
    status?.sanitized !== true ||
    status?.schema_version !== 1 ||
    status?.request_id !== identity.requestId ||
    Number(status?.run_id) !== Number(identity.runId) ||
    Number(status?.run_attempt) !== Number(identity.runAttempt) ||
    !/^[a-f0-9]{64}$/iu.test(String(status?.result_sha256 || "")) ||
    (await sha256Hex(resultBytes)) !== String(status.result_sha256).toLowerCase()
  ) {
    throw new Error("artifact 身份、安全标记或摘要校验失败");
  }
  validateExport(result, identity.requestId, status);
  return { status, result };
}

export function validateExport(result, requestId = result?.id, status = { status: "completed", total: result?.total, completed: result?.completed, counts: result?.manifest?.counts, usable: result?.partial_count === 0 && result?.failed_count === 0 }) {
  if (
    result?.id !== requestId ||
    result?.status !== "completed" ||
    result?.manifest_ready !== true ||
    !Array.isArray(result?.results) ||
    !result?.manifest ||
    result.manifest.job_id !== requestId ||
    result.manifest.status !== "completed" ||
    result.manifest.total !== result.results.length ||
    result.completed !== result.total ||
    result.total !== result.results.length
  ) {
    throw new Error("artifact 结果完整性校验失败");
  }
  const manifest = result.manifest;
  if (manifest.schema_version !== 1 || manifest.completed !== result.completed || manifest.all_records_present !== true || manifest.complete !== true || result.cleanup_confirmed !== true || status.status !== "completed") throw new Error("artifact 完成或清理证据无效");
  const counts = manifest.counts;
  if (
    !counts ||
    !Array.isArray(manifest.records) ||
    manifest.records.length !== result.results.length ||
    !Number.isInteger(result.total) ||
    result.total < 1 ||
    !Number.isInteger(result.completed) ||
    result.completed !== result.total ||
    !Number.isInteger(status.total) ||
    status.total !== result.total ||
    !Number.isInteger(status.completed) ||
    status.completed !== result.completed ||
    !sameJson(status.counts, counts)
  ) {
    throw new Error("artifact 计数或 manifest 结构校验失败");
  }

  const derived = { success: 0, partial: 0, failed: 0, complete: 0 };
  result.results.forEach((record, index) => {
    if (
      !record ||
      record.job_id !== requestId ||
      record.node_index !== index ||
      !["success", "partial", "failed"].includes(record.status)
    ) {
      throw new Error("artifact 节点身份或状态校验失败");
    }
    derived[record.status] += 1;
    if (record.completeness?.complete === true) derived.complete += 1;
    const entry = manifest.records[index];
    if (
      !entry ||
      entry.index !== index ||
      entry.file !== `${String(index).padStart(4, "0")}.json` ||
      !sameJson(entry.summary, summary(record))
    ) {
      throw new Error("artifact manifest 与节点结果不一致");
    }
  });
  if (!sameJson(derived, counts)) throw new Error("artifact 状态计数不一致");
  if (
    result.success_count !== counts.success ||
    result.partial_count !== counts.partial ||
    result.failed_count !== counts.failed ||
    status.usable !== (counts.failed === 0 && counts.partial === 0)
  ) {
    throw new Error("artifact 任务计数或可用性不一致");
  }

  return result;
}
