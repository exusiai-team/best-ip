/* ============================================================================
   页内发起扫描：订阅地址输入、开始 / 停止、一行进度
   ----------------------------------------------------------------------------
   加密、派发、轮询、取产物都在 app/scan.js；这里只管「什么时候调它」和「把话说给谁看」。
   同一时刻只允许一次扫描：generation 作废上一轮还在路上的轮询。扫描凭证只在内存里，
   刷新页面即丢弃。表格怎么换由调用方的 view 决定（见 main.js）。
   ========================================================================== */

import {
  startScan, pollScan, cancelScan, validateSubscriptionUrl, SCAN_POLL_FIRST_MS, SCAN_POLL_MAX_MS,
} from "./scan.js";

/**
 * @param {object} view
 * @param {(title: string, desc: string) => void} view.showPending 清表并显示一句状态
 * @param {(payload: object) => void} view.showPayload 用扫描产物替换整张表
 * @param {() => number} view.nodeCount
 * @param {(message: string) => void} view.toast
 */
export function initScanUi(view) {
  const ui = {
    form: document.getElementById("scanForm"),
    url: document.getElementById("subscriptionUrl"),
    start: document.getElementById("startScan"),
    stop: document.getElementById("stopScan"),
    progress: document.getElementById("scanProgress"),
  };
  if (!ui.form) return;

  const scan = {
    session: null,
    generation: 0,
    timer: null,
    delay: SCAN_POLL_FIRST_MS,
    // starting 是同步闸：加密 + POST 期间 session 还是 null，只靠它挡不住连点两次。
    starting: false,
    cancelling: false,
    pendingCancel: false,
  };

  function setProgress(text) {
    if (!ui.progress) return;
    ui.progress.textContent = text;
    ui.progress.hidden = !text;
  }

  function setBusy(busy) {
    if (ui.start) ui.start.disabled = busy;
    if (ui.url) ui.url.disabled = busy;
    if (ui.stop) {
      ui.stop.hidden = !busy;
      ui.stop.disabled = false;
    }
  }

  function stopPolling() {
    if (scan.timer !== null) clearTimeout(scan.timer);
    scan.timer = null;
  }

  function resetFlags() {
    scan.starting = false;
    scan.cancelling = false;
    scan.pendingCancel = false;
  }

  function finish() {
    stopPolling();
    scan.session = null;
    resetFlags();
    setBusy(false);
  }

  /**
   * keepSession：「等不到确认」的两种情形（cancel_timeout / scan_deadline）任务可能还在跑，
   * 会话与「停止」按钮都要留着，文案里那句「请再点一次停止」才兑现得了。
   * keepTable：派发就没被接受时表里还是上一轮的结果，没理由连带丢掉。
   */
  function fail(message, { keepSession = false, keepTable = false } = {}) {
    if (keepSession && scan.session) {
      stopPolling();
      resetFlags();
      setBusy(true);
    } else {
      finish();
      if (!keepTable) view.showPending("扫描没有成功", `${message}。稍后可以再点一次「开始扫描」。`);
    }
    setProgress(message);
    view.toast(`扫描失败：${message}`);
  }

  function nextDelay() {
    scan.delay = Math.min(SCAN_POLL_MAX_MS, Math.round(scan.delay * 1.5));
    return scan.delay;
  }

  function schedulePoll(generation, delay) {
    stopPolling();
    scan.timer = setTimeout(() => {
      pollOnce(generation).catch((error) => {
        if (generation === scan.generation) fail(error?.message || "扫描状态读取失败");
      });
    }, delay);
  }

  async function begin() {
    const url = String(ui.url?.value || "").trim();
    try {
      validateSubscriptionUrl(url);
    } catch (error) {
      // 地址写错时不动表格，只说原因。
      const message = error?.message || String(error);
      view.toast(message);
      setProgress(message);
      ui.url?.focus();
      return;
    }
    scan.generation += 1;
    const generation = scan.generation;
    stopPolling();
    scan.delay = SCAN_POLL_FIRST_MS;
    scan.session = null;
    resetFlags();
    scan.starting = true;
    setBusy(true);
    setProgress("正在加密订阅地址并交给扫描网关…");
    let session;
    try {
      session = await startScan(url, globalThis.BEST_IP_CONFIG, {});
    } catch (error) {
      if (generation === scan.generation) fail(error?.message || String(error), { keepTable: true });
      return;
    }
    if (generation !== scan.generation) return;
    scan.session = session;
    scan.starting = false;
    // 派发被接受（202）之后才清表：网关拒绝时用户不该丢掉上一屏结果。
    view.showPending("正在扫描…", "扫描完成后这张表会自动填上刚扫出来的节点。");
    if (scan.pendingCancel) sendStop(generation);
    else schedulePoll(generation, SCAN_POLL_FIRST_MS);
  }

  async function pollOnce(generation) {
    if (generation !== scan.generation || !scan.session) return;
    let state;
    try {
      state = await pollScan(scan.session, {});
    } catch (error) {
      if (generation !== scan.generation) return;
      if (error.retryable !== true) {
        const keepSession = error.code === "cancel_timeout" || error.code === "scan_deadline";
        fail(error?.message || String(error), { keepSession });
        return;
      }
      setProgress(`读取扫描状态失败，正在重试：${error?.message || String(error)}`);
      schedulePoll(generation, nextDelay());
      return;
    }
    if (generation !== scan.generation) return;
    setProgress(state.detail ? `${state.label} · ${state.detail}` : state.label);
    if (state.done) {
      if (state.failure) {
        const stopped = Boolean(scan.session?.cancelRequestedAt) && state.status === "cancelled";
        fail(stopped ? "已按你的请求停止这次扫描，没有可显示的结果" : state.failure);
        return;
      }
      finish();
      view.showPayload(state.payload);
      view.toast(`扫描完成：已显示刚扫出来的 ${view.nodeCount()} 个节点。`);
      return;
    }
    if (scan.pendingCancel) {
      await requestStop(generation);
      return;
    }
    schedulePoll(generation, nextDelay());
  }

  async function requestStop(generation) {
    if (!scan.session) return;
    scan.pendingCancel = false;
    try {
      const result = await cancelScan(scan.session, {});
      if (generation !== scan.generation || !scan.session) return;
      if (result.requested === false) {
        // 运行还没出现：等下一轮轮询看到 run 再补发。
        scan.pendingCancel = true;
        setProgress("扫描运行还没建立，停止请求会在它出现后立刻补发…");
      } else {
        scan.cancelling = false;
        setProgress("已请求停止，等待执行端确认结束（取消不等于清理已完成）…");
      }
    } catch (error) {
      if (generation !== scan.generation || !scan.session) return;
      // 一次没发出去不能把按钮废掉：复位后用户才点得动第二次。
      scan.cancelling = false;
      scan.pendingCancel = true;
      setProgress(`停止失败：${error?.message || String(error)}`);
    }
    if (generation === scan.generation && scan.session) schedulePoll(generation, nextDelay());
  }

  function sendStop(generation) {
    requestStop(generation).catch((error) => {
      if (generation !== scan.generation) return;
      scan.cancelling = false;
      scan.pendingCancel = false;
      setProgress(`停止失败：${error?.message || String(error)}`);
    });
  }

  function onStop() {
    if (!scan.session) {
      // 提交窗口里会话还没到手：记下来，202 一到立刻发停止。
      if (scan.starting) {
        scan.pendingCancel = true;
        scan.cancelling = true;
        setProgress("扫描正在提交，已记下停止请求：一旦开始运行就立刻停止…");
      }
      return;
    }
    if (scan.cancelling) return;
    stopPolling();
    scan.cancelling = true;
    scan.pendingCancel = true;
    setProgress("正在请求停止…");
    sendStop(scan.generation);
  }

  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!scan.session && !scan.starting) begin();
  });
  ui.stop?.addEventListener("click", onStop);

  ui.form.hidden = false;
  // 没有扫描公钥时按钮必然被网关拒绝：直接关掉并说明原因。
  if (!/^[a-f0-9]{64}$/u.test(String(globalThis.BEST_IP_CONFIG?.keyId || ""))) {
    if (ui.start) ui.start.disabled = true;
    setProgress("本站没有配置扫描公钥（SCAN_KEY_ID），这个页面暂时发不了扫描。");
  }
}
