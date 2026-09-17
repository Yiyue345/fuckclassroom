(() => {
  const D = document;
  const stage = D.querySelector("[data-ppt-stage]");
  const card = D.querySelector("[data-ppt-card]");
  if (!stage || !card) return;

  const scanned = new Set();
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
    body.className = "qr-assistant-body";
    addText(body, "strong", `检测到${match.label || "签到"}二维码`);
    addText(
      body,
      "span",
      "已自动识别二维码，请确认你正在课堂后，在官方页面或客户端完成签到。",
      "muted",
    );
    notice.append(body);

    const actions = D.createElement("div");
    actions.className = "qr-assistant-actions";
    if (match.action_url) {
      const open = D.createElement("a");
      open.className = "btn small primary";
      open.href = match.action_url;
      open.target = "_blank";
      open.rel = "noreferrer noopener";
      open.textContent = "打开官方签到";
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
    addText(notice, "strong", "直播 PPT 二维码识别组件未就绪");
    addText(notice, "span", message || "请按 docs/QR_ASSISTANT.md 构建 PyO3 扩展。", "muted");
  }

  function currentLiveEndpoint(slideId) {
    const row = D.querySelector("[data-lesson-row].selected");
    if (!row || row.dataset.live !== "true") return "";
    const base = row.dataset.livePptUrl || "";
    return base ? `${base}/${encodeURIComponent(slideId)}/qr` : "";
  }

  async function scanPage(page) {
    if (scannerUnavailable || !page?.classList?.contains("live-ppt-slide")) return;
    const slideId = page.dataset.liveSlideId;
    if (!slideId) return;
    const endpoint = currentLiveEndpoint(slideId);
    if (!endpoint) return;
    const key = `${endpoint}`;
    if (scanned.has(key)) return;
    scanned.add(key);

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 503) {
          scannerUnavailable = true;
          showUnavailable(payload.detail || "请先构建 wxscan PyO3 扩展。 ");
          return;
        }
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const matches = Array.isArray(payload.matches) ? payload.matches : [];
      const signMatch = matches.find((item) => ["changke", "ketangpai"].includes(item.platform));
      if (signMatch) showMatch(signMatch);
    } catch (error) {
      console.warn("Live PPT QR scan failed:", error);
    }
  }

  function scanAddedNode(node) {
    if (!(node instanceof Element)) return;
    if (node.matches(".live-ppt-slide")) scanPage(node);
    node.querySelectorAll?.(".live-ppt-slide").forEach(scanPage);
  }

  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      mutation.addedNodes.forEach(scanAddedNode);
    }
  }).observe(stage, { childList: true, subtree: true });

  stage.querySelectorAll(".live-ppt-slide").forEach(scanPage);
  D.querySelectorAll("[data-lesson-row]").forEach((row) => {
    row.addEventListener("click", hideNotice);
  });
})();
