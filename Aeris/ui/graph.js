/* Aeris — force-directed note graph on a 2D canvas.
 *
 * Canvas rather than SVG on purpose: SVG needs a DOM node per circle and per
 * line, and stalls somewhere past ~1,500 nodes. Repulsion here goes through a
 * uniform spatial grid with a hard distance cutoff, so each tick visits only
 * nearby pairs and cost stays close to linear in the node count.
 */
(function (global) {
  'use strict';

  /* ---- tunables ------------------------------------------------------- */
  const REPEL      = 3400;   // charge strength
  const CUTOFF     = 380;    // px beyond which two nodes ignore each other
  const SPRING     = 0.034;  // wikilink stiffness
  const LINK_LEN   = 58;     // rest length
  const CENTER     = 0.0012; // pull toward the middle — gentle, it only stops
                             // the whole cloud drifting off screen
  const DAMP       = 0.855;  // velocity decay
  const ALPHA_MIN  = 0.13;   // never fully freezes — this is the "breathing"
  const ALPHA_DECAY= 0.017;
  const PULSE_EVERY= 2600;   // ms between idle pulses
  const PULSE_MS   = 1250;

  const KIND_COLOR = {
    project:  '#f6b73c', client:   '#ff5c8a', research: '#4d8cff',
    model:    '#a855f7', dataset:  '#22d38a', note:     '#dbe4f0',
    meeting:  '#fb923c', idea:     '#d9f24a', task:     '#38bdf8',
    invoice:  '#2dd4bf', person:   '#f472b6', paper:    '#818cf8',
    journal:  '#94a3b8', archive:  '#64748b'
  };
  const FALLBACK = ['#7dd3fc', '#fca5a5', '#fcd34d', '#c4b5fd', '#86efac',
                    '#f9a8d4', '#a5b4fc', '#fdba74'];
  const kindColors = {};
  function colorFor(kind) {
    if (KIND_COLOR[kind]) return KIND_COLOR[kind];
    if (!kindColors[kind]) {
      let h = 0;
      for (let i = 0; i < kind.length; i++) h = (h * 31 + kind.charCodeAt(i)) | 0;
      kindColors[kind] = FALLBACK[Math.abs(h) % FALLBACK.length];
    }
    return kindColors[kind];
  }

  /* Seeded PRNG so the layout is identical on every load — a demo recorded
   * today looks like the one recorded next week. */
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  /* ---- state ---------------------------------------------------------- */
  let cv, ctx, W = 0, H = 0, dpr = 1;
  let nodes = [], edges = [], byId = new Map(), adj = new Map();
  let view = { x: 0, y: 0, k: 1 };
  let alpha = 1, running = false, raf = 0;
  let hover = null, focused = null, pathIds = null, pathSet = new Set();
  let hidden = new Set();          // kinds toggled off
  let showLabels = true, showPulse = true;
  let pulses = [], lastPulse = 0;
  let drag = null, panning = null, moved = false, autoFit = false;
  let hooks = {};

  /* ---- build ---------------------------------------------------------- */
  function load(payload) {
    const rnd = mulberry32(0x5EED);
    const n = payload.nodes.length;
    const R = Math.max(240, Math.sqrt(n) * 34);
    nodes = payload.nodes.map(function (d, i) {
      // golden-angle spiral seed: no initial clumps, deterministic
      const a = i * 2.399963229728653;
      const r = R * Math.sqrt((i + 0.6) / n) * (0.72 + rnd() * 0.5);
      return {
        id: d.id, title: d.title, kind: d.kind, degree: d.degree,
        tags: d.tags || [], modified: d.modified,
        x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0,
        r: 3.2 + Math.sqrt(d.degree) * 2.15,
        m: 1 + d.degree * 0.13,
        // charge grows with connectivity so two hubs shove each other apart
        // instead of stacking in the middle
        q: 1 + d.degree * 0.085,
        color: colorFor(d.kind),
        lift: 0, on: true
      };
    });
    byId = new Map(nodes.map(function (nd) { return [nd.id, nd]; }));
    adj = new Map(nodes.map(function (nd) { return [nd.id, []]; }));
    edges = [];
    payload.edges.forEach(function (e) {
      const a = byId.get(e[0]), b = byId.get(e[1]);
      if (!a || !b) return;
      edges.push({ a: a, b: b });
      adj.get(a.id).push(b.id);
      adj.get(b.id).push(a.id);
    });
    hidden = new Set();
    focused = null; pathIds = null; pathSet = new Set(); hover = null;
    alpha = 1;
    resize();
    fit(true);
    start();
    // The seed spiral is not the settled layout, so refit as the forces
    // resolve. Cancelled the moment the user pans, zooms or drags.
    autoFit = true;
    setTimeout(function () { if (autoFit) fit(false); }, 1100);
    setTimeout(function () { if (autoFit) fit(false); }, 2600);
    setTimeout(function () { if (autoFit) { fit(false); autoFit = false; } }, 4800);
  }

  /* ---- physics -------------------------------------------------------- */
  const grid = new Map();
  function key(cx, cy) { return cx * 73856093 ^ cy * 19349663; }

  function tick() {
    const live = nodes.filter(function (n) { return n.on; });
    grid.clear();
    for (let i = 0; i < live.length; i++) {
      const n = live[i];
      const k = key(Math.floor(n.x / CUTOFF), Math.floor(n.y / CUTOFF));
      let cell = grid.get(k);
      if (!cell) { cell = []; grid.set(k, cell); }
      cell.push(n);
    }

    // repulsion, neighbours only
    const cut2 = CUTOFF * CUTOFF;
    for (let i = 0; i < live.length; i++) {
      const a = live[i];
      const cx = Math.floor(a.x / CUTOFF), cy = Math.floor(a.y / CUTOFF);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const cell = grid.get(key(cx + ox, cy + oy));
          if (!cell) continue;
          for (let j = 0; j < cell.length; j++) {
            const b = cell[j];
            if (b === a || b.id < a.id) continue;   // each pair once
            let dx = b.x - a.x, dy = b.y - a.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > cut2) continue;
            if (d2 < 1e-4) { dx = (Math.random() - 0.5) * 0.1; dy = (Math.random() - 0.5) * 0.1; d2 = 0.01; }
            const d = Math.sqrt(d2);
            const min = a.r + b.r + 5;
            let f = REPEL * a.q * b.q / d2;
            if (d < min) f += (min - d) * 0.34;      // soft collision
            const fx = (dx / d) * f, fy = (dy / d) * f;
            a.vx -= fx / a.m; a.vy -= fy / a.m;
            b.vx += fx / b.m; b.vy += fy / b.m;
          }
        }
      }
    }

    // springs
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      if (!e.a.on || !e.b.on) continue;
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      // Rest length grows with how connected both ends are. A hub needs to
      // sit further from its satellites than two leaves do from each other,
      // and that is also what stops the hubs stacking on the centre.
      const rest = LINK_LEN + (e.a.r + e.b.r) * 1.6
                 + Math.sqrt(e.a.degree * e.b.degree) * 2.4;
      const shared = Math.min(e.a.degree, e.b.degree) || 1;
      const f = (d - rest) * SPRING / Math.sqrt(shared);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      e.a.vx += fx / e.a.m; e.a.vy += fy / e.a.m;
      e.b.vx -= fx / e.b.m; e.b.vy -= fy / e.b.m;
    }

    // centring + integrate
    for (let i = 0; i < live.length; i++) {
      const n = live[i];
      if (drag && drag.node === n) continue;
      n.vx -= n.x * CENTER;
      n.vy -= n.y * CENTER;
      n.vx *= DAMP; n.vy *= DAMP;
      const sp = Math.hypot(n.vx, n.vy);
      if (sp > 34) { n.vx = n.vx / sp * 34; n.vy = n.vy / sp * 34; }
      // alpha is a movement budget, not a force multiplier. Scaling the
      // forces instead makes the settled equilibrium depend on the cooling
      // schedule, which is what collapsed the graph into a ball.
      n.x += n.vx * alpha; n.y += n.vy * alpha;
    }

    if (alpha > ALPHA_MIN) alpha = Math.max(ALPHA_MIN, alpha - alpha * ALPHA_DECAY);
    else alpha = ALPHA_MIN;
  }

  /* ---- drawing -------------------------------------------------------- */
  function toScreen(x, y) { return [x * view.k + view.x, y * view.k + view.y]; }
  function toWorld(sx, sy) { return [(sx - view.x) / view.k, (sy - view.y) / view.k]; }

  function draw(now) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const dim = hover || focused || pathIds;
    const litIds = new Set();
    if (pathIds) pathIds.forEach(function (i) { litIds.add(i); });
    else if (hover || focused) {
      const id = (hover || focused).id;
      litIds.add(id);
      (adj.get(id) || []).forEach(function (i) { litIds.add(i); });
    }

    /* edges */
    ctx.lineWidth = Math.max(0.5, 0.85 * view.k);
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      if (!e.a.on || !e.b.on) continue;
      let alphaE = 0.16;
      if (dim) {
        const inPath = pathIds && pathSet.has(e.a.id) && pathSet.has(e.b.id) &&
                       Math.abs(pathIds.indexOf(e.a.id) - pathIds.indexOf(e.b.id)) === 1;
        const inLit = litIds.has(e.a.id) && litIds.has(e.b.id);
        alphaE = inPath ? 0.95 : (inLit ? 0.42 : 0.022);
        if (inPath) { ctx.lineWidth = Math.max(1.4, 2.1 * view.k); }
        else { ctx.lineWidth = Math.max(0.5, 0.85 * view.k); }
      }
      const [ax, ay] = toScreen(e.a.x, e.a.y);
      const [bx, by] = toScreen(e.b.x, e.b.y);
      if ((ax < -60 && bx < -60) || (ax > W + 60 && bx > W + 60) ||
          (ay < -60 && by < -60) || (ay > H + 60 && by > H + 60)) continue;
      ctx.strokeStyle = (dim && pathIds && pathSet.has(e.a.id) && pathSet.has(e.b.id))
        ? 'rgba(53,224,240,' + alphaE + ')'
        : 'rgba(126,214,224,' + alphaE + ')';
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    }

    /* pulses travelling a link */
    if (showPulse) {
      if (now - lastPulse > PULSE_EVERY && edges.length && !dim) {
        lastPulse = now;
        const e = edges[(Math.random() * edges.length) | 0];
        if (e.a.on && e.b.on) pulses.push({ e: e, t0: now });
      }
      pulses = pulses.filter(function (p) { return now - p.t0 < PULSE_MS; });
      pulses.forEach(function (p) {
        const t = (now - p.t0) / PULSE_MS;
        const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        const x = p.e.a.x + (p.e.b.x - p.e.a.x) * ease;
        const y = p.e.a.y + (p.e.b.y - p.e.a.y) * ease;
        const [sx, sy] = toScreen(x, y);
        const fade = Math.sin(t * Math.PI);
        ctx.beginPath();
        ctx.arc(sx, sy, 2.1 * view.k + 0.9, 0, 6.2832);
        ctx.fillStyle = 'rgba(53,224,240,' + (0.85 * fade) + ')';
        ctx.shadowColor = 'rgba(53,224,240,0.85)'; ctx.shadowBlur = 11 * fade;
        ctx.fill(); ctx.shadowBlur = 0;
      });
    }

    /* nodes */
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (!n.on) continue;
      const target = (hover === n || focused === n) ? 1 : 0;
      n.lift += (target - n.lift) * 0.22;

      let a = 1;
      if (dim) a = litIds.has(n.id) ? 1 : 0.10;
      const [sx, sy] = toScreen(n.x, n.y);
      const rad = (n.r + n.lift * 3.4) * view.k;
      if (sx < -40 || sx > W + 40 || sy < -40 || sy > H + 40) continue;

      if (n.lift > 0.02 || (pathIds && pathSet.has(n.id))) {
        ctx.beginPath();
        ctx.arc(sx, sy, rad + 7 * view.k, 0, 6.2832);
        ctx.fillStyle = hexA(n.color, 0.14 * Math.max(n.lift, pathIds && pathSet.has(n.id) ? 0.9 : 0));
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(sx, sy, rad, 0, 6.2832);
      ctx.fillStyle = hexA(n.color, a);
      if (n.lift > 0.02) { ctx.shadowColor = n.color; ctx.shadowBlur = 16 * n.lift; }
      ctx.fill();
      ctx.shadowBlur = 0;
      if (focused === n || (pathIds && (n.id === pathIds[0] || n.id === pathIds[pathIds.length - 1]))) {
        ctx.beginPath();
        ctx.arc(sx, sy, rad + 3.5, 0, 6.2832);
        ctx.strokeStyle = 'rgba(255,255,255,0.9)'; ctx.lineWidth = 1.4; ctx.stroke();
      }
    }

    /* labels — most connected first, and anything that collides is dropped.
       Without this the hub cluster turns to mush. */
    if (showLabels) {
      const placed = [];
      // Seed the occupied boxes with the node circles themselves, so a label
      // never lands on a dot. Label-vs-label alone is not enough.
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        if (!n.on) continue;
        const [sx, sy] = toScreen(n.x, n.y);
        const rad = (n.r + n.lift * 3.4) * view.k;
        if (sx < -40 || sx > W + 40 || sy < -40 || sy > H + 40) continue;
        placed.push([sx - rad - 2, sy - rad - 2, sx + rad + 2, sy + rad + 2]);
      }
      const order = nodes.slice().sort(function (a, b) {
        const ap = (a === hover || a === focused || (pathSet.has(a.id) ? 1 : 0)) ? 1e6 : 0;
        const bp = (b === hover || b === focused || (pathSet.has(b.id) ? 1 : 0)) ? 1e6 : 0;
        return (bp + b.degree) - (ap + a.degree);
      });
      ctx.textBaseline = 'middle';
      for (let i = 0; i < order.length; i++) {
        const n = order[i];
        if (!n.on) continue;
        const important = n === hover || n === focused || pathSet.has(n.id);
        if (!important) {
          if (dim && !litIds.has(n.id)) continue;
          // The further out you are, the more connected a node has to be to
          // earn a name. Collision rejection below then thins what is left.
          if (view.k < 0.42) continue;
          if (view.k < 0.75 && n.degree < 10) continue;
          if (view.k < 1.30 && n.degree < 4) continue;
          if (view.k < 2.00 && n.degree < 2) continue;
        }
        const [sx, sy] = toScreen(n.x, n.y);
        if (sx < -80 || sx > W + 80 || sy < -30 || sy > H + 30) continue;

        const size = important ? 12.5 : Math.min(12, 9 + Math.sqrt(n.degree) * 0.6);
        ctx.font = (important ? '600 ' : '500 ') + size + 'px -apple-system, Inter, Segoe UI, sans-serif';
        let text = n.title;
        if (text.length > 30) text = text.slice(0, 29) + '…';
        const w = ctx.measureText(text).width;
        const x = sx + (n.r + n.lift * 3.4) * view.k + 6;
        const y = sy;
        const box = [x - 2, y - size * 0.62, x + w + 2, y + size * 0.62];

        let clash = false;
        for (let p = 0; p < placed.length; p++) {
          const q = placed[p];
          if (box[0] < q[2] && box[2] > q[0] && box[1] < q[3] && box[3] > q[1]) { clash = true; break; }
        }
        if (clash) continue;
        placed.push(box);

        const la = important ? 0.98 : (dim ? 0.55 : Math.min(0.72, 0.30 + n.degree * 0.035));
        ctx.fillStyle = important ? 'rgba(255,255,255,' + la + ')' : hexA(n.color, la);
        if (important) { ctx.shadowColor = 'rgba(0,0,0,0.9)'; ctx.shadowBlur = 6; }
        ctx.fillText(text, x, y);
        ctx.shadowBlur = 0;
      }
    }
  }

  function frame(now) {
    tick();
    draw(now || performance.now());
    raf = requestAnimationFrame(frame);
  }
  function start() { if (!running) { running = true; raf = requestAnimationFrame(frame); } }

  /* ---- picking -------------------------------------------------------- */
  function pick(sx, sy) {
    const [wx, wy] = toWorld(sx, sy);
    let best = null, bestD = Infinity;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (!n.on) continue;
      const d = Math.hypot(n.x - wx, n.y - wy);
      const hitR = Math.max(n.r + 5, 11 / view.k);
      if (d < hitR && d < bestD) { bestD = d; best = n; }
    }
    return best;
  }

  /* ---- viewport ------------------------------------------------------- */
  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = cv.clientWidth || window.innerWidth;
    H = cv.clientHeight || window.innerHeight;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  }

  function fit(instant) {
    const live = nodes.filter(function (n) { return n.on; });
    if (!live.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    live.forEach(function (n) {
      x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
    });
    const padL = 350, padR = 260, padY = 120;
    const availW = Math.max(240, W - padL - padR);
    const availH = Math.max(240, H - padY * 2);
    const k = Math.min(availW / Math.max(1, x1 - x0),
                       availH / Math.max(1, y1 - y0), 2.4);
    const target = {
      k: Math.max(0.18, k * 0.94),
      x: 0, y: 0
    };
    target.x = (W + padL - padR) / 2 - ((x0 + x1) / 2) * target.k;
    target.y = H / 2 - ((y0 + y1) / 2) * target.k;
    if (instant) { view = target; return; }
    const from = { x: view.x, y: view.y, k: view.k }, t0 = performance.now();
    (function ease() {
      const t = Math.min(1, (performance.now() - t0) / 420);
      const e = 1 - Math.pow(1 - t, 3);
      view.x = from.x + (target.x - from.x) * e;
      view.y = from.y + (target.y - from.y) * e;
      view.k = from.k + (target.k - from.k) * e;
      if (t < 1) requestAnimationFrame(ease);
    })();
  }

  function centerOn(n, zoom) {
    const target = { k: zoom || Math.max(view.k, 1.15), x: 0, y: 0 };
    target.x = (W + 350 - 260) / 2 - n.x * target.k;
    target.y = H / 2 - n.y * target.k;
    const from = { x: view.x, y: view.y, k: view.k }, t0 = performance.now();
    (function ease() {
      const t = Math.min(1, (performance.now() - t0) / 460);
      const e = 1 - Math.pow(1 - t, 3);
      view.x = from.x + (target.x - from.x) * e;
      view.y = from.y + (target.y - from.y) * e;
      view.k = from.k + (target.k - from.k) * e;
      if (t < 1) requestAnimationFrame(ease);
    })();
  }

  /* ---- input ---------------------------------------------------------- */
  function bind() {
    cv.addEventListener('mousemove', function (ev) {
      const sx = ev.clientX, sy = ev.clientY;
      if (drag) {
        autoFit = false;
        const [wx, wy] = toWorld(sx, sy);
        drag.node.x = wx - drag.dx; drag.node.y = wy - drag.dy;
        drag.node.vx = 0; drag.node.vy = 0;
        alpha = Math.max(alpha, 0.55); moved = true;
        return;
      }
      if (panning) {
        autoFit = false;
        view.x = panning.vx + (sx - panning.sx);
        view.y = panning.vy + (sy - panning.sy);
        moved = true;
        return;
      }
      const n = pick(sx, sy);
      if (n !== hover) {
        hover = n;
        cv.classList.toggle('picking', !!n);
        if (hooks.onHover) hooks.onHover(n, sx, sy);
      } else if (n && hooks.onHover) hooks.onHover(n, sx, sy);
    });

    cv.addEventListener('mousedown', function (ev) {
      moved = false;
      const n = pick(ev.clientX, ev.clientY);
      if (n) {
        const [wx, wy] = toWorld(ev.clientX, ev.clientY);
        drag = { node: n, dx: wx - n.x, dy: wy - n.y };
      } else {
        panning = { sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y };
        cv.classList.add('dragging');
      }
    });

    window.addEventListener('mouseup', function (ev) {
      const wasDrag = drag, wasMoved = moved;
      drag = null; panning = null; cv.classList.remove('dragging');
      if (!wasMoved) {
        const n = pick(ev.clientX, ev.clientY);
        if (n) {
          if (ev.shiftKey && focused && focused !== n) {
            if (hooks.onTrace) hooks.onTrace(focused.id, n.id);
          } else {
            pathIds = null; pathSet = new Set();
            focused = n;
            if (hooks.onFocus) hooks.onFocus(n.id);
          }
        } else if (!wasDrag) {
          focused = null; pathIds = null; pathSet = new Set();
          if (hooks.onFocus) hooks.onFocus(null);
        }
      }
    });

    cv.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      autoFit = false;
      const f = Math.exp(-ev.deltaY * 0.0016);
      const k2 = Math.min(4.5, Math.max(0.12, view.k * f));
      const [wx, wy] = toWorld(ev.clientX, ev.clientY);
      view.k = k2;
      view.x = ev.clientX - wx * k2;
      view.y = ev.clientY - wy * k2;
    }, { passive: false });

    cv.addEventListener('mouseleave', function () {
      hover = null; if (hooks.onHover) hooks.onHover(null);
    });

    window.addEventListener('resize', resize);
  }

  /* ---- api ------------------------------------------------------------ */
  global.Graph = {
    KIND_COLOR: KIND_COLOR,
    colorFor: colorFor,
    init: function (canvas, h) {
      cv = canvas; ctx = cv.getContext('2d', { alpha: true });
      hooks = h || {}; resize(); bind();
    },
    load: load,
    focus: function (id, quiet) {
      const n = byId.get(id);
      if (!n) return false;
      pathIds = null; pathSet = new Set();
      focused = n; hover = null;
      if (!n.on) { hidden.delete(n.kind); applyFilter(); }
      centerOn(n);
      if (!quiet && hooks.onFocus) hooks.onFocus(id);
      return true;
    },
    clearFocus: function () { focused = null; pathIds = null; pathSet = new Set(); },
    showPath: function (ids) {
      pathIds = ids && ids.length ? ids : null;
      pathSet = new Set(ids || []);
      if (pathIds) {
        const live = ids.map(function (i) { return byId.get(i); }).filter(Boolean);
        if (live.length) {
          const cx = live.reduce(function (s, n) { return s + n.x; }, 0) / live.length;
          const cy = live.reduce(function (s, n) { return s + n.y; }, 0) / live.length;
          centerOn({ x: cx, y: cy }, Math.min(view.k, 1.0));
        }
      }
    },
    toggleKind: function (kind) {
      if (hidden.has(kind)) hidden.delete(kind); else hidden.add(kind);
      applyFilter();
      return !hidden.has(kind);
    },
    hiddenKinds: function () { return hidden; },
    setLabels: function (v) { showLabels = v; },
    setPulse: function (v) { showPulse = v; if (!v) pulses = []; },
    fit: function () { fit(false); },
    reheat: function () { alpha = Math.max(alpha, 0.85); },
    adjacency: function () { return adj; },
    __view: function () { return view; },
    node: function (id) { return byId.get(id); }
  };

  function applyFilter() {
    nodes.forEach(function (n) { n.on = !hidden.has(n.kind); });
    if (focused && !focused.on) focused = null;
    alpha = Math.max(alpha, 0.7);
  }
})(window);
