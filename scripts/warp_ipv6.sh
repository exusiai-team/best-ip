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
sudo "$wg_bin" set warp private-key "$key_file"   peer "$peer_key" allowed-ips ::/0 persistent-keepalive 25
rm -f "$key_file"
sudo ip -6 address add "${address_v6}/128" dev warp
sudo ip link set warp mtu 1280 up
sudo ip -6 route replace default dev warp

# 先看 WireGuard 握手，再做探测。2026-11-11 一次正式扫描里隧道「建好」却没握手，旧的探测
# 循环（10 次 × 8 秒超时）白等了 89 秒，整轮扫描跟着晚开始，IPv6 节点照样全失败。
# 握手不上就换 WARP 的另一个端点端口（同一台主机，2408 之外还监听 500 / 1701 / 4500，
# UDP 某个端口被路径上丢包时换一个往往就通）。最坏约 4 × 2.5s + 2 × 5s ≈ 20 秒。
handshake() {
  local deadline=$((SECONDS + 3))
  while (( SECONDS < deadline )); do
    if sudo "$wg_bin" show warp latest-handshakes | awk '{exit !($2 > 0)}'; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

for port in 2408 500 1701 4500; do
  sudo "$wg_bin" set warp peer "$peer_key" endpoint "${endpoint_host}:${port}"
  # 让内核立刻发起握手（persistent-keepalive 要等有包才动）。
  ping -6 -c 1 -W 1 2606:4700:4700::1111 >/dev/null 2>&1 || true
  if ! handshake; then
    echo "WARP 端点 ${endpoint_host}:${port} 没有握手，换下一个端口" >&2
    continue
  fi
  for attempt in 1 2; do
    # 验证真的通了：经 IPv6 问 1ms who 自己是谁（不打印完整地址之外的任何东西）。
    if seen="$(curl -6 -fsS -m 5 https://who.1ms.dog/api/me | jq -er '.ip')"; then
      echo "WARP IPv6 出口已就绪（端口 ${port}）：${seen}"
      exit 0
    fi
  done
  echo "WARP 端点 ${endpoint_host}:${port} 已握手但 IPv6 探测失败" >&2
  break
done
echo "WARP IPv6 出口不可用，IPv6 节点本轮会失败" >&2
ip -6 route >&2 || true
exit 1
