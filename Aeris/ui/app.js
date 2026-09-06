/* Aeris — browser side.
 *
 * Voice goes through the Python server in both directions. The Web Speech API
 * is deliberately not used: it is Chrome-only, it ships audio to Google, and
 * in Brave it is a stub that fails silently — you talk and nothing happens,
 * with no error. MediaRecorder + server-side Scribe works everywhere and,
 * when it breaks, says so on screen.
 */
(function () {
  'use strict';

  /* ================= turn-taking — tune these ========================= */
  const SILENCE_HANG_MS  = 900;   // quiet for this long ends your turn
  const SPEECH_LEVEL     = 0.055; // RMS above this counts as you talking
  const SILENCE_LEVEL    = 0.030; // RMS below this counts as quiet
  const LEVEL_TICK_MS    = 50;    // setInterval, NOT rAF — rAF dies in a
                                  // background tab and the mic goes deaf
                                  // with no error at all
  const LEAD_IN_MS       = 800;   // grace period before silence can end a turn
  const MIN_UTTERANCE_MS = 450;   // shorter than this is a cough, not a turn
  const MAX_UTTERANCE_MS = 25000; // hard stop so a stuck mic cannot run on
  const MAX_SPEECH_MS    = 90000; // she is never allowed to hold the turn longer
  const REARM_TICK_MS    = 1500;  // watchdog: hands-free must never die quietly
  /* =================================================================== */

  const $ = function (s) { return document.querySelector(s); };
  const el = {
    canvas: $('#graph'), reactor: $('#reactor'), stateLabel: $('#state-label'),
    badges: $('#badges'), left: $('#left'), inspector: $('#inspector'),
    hubs: $('#hubs'), filters: $('#filters'), search: $('#search'),
    results: $('#results'), ask: $('#ask'), caption: $('#caption'),
    card: $('#card'), tip: $('#tip'), toasts: $('#toasts'), bars: $('#bars'),
    mic: $('#btn-mic'), mute: $('#btn-mute'), send: $('#btn-send'),
    wake: $('#btn-wake'), share: $('#btn-share'),
    brandSub: $('#brand-sub'), modeChip: $('#mode-chip')
  };
  const barEls = Array.prototype.slice.call(el.bars.children);

  let graphData = null, status = null;
  let state = 'idle';            // idle | listening | thinking | speaking
  let muted = false, continuous = false;
  let level = 0;

  const EXAMPLES = [
    'what did I write about prompt injection?',
    'how much has Marwah Clinic paid?',
    'brief me',
    'plan my day',
    'read my inbox',
    'remember that I prefer per-case pricing',
    'which model has the best F1?',
    'who is Bluecrest Payments?'
  ];

  /* ---------------------------------------------------------- utilities */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function toast(msg, kind, ms) {
    const t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = msg;
    el.toasts.appendChild(t);
    setTimeout(function () {
      t.style.transition = 'opacity .3s'; t.style.opacity = '0';
      setTimeout(function () { t.remove(); }, 320);
    }, ms || 5200);
  }
  function caption(html, cls) {
    el.caption.innerHTML = cls ? '<span class="' + cls + '">' + html + '</span>' : html;
    el.caption.classList.toggle('show', !!html);
  }
  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return res;
  }
  const json = function (p, o) { return api(p, o).then(function (r) { return r.json(); }); };

  /* ------------------------------------------------------------ reactor */
  const rctx = el.reactor.getContext('2d');
  const R_SIZE = 396, R_MID = R_SIZE / 2;
  let rPhase = 0;

  function drawReactor() {
    const c = rctx;
    c.clearRect(0, 0, R_SIZE, R_SIZE);
    rPhase += 0.014;

    const palette = {
      idle:      ['#35e0f0', 0.30],
      // Awake but not listening to him. Dimmer than listening on purpose —
      // the ring should not look like it is taking anything in.
      waiting:   ['#8a7bd8', 0.45],
      listening: ['#35e0f0', 1.00],
      thinking:  ['#f6b73c', 0.85],
      speaking:  ['#7ef0a0', 0.95],
      error:     ['#ff5c72', 0.90]
    }[state] || ['#35e0f0', 0.3];
    const col = palette[0], energy = palette[1];
    const react = state === 'listening' ? Math.min(1, level * 9)
                : state === 'speaking' ? Math.min(1, level * 7)
                : 0;

    /* outer tick ring */
    c.save(); c.translate(R_MID, R_MID);
    const ticks = 72;
    for (let i = 0; i < ticks; i++) {
      const a = (i / ticks) * Math.PI * 2 - Math.PI / 2;
      const wave = Math.sin(rPhase * 2 + i * 0.4);
      const len = 8 + (state === 'idle' ? wave * 1.5 : wave * 4 * energy) + react * 12;
      const r0 = 176, r1 = r0 - Math.max(3, len);
      c.beginPath();
      c.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
      c.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
      c.strokeStyle = 'rgba(255,255,255,' + (0.05 + 0.10 * energy) + ')';
      c.lineWidth = 1.4; c.stroke();
    }

    /* rings */
    [[152, 0.11], [128, 0.07]].forEach(function (r) {
      c.beginPath(); c.arc(0, 0, r[0], 0, 6.2832);
      c.strokeStyle = 'rgba(255,255,255,' + r[1] + ')'; c.lineWidth = 1; c.stroke();
    });

    /* the state arc — sweeps while thinking, pulses otherwise */
    const sweep = state === 'thinking' ? 1.5 : (0.7 + react * 1.8);
    const start = state === 'thinking' ? rPhase * 2.4 : -Math.PI / 2 - sweep / 2;
    c.beginPath(); c.arc(0, 0, 152, start, start + sweep);
    c.strokeStyle = col; c.lineWidth = 2.4; c.lineCap = 'round';
    c.shadowColor = col; c.shadowBlur = 16 * energy; c.stroke(); c.shadowBlur = 0;

    /* amber counter-arc, always present, quiet */
    c.beginPath();
    c.arc(0, 0, 165, -rPhase * 0.6, -rPhase * 0.6 + 0.9);
    c.strokeStyle = 'rgba(246,183,60,0.42)'; c.lineWidth = 1.6; c.stroke();

    /* core */
    const coreR = 74 + react * 12 + Math.sin(rPhase * 1.6) * 2;
    const grad = c.createRadialGradient(0, 0, 4, 0, 0, coreR);
    grad.addColorStop(0, hexA(col, 0.30 + 0.34 * energy));
    grad.addColorStop(0.6, hexA(col, 0.07));
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    c.beginPath(); c.arc(0, 0, coreR, 0, 6.2832); c.fillStyle = grad; c.fill();
    c.beginPath(); c.arc(0, 0, 96, 0, 6.2832);
    c.strokeStyle = hexA(col, 0.30 + 0.4 * energy); c.lineWidth = 1.2; c.stroke();

    /* wordmark */
    c.textAlign = 'center'; c.textBaseline = 'middle';
    c.font = '700 25px -apple-system, Inter, Segoe UI, sans-serif';
    c.letterSpacing = '10px';
    c.fillStyle = hexA(col, 0.55 + 0.4 * energy);
    c.shadowColor = col; c.shadowBlur = 12 * energy;
    c.fillText('AERIS', 5, 0);
    c.shadowBlur = 0;
    c.restore();
    requestAnimationFrame(drawReactor);
  }
  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function setState(s) {
    state = s;
    el.stateLabel.textContent = s === 'waiting' ? 'waiting for “' + wakeWordLabel() + '”' : s;
    el.mic.classList.toggle('rec', s === 'listening');
    el.mic.classList.toggle('on', s === 'thinking' || s === 'speaking');
  }

  /* ---- the level bars. setInterval so a background tab keeps working -- */
  setInterval(function () {
    const active = state === 'listening' || state === 'speaking';
    for (let i = 0; i < barEls.length; i++) {
      const centre = 1 - Math.abs(i - (barEls.length - 1) / 2) / barEls.length;
      const h = active
        ? 3 + Math.min(17, level * 150 * (0.55 + centre) * (0.75 + Math.random() * 0.5))
        : 3;
      barEls[i].style.height = h.toFixed(1) + 'px';
      barEls[i].style.opacity = active ? String(0.5 + Math.min(0.5, level * 5)) : '0.35';
    }
  }, LEVEL_TICK_MS);

  /* --------------------------------------------------------------- boot */
  async function boot() {
    Graph.init(el.canvas, { onFocus: onFocus, onHover: onHover, onTrace: onTrace });
    drawReactor();
    rotateExample();

    try {
      status = await json('/api/status');
    } catch (e) {
      caption('Cannot reach the Aeris server. Is <code>python3 -m agent.main</code> still running?', 'err');
      toast('Server unreachable: ' + e.message, 'bad', 12000);
      setState('error');
      return;
    }
    renderBadges();

    try {
      graphData = await json('/api/graph');
    } catch (e) {
      toast('Could not load the graph: ' + e.message, 'bad', 12000);
      return;
    }
    window.__gd = graphData;
    Graph.load(graphData);
    renderMeta(); renderFilters(); renderHubs();
    (graphData.warnings || []).forEach(function (w) { toast(w, 'warn', 12000); });
  }

  function renderMeta() {
    el.brandSub.textContent = graphData.total_notes + ' notes · ' +
      graphData.total_edges + ' connections';
    el.modeChip.textContent = graphData.mode;
    el.modeChip.className = 'mode-chip ' + graphData.mode;
    el.modeChip.title = graphData.mode === 'demo'
      ? 'Invented fixtures. Safe to screen-record. Set AERIS_DEMO=0 in Aeris/.env for your real folders.'
      : 'YOUR REAL FOLDERS, read-only. Set AERIS_DEMO=1 to go back to fixtures.';
  }

  function renderBadges() {
    const out = [];
    if (!status.model.ok) {
      out.push(['MODEL OFFLINE', 'warn',
        'No language model is reachable. Conversation vs search is decided by scoring your ' +
        'question against the file index — that is keyword matching, not a model. ' +
        (status.model.detail || '')]);
    } else {
      out.push([status.model.backend.toUpperCase() + ' · ' + status.model.name, 'ok',
        status.model.detail]);
    }
    if (!status.voice.ok) out.push(['NO VOICE', 'bad', status.voice.detail]);
    else out.push(['ELEVENLABS', 'ok', 'Speech in and out. The key stays on the server.']);
    if (status.usage && status.usage.budget_hit) {
      out.push(['SPEECH BUDGET SPENT', 'bad', 'No further ElevenLabs spend this session.']);
    }
    el.badges.innerHTML = out.map(function (b) {
      return '<span class="badge ' + b[1] + '" title="' + esc(b[2]) + '">' + esc(b[0]) + '</span>';
    }).join('');
    if (!status.model.ok) {
      toast('No language model reachable — routing by scoring your question against the files. ' +
            'It is not pretending to be a model.', 'warn', 10000);
    }
    if (!status.voice.ok) toast(status.voice.detail, 'bad', 12000);
  }

  function renderFilters() {
    const counts = graphData.counts || {};
    el.filters.innerHTML = Object.keys(counts).map(function (k) {
      return '<div class="filter" data-kind="' + esc(k) + '">' +
        '<span class="dot" style="background:' + Graph.colorFor(k) + '"></span>' +
        '<span class="filter-t">' + esc(k) + '</span>' +
        '<span class="filter-n">' + counts[k] + '</span></div>';
    }).join('');
    el.filters.querySelectorAll('.filter').forEach(function (node) {
      node.addEventListener('click', function () {
        const on = Graph.toggleKind(node.dataset.kind);
        node.classList.toggle('off', !on);
      });
    });
  }

  function renderHubs() {
    el.hubs.innerHTML = (graphData.hubs || []).slice(0, 8).map(function (h) {
      return '<div class="hub" data-id="' + esc(h.id) + '">' +
        '<span class="dot" style="background:' + Graph.colorFor(h.kind) + '"></span>' +
        '<span class="hub-t">' + esc(h.title) + '</span>' +
        '<span class="hub-n">' + h.degree + '</span></div>';
    }).join('');
    el.hubs.querySelectorAll('.hub').forEach(function (node) {
      node.addEventListener('click', function () { Graph.focus(node.dataset.id); });
    });
  }

  /* ------------------------------------------------------------- graph */
  function onHover(node, x, y) {
    if (!node) { el.tip.classList.remove('show'); return; }
    el.tip.innerHTML = '<div class="tip-t">' + esc(node.title) + '</div>' +
      '<div class="tip-m">' + esc(node.kind) + ' · ' + node.degree +
      ' links — click to focus</div>';
    el.tip.classList.add('show');
    const r = el.tip.getBoundingClientRect();
    el.tip.style.left = Math.min(x + 16, window.innerWidth - r.width - 12) + 'px';
    el.tip.style.top = Math.min(y + 16, window.innerHeight - r.height - 12) + 'px';
  }

  /* Just enough markdown for a note to read like a note. Runs on text that
     has already been escaped, so it can only ever add the tags below. */
  function md(escaped) {
    const lines = escaped.split('\n');
    const out = [];
    let inList = false, inQuote = false;
    const close = function () {
      if (inList) { out.push('</ul>'); inList = false; }
      if (inQuote) { out.push('</blockquote>'); inQuote = false; }
    };
    const inline = function (t) {
      return t
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>');
    };
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      const line = raw.trim();
      let m;
      if (!line) { close(); continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        close();
        out.push('<h' + Math.min(4, m[1].length + 2) + '>' + inline(m[2]) +
                 '</h' + Math.min(4, m[1].length + 2) + '>');
      } else if ((m = line.match(/^[-*+]\s+(.*)$/))) {
        if (inQuote) { out.push('</blockquote>'); inQuote = false; }
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + inline(m[1]) + '</li>');
      } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
        if (inList) { out.push('</ul>'); inList = false; }
        if (!inQuote) { out.push('<blockquote>'); inQuote = true; }
        out.push(inline(m[1]) + ' ');
      } else if (/^-{3,}$/.test(line)) {
        close(); out.push('<div class="hr"></div>');
      } else {
        close(); out.push('<p>' + inline(line) + '</p>');
      }
    }
    close();
    return out.join('');
  }

  async function onFocus(id) {
    if (!id) {
      el.inspector.innerHTML =
        '<div class="hint">Click a node to focus it — only that node and its links stay lit, ' +
        'and the note opens here. Shift-click a second node to trace the shortest path.</div>';
      return;
    }
    let n;
    try { n = await json('/api/note?id=' + encodeURIComponent(id)); }
    catch (e) { el.inspector.innerHTML = '<div class="warn">' + esc(e.message) + '</div>'; return; }

    const body = md(esc(n.body)).replace(/\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g, function (m, t, a) {
      return '<a class="wl" data-t="' + esc(t) + '">' + esc(a || t) + '</a>';
    });
    let html =
      '<div class="note-kind" style="color:' + Graph.colorFor(n.kind) + '">' +
        '<span class="dot" style="background:' + Graph.colorFor(n.kind) + '"></span>' +
        esc(n.kind) + '</div>' +
      '<div class="note-title">' + esc(n.title) + '</div>' +
      '<div class="note-meta">' + esc(n.path) + ' · ' + n.degree + ' links · ' +
        esc(n.modified) + '</div>';

    if (n.extract === 'partial' || n.extract === 'none') {
      html += '<div class="warn"><b>PDF text ' + esc(n.extract) + '.</b> This file is in the ' +
        'graph but little or none of it could be read, so it will not answer searches well.</div>';
    }
    if (n.injection && n.injection.length) {
      html += '<div class="warn"><b>This file contains instructions aimed at an assistant.</b> ' +
        'Reported, not followed:<br>' +
        n.injection.map(function (l) { return '“' + esc(l) + '”'; }).join('<br>') + '</div>';
    }
    if (n.tags && n.tags.length) {
      html += '<div class="linkrow">' + n.tags.map(function (t) {
        return '<span class="chip static">' + esc(t) + '</span>'; }).join('') + '</div>';
    }
    html += '<div class="note-body">' + body + '</div>';
    if (n.links.length || n.backlinks.length) {
      html += '<div class="hr"></div><div class="eyebrow">Links</div><div class="linkrow">' +
        n.links.concat(n.backlinks).slice(0, 24).map(function (l) {
          return '<span class="chip" data-id="' + esc(l.id) + '">' + esc(l.title) + '</span>';
        }).join('') + '</div>';
    }
    el.inspector.innerHTML = html;
    el.inspector.scrollTop = 0;
    el.inspector.querySelectorAll('.chip[data-id]').forEach(function (c) {
      c.addEventListener('click', function () { Graph.focus(c.dataset.id); });
    });
    el.inspector.querySelectorAll('.wl').forEach(function (a) {
      a.addEventListener('click', function () {
        const t = a.dataset.t.toLowerCase();
        const hit = (graphData.nodes || []).find(function (nd) {
          return nd.title.toLowerCase() === t;
        });
        if (hit) Graph.focus(hit.id); else toast('“' + a.dataset.t + '” is not a note that exists.', 'warn');
      });
    });
  }

  async function onTrace(a, b) {
    try {
      const r = await json('/api/path?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b));
      if (!r.found) { toast('No path between those two — they are in separate clusters.', 'warn'); return; }
      Graph.showPath(r.path);
      showCard({
        title: 'shortest path',
        subtitle: r.hops + ' hop' + (r.hops === 1 ? '' : 's'),
        note: 'Every step is a [[wikilink]] you wrote.',
        items: r.path.map(function (id, i) {
          return { title: (i + 1) + '. ' + r.titles[i], id: id, meta: i === 0 ? 'from'
                   : (i === r.path.length - 1 ? 'to' : 'via') };
        })
      });
    } catch (e) { toast(e.message, 'bad'); }
  }

  /* ------------------------------------------------------------ search */
  el.search.addEventListener('input', function () {
    const q = el.search.value.trim().toLowerCase();
    if (q.length < 2) { el.results.innerHTML = ''; return; }
    const hits = (graphData.nodes || []).filter(function (n) {
      return n.title.toLowerCase().indexOf(q) >= 0 || n.kind.indexOf(q) === 0 ||
             (n.tags || []).some(function (t) { return t.indexOf(q) === 0; });
    }).sort(function (a, b) { return b.degree - a.degree; }).slice(0, 8);
    el.results.innerHTML = hits.length ? hits.map(function (n) {
      return '<div class="result" data-id="' + esc(n.id) + '">' +
        '<div class="result-t">' + esc(n.title) + '</div>' +
        '<div class="result-m">' + esc(n.kind) + ' · ' + n.degree + ' links</div></div>';
    }).join('') : '<div class="result"><div class="result-m">no title matches — ' +
      'ask in the bar below to search inside the files</div></div>';
    el.results.querySelectorAll('.result[data-id]').forEach(function (r) {
      r.addEventListener('click', function () {
        Graph.focus(r.dataset.id); el.results.innerHTML = ''; el.search.value = '';
      });
    });
  });

  /* -------------------------------------------------------------- card */
  function showCard(card) {
    if (!card) { el.card.classList.remove('show'); return; }
    let h = '<div class="card-head"><span class="card-tool">' + esc(card.title || 'result') +
      '</span><button class="card-x" title="close">&times;</button></div>';
    if (card.subtitle) h += '<div class="card-sub">' + esc(card.subtitle) + '</div>';
    if (card.query) h += '<div class="card-title">' + esc(card.query) + '</div>';

    (card.rows || []).forEach(function (r) {
      h += '<div class="row"><div class="row-k">' + esc(r.k) + '</div><div class="row-v">' +
        (r.v || '') + (r.qualifier ? '<span class="qual">↳ ' + esc(r.qualifier) + '</span>' : '') +
        '</div></div>';
    });

    if (card.injection && card.injection.length) {
      h += '<div class="warn"><b>Instructions found inside your data.</b> Reported, not ' +
        'followed:<br>' + card.injection.map(function (i) {
          return esc(i.file) + ': “' + esc(i.line) + '”'; }).join('<br>') + '</div>';
    }
    if (card.confirm) {
      h += '<div class="confirm">' +
           '<div class="confirm-q">' + esc(card.confirm.summary) + '</div>' +
           '<div class="confirm-row">' +
             '<button class="confirm-yes">Do it</button>' +
             '<button class="confirm-no">No</button>' +
           '</div>' +
           '<div class="confirm-note">Nothing has happened yet.</div>' +
           '</div>';
    }
    h += renderItems(card.items);
    if (card.own && card.own.length) {
      h += '<div class="hr"></div><div class="eyebrow">' +
        esc(card.own_label || 'From your own files') + '</div>' + renderItems(card.own);
    }
    if (card.note) h += '<div class="hr"></div><div class="card-sub">' + esc(card.note) + '</div>';

    el.card.innerHTML = h;
    el.card.classList.add('show');
    el.card.scrollTop = 0;
    el.card.querySelector('.card-x').addEventListener('click', function () {
      el.card.classList.remove('show');
    });
    el.card.querySelectorAll('.item[data-id]').forEach(function (it) {
      it.addEventListener('click', function () { Graph.focus(it.dataset.id); });
    });

    if (card.confirm) {
      const decide = async function (approved) {
        el.card.querySelectorAll('.confirm button').forEach(function (b) { b.disabled = true; });
        let r;
        try {
          r = await json('/api/confirm', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: card.confirm.token, approved: approved })
          });
        } catch (e) { toast('Could not send that decision: ' + e.message, 'bad', 9000); return; }

        const res = r.result || {};
        const line = r.status === 'denied' ? 'Not done. You said no.'
                   : r.status === 'expired' ? r.summary
                   : (res.summary || 'Done.');
        el.card.querySelector('.confirm').innerHTML =
          '<div class="confirm-done">' + esc(line) + '</div>';
        caption(esc(line));

        // If this approval was what stopped an agent run, pick the run back up.
        if (card.confirm.resume) { runAgent('', card.confirm.resume); return; }
        if (!muted) speak(line);
      };
      el.card.querySelector('.confirm-yes').addEventListener('click', function () { decide(true); });
      el.card.querySelector('.confirm-no').addEventListener('click', function () { decide(false); });
    }
  }

  function renderItems(items) {
    if (!items || !items.length) return '';
    return '<div class="stack">' + items.map(function (i) {
      const clickable = !!i.id;
      let s = '<div class="item' + (clickable ? '' : ' static') + '"' +
        (clickable ? ' data-id="' + esc(i.id) + '"' : '') + '>';
      s += '<div class="item-t">' + esc(i.title) +
           (i.flagged ? ' <span style="color:#ff5c72">⚑</span>' : '') + '</div>';
      if (i.subtitle) s += '<div class="item-s">' + esc(i.subtitle) + '</div>';
      if (i.file) s += '<div class="item-f">' + esc(i.file) + '</div>';
      if (i.url) s += '<div class="item-f">' + esc(i.url).slice(0, 70) + '</div>';
      if (i.meta) s += '<div class="item-s" style="opacity:.6">' + esc(i.meta) + '</div>';
      return s + '</div>';
    }).join('') + '</div>';
  }

  /* --------------------------------------------------------------- ask */
  let asking = false;
  async function ask(text, spoken) {
    text = (text || '').trim();
    if (!text || asking) return;
    // While he is sharing, a question about what is in front of him is
    // answered from the frame rather than from his files.
    if (shareStream && SCREEN_Q.test(text)) { return seeScreen(text); }
    asking = true;
    caption('<span class="you">' + esc(text) + '</span>');
    setState('thinking');
    let r;
    try {
      r = await json('/api/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text })
      });
    } catch (e) {
      asking = false;
      caption('Aeris could not answer: ' + esc(e.message), 'err');
      toast('Ask failed: ' + e.message, 'bad', 9000);
      resumeListening();                      // a failed turn still hands the mic back
      return;
    }
    asking = false;

    caption(esc(r.spoken));
    showCard(r.card);
    if (r.focus) Graph.focus(r.focus, true);
    else if (r.card && r.card.sources && r.card.sources.length) Graph.focus(r.card.sources[0].id, true);
    if (r.model_error) toast('Model call failed, fell back to scoring: ' + r.model_error, 'warn', 9000);

    if (!muted) await speak(r.spoken);
    else resumeListening();
  }

  /* ------------------------------------------------------------- speak */
  /* One audio element for the life of the page. A fresh Audio() per answer
     meant a fresh createMediaElementSource() per answer on the same context —
     the old nodes were never released, and a stale in-flight speak() could
     null out the element the *current* answer was still playing through. */
  let outCtx = null, outAnalyser = null, outSource = null, outEl = null;
  let outUrl = null, speakSeq = 0, endTurn = null;

  /* Call this from a real click. An AudioContext created outside a user
     gesture can stay suspended, and a media element routed into a suspended
     context plays silently and never fires 'ended'. */
  function ensureOutput() {
    if (!outEl) {
      outEl = new Audio();
      outEl.preload = 'auto';
    }
    if (!outCtx) {
      try {
        outCtx = new (window.AudioContext || window.webkitAudioContext)();
        outSource = outCtx.createMediaElementSource(outEl);
        outAnalyser = outCtx.createAnalyser();
        outAnalyser.fftSize = 512;
        outSource.connect(outAnalyser);
        outAnalyser.connect(outCtx.destination);
      } catch (e) { outAnalyser = null; }     // plain playback still works
    }
    if (outCtx && outCtx.state === 'suspended') outCtx.resume().catch(function () {});
  }

  function isPlaying() {
    return !!(outEl && !outEl.paused && !outEl.ended && outEl.currentTime > 0);
  }

  /* The single place that decides what happens when she stops talking. */
  function resumeListening() {
    level = 0;
    if (waking && stream) {
      // Back to waiting for her name, not to an open microphone.
      listenFor = 'wake';
      setState('waiting');
      caption('');
      armRecorder();
      return;
    }
    if (continuous && stream) { listenFor = 'command'; setState('listening'); armRecorder(); }
    else setState('idle');
  }

  async function speak(text) {
    const mine = ++speakSeq;                  // any later call supersedes this
    if (!text) { resumeListening(); return; }

    let blob;
    try {
      const res = await api('/api/speak', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text })
      });
      blob = await res.blob();
    } catch (e) {
      toast('No speech: ' + e.message, 'bad', 9000);
      if (mine === speakSeq) resumeListening();
      return;
    }
    if (mine !== speakSeq) return;

    // The mic must be deaf while she talks, or she transcribes herself
    // through the speakers and talks to herself forever.
    disarmRecorder();
    setState('speaking');
    ensureOutput();

    if (outUrl) URL.revokeObjectURL(outUrl);
    outUrl = URL.createObjectURL(blob);
    outEl.src = outUrl;

    const buf = outAnalyser ? new Uint8Array(outAnalyser.fftSize) : null;
    const meter = setInterval(function () {
      if (!outAnalyser) { level = isPlaying() ? 0.05 + Math.random() * 0.04 : 0; return; }
      outAnalyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; sum += d * d; }
      level = Math.sqrt(sum / buf.length);
    }, LEVEL_TICK_MS);

    await new Promise(function (resolve) {
      let done = false;
      const finish = function () {
        if (done) return;
        done = true;
        clearTimeout(guard);
        if (endTurn === finish) endTurn = null;
        resolve();
      };
      // stopSpeaking() reaches in through this, so a barge-in always lands
      // even after playback has already ended.
      endTurn = finish;
      // Last resort: a routed element that never fires 'ended' must not wedge
      // the turn forever. Generous, and only ever fires when something broke.
      const guard = setTimeout(finish, MAX_SPEECH_MS);
      outEl.onended = finish;
      outEl.onerror = function () {
        toast('The browser could not play that audio.', 'bad'); finish();
      };
      outEl.play().catch(function () {
        toast('Playback blocked by the browser — click anywhere once, then try again.',
              'warn', 9000);
        finish();
      });
    });

    clearInterval(meter); level = 0;
    if (mine !== speakSeq) return;            // a newer answer owns the state
    resumeListening();
  }

  /* Barge-in. Safe to call at any time, playing or not — it can never leave
     the page stuck in 'speaking'. */
  function stopSpeaking() {
    speakSeq++;                               // invalidate any in-flight speak()
    if (outEl) { try { outEl.pause(); } catch (e) { /* nothing playing */ } }
    if (endTurn) { const f = endTurn; endTurn = null; f(); }
    if (state === 'speaking') resumeListening();
  }

  /* ------------------------------------------------------------ listen */
  let stream = null, recorder = null, chunks = [], micTimer = null;
  let inCtx = null, analyser = null, inBuf = null;
  let speechSeen = false, quietSince = 0, turnStart = 0;

  /* What the next recording is for. 'command' is a turn she answers; 'wake'
     is a chunk that is thrown away unless it contained her name. */
  let listenFor = 'command';
  let waking = false;              // wake mode armed
  let wakeInfo = null;             // what the server says about wake support

  async function ensureMic() {
    if (stream) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      caption('This browser exposes no microphone API. Type instead.', 'err');
      toast('No microphone API in this browser.', 'bad', 12000);
      setState('error'); return false;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch (e) {
      // A blocked mic that produces no error is the single most confusing
      // failure in this build, so say exactly what happened.
      const why = {
        NotAllowedError: 'You (or the browser) denied microphone access. Click the padlock in ' +
                         'the address bar and allow the microphone for localhost.',
        NotFoundError: 'No microphone is connected.',
        NotReadableError: 'The microphone is in use by another app.',
        SecurityError: 'The browser blocked the microphone. localhost should be allowed — ' +
                       'check your browser settings.'
      }[e.name] || ('Microphone error: ' + e.name + ' — ' + e.message);
      caption(esc(why), 'err');
      toast(why, 'bad', 14000);
      setState('error');
      return false;
    }
    inCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = inCtx.createMediaStreamSource(stream);
    analyser = inCtx.createAnalyser();
    analyser.fftSize = 1024;
    src.connect(analyser);
    inBuf = new Uint8Array(analyser.fftSize);
    return true;
  }

  function micLevel() {
    if (!analyser) return 0;
    analyser.getByteTimeDomainData(inBuf);
    let sum = 0;
    for (let i = 0; i < inBuf.length; i++) { const d = (inBuf[i] - 128) / 128; sum += d * d; }
    return Math.sqrt(sum / inBuf.length);
  }

  function armRecorder() {
    // Keyed off what is actually true, not off the state label. The label
    // getting stuck used to leave the mic permanently deaf with no error.
    if (!stream) return;
    if (recorder && recorder.state === 'recording') return;   // already listening
    if (isPlaying() || state === 'thinking') return;          // don't hear herself
    let mime = '';
    ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
      .some(function (m) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) { mime = m; return true; }
        return false;
      });
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch (e) {
      toast('MediaRecorder is unavailable in this browser: ' + e.message, 'bad', 12000);
      setState('error'); return;
    }
    chunks = [];
    recorder.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
    recorder.onstop = onRecorderStop;
    recorder.start();

    speechSeen = false; quietSince = 0; turnStart = Date.now();
    if (listenFor === 'wake') {
      setState('waiting');
    } else {
      setState('listening');
      caption('listening…');
    }

    clearInterval(micTimer);
    // setInterval, not requestAnimationFrame: rAF is throttled to nothing in a
    // background tab and the endpointer would silently stop firing.
    micTimer = setInterval(function () {
      if (!recorder || recorder.state !== 'recording') return;
      level = micLevel();
      const now = Date.now(), age = now - turnStart;

      if (level > SPEECH_LEVEL) {
        speechSeen = true; quietSince = 0;
        // In wake mode the caption stays empty. Drawing a live meter for every
        // overheard sentence would make it look like she is transcribing the
        // room, which is exactly what she is not doing.
        if (listenFor !== 'wake') {
          caption('listening… <span style="opacity:.5">' + '▍'.repeat(
            Math.min(18, 1 + Math.round(level * 90))) + '</span>');
        }
      } else if (level < SILENCE_LEVEL) {
        if (!quietSince) quietSince = now;
      } else {
        quietSince = 0;
      }

      if (age > MAX_UTTERANCE_MS) { caption('that was a long one — sending it'); stopRecorder(); return; }
      if (!speechSeen || age < LEAD_IN_MS) return;
      if (quietSince && now - quietSince >= SILENCE_HANG_MS && age > MIN_UTTERANCE_MS) {
        stopRecorder();
      }
    }, LEVEL_TICK_MS);
  }

  function stopRecorder() {
    clearInterval(micTimer); micTimer = null; level = 0;
    if (recorder && recorder.state === 'recording') recorder.stop();
  }

  function disarmRecorder() {
    clearInterval(micTimer); micTimer = null; level = 0;
    if (recorder && recorder.state === 'recording') {
      recorder.onstop = null;                 // do not transcribe her own voice
      try { recorder.stop(); } catch (e) { /* already stopping */ }
    }
    recorder = null; chunks = [];
  }

  /* Hands-free has to survive anything — a dropped 'ended' event, a request
     that threw between states, a recorder the browser killed on its own. If
     the mic is meant to be open and nothing is stopping it, open it. */
  setInterval(function () {
    if (!continuous || !stream) return;
    if (isPlaying() || state === 'thinking' || asking) return;
    if (recorder && recorder.state === 'recording') return;
    armRecorder();
  }, REARM_TICK_MS);

  async function onRecorderStop() {
    const heard = speechSeen;
    const purpose = listenFor;
    const blob = new Blob(chunks, { type: (recorder && recorder.mimeType) || 'audio/webm' });
    chunks = [];
    if (!heard || blob.size < 1500) {
      caption(purpose === 'wake' ? '' : (continuous ? 'listening…' : ''));
      resumeListening();
      return;
    }

    /* Wake mode. This chunk was overheard, not addressed to her. It goes to
       the server, is checked for her name, and unless it matched, nothing
       about it is kept — not a transcript, not a caption, not a log line. */
    if (purpose === 'wake') {
      let r;
      try {
        r = await json('/api/wake', {
          method: 'POST',
          headers: { 'Content-Type': blob.type || 'audio/webm' },
          body: blob
        });
      } catch (e) {
        // Don't shout on every overheard sentence — say it once and stop.
        toast('Wake word check failed: ' + e.message + ' — switching wake off.',
              'bad', 11000);
        stopWake();
        return;
      }
      if (!r.woke) { resumeListening(); return; }

      if (r.command) {
        // "Aeris, what's broken" in one breath. Nothing more to wait for.
        await ask(r.command, true);
      } else {
        // Just her name. Answer, then take the next thing he says as the turn.
        listenFor = 'command';
        caption('<span class="you">' + esc(wakeWordLabel()) + '</span> — go on');
        if (!muted) await speak('Yes, SIR?'); else resumeListening();
      }
      return;
    }

    setState('thinking');
    caption('transcribing…');
    let r;
    try {
      r = await json('/api/listen', {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'audio/webm' },
        body: blob
      });
    } catch (e) {
      caption('Could not transcribe: ' + esc(e.message), 'err');
      toast('Transcription failed: ' + e.message, 'bad', 11000);
      resumeListening();
      return;
    }
    if (!r.text) {
      caption(continuous ? 'listening…' : 'nothing heard');
      resumeListening();
      return;
    }
    await ask(r.text, true);
  }

  async function toggleMic() {
    // Barge-in only counts while sound is actually coming out. Returning here
    // on the *label* alone is what used to wedge the button: once playback had
    // finished, every click and every Space hit this line and did nothing.
    if (isPlaying()) { stopSpeaking(); return; }
    if (state === 'speaking' || state === 'error') {
      speakSeq++; endTurn = null;             // clear a turn that never landed
      setState('idle');
    }
    if (continuous) {
      continuous = false; disarmRecorder(); setState('idle'); caption('');
      return;
    }
    // Built here, inside the click, so the browser counts it as a user gesture.
    ensureOutput();
    if (!(await ensureMic())) return;
    if (inCtx && inCtx.state === 'suspended') await inCtx.resume();
    continuous = true;
    armRecorder();
    toast('Mic on. Just talk — I end your turn after ' + SILENCE_HANG_MS +
          'ms of quiet, answer, then listen again. Space or Esc to cut me off.',
          'ok', 7000);
  }

  /* --------------------------------------------------------- wake word */
  function wakeWordLabel() {
    return (status && status.voice && status.voice.wake_word) || 'Aeris';
  }

  async function startWake() {
    wakeInfo = (status && status.voice && status.voice.wake) || null;
    if (wakeInfo && !wakeInfo.ok) {
      // Refused on purpose. Metered speech-to-text would bill him for every
      // sentence spoken near the microphone.
      caption(esc(wakeInfo.detail), 'err');
      toast(wakeInfo.detail, 'bad', 15000);
      return;
    }
    ensureOutput();
    if (!(await ensureMic())) return;
    if (inCtx && inCtx.state === 'suspended') await inCtx.resume();

    waking = true; continuous = false;
    el.wake.classList.add('on');
    listenFor = 'wake';
    armRecorder();
    setState('waiting');
    caption('');
    toast('Waiting for “' + wakeWordLabel() + '”. Nothing is sent or kept until '
          + 'you say it. Say “' + wakeWordLabel() + ', brief me” in one breath.',
          'ok', 9000);
  }

  function stopWake() {
    waking = false;
    listenFor = 'command';
    el.wake.classList.remove('on');
    disarmRecorder();
    setState('idle');
    caption('');
  }

  function toggleWake() {
    if (waking) { stopWake(); toast('Wake word off. The mic is closed.', 'ok', 4000); }
    else startWake();
  }

  /* ------------------------------------------------------- screen share */
  /* He picks the window, the browser asks him, and a frame is only ever read
     when he asks a question about it. Better than capturing the screen
     server-side: he chooses what is visible, and macOS never has to grant the
     terminal blanket Screen Recording permission. */
  let shareStream = null, shareVideo = null;
  /* Deliberately narrow. "what is this" while sharing means the screen; "what
     is the scam platform" does not, and hijacking that would be worse than
     making him say the word "screen". */
  const SCREEN_Q = new RegExp(
    '\\b(' +
    'screen|display' +
    '|what(\'s| is| am i)? ?(this|that|here|looking at)' +
    '|read (this|that|it|the)' +
    '|(this|that|the) (error|message|dialog|log|page|code)' +
    '|see (this|that|my)' +
    '|what does (this|that|it)' +
    '|explain (this|that)' +
    ')\\b', 'i');

  async function startShare() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      toast('This browser cannot share a screen.', 'bad', 9000);
      return;
    }
    try {
      shareStream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 1 }, audio: false
      });
    } catch (e) {
      if (e.name !== 'NotAllowedError') toast('Screen share failed: ' + e.message, 'bad', 9000);
      return;
    }
    shareVideo = document.createElement('video');
    shareVideo.srcObject = shareStream;
    shareVideo.muted = true;
    await shareVideo.play().catch(function () {});

    // Stopping from the browser's own "stop sharing" bar must also update us.
    shareStream.getVideoTracks().forEach(function (t) {
      t.addEventListener('ended', function () { stopShare(true); });
    });
    el.share.classList.add('on');
    toast('Sharing. Ask me about what is on it — “what does this error say?” '
          + 'Nothing is captured until you ask.', 'ok', 9000);
  }

  function stopShare(silent) {
    if (shareStream) shareStream.getTracks().forEach(function (t) { t.stop(); });
    shareStream = null; shareVideo = null;
    el.share.classList.remove('on');
    if (!silent) toast('Stopped sharing.', 'ok', 3500);
  }

  function toggleShare() { if (shareStream) stopShare(); else startShare(); }

  function grabFrame() {
    if (!shareVideo || !shareVideo.videoWidth) return null;
    const canvas = document.createElement('canvas');
    canvas.width = shareVideo.videoWidth;
    canvas.height = shareVideo.videoHeight;
    canvas.getContext('2d').drawImage(shareVideo, 0, 0);
    return new Promise(function (resolve) { canvas.toBlob(resolve, 'image/png'); });
  }

  async function seeScreen(question) {
    const frame = await grabFrame();
    if (!frame) { toast('No frame from the shared screen yet — try again.', 'warn'); return; }
    setState('thinking');
    caption('<span class="you">' + esc(question) + '</span> — looking…');
    let r;
    try {
      r = await json('/api/see', {
        method: 'POST',
        headers: { 'Content-Type': 'image/png', 'X-Aeris-Question': question },
        body: frame
      });
    } catch (e) {
      caption('Could not read the screen: ' + esc(e.message), 'err');
      resumeListening();
      return;
    }
    if (!r.ok) {
      caption(esc(r.summary), 'err');
      toast(r.summary, 'bad', 11000);
      resumeListening();
      return;
    }
    caption(esc(r.summary));
    showCard({
      title: 'your screen',
      subtitle: 'one frame, read by ' + (r.read_by || '?') + ', not kept',
      rows: [{ k: 'Question', v: esc(question) },
             { k: 'Kept', v: 'nothing — the frame is already gone' }],
      note: 'Captured because you asked. Aeris never takes a frame on her own.'
    });
    if (!muted) await speak(r.summary); else resumeListening();
  }

  /* ------------------------------------------------------------- wiring */
  el.mic.addEventListener('click', toggleMic);
  el.wake.addEventListener('click', toggleWake);
  el.share.addEventListener('click', toggleShare);
  el.send.addEventListener('click', function () {
    const t = el.ask.value.trim(); if (t) { el.ask.value = ''; ask(t); }
  });
  el.ask.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      const t = el.ask.value.trim(); if (t) { el.ask.value = ''; ask(t); }
    }
  });
  el.mute.addEventListener('click', function () {
    muted = !muted;
    el.mute.classList.toggle('muted', muted);
    if (muted) stopSpeaking();
    toast(muted ? 'Muted — she still answers, just in text.' : 'Voice back on.', 'ok', 3200);
  });

  document.querySelectorAll('[data-ask]').forEach(function (b) {
    b.addEventListener('click', function () { ask(b.dataset.ask); });
  });

  document.querySelectorAll('.tool').forEach(function (b) {
    b.addEventListener('click', async function () {
      const act = b.dataset.act;
      if (act === 'fit') { Graph.fit(); return; }
      if (act === 'labels') { b.classList.toggle('on'); Graph.setLabels(b.classList.contains('on')); return; }
      if (act === 'pulse') { b.classList.toggle('on'); Graph.setPulse(b.classList.contains('on')); return; }
      if (act === 'reload') {
        b.textContent = '…';
        try {
          graphData = await json('/api/reindex', { method: 'POST' });
          window.__gd = graphData;
    Graph.load(graphData); renderMeta(); renderFilters(); renderHubs();
          status = await json('/api/status'); renderBadges();
          toast('Reindexed: ' + graphData.total_notes + ' notes.', 'ok');
        } catch (e) { toast('Reindex failed: ' + e.message, 'bad'); }
        b.textContent = 'Reindex';
      }
    });
  });

  /* ------------------------------------------------------- agent runs */
  /* A job rather than a question: she plans, acts, looks at what happened,
     and goes again. A step that needs approval stops the whole run. */
  async function runAgent(goal, resumeId) {
    setState('thinking');
    caption('<span class="you">' + esc(goal) + '</span> — working…');
    let r;
    try {
      r = resumeId
        ? await json('/api/agent/resume', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: resumeId })
          })
        : await json('/api/agent', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal: goal })
          });
    } catch (e) {
      caption('That job failed: ' + esc(e.message), 'err');
      toast('Agent failed: ' + e.message, 'bad', 9000);
      resumeListening();
      return;
    }

    showCard({
      title: 'agent · ' + r.status,
      subtitle: r.goal,
      note: r.status === 'waiting'
        ? 'The run is stopped here. It continues only if you approve it.'
        : (r.steps.length + ' step' + (r.steps.length === 1 ? '' : 's') +
           ' · every action was logged to audit/actions.jsonl'),
      confirm: r.pending
        ? { token: r.pending.token, summary: r.pending.summary, resume: r.id }
        : null,
      items: (r.steps || []).map(function (s) {
        return { title: s.n + '. ' + s.tool, subtitle: s.thought, meta: s.observation };
      })
    });

    caption(esc(r.say));
    if (!muted) await speak(r.say); else resumeListening();
  }

  document.querySelector('[data-act="agent"]').addEventListener('click', function () {
    const goal = el.ask.value.trim();
    if (!goal) {
      toast('Type the job first — “find every note where I argued about pricing ' +
            'and write me one page” — then press Do it.', 'warn', 7000);
      el.ask.focus();
      return;
    }
    el.ask.value = '';
    runAgent(goal);
  });

  document.querySelector('[data-act="memory"]').addEventListener('click', async function () {
    try {
      const m = await json('/api/memory');
      showCard({
        title: 'memory', subtitle: m.count + ' remembered ' + (m.count === 1 ? 'fact' : 'facts'),
        note: 'One fact per dated file in memory/. Nothing else on your disk is ever written to.',
        items: m.items.length ? m.items.map(function (i) {
          return { title: i.fact, file: i.path, meta: i.created };
        }) : [{ title: 'Nothing remembered yet.',
                subtitle: 'Say “remember that …” and I will write one file and read it back to you.' }]
      });
    } catch (e) { toast(e.message, 'bad'); }
  });

  document.querySelector('[data-act="voice"]').addEventListener('click', async function () {
    try {
      const r = await json('/api/voices');
      showCard({
        title: 'voice', subtitle: 'click one to switch — it saves to .env',
        note: 'Auditioning is free; switching costs nothing until she next speaks.',
        items: r.voices.slice(0, 14).map(function (v) {
          return { title: v.name + (v.current ? '  ·  current' : ''),
                   subtitle: [v.gender, v.accent, v.description].filter(Boolean).join(' · '),
                   file: v.id, voice: true };
        })
      });
      el.card.querySelectorAll('.item').forEach(function (item, i) {
        item.classList.remove('static');
        item.style.cursor = 'pointer';
        item.addEventListener('click', async function () {
          const id = r.voices[i].id;
          try {
            await json('/api/voice', { method: 'POST',
              headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) });
            toast('Voice set to ' + r.voices[i].name + '.', 'ok');
            if (!muted) speak('Aviral, SIR. This is how I sound now.');
          } catch (e) { toast(e.message, 'bad'); }
        });
      });
    } catch (e) {
      toast('Could not list voices: ' + e.message, 'bad', 9000);
    }
  });

  window.addEventListener('keydown', function (ev) {
    const typing = document.activeElement === el.ask || document.activeElement === el.search;
    if (ev.key === 'Escape') {
      if (isPlaying()) { stopSpeaking(); return; }
      el.card.classList.remove('show'); Graph.clearFocus(); onFocus(null);
      if (typing) document.activeElement.blur();
      return;
    }
    if (typing) return;
    if (ev.code === 'Space') {
      ev.preventDefault();
      toggleMic();                            // handles barge-in itself
    }
    if (ev.key === '/') { ev.preventDefault(); el.ask.focus(); }
  });

  function rotateExample() {
    let i = 0;
    setInterval(function () {
      if (document.activeElement === el.ask || el.ask.value) return;
      i = (i + 1) % EXAMPLES.length;
      el.ask.placeholder = 'Ask · “' + EXAMPLES[i] + '”';
    }, 4200);
  }

  boot();
})();
