/* ============================================================================
   IPure 分数色带（逐字移植自旧前端 frontend/src/results.js，分段、插值、通道值都不改）
   ----------------------------------------------------------------------------
   旧前端已删除，这里是唯一实现。

   移植时只保留页面真正用到的三个入口：channels / color / inlineStyle。
   ========================================================================== */

// 色相分段取自 ipure.dev 官网（0 品红 → 25 橙红 → 50 黄 → 75 绿 → 100 青），
// 相邻停靠点之间线性插值、逐通道取整。每个锚点的**色调**是应用自己的配方
// （与 Coffee 的 --good / --warn / --danger 同一亮度带），保证白底可读。
const STOPS = {
  light: [
    [0, [179, 32, 89]],
    [25, [169, 59, 23]],
    [50, [122, 93, 23]],
    [75, [51, 110, 23]],
    [100, [26, 109, 93]],
  ],
  dark: [
    [0, [248, 115, 153]],
    [25, [248, 122, 85]],
    [50, [198, 154, 45]],
    [75, [88, 181, 44]],
    [100, [49, 179, 153]],
  ],
};

const THEMES = ["light", "dark"];
const DEFAULT_THEME = "light";

// 主题 → CSS 自定义属性名。必须显式写出来：styles.css 只读 --sc-l / --sc-d，
// 若按主题名首字母现拼，将来加主题只会写出一组没人读的变量，静默退回浅色通道。
const STYLE_VARS = { light: "--sc-l", dark: "--sc-d" };

// 地区受限不是「分数低」：-1 在 0..100 刻度之外，硬插值会落成 0 分位的品红，
// 等于把「该地区受限」谎报成「分数极低」。保留官网的中性灰色相，只把亮度修到可读。
export const RESTRICTED_SCORE = -1;
export const RESTRICTED_CHANNELS = {
  light: [85, 99, 114],
  dark: [147, 163, 179],
};

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// 唯一的分数取色入口：总分砖、场景 chip 都必须走这里，
// 否则同一个分数会在两处显示成不同颜色。theme 只决定用哪一套墨色，分段与插值两套相同。
export function ipureScoreChannels(score, theme = DEFAULT_THEME) {
  const value = toNumber(score);
  if (value === null) return null;
  if (value === RESTRICTED_SCORE) {
    return RESTRICTED_CHANNELS[theme] ?? RESTRICTED_CHANNELS[DEFAULT_THEME];
  }
  if (value < 0 || value > 100) return null;
  const stops = STOPS[theme] ?? STOPS[DEFAULT_THEME];
  for (let index = 0; index < stops.length - 1; index += 1) {
    const [low, lowChannels] = stops[index];
    const [high, highChannels] = stops[index + 1];
    if (value < low || value > high) continue;
    const ratio = (value - low) / (high - low);
    return lowChannels.map((channel, channelIndex) =>
      Math.round(channel + (highChannels[channelIndex] - channel) * ratio));
  }
  return null;
}

// 只写入两组通道（--sc-l / --sc-d），不写具体颜色：选哪一套、前景/描边/底色各占多少
// 由 styles.css 决定。内联样式拿不到当前主题（深色靠 prefers-color-scheme，切换不会重渲染），
// 所以主题分发必须留在 CSS 里。
export function ipureScoreInlineStyle(score) {
  return THEMES
    .map((theme) => {
      const channels = ipureScoreChannels(score, theme);
      return channels ? `${STYLE_VARS[theme]}:${channels.join(" ")}` : "";
    })
    .filter(Boolean)
    .join(";");
}

// -1 不参与数值比较：受限节点既不是「最低分」，也不该被分数筛选当成低分命中。
export function comparableScore(value) {
  const score = toNumber(value);
  return score === null || score === RESTRICTED_SCORE ? null : score;
}
