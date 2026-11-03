/* ============================================================================
   合成示例数据：静态设计示例的唯一数据源，页面不发起任何网络请求
   ----------------------------------------------------------------------------
   IP 全部取自 RFC 5737 文档网段，节点名、ASN、评分都是编的。形状与 app/records.js
   的 toRow() 输出一致，渲染器不区分示例与真实数据。示例刻意覆盖这些边界：
   完整 / 部分 / 失败、IPure 总分为 null、场景 -1（受限）与 null（无数据）、
   1ms 的住宅 / 移动 / 机房 / 商业四种类型与原生 / 广播 / 未知三态。报告链接留空：
   文档网段的 IP 在第三方站点上查不到东西。
   ========================================================================== */

export const SNAPSHOT_META = {
  // 固定值而不是 new Date()：示例每次打开都长一样，截图才有可比性。
  generatedAt: "2026-11-03 11:40",
  source: "示例数据",
};

const BASE = {
  status: "success",
  ipqs_deductions: [],
  ipqs_report_url: null,
  ipure_report_url: null,
  error: null,
  error_step: null,
};

function node(fields) {
  return { ...BASE, ...fields, ipqs_country: fields.ipqs_country ?? null, ipqs_registered_country: fields.ipqs_registered_country ?? null };
}

function failed(fields) {
  return {
    ...BASE,
    status: "failed",
    exit_ip: null,
    country: null,
    city: null,
    ipqs_score: null,
    ipqs_grade: null,
    ipqs_type: null,
    ipqs_native: null,
    ipqs_country: null,
    ipqs_registered_country: null,
    coffee_score: null,
    score: null,
    ipure_scores: null,
    ...fields,
  };
}

