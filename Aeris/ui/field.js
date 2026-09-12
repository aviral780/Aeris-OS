/* Aeris — the voice field.

   A scatter of dots across the whole window that breathes on its own and
   moves to whatever is being said. Purple through blue, shifting slowly.

   It sits at z-index 0, behind the graph, so it fills the room the graph
   leaves empty — which on a fresh vault is all of it — without ever
   competing with a node for attention.

   Two deliberate choices about cost, because this runs at 60fps behind
   everything else:

   * Neighbour pairs are worked out once per layout, not per frame. Drawing
     constellation lines by scanning every dot against every other dot is
     O(n²) sixty times a second, which is how a background animation ends up
     costing more than the assistant it decorates.
   * Dots are drawn as squares via fillRect when they are small. An arc()
     plus fill() per dot per frame is the single most expensive thing in a
     field this size, and below about three pixels nobody can tell.

   app.js drives it: AerisField.push(state, level, bands) once per frame.
   It holds no opinion about audio and never touches the microphone itself.
*/
(function () {
  'use strict';

  var canvas = document.createElement('canvas');
  canvas.id = 'field';
  var graph = document.getElementById('graph');
  if (graph && graph.parentNode) graph.parentNode.insertBefore(canvas, graph);
  else document.body.appendChild(canvas);

  var c = canvas.getContext('2d');
  var W = 0, H = 0, dpr = 1;
  var dots = [], pairs = [];
  var phase = 0;

  // What the field is reacting to. Set from outside, eased inside — a jump
  // straight to a new level reads as a flicker rather than a response.
  var target = 0, energy = 0, state = 'idle';
  var bands = [];

  var PURPLE = [168, 85, 247];
  var BLUE = [56, 189, 248];
  var HOT = [236, 110, 240];        // only at real volume, and only briefly

  function mix(a, b, t) {
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    return [a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t];
  }

  function rgba(col, alpha) {
    return 'rgba(' + (col[0] | 0) + ',' + (col[1] | 0) + ',' + (col[2] | 0) + ',' + alpha + ')';
  }

  var BANDS = 24;

  function layout() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    c.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Density by area, not a fixed count: the same number of dots that reads
    // as a field on a laptop reads as dust on a 5K display.
    var count = Math.round((W * H) / 9000);
    count = Math.max(70, Math.min(300, count));

    var cx = W / 2, cy = H / 2;
    var maxR = Math.sqrt(cx * cx + cy * cy);
    dots = [];
    for (var i = 0; i < count; i++) {
      // Phyllotaxis, so the scatter is even without looking like a grid and
      // without the clumping that plain random placement gives.
      var t = i / count;
      var a = i * 2.39996323;                 // golden angle
      var r = Math.sqrt(t) * maxR * 1.02;
      var x = cx + Math.cos(a) * r;
      var y = cy + Math.sin(a) * r * 0.82;    // slightly flattened: screens are wide
      dots.push({
        hx: x, hy: y, x: x, y: y,
        ang: a,
        // Band by angle, so different frequencies push in different
        // directions and the field looks like it is listening rather than
        // just throbbing.
        band: Math.floor(((a % 6.2832) / 6.2832) * BANDS) % BANDS,
        depth: 0.35 + 0.65 * (1 - t),         // near dots brighter and larger
        drift: 0.4 + Math.random() * 1.1,
        seed: Math.random() * 6.2832,
        size: 0
      });
    }
    for (var j = 0; j < dots.length; j++) {
      dots[j].size = 0.7 + dots[j].depth * 1.9;
    }
    buildPairs();
  }

  function buildPairs() {
    pairs = [];
    var REACH = Math.min(150, Math.max(80, W / 12));
    for (var i = 0; i < dots.length; i++) {
      var found = 0;
      for (var j = i + 1; j < dots.length && found < 2; j++) {
        var dx = dots[i].hx - dots[j].hx, dy = dots[i].hy - dots[j].hy;
        var d2 = dx * dx + dy * dy;
        if (d2 < REACH * REACH) { pairs.push([i, j]); found++; }
      }
    }
  }

  function draw() {
    // Ease towards whatever app.js last reported. Fast up, slow down: a
    // consonant should register instantly, and the decay is what makes it
    // look like it is settling rather than being switched off.
    energy += (target - energy) * (target > energy ? 0.34 : 0.07);
    phase += 0.006;

    c.clearRect(0, 0, W, H);

    var hue = (Math.sin(phase * 0.42) + 1) / 2;         // purple <-> blue drift
    var base = mix(PURPLE, BLUE, hue);
    if (state === 'error') base = [255, 92, 114];
    var lively = state === 'listening' || state === 'speaking';
    var reach = energy * 26;

    var i, d;
    for (i = 0; i < dots.length; i++) {
      d = dots[i];
      var b = bands.length ? bands[d.band % bands.length] : 0;

      // Ambient motion, always on. Two unrelated frequencies so it never
      // settles into a visible cycle.
      var wob = Math.sin(phase * 1.6 * d.drift + d.seed) * 3.4 * d.depth
              + Math.cos(phase * 0.9 + d.seed * 1.7) * 2.1;

      var push = lively ? (b * reach + energy * 7) * d.depth : energy * 4 * d.depth;
      d.x = d.hx + Math.cos(d.ang) * (wob + push);
      d.y = d.hy + Math.sin(d.ang) * (wob + push) * 0.9;
    }

    // Constellation lines first, so dots sit on top of them.
    if (energy > 0.04 || state === 'thinking') {
      var lineA = Math.min(0.3, 0.035 + energy * 0.5);
      c.strokeStyle = rgba(base, lineA);
      c.lineWidth = 0.7;
      c.beginPath();
      for (i = 0; i < pairs.length; i++) {
        var p = pairs[i], a = dots[p[0]], bb = dots[p[1]];
        c.moveTo(a.x, a.y);
        c.lineTo(bb.x, bb.y);
      }
      c.stroke();
    }

    for (i = 0; i < dots.length; i++) {
      d = dots[i];
      var bandE = bands.length ? bands[d.band % bands.length] : 0;
      var col = bandE > 0.62 && lively ? mix(base, HOT, (bandE - 0.62) * 2.2) : base;
      var alpha = (0.14 + d.depth * 0.26) * (0.55 + energy * 0.9)
                + (lively ? bandE * 0.35 : 0);
      if (alpha > 0.92) alpha = 0.92;
      var s = d.size * (1 + (lively ? bandE * 0.9 : 0) + energy * 0.35);

      c.fillStyle = rgba(col, alpha);
      if (s < 3) {
        // Cheap path: a square this small is indistinguishable from a disc.
        c.fillRect(d.x - s / 2, d.y - s / 2, s, s);
      } else {
        c.beginPath();
        c.arc(d.x, d.y, s / 2, 0, 6.2832);
        c.fill();
      }
    }

    requestAnimationFrame(draw);
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(layout, 120);
  });

  layout();
  requestAnimationFrame(draw);

  window.AerisField = {
    /* level: 0..1 overall loudness. bands: array of 0..1 per frequency band. */
    push: function (s, level, freqBands) {
      state = s || 'idle';
      target = Math.max(0, Math.min(1, level || 0));
      if (freqBands && freqBands.length) bands = freqBands;
      else if (!level) bands = [];
    }
  };
}());
