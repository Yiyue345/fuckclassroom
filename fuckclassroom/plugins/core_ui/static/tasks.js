(() => {
  const page = document.querySelector("[data-task-overview]");
  if (!page) return;

  const list = page.querySelector("[data-task-list]");
  const refreshStatus = page.querySelector("[data-task-refresh-status]");
  const runtime = window.FuckClassroomPage?.current?.();
  const signal = runtime?.signal;
  const schedule = runtime?.setTimeout
    ? (callback, delay) => runtime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);
  const cancelSchedule = runtime?.clearTimeout
    ? (timer) => runtime.clearTimeout(timer)
    : (timer) => window.clearTimeout(timer);

  let timer = null;
  let inFlight = false;
  let stopped = false;

  const disposed = () => stopped || Boolean(signal?.aborted) || !page.isConnected;

  function statusLabel(task) {
    if (task.status === "succeeded") return "已完成";
    if (task.status === "failed") return "失败";
    if (task.status === "cancelled") return "已取消";
    if (task.cancel_requested) return "取消中";
    return `进行中 ${Math.max(0, Math.min(100, Number(task.progress) || 0))}%`;
  }

  function statusColor(task) {
    if (task.status === "succeeded") return "green";
    if (task.status === "failed") return "red";
    if (task.status === "cancelled") return "gray";
    return "blue";
  }

  function emptyState() {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.dataset.taskEmpty = "";
    const inner = document.createElement("div");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.classList.add("icon");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "/static/icons.svg#list-todo");
    icon.append(use);
    const text = document.createElement("p");
    text.textContent = "本次运行期间还没有任务";
    inner.append(icon, text);
    empty.append(inner);
    return empty;
  }

  function createRow(task) {
    const row = document.createElement("a");
    row.className = "task-item";
    row.dataset.taskId = task.id;
    row.href = `/tasks/${encodeURIComponent(task.id)}`;

    const top = document.createElement("div");
    top.className = "task-top";
    const text = document.createElement("div");
    const title = document.createElement("div");
    title.className = "task-title";
    title.dataset.taskTitle = "";
    const meta = document.createElement("div");
    meta.className = "task-meta";

    const message = document.createElement("span");
    message.dataset.taskMessage = "";
    const created = document.createElement("span");
    created.dataset.taskCreated = "";
    const logs = document.createElement("span");
    logs.dataset.taskLogsCount = "";
    meta.append(message, " · 创建于 ", created, " · ", logs, " 条日志");
    text.append(title, meta);

    const badge = document.createElement("span");
    badge.dataset.taskBadge = "";
    top.append(text, badge);

    const progress = document.createElement("div");
    progress.className = "progress";
    progress.dataset.taskProgress = "";
    const bar = document.createElement("span");
    bar.dataset.taskBar = "";
    progress.append(bar);

    row.append(top, progress);
    return row;
  }

  function renderRow(row, task) {
    // textContent intentionally treats plugin-supplied task text as plain text.
    row.querySelector("[data-task-title]").textContent = String(task.title || "");
    row.querySelector("[data-task-message]").textContent = String(task.message || "");
    row.querySelector("[data-task-created]").textContent = String(task.created_at || "");
    row.querySelector("[data-task-logs-count]").textContent = String(task.logs_count ?? 0);
    const badge = row.querySelector("[data-task-badge]");
    badge.className = `status ${statusColor(task)}`;
    badge.textContent = statusLabel(task);
    const progress = row.querySelector("[data-task-progress]");
    progress.classList.toggle("hidden", task.status !== "running");
    row.querySelector("[data-task-bar]").style.width =
      `${Math.max(0, Math.min(100, Number(task.progress) || 0))}%`;
  }

  function renderTasks(tasks) {
    if (disposed()) return;
    const items = Array.isArray(tasks) ? tasks : [];
    const existing = new Map(
      Array.from(list.querySelectorAll("[data-task-id]")).map((row) => [row.dataset.taskId, row])
    );
    const seen = new Set();

    items.forEach((task, index) => {
      if (!task || typeof task.id !== "string" || seen.has(task.id)) return;
      seen.add(task.id);
      const row = existing.get(task.id) || createRow(task);
      renderRow(row, task);
      // Reorder only if necessary, keeping focus/scroll stable on normal polls.
      const atIndex = list.querySelectorAll("[data-task-id]")[index];
      if (atIndex !== row) list.insertBefore(row, atIndex || null);
    });

    existing.forEach((row, id) => {
      if (!seen.has(id)) row.remove();
    });

    const empty = list.querySelector("[data-task-empty]");
    if (seen.size === 0) {
      if (!empty) list.append(emptyState());
    } else {
      empty?.remove();
    }
  }

  function queue(delay) {
    if (disposed()) return;
    if (timer !== null) cancelSchedule(timer);
    timer = schedule(() => {
      timer = null;
      void refresh();
    }, delay);
  }

  async function refresh() {
    if (disposed() || inFlight) return;
    if (document.hidden) {
      queue(5000);
      return;
    }
    inFlight = true;
    try {
      const response = await fetch("/api/tasks", { cache: "no-store", signal });
      if (!response.ok) throw new Error(String(response.status));
      const payload = await response.json();
      if (disposed()) return;
      if (!Array.isArray(payload.tasks)) throw new Error("Invalid tasks payload");
      renderTasks(payload.tasks);
      if (refreshStatus) refreshStatus.textContent = "实时更新中";
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      if (refreshStatus) refreshStatus.textContent = "连接暂时中断，正在重试…";
    } finally {
      inFlight = false;
      queue(document.hidden ? 5000 : 1200);
    }
  }

  function onVisible() {
    if (!document.hidden && !disposed()) queue(0);
  }
  document.addEventListener("visibilitychange", onVisible, { signal });

  const back = page.querySelector("[data-task-back]");
  back?.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const hasShellHistory = Number(history.state?.fuckClassroomHistoryIndex) > 0;
    let fromThisApp = false;
    try {
      const referrer = new URL(document.referrer);
      fromThisApp = referrer.origin === window.location.origin
        && referrer.pathname !== window.location.pathname
        && history.length > 1;
    } catch (_) {
      // Directly opened task center: use the normal "/" link fallback.
    }
    if (hasShellHistory || fromThisApp) {
      event.preventDefault();
      history.back();
    }
  }, { signal });

  if (runtime?.onDispose) {
    runtime.onDispose(() => {
      stopped = true;
      if (timer !== null) cancelSchedule(timer);
    });
  }

  void refresh();
})();
