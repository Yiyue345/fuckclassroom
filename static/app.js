(() => {
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function resolveTheme(mode) {
    return mode === "auto" ? (media.matches ? "dark" : "light") : mode;
  }

  function applyTheme(mode) {
    const normalized = ["light", "dark", "auto"].includes(mode) ? mode : "auto";
    root.dataset.themeMode = normalized;
    root.dataset.theme = resolveTheme(normalized);
    document.querySelectorAll("[data-theme-option]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeOption === normalized);
    });
  }

  function saveTheme(mode) {
    localStorage.setItem("ui-theme", mode);
    applyTheme(mode);
  }

  document.querySelectorAll("[data-theme-option]").forEach((button) => {
    button.addEventListener("click", () => saveTheme(button.dataset.themeOption || "auto"));
  });

  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      saveTheme(root.dataset.theme === "dark" ? "light" : "dark");
    });
  });

  media.addEventListener("change", () => {
    if ((localStorage.getItem("ui-theme") || "auto") === "auto") {
      applyTheme("auto");
    }
  });

  const switcher = document.querySelector("[data-workspace-switcher]");
  const switcherButton = document.querySelector("[data-workspace-button]");
  if (switcher && switcherButton) {
    switcherButton.addEventListener("click", () => {
      const open = switcher.classList.toggle("open");
      switcherButton.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (event) => {
      if (!switcher.contains(event.target)) {
        switcher.classList.remove("open");
        switcherButton.setAttribute("aria-expanded", "false");
      }
    });
  }

  document.querySelectorAll("[data-filter-input]").forEach((input) => {
    const target = document.querySelector(input.dataset.filterInput || "");
    if (!target) return;
    const rows = Array.from(target.querySelectorAll("[data-filter-row]"));
    input.addEventListener("input", () => {
      const keyword = input.value.trim().toLocaleLowerCase();
      rows.forEach((row) => {
        row.classList.toggle("hidden", keyword && !row.textContent.toLocaleLowerCase().includes(keyword));
      });
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    if (form.matches("form[data-selection-action]")) return;
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "确认执行此操作？")) {
        event.preventDefault();
      }
    });
  });

  const sleep = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

  document.querySelectorAll("form[data-inline-task]").forEach((form) => {
    const statusBox = document.querySelector(form.dataset.taskStatus || "");
    const submitButton = form.querySelector("[data-task-submit]");
    const message = statusBox && statusBox.querySelector("[data-task-message]");
    const progress = statusBox && statusBox.querySelector("[data-task-progress]");
    if (!statusBox || !message || !progress) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (form.dataset.taskRunning === "true") return;

      form.dataset.taskRunning = "true";
      form.setAttribute("aria-busy", "true");
      if (submitButton) submitButton.disabled = true;
      statusBox.classList.remove("hidden", "error", "success");
      message.textContent = "正在启动刷新任务";
      progress.style.width = "4%";

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("启动刷新失败（" + response.status + "）");
        const started = await response.json();
        if (!started.status_url) throw new Error("刷新任务没有返回状态地址");

        while (true) {
          const taskResponse = await fetch(started.status_url, {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!taskResponse.ok) throw new Error("读取刷新进度失败（" + taskResponse.status + "）");
          const task = await taskResponse.json();
          const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
          progress.style.width = percent + "%";
          message.textContent = task.error || task.message || "正在刷新课程";

          if (task.status === "failed") {
            throw new Error(task.error || "刷新课程失败");
          }
          if (task.status === "succeeded") {
            statusBox.classList.add("success");
            message.textContent = "课程信息已更新";
            progress.style.width = "100%";
            await sleep(450);
            window.location.assign(task.result_url || started.result_url || window.location.href);
            return;
          }
          await sleep(700);
        }
      } catch (error) {
        statusBox.classList.add("error");
        message.textContent = error && error.message ? error.message : "刷新课程失败";
        progress.style.width = "100%";
      } finally {
        form.dataset.taskRunning = "false";
        form.removeAttribute("aria-busy");
        if (submitButton) submitButton.disabled = false;
      }
    });
  });

  async function readResponseError(response, fallback) {
    try {
      const payload = await response.json();
      return payload.detail || payload.error || fallback;
    } catch {
      return fallback;
    }
  }

  function showSelectionToast(title, copy, isError = false) {
    document.querySelectorAll("[data-toast]").forEach((item) => item.classList.add("hidden"));
    const toast = document.createElement("aside");
    toast.className = "toast" + (isError ? " error" : "");
    toast.dataset.toast = "";
    toast.innerHTML = '<div class="toast-icon"><svg class="icon small"><use href="/static/icons.svg#' +
      (isError ? "circle-alert" : "circle-check") +
      '"></use></svg></div><div><div class="toast-title"></div><p class="toast-copy"></p></div>';
    toast.querySelector(".toast-title").textContent = title;
    toast.querySelector(".toast-copy").textContent = copy;
    document.body.appendChild(toast);
    window.setTimeout(() => toast.remove(), 6000);
  }

  function applySelectionActionState(form, action) {
    const row = form.closest("tr[data-lesson-id]");
    const button = form.querySelector("[data-selection-submit]");
    if (!row || !button) return;
    const selected = action === "select";
    row.dataset.selected = selected ? "true" : "false";
    const statusCell = row.querySelector("[data-status-cell]");
    if (statusCell) {
      const status = document.createElement("span");
      status.className = "status " + (selected ? "green" : "blue");
      status.textContent = selected ? "已选" : "可选";
      statusCell.replaceChildren(status);
    }
    form.dataset.selectionAction = selected ? "drop" : "select";
    form.action = selected ? "/selection/info/drop" : "/selection/info/select";
    if (selected) {
      form.dataset.confirm = "确认退选 " + (form.dataset.courseName || "这门课程") + "？";
    } else {
      delete form.dataset.confirm;
    }
    button.classList.toggle("primary", !selected);
    button.classList.toggle("danger", selected);
    button.textContent = selected ? "退课" : "选课";
    button.disabled = false;
  }

  document.querySelectorAll("form[data-selection-action]").forEach((form) => {
    const statusBox = document.querySelector(form.dataset.taskStatus || "");
    const submitButton = form.querySelector("[data-selection-submit]");
    const message = statusBox && statusBox.querySelector("[data-task-message]");
    const progress = statusBox && statusBox.querySelector("[data-task-progress]");
    if (!statusBox || !submitButton || !message || !progress) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (statusBox.dataset.taskRunning === "true") return;
      const action = form.dataset.selectionAction || "select";
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      const actionLabel = action === "drop" ? "退课" : "选课";

      statusBox.dataset.taskRunning = "true";
      statusBox.classList.remove("hidden", "error", "success");
      message.textContent = "正在启动" + actionLabel + "任务";
      progress.style.width = "4%";
      submitButton.disabled = true;

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, "启动" + actionLabel + "失败"));
        const started = await response.json();
        if (!started.status_url) throw new Error(actionLabel + "任务没有返回状态地址");

        while (true) {
          const taskResponse = await fetch(started.status_url, {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!taskResponse.ok) throw new Error("读取" + actionLabel + "进度失败（" + taskResponse.status + "）");
          const task = await taskResponse.json();
          const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
          progress.style.width = percent + "%";
          message.textContent = task.error || task.message || ("正在" + actionLabel);
          if (task.status === "failed") throw new Error(task.error || (actionLabel + "失败"));
          if (task.status === "succeeded") break;
          await sleep(250);
        }

        let result = {};
        try {
          const resultResponse = await fetch("/api/selection/action-result", { cache: "no-store" });
          if (resultResponse.ok) result = await resultResponse.json();
        } catch {
          result = {};
        }
        const resultMessage = result.message || (actionLabel + "成功");
        statusBox.classList.add("success");
        message.textContent = resultMessage;
        progress.style.width = "100%";
        applySelectionActionState(form, action);
        showSelectionToast(actionLabel + "成功", (form.dataset.courseName || "课程") + "：" + resultMessage);
        window.setTimeout(() => statusBox.classList.add("hidden"), 4000);
      } catch (error) {
        const errorMessage = error && error.message ? error.message : (actionLabel + "失败");
        statusBox.classList.add("error");
        message.textContent = errorMessage;
        progress.style.width = "100%";
        submitButton.disabled = false;
        showSelectionToast(actionLabel + "未完成", errorMessage, true);
      } finally {
        statusBox.dataset.taskRunning = "false";
      }
    });
  });

  const countRefreshToggle = document.querySelector("[data-count-refresh-toggle]");
  const countRefreshStatus = document.querySelector("[data-count-refresh-status]");
  if (countRefreshToggle && countRefreshStatus) {
    const storageKey = "selection-count-refresh-enabled";
    let refreshTimer = 0;
    let refreshRunning = false;

    const scheduleRefresh = () => {
      window.clearTimeout(refreshTimer);
      if (countRefreshToggle.checked && !document.hidden) {
        refreshTimer = window.setTimeout(refreshCounts, 30000);
      }
    };

    const renderCount = (lessonId, countInfo) => {
      const row = document.querySelector('tr[data-lesson-id="' + CSS.escape(String(lessonId)) + '"]');
      if (!row) return;
      const selectedCount = Number(countInfo && countInfo.selected_count);
      const apiMax = Number(countInfo && countInfo.max_count);
      const cachedMax = Number(row.dataset.maxCount);
      const maxCount = Number.isFinite(apiMax) && apiMax > 0 ? apiMax : cachedMax;
      if (!Number.isFinite(selectedCount) || !Number.isFinite(maxCount) || maxCount <= 0) return;

      row.dataset.maxCount = String(maxCount);
      const full = selectedCount >= maxCount;
      const percent = Math.max(0, Math.min(100, selectedCount * 100 / maxCount));
      const capacityCell = row.querySelector("[data-capacity-cell]");
      if (capacityCell) {
        const capacity = document.createElement("div");
        capacity.className = "capacity";
        const progressTrack = document.createElement("div");
        progressTrack.className = "progress";
        const progressBar = document.createElement("span");
        progressBar.style.width = percent + "%";
        progressBar.style.background = full ? "var(--red)" : (percent > 85 ? "var(--amber)" : "var(--green)");
        progressTrack.appendChild(progressBar);
        const label = document.createElement("span");
        label.className = "capacity-label";
        label.textContent = selectedCount + "/" + maxCount;
        capacity.append(progressTrack, label);
        capacityCell.replaceChildren(capacity);
      }

      if (row.dataset.selected !== "true") {
        const statusCell = row.querySelector("[data-status-cell]");
        if (statusCell) {
          const status = document.createElement("span");
          status.className = "status " + (full ? "red" : "blue");
          status.textContent = full ? "已满" : "可选";
          statusCell.replaceChildren(status);
        }
        const form = row.querySelector('form[data-selection-action="select"]');
        const button = form && form.querySelector("[data-selection-submit]");
        if (button) button.disabled = full;
      }
    };

    async function refreshCounts() {
      if (refreshRunning || !countRefreshToggle.checked || document.hidden) {
        scheduleRefresh();
        return;
      }
      refreshRunning = true;
      countRefreshStatus.textContent = "刷新中";
      try {
        const response = await fetch("/api/selection/counts", {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          const errorMessage = await readResponseError(response, "人数刷新失败");
          if (response.status === 409) {
            countRefreshToggle.checked = false;
            localStorage.setItem(storageKey, "false");
          }
          throw new Error(errorMessage);
        }
        const payload = await response.json();
        Object.entries(payload.courses || {}).forEach(([lessonId, countInfo]) => renderCount(lessonId, countInfo));
        countRefreshStatus.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      } catch (error) {
        countRefreshStatus.textContent = error && error.message ? error.message : "人数刷新失败";
      } finally {
        refreshRunning = false;
        scheduleRefresh();
      }
    }

    countRefreshToggle.checked = localStorage.getItem(storageKey) === "true";
    countRefreshToggle.addEventListener("change", () => {
      localStorage.setItem(storageKey, String(countRefreshToggle.checked));
      if (countRefreshToggle.checked) {
        refreshCounts();
      } else {
        window.clearTimeout(refreshTimer);
        countRefreshStatus.textContent = "已暂停";
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        window.clearTimeout(refreshTimer);
      } else if (countRefreshToggle.checked) {
        refreshCounts();
      }
    });
    if (countRefreshToggle.checked) refreshCounts();
  }

  document.querySelectorAll("[data-toast]").forEach((toast) => {
    window.setTimeout(() => toast.classList.add("hidden"), 6000);
  });

  applyTheme(localStorage.getItem("ui-theme") || "auto");
})();