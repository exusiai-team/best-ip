/* ============================================================================
   订阅地址的信封加密
   ----------------------------------------------------------------------------
   原是旧前端 frontend/src/crypto.js 的逐字副本；旧前端已删除，这里是唯一实现。

   它做的事：用 ./scan-public.pem 里的 RSA-3072 公钥 + 一把一次性 AES-256-GCM 密钥，
   把订阅地址封装成 worker/scans.js 的 validateEnvelope 能认的信封。明文订阅地址
   从不离开浏览器；公钥指纹（PEM 的 DER SHA-256）在 importPublicKey 里与 Worker 的
   SCAN_KEY_ID 逐字符核对，对不上就当场失败，不发出一个必然被 400 拒绝的请求。
   ========================================================================== */

const TEXT_ENCODER = new TextEncoder();
export function requestId() {
  if (globalThis.crypto?.randomUUID) return `req-${globalThis.crypto.randomUUID()}`;
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `req-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export function base64Url(bytes) {
  const data = bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : bytes;
  let binary = "";
  for (const value of data) binary += String.fromCharCode(value);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

export function fromBase64(value) {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

export async function importPublicKey(config, fetchImpl, baseURI) {
  const path = String(config.publicKeyPath || "").trim();
  const configuredKeyId = String(config.keyId || "").trim().toLowerCase();
  if (!path || !/^[a-f0-9]{64}$/u.test(configuredKeyId)) {
    throw new Error("GitHub Actions 加密公钥尚未配置");
  }
  const response = await fetchImpl(new URL(path, baseURI), { cache: "no-store" });
  if (!response.ok) throw new Error("无法加载 GitHub Actions 加密公钥");
  const pem = await response.text();
  const body = pem.replace(/-----BEGIN PUBLIC KEY-----|-----END PUBLIC KEY-----|\s+/gu, "");
  if (!body || body.length > 20_000) throw new Error("GitHub Actions 加密公钥格式无效");
  const der = fromBase64(body);
  if (await sha256Hex(der) !== configuredKeyId) {
    throw new Error("GitHub Actions 加密公钥指纹不匹配");
  }
  return globalThis.crypto.subtle.importKey(
    "spki",
    der,
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"],
  );
}

export async function encryptSubscriptionUrl(subscriptionUrl, id, config, { fetchImpl = globalThis.fetch, baseURI = globalThis.document?.baseURI } = {}) {
  let parsed;
  try {
    parsed = new URL(subscriptionUrl);
  } catch {
    throw new Error("订阅地址格式无效");
  }
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("订阅地址必须是公开 HTTP/HTTPS 地址，且不能携带认证信息");
  }
  const now = Math.floor(Date.now() / 1000);
  const expires = now + 900;
  const aes = await globalThis.crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    true,
    ["encrypt", "decrypt"],
  );
  const rawAes = await globalThis.crypto.subtle.exportKey("raw", aes);
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const plaintext = TEXT_ENCODER.encode(subscriptionUrl);
  const additionalData = TEXT_ENCODER.encode(`${id}:${config.keyId}:${expires}`);
  const ciphertext = await globalThis.crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData },
    aes,
    plaintext,
  );
  const publicKey = await importPublicKey(config, fetchImpl, baseURI);
  const wrappedKey = await globalThis.crypto.subtle.encrypt(
    { name: "RSA-OAEP" },
    publicKey,
    rawAes,
  );
  return {
    v: 1,
    kid: String(config.keyId),
    alg: "RSA-OAEP-3072-SHA256+AES-256-GCM",
    request_id: id,
    issued_at: now,
    expires_at: expires,
    ek: base64Url(wrappedKey),
    iv: base64Url(iv),
    aad: base64Url(additionalData),
    ct: base64Url(ciphertext),
  };
}
