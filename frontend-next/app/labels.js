/* 展示文案的唯一来源：记录映射、渲染、检索与排序都从这里取，避免同一个词写出两种叫法。 */

// IPure 六项场景，顺序与 id 对齐 https://ipure.dev/docs/api 的 scenarios[].id
export const SCENARIO_LABELS = [
  ["ai", "AI"],
  ["social", "社交"],
  ["streaming", "流媒体"],
  ["gaming", "游戏"],
  ["ecommerce", "电商"],
  ["email", "邮件"],
];

// 1ms IPQS 多源共识的接入类型（backend/app/sources/ipqs.py 的 IPQS_TYPES）。
// tone 决定药丸颜色：住宅与移动是「干净的家宽出口」，机房是主要风险信号，其余只做中性说明。
export const IPQS_TYPE_LABELS = {
  residential: { label: "住宅", tone: "good" },
  mobile: { label: "移动", tone: "good" },
  hosting: { label: "机房", tone: "warn" },
  business: { label: "商业", tone: "neutral" },
  education: { label: "教育", tone: "neutral" },
  government: { label: "政府", tone: "neutral" },
};

// 1ms 评分的扣分项（done.parts[].k 去掉 p_ 前缀）。
export const IPQS_DEDUCTION_LABELS = {
  hosting: "机房 IP",
  business: "商业网络",
  proxy: "代理标记",
  vpn: "VPN 标记",
  tor: "Tor 出口",
  abuser: "滥用记录",
  risk: "第三方风险分",
  dnsbl: "DNS 黑名单",
};

export const STATUS_LABELS = { success: "完整", partial: "部分", failed: "失败" };
