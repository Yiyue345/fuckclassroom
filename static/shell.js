(() => {
  const sidebar = document.querySelector('#app-navigation');
  const sidebarNav = sidebar?.querySelector('.sidebar-nav');
  const toggle = document.querySelector('[data-nav-toggle]');
  const backdrop = document.querySelector('.nav-backdrop');
  const narrow = window.matchMedia('(max-width: 760px)');
  const main = document.querySelector('.main');
  let dirty = false;
  let sectionController = null;
  let navigationController = null;
  let navigationSequence = 0;
  let historyIndex = Number.isInteger(history.state?.fuckClassroomHistoryIndex)
    ? history.state.fuckClassroomHistoryIndex
    : 0;
  let displayedUrl = window.location.href;
  let restoringHistory = false;
  const nativeSetInterval = window.setInterval.bind(window);
  const pageIntervals = new Set();
  let currentPageRuntime = null;

  function abortError() {
    try {
      return new DOMException('页面已切换', 'AbortError');
    } catch (_) {
      const error = new Error('页面已切换');
      error.name = 'AbortError';
      return error;
    }
  }

  function createPageRuntime() {
    const controller = new AbortController();
    const timers = new Set();
    const disposers = new Set();

    const runtime = {
      signal: controller.signal,
      setTimeout(callback, delay, ...args) {
        if (controller.signal.aborted) return 0;
        const timer = window.setTimeout(() => {
          timers.delete(timer);
          if (!controller.signal.aborted) callback(...args);
        }, delay);
        timers.add(timer);
        return timer;
      },
      clearTimeout(timer) {
        window.clearTimeout(timer);
        timers.delete(timer);
      },
      sleep(delay) {
        return new Promise((resolve, reject) => {
          if (controller.signal.aborted) {
            reject(abortError());
            return;
          }
          const timer = runtime.setTimeout(() => {
            controller.signal.removeEventListener('abort', onAbort);
            resolve();
          }, delay);
          const onAbort = () => {
            runtime.clearTimeout(timer);
            reject(abortError());
          };
          controller.signal.addEventListener('abort', onAbort, { once: true });
        });
      },
      onDispose(callback) {
        if (controller.signal.aborted) {
          callback();
          return () => {};
        }
        disposers.add(callback);
        return () => disposers.delete(callback);
      },
      dispose() {
        if (controller.signal.aborted) return;
        controller.abort();
        timers.forEach((timer) => window.clearTimeout(timer));
        timers.clear();
        disposers.forEach((callback) => {
          try {
            callback();
          } catch (error) {
            console.warn('页面清理失败:', error);
          }
        });
        disposers.clear();
      },
    };
    return runtime;
  }

  currentPageRuntime = createPageRuntime();
  window.FuckClassroomPage = Object.freeze({
    current: () => currentPageRuntime,
  });

  function setNavigation(open, restoreFocus = true) {
    open = open && narrow.matches;
    document.body.classList.toggle('nav-open', open);
    toggle?.setAttribute('aria-expanded', String(open));
    if (backdrop) backdrop.hidden = !open;
    if (sidebar) sidebar.inert = narrow.matches && !open;
    if (main) main.inert = open;
    if (open) sidebar?.querySelector('a, button')?.focus();
    else if (restoreFocus) toggle?.focus();
  }

  toggle?.addEventListener('click', () => setNavigation(!document.body.classList.contains('nav-open')));
  document.querySelectorAll('[data-nav-close]').forEach((button) => {
    button.addEventListener('click', () => setNavigation(false));
  });
  narrow.addEventListener('change', () => setNavigation(false, false));
  document.addEventListener('keydown', (event) => {
    if (!document.body.classList.contains('nav-open')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      setNavigation(false);
      return;
    }
    if (event.key !== 'Tab') return;
    const controls = Array.from(
      sidebar?.querySelectorAll('a[href], button:not(:disabled), input, select, [tabindex="0"]') || []
    ).filter((element) => element.getClientRects().length);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  });
  setNavigation(false, false);

  function initSettingsPage(rootNode = document) {
    dirty = false;
    const settings = rootNode.querySelector('#settings-form');
    const status = rootNode.querySelector('[data-settings-status]');
    settings?.addEventListener('input', () => {
      dirty = true;
      if (status) status.textContent = '有未保存的更改';
    });
    settings?.addEventListener('submit', () => {
      dirty = false;
    });
  }

  function initSectionNavigation(rootNode = document) {
    sectionController?.abort();
    sectionController = new AbortController();
    const { signal } = sectionController;
    const sectionLinks = Array.from(rootNode.querySelectorAll('[data-section-nav] a[href^="#"]'));
    const sections = sectionLinks
      .map((link) => rootNode.querySelector(link.hash))
      .filter(Boolean);

    function activate(id) {
      sectionLinks.forEach((link) => {
        const active = link.hash === '#' + id;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }

    let scheduled = false;
    function updateSection() {
      scheduled = false;
      if (!sections.length) return;
      const current = sections.filter((section) => section.getBoundingClientRect().top <= 150).pop() || sections[0];
      activate(current.id);
    }

    window.addEventListener('scroll', () => {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(updateSection);
    }, { passive: true, signal });
    updateSection();
  }

  function initPage(rootNode = document) {
    initSettingsPage(rootNode);
    initSectionNavigation(rootNode);
  }

  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });

  function isBaseScript(script) {
    if (!script.src) return false;
    const url = new URL(script.src, window.location.href);
    return url.pathname === '/static/app.js' || url.pathname === '/static/shell.js';
  }

  function isBaseStyle(link) {
    const url = new URL(link.href, window.location.href);
    return url.pathname === '/static/tokens.css' || url.pathname === '/static/app.css';
  }

  function beginIntervalCapture() {
    const previous = window.setInterval;
    window.setInterval = (handler, timeout, ...args) => {
      const timer = nativeSetInterval(handler, timeout, ...args);
      pageIntervals.add(timer);
      return timer;
    };
    return () => {
      window.setInterval = previous;
    };
  }

  function clearPageIntervals() {
    pageIntervals.forEach((timer) => window.clearInterval(timer));
    pageIntervals.clear();
  }

  function markInitialPageScripts() {
    document.body.querySelectorAll('script').forEach((script) => {
      if (!isBaseScript(script)) script.dataset.pjaxPageScript = 'true';
    });
  }

  function pageScripts(nextDocument, baseUrl) {
    return Array.from(nextDocument.body.querySelectorAll('script'))
      .filter((script) => !isBaseScript(script))
      .map((script) => ({
        src: script.getAttribute('src')
          ? new URL(script.getAttribute('src'), baseUrl).href
          : '',
        text: script.getAttribute('src') ? '' : script.textContent || '',
        type: script.type || '',
        async: script.async,
        defer: script.defer,
        noModule: script.noModule,
        crossOrigin: script.crossOrigin || '',
        referrerPolicy: script.referrerPolicy || '',
      }));
  }

  async function executePageScript(descriptor) {
    if (descriptor.src) {
      const url = new URL(descriptor.src, window.location.href);
      if (url.pathname === '/static/vendor/hls.min.js' && window.Hls) return;
    }

    await new Promise((resolve) => {
      const restoreIntervalCapture = beginIntervalCapture();
      const script = document.createElement('script');
      script.dataset.pjaxPageScript = 'true';
      if (descriptor.type) script.type = descriptor.type;
      if (descriptor.crossOrigin) script.crossOrigin = descriptor.crossOrigin;
      if (descriptor.referrerPolicy) script.referrerPolicy = descriptor.referrerPolicy;
      script.noModule = descriptor.noModule;

      const finish = () => {
        restoreIntervalCapture();
        resolve();
      };

      if (descriptor.src) {
        script.src = descriptor.src;
        script.async = false;
        script.addEventListener('load', finish, { once: true });
        script.addEventListener('error', () => {
          console.warn('页面脚本加载失败:', descriptor.src);
          finish();
        }, { once: true });
      } else {
        script.textContent = descriptor.text;
      }
      document.body.append(script);
      if (!descriptor.src) finish();
    });
  }

  function cleanupPageRuntime() {
    document.dispatchEvent(new CustomEvent('academic:page-before-swap'));
    currentPageRuntime?.dispose();
    clearPageIntervals();
    document.querySelectorAll('script[data-pjax-page-script="true"]').forEach((script) => script.remove());
  }

  async function syncPageStyles(nextDocument) {
    const desiredLinks = Array.from(nextDocument.head.querySelectorAll('link[rel="stylesheet"]'))
      .filter((link) => !isBaseStyle(link));
    const desired = new Map(desiredLinks.map((link) => [new URL(link.href, window.location.href).href, link]));
    const currentLinks = Array.from(document.head.querySelectorAll('link[rel="stylesheet"]'))
      .filter((link) => !isBaseStyle(link));
    const current = new Map(currentLinks.map((link) => [new URL(link.href, window.location.href).href, link]));

    const pending = [];
    desired.forEach((source, href) => {
      if (current.has(href)) return;
      const link = document.createElement('link');
      Array.from(source.attributes).forEach((attribute) => link.setAttribute(attribute.name, attribute.value));
      link.dataset.pjaxStyle = 'true';
      pending.push(new Promise((resolve) => {
        link.addEventListener('load', resolve, { once: true });
        link.addEventListener('error', resolve, { once: true });
      }));
      document.head.append(link);
    });
    await Promise.all(pending);

    return () => {
      current.forEach((link, href) => {
        if (!desired.has(href)) link.remove();
      });
    };
  }

  function syncNavigationState(nextDocument) {
    const nextItems = new Map(
      Array.from(nextDocument.querySelectorAll('#app-navigation .nav-item')).map((item) => [
        item.getAttribute('href') || '',
        item,
      ])
    );
    sidebar?.querySelectorAll('.nav-item').forEach((item) => {
      const source = nextItems.get(item.getAttribute('href') || '');
      const active = Boolean(source?.classList.contains('active'));
      item.classList.toggle('active', active);
      if (active) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });

    const nextSessionList = nextDocument.querySelector('#app-navigation .session-list');
    const currentSessionList = sidebar?.querySelector('.session-list');
    if (nextSessionList && currentSessionList) {
      currentSessionList.innerHTML = nextSessionList.innerHTML;
    }

    const currentStatus = Array.from(sidebar?.querySelectorAll('.nav-label-status') || []);
    const nextStatus = Array.from(nextDocument.querySelectorAll('#app-navigation .nav-label-status'));
    currentStatus.forEach((node, index) => {
      if (nextStatus[index]) node.innerHTML = nextStatus[index].innerHTML;
    });
  }

  function swapPage(nextDocument) {
    const nextContent = nextDocument.querySelector('#main-content');
    const currentContent = document.querySelector('#main-content');
    const nextBreadcrumb = nextDocument.querySelector('.breadcrumb');
    const currentBreadcrumb = document.querySelector('.breadcrumb');
    const nextTopActions = nextDocument.querySelector('.top-actions');
    const currentTopActions = document.querySelector('.top-actions');

    if (!nextContent || !currentContent || !nextBreadcrumb || !currentBreadcrumb) {
      throw new Error('目标页面缺少应用外壳');
    }

    document.title = nextDocument.title;
    currentBreadcrumb.innerHTML = nextBreadcrumb.innerHTML;
    if (nextTopActions && currentTopActions) currentTopActions.innerHTML = nextTopActions.innerHTML;
    currentContent.replaceWith(document.importNode(nextContent, true));
    syncNavigationState(nextDocument);
  }

  function saveCurrentHistoryScroll() {
    const state = {
      ...(history.state || {}),
      fuckClassroomPjax: true,
      fuckClassroomHistoryIndex: historyIndex,
      scrollY: window.scrollY,
    };
    history.replaceState(state, '', displayedUrl);
  }

  function allowNavigation() {
    if (!dirty) return true;
    if (!window.confirm('有未保存的更改，确定离开当前页面？')) return false;
    dirty = false;
    return true;
  }

  function restorePagePosition(url, scrollY = 0) {
    const parsed = new URL(url, window.location.href);
    if (parsed.hash) {
      const target = document.getElementById(decodeURIComponent(parsed.hash.slice(1)));
      if (target) {
        target.scrollIntoView();
        return;
      }
    }
    window.scrollTo(0, scrollY);
  }

  function canNavigate(link, event) {
    if (!link || event.defaultPrevented || event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== '_self') return false;
    if (link.hasAttribute('download') || link.hasAttribute('data-no-pjax')) return false;
    const raw = link.getAttribute('href') || '';
    if (!raw || raw.startsWith('#')) return false;

    let url;
    try {
      url = new URL(link.href, window.location.href);
    } catch {
      return false;
    }
    if (url.origin !== window.location.origin) return false;
    if (!['http:', 'https:'].includes(url.protocol)) return false;
    if (url.pathname.startsWith('/downloads/file/')) return false;

    const current = new URL(window.location.href);
    if (url.pathname === current.pathname && url.search === current.search && url.hash) return false;
    return true;
  }

  async function navigate(url, options = {}) {
    if (!options.skipGuard && !options.fromPopState && !allowNavigation()) return false;

    const destination = new URL(url, window.location.href);
    const sequence = ++navigationSequence;
    navigationController?.abort();
    navigationController = new AbortController();
    const { signal } = navigationController;
    const sidebarScroll = sidebarNav?.scrollTop || 0;

    if (!options.fromPopState) saveCurrentHistoryScroll();
    document.body.classList.add('pjax-loading');
    setNavigation(false, false);

    try {
      const response = await fetch(destination.href, {
        headers: {
          Accept: 'text/html',
          'X-Requested-With': 'FuckClassroom-PJAX',
        },
        cache: 'no-store',
        signal,
      });
      const contentType = response.headers.get('content-type') || '';
      if (!response.ok || !contentType.includes('text/html')) {
        throw new Error('目标不是可局部加载的 HTML 页面');
      }

      const html = await response.text();
      if (signal.aborted || sequence !== navigationSequence) return;
      const nextDocument = new DOMParser().parseFromString(html, 'text/html');
      if (!nextDocument.querySelector('#main-content')) {
        throw new Error('目标页面不支持局部导航');
      }

      const scripts = pageScripts(nextDocument, destination.href);
      const removeStaleStyles = await syncPageStyles(nextDocument);
      if (signal.aborted || sequence !== navigationSequence) return;

      cleanupPageRuntime();
      const performSwap = () => {
        swapPage(nextDocument);
        if (sidebarNav) sidebarNav.scrollTop = sidebarScroll;
      };

      if (typeof document.startViewTransition === 'function' && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        const transition = document.startViewTransition(performSwap);
        await transition.updateCallbackDone;
      } else {
        performSwap();
      }
      removeStaleStyles();

      currentPageRuntime = createPageRuntime();

      if (!options.fromPopState) {
        historyIndex += 1;
        history.pushState({
          fuckClassroomPjax: true,
          fuckClassroomHistoryIndex: historyIndex,
          scrollY: 0,
        }, '', destination.href);
      } else if (Number.isInteger(options.targetHistoryIndex)) {
        historyIndex = options.targetHistoryIndex;
      }
      displayedUrl = destination.href;

      const pageRoot = document.querySelector('.main') || document;
      window.FuckClassroomUI?.initPage?.(pageRoot);
      initPage(pageRoot);

      for (const descriptor of scripts) {
        if (signal.aborted || sequence !== navigationSequence) return;
        await executePageScript(descriptor);
      }

      document.dispatchEvent(new CustomEvent('academic:page-load', {
        detail: { url: destination.href },
      }));
      restorePagePosition(destination.href, options.scrollY || 0);
    } catch (error) {
      if (signal.aborted) return;
      console.warn('局部导航失败，回退到完整导航:', error);
      window.location.assign(destination.href);
      return;
    } finally {
      if (sequence === navigationSequence) document.body.classList.remove('pjax-loading');
    }
  }

  document.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href]');
    if (!canNavigate(link, event)) return;
    event.preventDefault();
    navigate(link.href);
  });

  window.addEventListener('popstate', (event) => {
    if (restoringHistory) {
      restoringHistory = false;
      return;
    }

    const targetHistoryIndex = Number.isInteger(event.state?.fuckClassroomHistoryIndex)
      ? event.state.fuckClassroomHistoryIndex
      : null;

    if (!allowNavigation()) {
      if (targetHistoryIndex !== null && targetHistoryIndex !== historyIndex) {
        restoringHistory = true;
        history.go(historyIndex - targetHistoryIndex);
      } else {
        history.replaceState({
          ...(history.state || {}),
          fuckClassroomPjax: true,
          fuckClassroomHistoryIndex: historyIndex,
          scrollY: window.scrollY,
        }, '', displayedUrl);
      }
      return;
    }

    navigate(window.location.href, {
      fromPopState: true,
      skipGuard: true,
      targetHistoryIndex,
      scrollY: Number(event.state?.scrollY) || 0,
    });
  });

  history.replaceState(
    {
      ...(history.state || {}),
      fuckClassroomPjax: true,
      fuckClassroomHistoryIndex: historyIndex,
      scrollY: window.scrollY,
    },
    '',
    window.location.href
  );

  initPage(document);
  window.FuckClassroomShell = Object.freeze({ navigate, initPage });

  const restoreInitialIntervalCapture = beginIntervalCapture();
  window.setTimeout(() => {
    restoreInitialIntervalCapture();
    markInitialPageScripts();
  }, 0);
})();
