import { fetchHandler } from "./router.js";
import { fail, secureResponse } from "./responses.js";

// Durable Object 类必须从 Worker 入口导出，wrangler.jsonc 的 SCAN_PROGRESS 绑定指向它。
export { ScanProgress } from "./progress.js";

export default {
  async fetch(request, env, ctx) {
    try {
      return await fetchHandler(request, env, ctx);
    } catch (error) {
      return secureResponse(fail(error), { noStore: true });
    }
  },
};
