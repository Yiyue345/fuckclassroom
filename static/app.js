(() => {
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let themeTransitionTimer = null;

  function resolveTheme(mode) {
    return mode === "auto" ? (media.matches ? "dark" : "light") : mode;
  }

  function themeTransitionDurationMs() {
    const raw = getComputedStyle(root).getPropertyValue("--theme-transition-duration").trim();
    const value = Number.parseFloat(raw);
    if (!Number.isFinite(value)) return 960;
    return raw.endsWith("ms") ? value : value * 1000;
  }

  function applyTheme(mode) {
    const normalized = ["light", "dark", "auto"].includes(mode) ? mode : "auto";
    const nextTheme = resolveTheme(normalized);
    const shouldAnimate = Boolean(root.dataset.theme)
      && root.dataset.theme !== nextTheme;

    if (themeTransitionTimer !== null) {
      window.clearTimeout(themeTransitionTimer);
      themeTransitionTimer = null;
    }
    if (shouldAnimate) {
      root.classList.add("theme-transitioning");
      void root.offsetWidth;
    } else {
      root.classList.remove("theme-transitioning");
    }

    root.dataset.themeMode = normalized;
    root.dataset.theme = nextTheme;
    document.querySelectorAll("[data-theme-option]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeOption === normalized);
    });

    if (shouldAnimate) {
      themeTransitionTimer = window.setTimeout(() => {
        root.classList.remove("theme-transitioning");
        themeTransitionTimer = null;
      }, themeTransitionDurationMs() + 40);
    }
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
    if (form.hasAttribute("data-confirm-handled")) return;
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "确认执行此操作？")) {
        event.preventDefault();
      }
    });
  });

  const sleep = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

  function markSessionLoggingIn(system) {
    document.querySelectorAll(`[data-session-indicator="${system}"]`).forEach((indicator) => {
      indicator.dataset.previousSessionState = indicator.classList.contains("valid") ? "valid" : "invalid";
      indicator.classList.remove("valid", "invalid");
      indicator.classList.add("logging-in");
    });
    document.querySelectorAll(`[data-session-label="${system}"]`).forEach((label) => {
      label.dataset.previousSessionLabel = label.textContent || "";
      label.textContent = "登录中";
    });
  }

  function restoreSessionState(system) {
    document.querySelectorAll(`[data-session-indicator="${system}"]`).forEach((indicator) => {
      const previousState = indicator.dataset.previousSessionState || "invalid";
      indicator.classList.remove("logging-in", "valid", "invalid");
      indicator.classList.add(previousState);
      delete indicator.dataset.previousSessionState;
    });
    document.querySelectorAll(`[data-session-label="${system}"]`).forEach((label) => {
      if (label.dataset.previousSessionLabel !== undefined) {
        label.textContent = label.dataset.previousSessionLabel;
        delete label.dataset.previousSessionLabel;
      }
    });
  }

  document.querySelectorAll("form[data-inline-task]").forEach((form) => {
    const statusBox = document.querySelector(form.dataset.taskStatus || "");
    const message = statusBox && statusBox.querySelector("[data-task-message]");
    const progress = statusBox && statusBox.querySelector("[data-task-progress]");
    if (!statusBox || !message || !progress) return;
    const sourceSelector = form.dataset.taskSourceForm || "";
    const sourceForm = (sourceSelector && document.querySelector(sourceSelector)) || form;
    const taskGroup = form.dataset.taskGroup || "";
    const groupForms = taskGroup
      ? Array.from(document.querySelectorAll("form[data-inline-task]")).filter(
          (item) => item.dataset.taskGroup === taskGroup
        )
      : [form];
    const groupButtons = Array.from(document.querySelectorAll("[data-task-submit]")).filter(
      (button) => groupForms.includes(button.form)
    );
    const fallbackText = form.dataset.taskFallback || "刷新课程失败";
    const loginSystem = form.dataset.sessionLogin || "";

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (groupForms.some((item) => item.dataset.taskRunning === "true")) return;
      let taskSucceeded = false;

      groupForms.forEach((item) => {
        item.dataset.taskRunning = "true";
        item.setAttribute("aria-busy", "true");
      });
      groupButtons.forEach((button) => { button.disabled = true; });
      statusBox.classList.remove("hidden", "error", "success");
      message.textContent = form.dataset.taskStarting || "正在启动刷新任务";
      progress.style.width = "4%";
      if (loginSystem) markSessionLoggingIn(loginSystem);

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(sourceForm)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, fallbackText));
        const started = await response.json();
        if (!started.status_url) throw new Error(form.dataset.taskMissing || "任务没有返回状态地址");

        while (true) {
          const taskResponse = await fetch(started.status_url, {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!taskResponse.ok) throw new Error("读取任务进度失败（" + taskResponse.status + "）");
          const task = await taskResponse.json();
          const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
          progress.style.width = percent + "%";
          message.textContent = task.error || task.message || form.dataset.taskRunningText || "任务正在进行";

          if (task.status === "failed") {
            throw new Error(task.error || fallbackText);
          }
          if (task.status === "succeeded") {
            taskSucceeded = true;
            statusBox.classList.add("success");
            message.textContent = form.dataset.taskSuccess || "任务已完成";
            progress.style.width = "100%";
            await sleep(450);
            window.location.assign(task.result_url || started.result_url || window.location.href);
            return;
          }
          await sleep(700);
        }
      } catch (error) {
        statusBox.classList.add("error");
        message.textContent = error && error.message ? error.message : fallbackText;
        progress.style.width = "100%";
      } finally {
        if (loginSystem && !taskSucceeded) restoreSessionState(loginSystem);
        groupForms.forEach((item) => {
          item.dataset.taskRunning = "false";
          item.removeAttribute("aria-busy");
        });
        groupButtons.forEach((button) => { button.disabled = false; });
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

  function showToast(title, copy, isError = false) {
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

  window.FuckClassroomUI = Object.freeze({ sleep, readResponseError, showToast });

  document.querySelectorAll("[data-toast]").forEach((toast) => {
    window.setTimeout(() => toast.classList.add("hidden"), 6000);
  });

  const authModal = document.querySelector("[data-auth-challenge]");
  if (authModal) {
    const system = authModal.querySelector("[data-auth-system]");
    const message = authModal.querySelector("[data-auth-message]");
    const mobile = authModal.querySelector("[data-auth-mobile]");
    const error = authModal.querySelector("[data-auth-error]");
    const code = authModal.querySelector("[data-auth-code]");
    const sendButton = authModal.querySelector("[data-auth-send]");
    const sendLabel = sendButton && sendButton.querySelector("span");
    const verifyButton = authModal.querySelector("[data-auth-verify]");
    const cancelButton = authModal.querySelector("[data-auth-cancel]");
    let challengeId = "";
    let retryAfter = 0;
    let retryUpdatedAt = 0;

    const setAuthError = (value) => {
      error.textContent = value || "";
      error.classList.toggle("hidden", !value);
    };

    const renderRetry = () => {
      const elapsed = Math.floor((Date.now() - retryUpdatedAt) / 1000);
      const remaining = Math.max(0, retryAfter - elapsed);
      if (sendLabel) sendLabel.textContent = remaining ? remaining + " 秒后重发" : "发送动态码";
      if (sendButton) sendButton.disabled = remaining > 0 || authModal.dataset.status === "sending";
    };

    const renderChallenge = (challenge) => {
      if (!challenge) {
        challengeId = "";
        authModal.classList.add("hidden");
        return;
      }
      const isNew = challenge.id !== challengeId;
      challengeId = challenge.id;
      authModal.dataset.status = challenge.status || "waiting";
      authModal.classList.remove("hidden");
      system.textContent = (challenge.system || "学校统一身份认证") + "登录";
      message.textContent = challenge.message || "需要短信动态码才能继续登录";
      mobile.textContent = challenge.mobile ? "动态码将发送至 " + challenge.mobile : "";
      mobile.classList.toggle("hidden", !challenge.mobile);
      setAuthError(challenge.error || "");
      retryAfter = Number(challenge.retry_after) || 0;
      retryUpdatedAt = Date.now();
      if (verifyButton) verifyButton.disabled = challenge.status === "verifying";
      renderRetry();
      if (isNew) {
        code.value = "";
        window.setTimeout(() => code.focus(), 80);
      }
    };

    const postAuthAction = async (action, payload) => {
      if (!challengeId) return;
      const response = await fetch("/api/auth/challenge/" + encodeURIComponent(challengeId) + "/" + action, {
        method: "POST",
        headers: payload ? {"Content-Type": "application/json", Accept: "application/json"} : {Accept: "application/json"},
        body: payload ? JSON.stringify(payload) : undefined,
      });
      if (!response.ok) throw new Error(await readResponseError(response, "验证码操作失败"));
    };

    const pollAuthChallenge = async () => {
      try {
        const response = await fetch("/api/auth/challenge", {headers: {Accept: "application/json"}, cache: "no-store"});
        if (!response.ok) return;
        const payload = await response.json();
        renderChallenge(payload.challenge);
      } catch {
        // The next poll will retry when the local server is available again.
      }
    };

    sendButton.addEventListener("click", async () => {
      setAuthError("");
      try {
        await postAuthAction("send");
        await pollAuthChallenge();
      } catch (requestError) {
        setAuthError(requestError.message || "短信动态码发送失败");
      }
    });
    verifyButton.addEventListener("click", async () => {
      const value = code.value.trim();
      if (!/^\d{6}$/.test(value)) {
        setAuthError("请输入 6 位短信动态码");
        return;
      }
      setAuthError("");
      try {
        await postAuthAction("verify", {code: value});
        await pollAuthChallenge();
      } catch (requestError) {
        setAuthError(requestError.message || "短信动态码验证失败");
      }
    });
    cancelButton.addEventListener("click", async () => {
      try {
        await postAuthAction("cancel");
      } catch {
        // Cancelling an already-completed challenge needs no extra prompt.
      }
      authModal.classList.add("hidden");
    });
    code.addEventListener("input", () => {
      code.value = code.value.replace(/\D/g, "").slice(0, 6);
    });
    code.addEventListener("keydown", (event) => {
      if (event.key === "Enter") verifyButton.click();
    });

    window.setInterval(renderRetry, 1000);
    window.setInterval(pollAuthChallenge, 800);
    pollAuthChallenge();
  }

  applyTheme(localStorage.getItem("ui-theme") || "auto");
})();
