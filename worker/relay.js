import { HttpError, json } from "./responses.js";
import { isRecord } from "./config.js";
import { assertRunnerToken } from "./runner.js";

export const SUBSCRIPTION_RELAY_PATH = "/api/subscription-relay";

const MAX_REQUEST_CHARS = 8192;
// UTF-8 worst case (4 bytes per character): the character cap stays authoritative.
const MAX_REQUEST_BYTES = MAX_REQUEST_CHARS * 4;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
const MAX_LOCATION_CHARS = 4096;
const TIMEOUT_MS = 20_000;
const MINIMUM_URL_CHARS = 8;
const MAXIMUM_URL_CHARS = 4096;
const UNSAFE_URL_CHARACTERS = /[\s\u0000-\u001f\u007f-\u009f]/u;
const IPV4_PATTERN = /^\d{1,3}(?:\.\d{1,3}){3}$/u;
const IPV6_MAPPED_HEX_PATTERN = /^::(?:ffff:)?([0-9a-f]{1,4}):([0-9a-f]{1,4})$/u;
const IPV6_MAPPED_DOTTED_PATTERN = /^::(?:ffff:)?((?:\d{1,3}\.){3}\d{1,3})$/u;

function invalidRequest() {
  return new HttpError(400, "订阅中继请求无效", "invalid_request");
}

function isBlockedIpv4(hostname) {
  if (!IPV4_PATTERN.test(hostname)) return false;
  const octets = hostname.split(".").map(Number);
  if (octets.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) return false;
  const [first, second, third] = octets;
  if (first === 0 || first === 10 || first === 127) return true;
  if (first === 100 && second >= 64 && second <= 127) return true;
  if (first === 169 && second === 254) return true;
  if (first === 172 && second >= 16 && second <= 31) return true;
  if (first === 192 && (second === 0 || second === 168)) return true;
  if (first === 198 && (second === 18 || second === 19 || second === 51)) return true;
  if (first === 203 && second === 0 && third === 113) return true;
  return first >= 224;
}

function isBlockedIpv6(hostname) {
  const value = hostname.replace(/^\[/u, "").replace(/\]$/u, "");
  if (value === "::" || value === "::1") return true;
  if (/^f[cd]/u.test(value) || /^fe[89ab]/u.test(value) || /^ff/u.test(value)) return true;
  const dotted = value.match(IPV6_MAPPED_DOTTED_PATTERN);
  if (dotted) return isBlockedIpv4(dotted[1]);
  const mapped = value.match(IPV6_MAPPED_HEX_PATTERN);
  if (mapped) {
    const high = Number.parseInt(mapped[1], 16);
    const low = Number.parseInt(mapped[2], 16);
    return isBlockedIpv4([high >> 8, high & 0xff, low >> 8, low & 0xff].join("."));
  }
  return false;
}

function isBlockedHostname(hostname) {
  const normalized = hostname.toLowerCase().replace(/\.$/u, "");
  if (normalized === "localhost" || normalized.endsWith(".localhost")) return true;
  if (normalized.endsWith(".internal") || normalized.endsWith(".local")) return true;
  if (normalized.startsWith("[")) return isBlockedIpv6(normalized);
  return isBlockedIpv4(normalized);
}

export function assertTargetUrl(value) {
  if (typeof value !== "string" || value.length < MINIMUM_URL_CHARS || value.length > MAXIMUM_URL_CHARS) {
    throw invalidRequest();
  }
  if (UNSAFE_URL_CHARACTERS.test(value)) throw invalidRequest();
  let target;
  try {
    target = new URL(value);
  } catch {
    throw invalidRequest();
  }
  if (target.protocol !== "http:" && target.protocol !== "https:") throw invalidRequest();
  if (!target.hostname) throw invalidRequest();
  if (target.username || target.password) throw invalidRequest();
  if (target.port) {
    const port = Number(target.port);
    if (!/^\d{1,5}$/u.test(target.port) || !Number.isSafeInteger(port) || port < 1 || port > 65535) {
      throw invalidRequest();
    }
  }
  if (isBlockedHostname(target.hostname)) throw invalidRequest();
  return target;
}

export function constantTimeEqual(left, right) {
  const encoder = new TextEncoder();
  const a = encoder.encode(String(left));
  const b = encoder.encode(String(right));
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

async function readTarget(request) {
  const contentLength = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_REQUEST_BYTES) throw invalidRequest();
  let raw;
  try {
    raw = await request.text();
  } catch {
    throw invalidRequest();
  }
  if (raw.length > MAX_REQUEST_CHARS) throw invalidRequest();
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw invalidRequest();
  }
  if (!isRecord(payload) || typeof payload.url !== "string") throw invalidRequest();
  return payload.url;
}

function encodeBase64(bytes) {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function isTimeoutError(error) {
  return [error?.name, error?.code, error?.cause?.name]
    .some((value) => value === "TimeoutError" || value === "AbortError" || value === "ABORT_ERR");
}

async function fetchUpstream(target) {
  try {
    return await fetch(target, {
      method: "GET",
      redirect: "manual",
      headers: {
        "User-Agent": "clash.meta",
        Accept: "application/yaml,text/yaml,text/plain,*/*",
      },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    if (isTimeoutError(error)) throw new HttpError(504, "订阅源响应超时", "upstream_timeout");
    throw new HttpError(502, "无法访问订阅源", "upstream_unreachable");
  }
}

async function readCappedBody(response) {
  const declaredLength = Number(response.headers.get("Content-Length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
    throw new HttpError(413, "订阅源响应超过安全上限", "response_too_large");
  }
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const parts = [];
  let received = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > MAX_RESPONSE_BYTES) throw new HttpError(413, "订阅源响应超过安全上限", "response_too_large");
      parts.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  }
  const body = new Uint8Array(received);
  let offset = 0;
  for (const part of parts) {
    body.set(part, offset);
    offset += part.byteLength;
  }
  return body;
}

export async function subscriptionRelay(request, env) {
  try {
    return await relay(request, env);
  } catch (error) {
    if (error instanceof HttpError) {
      return json({ error: error.code }, error.status, { "Cache-Control": "no-store" });
    }
    throw error;
  }
}

async function relay(request, env) {
  if (request.method !== "POST") throw new HttpError(405, "请求方法不支持", "method_not_allowed");
  await assertRunnerToken(request, env);
  const target = assertTargetUrl(await readTarget(request));
  const upstream = await fetchUpstream(target.href);
  if (upstream.status >= 300 && upstream.status <= 399) {
    const location = upstream.headers.get("Location");
    return json(
      { status: upstream.status, location: location ? location.slice(0, MAX_LOCATION_CHARS) : null, body_b64: null },
      200,
      { "Cache-Control": "no-store" },
    );
  }
  let body;
  try {
    body = await readCappedBody(upstream);
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(502, "无法读取订阅源响应", "upstream_unreachable");
  }
  return json({ status: upstream.status, location: null, body_b64: encodeBase64(body) }, 200, { "Cache-Control": "no-store" });
}
