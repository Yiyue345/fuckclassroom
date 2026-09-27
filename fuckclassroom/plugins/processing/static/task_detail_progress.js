(() => {
  const pageController = new AbortController();
  const pageSignal = pageController.signal;
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

  const transcriptionNodes = Array.from(document.querySelectorAll('[data-transcription-output-url]'));
  const transcriptionUrls = Array.from(new Set(
    transcriptionNodes
      .map((node) => node.dataset.transcriptionOutputUrl || '')
      .filter(Boolean)
  ));

  function cacheKey(outputUrl) {
    return 'fuckclassroom:transcribed:' + outputUrl;
  }

  function cacheCompleted(outputUrl) {
    try {
      window.sessionStorage.setItem(cacheKey(outputUrl), '1');
    } catch (_) {
      // Session storage may be unavailable in hardened browser modes.
    }
  }

  function isCachedCompleted(outputUrl) {
    try {
      return window.sessionStorage.getItem(cacheKey(outputUrl)) === '1';
    } catch (_) {
      return false;
    }
  }

  function matchingTranscriptionNodes(outputUrl) {
    return transcriptionNodes.filter(
      (node) => node.dataset.transcriptionOutputUrl === outputUrl
    );
  }

  function setCompletionCheckBusy(outputUrl, busy) {
    matchingTranscriptionNodes(outputUrl)
      .filter((node) => node.matches('form[data-transcription-form]'))
      .forEach((form) => {
        const button = form.querySelector('[data-transcription-submit]');
        if (!button) return;
        if (busy) {
          button.dataset.transcriptionWasDisabled = button.disabled ? 'true' : 'false';
          button.disabled = true;
          form.setAttribute('aria-busy', 'true');
        } else {
          if (button.dataset.transcriptionWasDisabled !== 'true') button.disabled = false;
          delete button.dataset.transcriptionWasDisabled;
          form.removeAttribute('aria-busy');
        }
      });
  }

  function markTranscriptionComplete(outputUrl) {
    cacheCompleted(outputUrl);
    matchingTranscriptionNodes(outputUrl).forEach((node) => {
      if (node.matches('form[data-transcription-form]')) {
        node.classList.add('hidden');
        node.removeAttribute('aria-busy');
      } else if (node.matches('[data-transcription-complete]')) {
        node.classList.remove('hidden');
      }
    });
  }

  async function checkTranscriptionComplete(outputUrl) {
    if (isCachedCompleted(outputUrl)) {
      markTranscriptionComplete(outputUrl);
      return;
    }

    setCompletionCheckBusy(outputUrl, true);
    try {
      const response = await fetch(outputUrl, {
        headers: { Accept: 'text/html' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(String(response.status));
      const documentText = await response.text();
      const resultDocument = new DOMParser().parseFromString(documentText, 'text/html');
      if (resultDocument.querySelector('[data-output-transcript]')) {
        markTranscriptionComplete(outputUrl);
        return;
      }
    } catch (_) {
      // If status lookup fails, keep the original action available.
    }
    setCompletionCheckBusy(outputUrl, false);
  }

  async function scanCompletedTranscriptions() {
    if (!transcriptionUrls.length) return;
    let nextIndex = 0;
    const workerCount = Math.min(4, transcriptionUrls.length);

    async function worker() {
      while (nextIndex < transcriptionUrls.length) {
        const outputUrl = transcriptionUrls[nextIndex];
        nextIndex += 1;
        await checkTranscriptionComplete(outputUrl);
      }
    }

    await Promise.all(Array.from({ length: workerCount }, () => worker()));
  }

  document.addEventListener('academic:task-progress', (event) => {
    const task = event.detail?.task;
    renderTaskDetail(task, event.detail?.statusBox);
    if (task?.status === 'succeeded' && task.result_url) {
      markTranscriptionComplete(String(task.result_url));
    }
  }, { signal: pageSignal });

  document.addEventListener('academic:page-before-swap', () => {
    pageController.abort();
  }, { once: true, signal: pageSignal });

  scanCompletedTranscriptions();
})();
