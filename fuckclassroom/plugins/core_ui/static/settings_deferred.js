(() => {
  'use strict';

  const placeholders = Array.from(document.querySelectorAll('[data-settings-deferred]'));
  if (!placeholders.length) return;

  const runtime = window.FuckClassroomPage?.current?.();
  const signal = runtime?.signal;
  const form = document.getElementById('settings-form');
  const saveButton = document.querySelector('[type="submit"][form="settings-form"]');
  if (saveButton) saveButton.disabled = true;
  let pending = placeholders.length;

  const settled = () => {
    pending -= 1;
    if (pending === 0 && saveButton && !signal?.aborted) saveButton.disabled = false;
  };

  const hydrate = async (placeholder) => {
    try {
      const key = placeholder.dataset.settingsDeferred;
      const response = await fetch('/settings/panels/' + encodeURIComponent(key), {
        credentials: 'same-origin',
        cache: 'no-store',
        signal,
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const markup = await response.text();
      if (signal?.aborted || !placeholder.isConnected) return;

      const holder = document.createElement('template');
      holder.innerHTML = markup;
      const scripts = Array.from(holder.content.querySelectorAll('script'));
      const inserted = Array.from(holder.content.childNodes);
      placeholder.replaceWith(...inserted);

      // Recreate trusted server-rendered inline scripts after inserting
      // the panel, since scripts parsed via a template are inert.
      for (const script of scripts) {
        if (signal?.aborted || !script.isConnected) break;
        const executable = document.createElement('script');
        for (const attr of script.attributes) {
          executable.setAttribute(attr.name, attr.value);
        }
        executable.textContent = script.textContent;
        script.replaceWith(executable);
      }
      settled();
    } catch (error) {
      if (signal?.aborted || error?.name === 'AbortError' || !placeholder.isConnected) return;
      const info = placeholder.querySelector('[role="status"]');
      if (info) info.textContent = '面板加载失败 ';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn';
      retry.textContent = '重试';
      retry.addEventListener('click', () => {
        retry.disabled = true;
        hydrate(placeholder);
      }, signal ? { signal } : undefined);
      info?.append(retry);
    }
  };

  // An incomplete form could silently turn unchecked/missing plugin
  // checkboxes into false, so saving is gated until all panels are ready.
  form?.addEventListener('submit', (event) => {
    if (pending > 0) event.preventDefault();
  }, signal ? { signal } : undefined);

  placeholders.forEach(hydrate);
})();
