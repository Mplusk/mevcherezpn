(function (root) {
  'use strict';

  // The world wraps at its edges; each generation is computed simultaneously.
  function nextGeneration(cells, width, height) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 3 || height < 3 || cells.length !== width * height) {
      throw new RangeError('Invalid world dimensions');
    }
    const next = new Uint8Array(cells.length);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        let neighbors = 0;
        for (let dy = -1; dy <= 1; dy++) {
          for (let dx = -1; dx <= 1; dx++) {
            if (dx || dy) neighbors += cells[((y + dy + height) % height) * width + (x + dx + width) % width];
          }
        }
        const i = y * width + x;
        next[i] = neighbors === 3 || (cells[i] && neighbors === 2) ? 1 : 0;
      }
    }
    return next;
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { nextGeneration };
  if (typeof document === 'undefined') return;

  const path = location.pathname.replace(/\/+$/, '') || '/';
  const page = ({ '/': 'home', '/rules': 'rules', '/patterns': 'patterns', '/about': 'about' })[path] || 'home';
  document.body.dataset.page = page;
  const nav = document.querySelector('[data-nav="' + page + '"]');
  if (nav) nav.setAttribute('aria-current', 'page');
  if (page !== 'home') return;

  const canvas = document.getElementById('world');
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const toggle = document.getElementById('toggle');
  const toggleLabel = document.getElementById('toggle-label');
  const counter = document.getElementById('generation');
  const population = document.getElementById('population');
  const density = document.getElementById('density');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const patterns = {
    glider: [[1, 0], [2, 1], [0, 2], [1, 2], [2, 2]],
    blinker: [[0, 1], [1, 1], [2, 1]],
    'r-pentomino': [[1, 0], [2, 0], [0, 1], [1, 1], [1, 2]]
  };
  const selectedPattern = new URLSearchParams(location.search).get('pattern');
  let width = 0, height = 0, cells, generation = 0, lastFrame = 0;
  let running = !reduceMotion, automatic = !patterns[selectedPattern], history = [];
  let paintValue = 1, painting = false, lastPainted = -1;

  function updateControls() {
    toggle.setAttribute('aria-pressed', String(running));
    toggleLabel.textContent = running ? 'Пауза' : 'Продолжить';
    toggle.querySelector('.play-symbol').textContent = running ? 'Ⅱ' : '▶';
  }
  function seed() {
    cells = new Uint8Array(width * height);
    for (let i = 0; i < cells.length; i++) cells[i] = Math.random() < 0.25 ? 1 : 0;
    generation = 0;
    history = [];
  }
  function placePattern(name) {
    cells.fill(0);
    const ox = Math.floor(width / 2) - 1, oy = Math.floor(height / 2) - 1;
    patterns[name].forEach(([x, y]) => { cells[(oy + y) * width + ox + x] = 1; });
    generation = 0;
    history = [];
  }
  function draw() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);
    const cw = w / width, ch = h / height;
    let count = 0;
    ctx.fillStyle = '#173b80';
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      if (!cells[y * width + x]) continue;
      count++;
      ctx.fillRect(Math.floor(x * cw) + 1, Math.floor(y * ch) + 1,
        Math.max(1, Math.ceil(cw) - 2), Math.max(1, Math.ceil(ch) - 2));
    }
    counter.textContent = String(generation);
    population.textContent = String(count);
    density.textContent = Math.round(count * 100 / cells.length) + '%';
  }
  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const newWidth = Math.max(20, Math.floor(w / 14)), newHeight = Math.max(14, Math.floor(h / 14));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!cells || newWidth !== width || newHeight !== height) {
      const previous = cells, oldWidth = width, oldHeight = height;
      width = newWidth; height = newHeight;
      if (!previous) {
        seed();
        if (patterns[selectedPattern]) placePattern(selectedPattern);
      } else {
        cells = new Uint8Array(width * height);
        for (let y = 0; y < Math.min(oldHeight, height); y++) {
          for (let x = 0; x < Math.min(oldWidth, width); x++) cells[y * width + x] = previous[y * oldWidth + x];
        }
        history = [];
      }
    }
    draw();
  }
  function advance() {
    cells = nextGeneration(cells, width, height);
    generation++;
    if (automatic) {
      // Exact state comparisons avoid treating a hash collision as a stable world.
      const state = Array.from(cells).join('');
      const isRepeat = history.includes(state);
      history.push(state);
      if (history.length > 32) history.shift();
      if (isRepeat || !cells.some(Boolean)) seed();
    }
    draw();
  }
  function frame(time) {
    if (running && cells && time - lastFrame >= 180) { advance(); lastFrame = time; }
    requestAnimationFrame(frame);
  }
  function pause() { running = false; updateControls(); }
  toggle.addEventListener('click', () => { running = !running; updateControls(); });
  document.getElementById('step').addEventListener('click', () => { pause(); automatic = false; advance(); });
  document.getElementById('randomize').addEventListener('click', () => { automatic = true; seed(); draw(); });
  document.getElementById('clear').addEventListener('click', () => { pause(); automatic = false; cells.fill(0); generation = 0; history = []; draw(); });
  function indexAt(event) {
    const rect = canvas.getBoundingClientRect();
    const x = Math.floor((event.clientX - rect.left) * width / rect.width);
    const y = Math.floor((event.clientY - rect.top) * height / rect.height);
    return x < 0 || x >= width || y < 0 || y >= height ? -1 : y * width + x;
  }
  canvas.addEventListener('pointerdown', event => {
    const i = indexAt(event);
    if (i < 0) return;
    pause(); automatic = false; painting = true; lastPainted = i;
    paintValue = cells[i] ? 0 : 1; cells[i] = paintValue;
    canvas.setPointerCapture(event.pointerId); draw();
  });
  canvas.addEventListener('pointermove', event => {
    if (!painting) return;
    const i = indexAt(event);
    if (i >= 0 && i !== lastPainted) { cells[i] = paintValue; lastPainted = i; draw(); }
  });
  canvas.addEventListener('pointerup', () => { painting = false; });
  canvas.addEventListener('pointercancel', () => { painting = false; });
  if (typeof ResizeObserver === 'function') new ResizeObserver(resize).observe(canvas);
  else window.addEventListener('resize', resize);
  resize(); updateControls(); requestAnimationFrame(frame);
})(typeof globalThis === 'undefined' ? this : globalThis);
