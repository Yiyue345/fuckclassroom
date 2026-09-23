(() => {
  const sidebar = document.querySelector('#app-navigation');
  const toggle = document.querySelector('[data-nav-toggle]');
  const backdrop = document.querySelector('.nav-backdrop');
  const narrow = window.matchMedia('(max-width: 760px)');
  const main = document.querySelector('.main');

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
  document.querySelectorAll('[data-nav-close]').forEach(button => button.addEventListener('click', () => setNavigation(false)));
  sidebar?.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setNavigation(false, false)));
  narrow.addEventListener('change', () => setNavigation(false, false));
  document.addEventListener('keydown', event => {
    if (!document.body.classList.contains('nav-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); setNavigation(false); }
    if (event.key === 'Tab') {
      const controls = Array.from(sidebar.querySelectorAll('a[href], button:not(:disabled), input, select, [tabindex="0"]')).filter(el => el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  setNavigation(false, false);

  const settings = document.querySelector('#settings-form');
  const status = document.querySelector('[data-settings-status]');
  let dirty = false;
  settings?.addEventListener('input', () => {
    dirty = true;
    if (status) status.textContent = '有未保存的更改';
  });
  settings?.addEventListener('submit', () => { dirty = false; });
  window.addEventListener('beforeunload', event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });

  const sectionLinks = Array.from(document.querySelectorAll('[data-section-nav] a[href^="#"]'));
  const sections = sectionLinks.map(link => document.getElementById(link.hash.slice(1))).filter(Boolean);
  function activate(id) {
    sectionLinks.forEach(link => {
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
    const current = sections.filter(section => section.getBoundingClientRect().top <= 150).pop() || sections[0];
    activate(current.id);
  }
  window.addEventListener('scroll', () => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(updateSection); }
  }, { passive: true });
  updateSection();
})();