export const NODES = [
  node({
    node: "东京 NTT 01", type: "hysteria2", exit_ip: "192.0.2.18", country: "日本", city: "东京",
    isp: "NTT Communications", asn: 4713,
    ipqs_score: 100, ipqs_grade: "A", ipqs_type: "residential", ipqs_native: true, ipqs_country: "JP", ipqs_registered_country: "JP",
    coffee_score: 92, score: 91,
    ipure_scores: { ai: 90, social: 85, streaming: 92, gaming: 88, ecommerce: 86, email: 84 },
  }),
  node({
    node: "大阪 IIJ 02", type: "vmess", exit_ip: "192.0.2.44", country: "日本", city: "大阪",
    isp: "Internet Initiative Japan", asn: 2497,
    ipqs_score: 80, ipqs_grade: "B", ipqs_type: "hosting", ipqs_native: true, ipqs_country: "JP", ipqs_registered_country: "JP",
    ipqs_deductions: [{ key: "hosting", points: 20 }],
    coffee_score: 88, score: 87,
    ipure_scores: { ai: 88, social: 82, streaming: 90, gaming: 85, ecommerce: 83, email: 80 },
  }),
  node({
    node: "新加坡 SG-01", type: "trojan", exit_ip: "198.51.100.7", country: "新加坡", city: "新加坡",
    isp: "Singtel", asn: 3758,
    ipqs_score: 95, ipqs_grade: "A", ipqs_type: "mobile", ipqs_native: true, ipqs_country: "SG", ipqs_registered_country: "SG",
    ipqs_deductions: [{ key: "risk", points: 5 }],
    coffee_score: 86, score: 89,
    ipure_scores: { ai: 92, social: 88, streaming: 86, gaming: 90, ecommerce: 88, email: 86 },
  }),
  node({
    node: "香港 HKT-02", type: "shadowsocks", exit_ip: "203.0.113.11", country: "中国香港", city: "香港",
    isp: "HKT Limited", asn: 4760,
    ipqs_score: 85, ipqs_grade: "A", ipqs_type: "residential", ipqs_native: true, ipqs_country: "HK", ipqs_registered_country: "HK",
    ipqs_deductions: [{ key: "vpn", points: 15 }],
    coffee_score: 84, score: 82,
    ipure_scores: { ai: 84, social: 80, streaming: 83, gaming: 79, ecommerce: 81, email: 77 },
  }),
  node({
    node: "首尔 KT-01", type: "trojan", exit_ip: "203.0.113.29", country: "韩国", city: "首尔",
    isp: "Korea Telecom", asn: 4766,
    ipqs_score: 92, ipqs_grade: "A", ipqs_type: "residential", ipqs_native: true, ipqs_country: "KR", ipqs_registered_country: "KR",
    ipqs_deductions: [{ key: "vpn", points: 8 }],
    coffee_score: 80, score: 79,
    // 电商 -1：该地区受限，不是低分，不参与排序。
    ipure_scores: { ai: 78, social: 74, streaming: 76, gaming: 72, ecommerce: -1, email: 70 },
  }),
  node({
    node: "伦敦 BT-01", type: "hysteria2", exit_ip: "198.51.100.120", country: "英国", city: "伦敦",
    isp: "British Telecommunications", asn: 2856,
    ipqs_score: 100, ipqs_grade: "A", ipqs_type: "residential", ipqs_native: true, ipqs_country: "GB", ipqs_registered_country: "GB",
    coffee_score: 78, score: 81,
    ipure_scores: { ai: 82, social: 79, streaming: 84, gaming: 76, ecommerce: 78, email: 75 },
  }),
  node({
    node: "阿姆斯特丹 LeaseWeb-01", type: "vmess", exit_ip: "203.0.113.77", country: "荷兰", city: "阿姆斯特丹",
    isp: "LeaseWeb", asn: 28753,
    ipqs_score: 60, ipqs_grade: "C", ipqs_type: "hosting", ipqs_native: false, ipqs_country: "NL", ipqs_registered_country: "DE",
    ipqs_deductions: [{ key: "hosting", points: 20 }, { key: "proxy", points: 20 }],
    coffee_score: 74, score: 72,
    ipure_scores: { ai: 73, social: 68, streaming: 75, gaming: 66, ecommerce: 70, email: 65 },
  }),
  node({
    node: "洛杉矶 ColoCrossing-03", type: "hysteria2", status: "partial", exit_ip: "198.51.100.66", country: "美国", city: "洛杉矶",
    isp: "ColoCrossing", asn: 36352,
    ipqs_score: 41, ipqs_grade: "D", ipqs_type: "hosting", ipqs_native: false, ipqs_country: "US", ipqs_registered_country: "CA",
    ipqs_deductions: [{ key: "hosting", points: 20 }, { key: "proxy", points: 20 }, { key: "vpn", points: 13 }, { key: "dnsbl", points: 6 }],
    coffee_score: 71, score: 64,
    // 电商是上游给的 -1（受限），邮件是上游没给值（无数据）。
    ipure_scores: { ai: 70, social: 66, streaming: 72, gaming: 61, ecommerce: -1, email: null },
  }),
  node({
    node: "多伦多 Bell-01", type: "trojan", status: "partial", exit_ip: "192.0.2.150", country: "加拿大", city: "多伦多",
    isp: "Bell Canada", asn: 577,
    ipqs_score: 90, ipqs_grade: "A", ipqs_type: "residential", ipqs_native: null, ipqs_country: "CA",
    ipqs_deductions: [{ key: "risk", points: 10 }],
    coffee_score: 69,
    // IPure 没返回纯净度总分：如实标记为「部分」，不伪造 0 分。
    score: null,
    ipure_scores: { ai: 68, social: 64, streaming: 70, gaming: 62, ecommerce: 66, email: null },
  }),
  node({
    node: "法兰克福 Hetzner-01", type: "vmess", status: "partial", exit_ip: "192.0.2.101", country: "德国", city: "法兰克福",
    isp: "Hetzner Online", asn: 24940,
    ipqs_score: 76, ipqs_grade: "B", ipqs_type: "business", ipqs_native: true, ipqs_country: "DE", ipqs_registered_country: "DE",
    ipqs_deductions: [{ key: "business", points: 4 }, { key: "risk", points: 20 }],
    coffee_score: 63, score: 58,
    ipure_scores: { ai: 60, social: 55, streaming: 62, gaming: 52, ecommerce: 57, email: 54 },
  }),
  failed({
    node: "台北 CHT-01", type: "shadowsocks", isp: "Chunghwa Telecom", asn: 3462,
    error: "Mihomo 启动后 30 秒内未建立任何可用连接", error_step: "节点连接",
  }),
  failed({
    node: "悉尼 Vocus-01", type: "vmess", isp: "Vocus", asn: 4826,
    error: "握手超时：3 次尝试全部失败", error_step: "出口 IP 确认",
  }),
];
