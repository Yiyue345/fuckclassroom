(() => {
  const nativeFetch = window.fetch.bind(window);

  function hideDetail(statusBox) {
    const detail = statusBox?.querySelector("[data-task-detail]");
    if (!detail) return;
    detail.classList.add("hidden");
    const bar = detail.querySelector("[data-task-detail-progress]");
    if (bar) bar.style.width = "0%";
  }

  function renderTaskDetail(task) {
    if (!task || typeof task !== "object") return;
    const statusBoxes = document.querySelectorAll(".inline-task-status [data-task-detail]");
    if (!statusBoxes.length) return;

    statusBoxes.forEach((detail) => {
      const statusBox = detail.closest(".inline-task-status");
      const message = detail.querySelector("[data-task-detail-message]");
      const bar = detail.querySelector("[data-task-detail-progress]");
      if (!statusBox || !message || !bar) return;

      const value = task.detail_progress;
      if (value === null || value === undefined || !task.detail_message) {
        hideDetail(statusBox);
        return;
      }

      const percent = Math.max(0, Math.min(100, Number(value) || 0));
      detail.classList.remove("hidden");
      message.textContent = task.detail_message;
      bar.style.width = `${percent}%`;
    });
  }

  document.querySelectorAll("form[data-inline-task]").forEach((form) => {
    form.addEventListener("submit", () => {
      const statusBox = document.querySelector(form.dataset.taskStatus || "");
      hideDetail(statusBox);
    }, { capture: true });
  });

  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    try {
      const input = args[0];
      const url = typeof input === "string" ? input : input?.url || "";
      if (url.includes("/api/tasks/")) {
        response.clone().json().then(renderTaskDetail).catch(() => {});
      }
    } catch (_) {
      // Detail progress is optional and must never interfere with the main task poller.
    }
    return response;
  };
})();
