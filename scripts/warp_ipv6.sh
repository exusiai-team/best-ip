#!/usr/bin/env bash
# 给 GitHub 托管 runner 补一条 IPv6 出口：只把 ::/0 接进 Cloudflare WARP，IPv4 原样直连。
#
# 托管 runner 没有 IPv6 默认路由（actions/runner-images#668，官方 wontfix），server 是
# IPv6 字面量或只有 AAAA 的节点因此全部「网络不可达」。这里每次运行现注册一个免费 WARP
# 账户（自己生成 WireGuard 密钥，不用第三方 action 里写死、所有人共用的私钥），起一个
# 只承载 IPv6 的 wg 接口。失败不影响扫描：IPv6 节点照旧失败，其余节点不受影响。
set -euo pipefail

if ! command -v wg >/dev/null 2>&1; then
  sudo apt-get install -y --no-install-recommends wireguard-tools >/dev/null 2>&1 \
    || { sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends wireguard-tools >/dev/null; }
fi

umask 077
private_key="$(wg genkey)"
public_key="$(printf '%s' "$private_key" | wg pubkey)"
body="$(jq -nc --arg key "$public_key" --arg tos "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
  '{key: $key, install_id: "", fcm_token: "", tos: $tos, type: "Linux", locale: "en_US"}')"

registration=""
for attempt in 1 2 3; do
  if registration="$(curl -fsS -m 20 -X POST https://api.cloudflareclient.com/v0a2158/reg \
      -H 'Content-Type: application/json' -H 'User-Agent: okhttp/3.12.1' \
      -H 'CF-Client-Version: a-6.30-2158' --data "$body")"; then
    break
  fi
  registration=""
  sleep "$attempt"
done
test -n "$registration"

peer_key="$(jq -er '.config.peers[0].public_key' <<<"$registration")"
address_v6="$(jq -er '.config.interface.addresses.v6' <<<"$registration")"
endpoint_v4="$(jq -er '.config.peers[0].endpoint.v4' <<<"$registration")"
endpoint_host="${endpoint_v4%:*}"

# 端点用 IPv4 字面量：隧道本身走 runner 的 IPv4，不能依赖还不存在的 IPv6。
sudo tee /etc/wireguard/warp.conf >/dev/null <<EOF
[Interface]
PrivateKey = ${private_key}
Address = ${address_v6}/128
MTU = 1280

[Peer]
PublicKey = ${peer_key}
AllowedIPs = ::/0
Endpoint = ${endpoint_host}:2408
PersistentKeepalive = 25
EOF
sudo wg-quick up warp >/dev/null

# 验证真的通了：经 IPv6 问 1ms who 自己是谁（不打印完整地址之外的任何东西）。
for attempt in 1 2 3 4 5; do
  if seen="$(curl -6 -fsS -m 8 https://who.1ms.dog/api/me | jq -er '.ip')"; then
    echo "WARP IPv6 出口已就绪：${seen}"
    exit 0
  fi
  sleep 2
done
echo "WARP 隧道已建立但 IPv6 探测失败" >&2
ip -6 route >&2 || true
exit 1
