(() => {
  const D = document;
  const stage = D.querySelector("[data-ppt-stage]");
  const card = D.querySelector("[data-ppt-card]");
  if (!stage || !card) return;

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
      window.setTimeout(() => { button.textContent = previous; }, 1500);
    } catch (_) {
      window.prompt("复制二维码内容：", value);
    }
  }

  function showMatch(match) {
    notice.replaceChildren();
    notice.classList.remove("hidden", "error");
    notice.classList.add("success");

    const body = D.createElement("div");
    body.className = "inline-task-body";
    addText(body, "div", `检测到${match.label || "课程"}二维码`, "inline-task-message");
    addText(body, "div", "已自动识别二维码，请确认内容后在官方页面继续操作。", "muted");
    notice.append(body);

    const actions = D.createElement("div");
    actions.style.display = "flex";
    actions.style.flexWrap = "wrap";
    actions.style.gap = "8px";
    if (match.action_url) {
      const open = D.createElement("a");
      open.className = "btn small primary";
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

  function showUnavailable(message) {
    notice.replaceChildren();
    notice.classList.remove("hidden", "success");
    notice.classList.add("error");
    const body = D.createElement("div");
    body.className = "inline-task-body";
    addText(body, "div", "直播 PPT 二维码识别组件未就绪", "inline-task-message");
    addText(body, "div", message || "请先安装 rust_module/wxscan_py 中的 PyO3 扩展，然后刷新页面。", "muted");
    notice.append(body);
  }

  function currentLiveEndpoint(slideId) {
    const row = D.querySelector("[data-lesson-row].selected");
    if (!row || row.dataset.live !== "true") return "";
    const base = row.dataset.livePptUrl || "";
    return base ? `${base}/${encodeURIComponent(slideId)}/qr` : "";
  }

  async function scanRequest(item) {
    if (scannerUnavailable || currentLiveEndpoint(item.slideId) !== item.endpoint) return;
    try {
      const response = await fetch(item.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (currentLiveEndpoint(item.slideId) !== item.endpoint) return;
      if (!response.ok) {
        if (response.status === 503) {
          scannerUnavailable = true;
          showUnavailable(payload.detail || "请先构建 wxscan PyO3 扩展。 ");
          return;
        }
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const matches = Array.isArray(payload.matches) ? payload.matches : [];
      const platformMatch = matches.find((candidate) => ["changke", "ketangpai"].includes(candidate.platform));
      if (platformMatch) showMatch(platformMatch);
    } catch (error) {
      console.warn("Live PPT QR scan failed:", error);
    }
  }

  async function drainQueue() {
    if (draining || scannerUnavailable) return;
    draining = true;
    try {
      while (pending.length && !scannerUnavailable) {
        await scanRequest(pending.shift());
      }
    } finally {
      draining = false;
    }
  }

  function queuePage(page) {
    if (scannerUnavailable || !page?.classList?.contains("live-ppt-slide")) return;
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

  new MutationObserver((mutations) => {
    for (const mutation of mutations) mutation.addedNodes.forEach(scanAddedNode);
  }).observe(stage, { childList: true, subtree: true });

  stage.querySelectorAll(".live-ppt-slide").forEach(queuePage);
  D.querySelectorAll("[data-lesson-row]").forEach((row) => row.addEventListener("click", hideNotice));
})();
