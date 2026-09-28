(() => {
  const D = document;
  const stage = D.querySelector("[data-ppt-stage]");
  const card = D.querySelector("[data-ppt-card]");
  if (!stage || !card) return;

  const pageRuntime = window.FuckClassroomPage?.current?.();
  const pageSignal = pageRuntime?.signal;
  const schedule = pageRuntime?.setTimeout
    ? (callback, delay) => pageRuntime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);
  const disposed = () => Boolean(pageSignal?.aborted || !stage.isConnected);
  const pageFetch = (input, init = {}) => window.fetch(input, { ...init, signal: pageSignal });

  const scheduled = new Set();
  const pending = [];
  const MAX_PENDING_PAGES = 4;
  let draining = false;
  let scannerUnavailable = false;

  const notice = D.createElement("div");
  notice.className = "notice qr-assistant-notice hidden";
  notice.setAttribute("role", "status");
  notice.setAttribute("aria-live", "polite");
  const header = card.querySelector(".course-media-card-header");
  if (header) header.after(notice);
  else card.prepend(notice);

  function hideNotice() {
    notice.classList.add("hidden");
    notice.replaceChildren();
  }

  function addText(container, tag, text, className = "") {
    const node = D.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    container.append(node);
    return node;
  }

  async function copyText(value, button) {
    try {
      await navigator.clipboard.writeText(value);
      const previous = button.textContent;
      button.textContent = "已复制";
      schedule(() => { if (button.isConnected) button.textContent = previous; }, 1500);
    } catch (_) {
      window.prompt("复制二维码内容：", value);
    }
  }

  function showMatch(match, item) {
    const sign = match.sign || null;
    const attempted = Boolean(sign?.attempted);
    const successful = Boolean(sign?.success);
    notice.replaceChildren();
    notice.classList.remove("hidden", "success", "error");
    notice.classList.add(attempted && !successful ? "error" : "success");

    const body = D.createElement("div");
    body.className = "inline-task-body";
    const title = successful
      ? "签到成功"
      : (attempted ? "签到未完成" : `检测到${match.label || "课程"}二维码`);
    const detail = sign?.message
      || "已自动识别二维码，请在官方页面继续操作。";
    addText(body, "div", title, "inline-task-message");
    addText(body, "div", detail, "muted");
    notice.append(body);

    const actions = D.createElement("div");
    actions.style.display = "flex";
    actions.style.flexWrap = "wrap";
    actions.style.gap = "8px";
    if (sign?.retryable && item) {
      const retry = D.createElement("button");
      retry.type = "button";
      retry.className = "btn small primary";
      retry.textContent = "重新尝试";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        retry.textContent = "正在重试";
        await scanRequest(item);
      });
      actions.append(retry);
    }
    if (sign?.account_url) {
      const account = D.createElement("a");
      account.className = "btn small primary";
      account.href = sign.account_url;
      account.textContent = "登录课堂派";
      actions.append(account);
    }
    if (match.action_url) {
      const open = D.createElement("a");
      open.className = `btn small${attempted ? "" : " primary"}`;
      open.href = match.action_url;
      open.target = "_blank";
      open.rel = "noreferrer noopener";
      open.textContent = "打开官方页面";
      actions.append(open);
    }
    const copy = D.createElement("button");
    copy.type = "button";
    copy.className = "btn small";
    copy.textContent = "复制二维码内容";
    copy.addEventListener("click", () => copyText(match.raw || "", copy));
    actions.append(copy);
    notice.append(actions);
  }

  function showRequestError(message) {
    notice.replaceChildren();
    notice.classList.remove("hidden", "success");
    notice.classList.add("error");
    const body = D.createElement("div");
    body.className = "inline-task-body";
    addText(body, "div", "二维码处理失败", "inline-task-message");
    addText(body, "div", message || "请稍后重试。", "muted");
    notice.append(body);
  }

  function showUnavailable(message) {
    notice.replaceChildren();
    notice.classList.remove("hidden", "success");
    notice.classList.add("error");
    const body = D.createElement("div");
    body.className = "inline-task-body";
    addText(body, "div", "直播 PPT 二维码识别组件未就绪", "inline-task-message");
    addText(body, "div", message || "请重新安装项目依赖或构建 wxscan 扩展，然后刷新页面。", "muted");
    notice.append(body);
  }

  function currentLiveEndpoint(slideId) {
    const row = D.querySelector("[data-lesson-row].selected");
    if (!row || row.dataset.live !== "true") return "";
    const slidesUrl = row.dataset.pptSlidesUrl || "";
    const base = row.dataset.livePptUrl
      || slidesUrl.replace(/\/ppt\/slides\/?$/, "/live/ppt");
    return base ? `${base.replace(/\/$/, "")}/${encodeURIComponent(slideId)}/qr` : "";
  }

  async function scanRequest(item) {
    if (disposed() || scannerUnavailable || currentLiveEndpoint(item.slideId) !== item.endpoint) return;
    try {
      const response = await pageFetch(item.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (disposed() || currentLiveEndpoint(item.slideId) !== item.endpoint) return;
      if (!response.ok) {
        if (response.status === 503) {
          scannerUnavailable = true;
          showUnavailable(payload.detail || "请重新安装项目依赖或构建 wxscan 扩展。");
          return;
        }
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const matches = Array.isArray(payload.matches) ? payload.matches : [];
      const platformMatches = matches.filter((candidate) => ["changke", "ketangpai"].includes(candidate.platform));
      const platformMatch = platformMatches.find((candidate) => candidate.sign?.success)
        || platformMatches.find((candidate) => candidate.sign?.attempted)
        || platformMatches[0];
      if (platformMatch) showMatch(platformMatch, item);
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      console.warn("Live PPT QR scan failed:", error);
      showRequestError(error?.message || "请稍后重试。");
    }
  }

  async function drainQueue() {
    if (disposed() || draining || scannerUnavailable) return;
    draining = true;
    try {
      while (!disposed() && pending.length && !scannerUnavailable) {
        // QR tokens can rotate every few seconds, so always prefer the newest PPT snapshot.
        await scanRequest(pending.pop());
      }
    } finally {
      draining = false;
    }
  }

  function queuePage(page) {
    if (disposed() || scannerUnavailable || !page?.classList?.contains("live-ppt-slide")) return;
    const slideId = page.dataset.liveSlideId;
    if (!slideId) return;
    const endpoint = currentLiveEndpoint(slideId);
    if (!endpoint || scheduled.has(endpoint)) return;
    scheduled.add(endpoint);
    pending.push({ slideId, endpoint });
    if (pending.length > MAX_PENDING_PAGES) pending.splice(0, pending.length - MAX_PENDING_PAGES);
    drainQueue();
  }

  function scanAddedNode(node) {
    if (!(node instanceof Element)) return;
    if (node.matches(".live-ppt-slide")) queuePage(node);
    node.querySelectorAll?.(".live-ppt-slide").forEach(queuePage);
  }

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) mutation.addedNodes.forEach(scanAddedNode);
  });
  observer.observe(stage, { childList: true, subtree: true });

  stage.querySelectorAll(".live-ppt-slide").forEach(queuePage);
  D.querySelectorAll("[data-lesson-row]").forEach((row) => row.addEventListener("click", hideNotice));
  const cleanupPage = () => {
    observer.disconnect();
    pending.length = 0;
    scheduled.clear();
  };
  pageRuntime?.onDispose?.(cleanupPage);
  D.addEventListener("academic:page-before-swap", cleanupPage, { once: true, signal: pageSignal });
})();
