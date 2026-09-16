(() => {
  const stage = document.querySelector('[data-ppt-stage]');
  if (!stage) return;

  function cropImageOnlySlide(svg) {
    if (!(svg instanceof SVGElement) || svg.dataset.autoCropped === '1') return;
    svg.dataset.autoCropped = '1';

    const children = [...svg.children];
    const background = children.find((node) => node.localName === 'rect');
    const visible = children.filter((node) => node !== background);
    if (visible.length !== 1 || visible[0].localName !== 'image') return;

    const image = visible[0];
    const viewBox = svg.viewBox?.baseVal;
    const x = Number(image.getAttribute('x'));
    const y = Number(image.getAttribute('y'));
    const width = Number(image.getAttribute('width'));
    const height = Number(image.getAttribute('height'));
    if (!viewBox || !viewBox.width || !viewBox.height || !width || !height) return;

    const areaRatio = (width * height) / (viewBox.width * viewBox.height);
    if (areaRatio < 0.35) return;

    const insideSlide = x >= viewBox.x && y >= viewBox.y
      && x + width <= viewBox.x + viewBox.width
      && y + height <= viewBox.y + viewBox.height;
    if (!insideSlide) return;

    const horizontalMargin = 1 - width / viewBox.width;
    const verticalMargin = 1 - height / viewBox.height;
    if (horizontalMargin < 0.02 && verticalMargin < 0.02) return;

    svg.setAttribute('viewBox', `${x} ${y} ${width} ${height}`);
  }

  function scan(root) {
    if (root instanceof SVGElement && root.classList.contains('ppt-slide-svg')) {
      cropImageOnlySlide(root);
    }
    root.querySelectorAll?.('svg.ppt-slide-svg').forEach(cropImageOnlySlide);
  }

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node instanceof Element) scan(node);
      }
    }
  });

  observer.observe(stage, { childList: true, subtree: true });
  scan(stage);
})();
