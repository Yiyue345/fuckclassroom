(() => {
  const page = document.querySelector("[data-task-page]");
  if (!page) return;

  const pageRuntime = window.FuckClassroomPage?.current?.();
  const pageSignal = pageRuntime?.signal;
  const schedule = pageRuntime?.setTimeout
    ? (callback, delay) => pageRuntime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);

  const taskId = page.dataset.taskId || "";
  let opened = false;
  let currentLogs = [];
  let logSignature = "";

  function disposed() {
    return Boolean(pageSignal?.aborted);
  }

  function statusLabel(task) {
    if (task.status === "succeeded") return "已完成";
    if (task.status === "failed") return "失败";
    if (task.status === "cancelled") return "已取消";
    if (task.cancel_requested) return "取消中";
    return "进行中";
  }

  function badgeClass(task) {
    if (task.status === "succeeded") return "green";
    if (task.status === "failed") return "red";
    if (task.status === "cancelled") return "gray";
    return "blue";
  }

  function renderLogs(logs) {
    if (disposed()) return;
    currentLogs = Array.isArray(logs) ? logs : [];
    const last = currentLogs[currentLogs.length - 1] || {};
    const signature = `${currentLogs.length}:${last.timestamp || ""}:${last.level || ""}:${last.message || ""}`;
    if (signature === logSignature) return;
    logSignature = signature;

    const container = document.getElementById("task-log");
    if (!container) return;
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 70;
    const fragment = document.createDocumentFragment();
    if (!currentLogs.length) {
      const empty = document.createElement("div");
      empty.className = "task-log-empty";
      empty.textContent = "暂无日志";
      fragment.append(empty);
    } else {
      currentLogs.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "task-log-line";
        row.dataset.level = entry.level || "info";

        const time = document.createElement("span");
        time.className = "task-log-time";
        time.textContent = String(entry.timestamp || "").split(" ").pop() || "--:--:--";

        const level = document.createElement("span");
        level.className = "task-log-level";
        level.textContent = entry.level || "info";

        const message = document.createElement("pre");
        message.className = "task-log-message";
        message.textContent = entry.message || "";

        row.append(time, level, message);
        fragment.append(row);
      });
    }
    container.replaceChildren(fragment);
    if (nearBottom || currentLogs.length <= 2) container.scrollTop = container.scrollHeight;
  }

  function renderTask(task) {
    if (disposed()) return;
    const bar = document.getElementById("bar");
    const progress = document.getElementById("progress");
    const status = document.getElementById("status");
    const message = document.getElementById("message");
    const badge = document.getElementById("status-badge");
    if (!bar || !progress || !status || !message || !badge) return;

    const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
    bar.style.width = `${percent}%`;
    progress.textContent = `${percent}%`;
    status.textContent = statusLabel(task);
    message.textContent = task.message || "";
    badge.className = `status ${badgeClass(task)}`;

    const detail = document.getElementById("detail-progress");
    if (detail) {
      if (task.detail_progress === null || task.detail_progress === undefined || !task.detail_message) {
        detail.classList.add("hidden");
      } else {
        const detailPercent = Math.max(0, Math.min(100, Number(task.detail_progress) || 0));
        detail.classList.remove("hidden");
        const detailMessage = document.getElementById("detail-message");
        const detailPercentNode = document.getElementById("detail-percent");
        const detailBar = document.getElementById("detail-bar");
        if (detailMessage) detailMessage.textContent = task.detail_message;
        if (detailPercentNode) detailPercentNode.textContent = `${detailPercent}%`;
        if (detailBar) detailBar.style.width = `${detailPercent}%`;
      }
    }

    const error = document.getElementById("error");
    if (error) {
      if (task.error) {
        error.textContent = task.error;
        error.classList.remove("hidden");
      } else {
        error.classList.add("hidden");
      }
    }

    const cancel = document.getElementById("cancel-task");
    const cancelLabel = cancel?.querySelector("span");
    if (cancel && cancelLabel) {
      if (task.status === "running") {
        cancel.classList.remove("hidden");
        cancel.disabled = Boolean(task.cancel_requested);
        cancelLabel.textContent = task.cancel_requested ? "正在取消…" : "取消任务";
      } else {
        cancel.classList.add("hidden");
      }
    }

    if (task.result_url) {
      const actions = document.getElementById("actions");
      const link = document.getElementById("result-link");
      if (link) link.href = task.result_url;
      actions?.classList.remove("hidden");
      if (task.auto_open_result && task.status === "succeeded" && !opened) {
        opened = true;
        if (window.FuckClassroomShell?.navigate) {
          window.FuckClassroomShell.navigate(task.result_url);
        } else {
          window.location.href = task.result_url;
        }
      }
    }

    renderLogs(task.logs);
  }

  function showRefreshError(message) {
    if (disposed()) return;
    const error = document.getElementById("error");
    if (!error) return;
    error.textContent = message;
    error.classList.remove("hidden");
  }

  async function refreshTask() {
    if (disposed()) return;
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        cache: "no-store",
        signal: pageSignal,
      });
      if (disposed()) return;
      if (!response.ok) {
        if (response.status === 404 || response.status === 410) {
          showRefreshError(`任务不存在或已被清理（${response.status}）`);
          return;
        }
        showRefreshError(`暂时无法读取任务状态（${response.status}），正在重试…`);
        schedule(refreshTask, 1500);
        return;
      }
      const task = await response.json();
      if (disposed()) return;
      renderTask(task);
      if (task.status === "running") schedule(refreshTask, 750);
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      showRefreshError("暂时无法连接本地服务，正在重试…");
      schedule(refreshTask, 1500);
    }
  }

  document.getElementById("cancel-task")?.addEventListener("click", async () => {
    if (!window.confirm("确认取消这个任务？已经完成的步骤不会回滚。")) return;
    const button = document.getElementById("cancel-task");
    if (!button || disposed()) return;
    button.disabled = true;
    button.querySelector("span").textContent = "正在取消…";
    try {
      const response = await fetch(`/api/tasks/${taskId}/cancel`, {
        method: "POST",
        headers: { Accept: "application/json" },
        signal: pageSignal,
      });
      if (!response.ok) throw new Error(`取消请求失败（${response.status}）`);
      if (disposed()) return;
      renderTask(await response.json());
      schedule(refreshTask, 250);
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      button.disabled = false;
      button.querySelector("span").textContent = "取消任务";
      window.alert(error && error.message ? error.message : "取消请求失败");
    }
  });

  document.getElementById("copy-logs")?.addEventListener("click", async () => {
    if (disposed()) return;
    const text = currentLogs
      .map((entry) => `[${entry.timestamp || ""}] [${String(entry.level || "info").toUpperCase()}] ${entry.message || ""}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text || "暂无日志");
      if (disposed()) return;
      const button = document.getElementById("copy-logs");
      if (!button) return;
      const previous = button.textContent;
      button.textContent = "已复制";
      schedule(() => {
        if (!disposed() && button.isConnected) button.textContent = previous;
      }, 1200);
    } catch (_) {
      if (!disposed()) window.alert("浏览器未允许复制，请手动选择日志文本。");
    }
  });

  refreshTask();
})();
