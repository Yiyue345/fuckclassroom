(() => {
  function ensureDetail(statusBox) {
    if (!statusBox) return null;
    let detail = statusBox.querySelector('[data-task-detail]');
    if (detail) return detail;

    const body = statusBox.querySelector('.inline-task-body');
    if (!body) return null;
    detail = document.createElement('div');
    detail.className = 'inline-task-detail hidden';
    detail.dataset.taskDetail = '';
    detail.innerHTML = '<div class="inline-task-detail-message" data-task-detail-message></div>'
      + '<div class="progress" aria-hidden="true"><span data-task-detail-progress></span></div>';
    body.append(detail);
    return detail;
  }

  function hideDetail(statusBox) {
    const detail = statusBox?.querySelector('[data-task-detail]');
    if (!detail) return;
    detail.classList.add('hidden');
    const bar = detail.querySelector('[data-task-detail-progress]');
    if (bar) bar.style.width = '0%';
  }

  function modelProgressFromMessage(task) {
    const text = String(task?.message || '');
    if (!text.includes('语音模型')) return null;
    const match = text.match(/[（(](\d{1,3})%[）)]/);
    if (!match) return null;
    return {
      percent: Math.max(0, Math.min(100, Number(match[1]) || 0)),
      message: text,
    };
  }

  function renderTaskDetail(task, statusBox) {
    if (!task || typeof task !== 'object') return;
    const explicit = task.detail_progress !== null
      && task.detail_progress !== undefined
      && task.detail_message
      ? {
          percent: Math.max(0, Math.min(100, Number(task.detail_progress) || 0)),
          message: String(task.detail_message),
        }
      : null;
    const progressState = explicit || modelProgressFromMessage(task);

    if (statusBox) {
      const detail = ensureDetail(statusBox);
      if (!detail) return;
      if (!progressState || task.status === 'failed' || task.status === 'succeeded' || task.status === 'cancelled') {
        hideDetail(statusBox);
        return;
      }

      const message = detail.querySelector('[data-task-detail-message]');
      const bar = detail.querySelector('[data-task-detail-progress]');
      if (!message || !bar) return;
      detail.classList.remove('hidden');
      message.textContent = progressState.message;
      bar.style.width = `${progressState.percent}%`;
    }
  }

  document.querySelectorAll('form[data-inline-task]').forEach((form) => {
    form.addEventListener('submit', () => {
      const statusBox = document.querySelector(form.dataset.taskStatus || '');
      hideDetail(statusBox);
    }, { capture: true });
  });

  document.addEventListener('academic:task-progress', (event) => {
    renderTaskDetail(event.detail?.task, event.detail?.statusBox);
  });
})();
