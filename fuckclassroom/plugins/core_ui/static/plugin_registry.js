(() => {
  const market = document.querySelector(".plugin-market-shell");
  if (!market) return;

  const pageRuntime = window.FuckClassroomPage?.current?.();
  const pageSignal = pageRuntime?.signal;
  const schedule = pageRuntime?.setTimeout
    ? (callback, delay) => pageRuntime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);

  const disposed = () => Boolean(pageSignal?.aborted);
  const clamp = (value) => Math.min(100, Math.max(0, Number(value) || 0));
  const terminal = new Set(["succeeded", "failed", "cancelled"]);
  const circumference = 2 * Math.PI * 18;

  async function readResponse(response) {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = typeof body.detail === "string"
        ? body.detail
        : `操作失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return body;
  }

  function cardUi(form) {
    const card = form.closest("[data-market-plugin]");
    return {
      card,
      form,
      button: form.querySelector("[data-market-install-button]"),
      state: card?.querySelector("[data-market-download-state]"),
      ring: card?.querySelector("[data-market-progress]"),
      arc: card?.querySelector("[data-market-progress-arc]"),
      percent: card?.querySelector("[data-market-percent]"),
      done: card?.querySelector("[data-market-done]"),
      note: card?.querySelector("[data-market-download-note]"),
      error: card?.querySelector("[data-market-download-error]"),
      status: card?.querySelector("[data-market-status-label]"),
      installed: card?.querySelector("[data-market-installed-version]"),
      progress: 0,
      running: false,
    };
  }

  function showProgress(ui, percent) {
    ui.progress = Math.max(ui.progress, clamp(percent));
    if (ui.arc) ui.arc.style.strokeDashoffset = String(circumference * (1 - ui.progress / 100));
    if (ui.percent) ui.percent.textContent = `${Math.round(ui.progress)}%`;
    if (ui.ring) ui.ring.setAttribute("aria-valuenow", String(Math.round(ui.progress)));
  }

  function begin(ui) {
    ui.running = true;
    ui.progress = 0;
    ui.button.disabled = true;
    ui.button.hidden = true;
    ui.state.hidden = false;
    ui.ring.hidden = false;
    ui.done.hidden = true;
    ui.note.hidden = false;
    ui.note.textContent = "正在准备下载…";
    ui.error.hidden = true;
    ui.error.textContent = "";
    if (ui.status) ui.status.textContent = "正在准备下载";
    showProgress(ui, 0);
  }

  function finish(ui, task) {
    if (disposed()) return;
    if (task.status === "succeeded") {
      showProgress(ui, 100);
      ui.note.textContent = "安装完成，默认保持停用";
      if (ui.status) ui.status.textContent = "下载并安装成功";
      if (ui.installed) ui.installed.textContent = `v${ui.card.dataset.marketVersion}`;
      // Let the circle reach 100% before replacing it with a success tick.
      schedule(() => {
        if (disposed()) return;
        ui.ring.hidden = true;
        ui.done.hidden = false;
        ui.running = false;
      }, 350);
      return;
    }
    const message = task.error || (task.status === "cancelled" ? "安装任务已取消" : "安装失败，请重试");
    fail(ui, message);
  }

  function fail(ui, message) {
    if (disposed()) return;
    ui.running = false;
    ui.state.hidden = true;
    ui.ring.hidden = false;
    ui.done.hidden = true;
    ui.button.hidden = false;
    ui.button.disabled = false;
    ui.note.hidden = true;
    ui.error.textContent = message;
    ui.error.hidden = false;
  }

  async function poll(ui, statusUrl, failures = 0) {
    if (disposed()) return;
    try {
      const response = await fetch(statusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: pageSignal,
      });
      const task = await readResponse(response);
      if (disposed()) return;

      // The downloader publishes byte progress in detail_progress; all other
      // phases publish the main task progress. Never move the ring backward.
      const detail = Number.isFinite(Number(task.detail_progress))
        && task.detail_progress !== null && task.detail_progress !== undefined
        ? task.detail_progress : null;
      showProgress(ui, detail === null ? task.progress : Math.max(task.progress, detail));
      if (ui.note) ui.note.textContent = task.detail_message || task.message || "正在安装…";
      if (ui.status) ui.status.textContent = ui.note?.textContent || "正在安装";

      if (terminal.has(task.status)) {
        finish(ui, task);
        return;
      }
      schedule(() => poll(ui, statusUrl), 700);
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      // Network hiccups must not trigger another installation operation.
      if (failures >= 10) {
        ui.note.textContent = "暂时无法读取安装进度；任务可能仍在后台执行，请从任务列表查看";
        if (ui.status) ui.status.textContent = ui.note.textContent;
        return;
      }
      ui.note.textContent = "连接暂时中断，正在重新读取进度…";
      schedule(() => poll(ui, statusUrl, failures + 1), 1500);
    }
  }

  function bindInstallForms(root) {
    root.querySelectorAll("[data-market-install-form]").forEach((form) => {
    const ui = cardUi(form);
    if (!ui.card || !ui.button || !ui.state || !ui.ring || !ui.done) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (disposed() || ui.running || ui.button.disabled) return;
      begin(ui);
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json" },
          signal: pageSignal,
        });
        const job = await readResponse(response);
        if (disposed()) return;
        if (!job.task_id || !job.status_url) throw new Error("服务端未返回安装任务信息");
        poll(ui, job.status_url);
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        fail(ui, error?.message || "无法创建安装任务");
      }
    });
    });
  }

  async function loadRegistryCards() {
    const container = market.querySelector("[data-registry-results-url]");
    if (!container) return;
    try {
      const response = await fetch(container.dataset.registryResultsUrl, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: pageSignal,
      });
      if (!response.ok) throw new Error(`目录请求失败（HTTP ${response.status}）`);
      const html = await response.text();
      if (disposed() || !container.isConnected) return;
      // Same-origin server-generated Jinja HTML: Registry fields are escaped
      // by Jinja and no remote HTML is inserted directly.
      container.innerHTML = html;
      container.setAttribute("aria-busy", "false");
      bindInstallForms(container);
    } catch (error) {
      if (disposed() || error?.name === "AbortError" || !container.isConnected) return;
      const notice = document.createElement("section");
      notice.className = "plugin-market-empty surface";
      notice.setAttribute("role", "alert");
      const heading = document.createElement("strong");
      heading.textContent = "无法加载在线插件";
      const detail = document.createElement("span");
      detail.textContent = String(error?.message || error);
      notice.append(heading, detail);
      container.replaceChildren(notice);
      container.setAttribute("aria-busy", "false");
    }
  }

  loadRegistryCards();
})();
