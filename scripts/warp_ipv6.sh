#!/usr/bin/env bash
# 给 GitHub 托管 runner 补一条 IPv6 出口：只把 ::/0 接进 Cloudflare WARP，IPv4 原样直连。
#
# 托管 runner 没有 IPv6 默认路由（actions/runner-images#668，官方 wontfix），server 是
# IPv6 字面量或只有 AAAA 的节点因此全部「网络不可达」。这里每次运行现注册一个免费 WARP
# 账户（自己生成 WireGuard 密钥，不用第三方 action 里写死、所有人共用的私钥），起一个
# 只承载 IPv6 的 wg 接口。失败不影响扫描：IPv6 节点照旧失败，其余节点不受影响。
#
# 为什么不 apt-get install wireguard-tools、不用 wg-quick：安装要跑 dpkg 触发器，实测占整步
# 9~16 秒里的大头。这里只要 wg 这一个二进制：先用缓存目录里的（$BEST_IP_TOOLS_DIR，随
# Python 环境按天缓存），没有就 apt-get download 后直接解包取出。wg-quick 的策略路由是为了
# 防止隧道流量绕回自己；这里端点是 IPv4、隧道只接 IPv6，一条 ::/0 路由就够。
set -euo pipefail

tools_dir="${BEST_IP_TOOLS_DIR:-$HOME/.best-ip/bin}"
wg_bin="$(command -v wg || true)"
if [[ -z "$wg_bin" && -x "$tools_dir/wg" ]]; then
  wg_bin="$tools_dir/wg"
fi
if [[ -z "$wg_bin" ]]; then
  scratch="$(mktemp -d)"
  if ! (cd "$scratch" && apt-get download wireguard-tools >/dev/null 2>&1); then
    sudo apt-get update -qq
    (cd "$scratch" && apt-get download wireguard-tools >/dev/null)
  fi
  dpkg-deb -x "$scratch"/wireguard-tools_*.deb "$scratch/root"
  mkdir -p "$tools_dir"
  install -m 0755 "$scratch/root/usr/bin/wg" "$tools_dir/wg"
  rm -rf "$scratch"
  wg_bin="$tools_dir/wg"
fi

umask 077
private_key="$("$wg_bin" genkey)"
public_key="$(printf '%s' "$private_key" | "$wg_bin" pubkey)"
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
key_file="$(mktemp)"
printf '%s' "$private_key" > "$key_file"
sudo ip link add warp type wireguard
sudo "$wg_bin" set warp private-key "$key_file" \
  peer "$peer_key" allowed-ips ::/0 endpoint "${endpoint_host}:2408" persistent-keepalive 25
rm -f "$key_file"
sudo ip -6 address add "${address_v6}/128" dev warp
sudo ip link set warp mtu 1280 up
sudo ip -6 route replace default dev warp

# 验证真的通了：经 IPv6 问 1ms who 自己是谁（不打印完整地址之外的任何东西）。
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if seen="$(curl -6 -fsS -m 8 https://who.1ms.dog/api/me | jq -er '.ip')"; then
    echo "WARP IPv6 出口已就绪：${seen}"
    exit 0
  fi
  sleep 1
done
echo "WARP 隧道已建立但 IPv6 探测失败" >&2
ip -6 route >&2 || true
exit 1
