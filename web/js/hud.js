/* ===========================================================================
   The readable half of the interface: gauges, telemetry, activity log, the
   dialogue line, the voice waveform and the boot sequence.
   =========================================================================== */
(function (global) {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 34;   // r=34 in the SVG

  const BOOT_LINES = [
    ['POST', 'memory, bus, interrupt table', 60],
    ['KERNEL', 'loading j.a.r.v.i.s. core', 90],
    ['SPEECH', 'recognition and synthesis online', 70],
    ['OPTICS', 'hand, body and facial tracking', 110],
    ['CONTROL', 'window server, input synthesis', 70],
    ['COGNITION', 'language model link', 120],
    ['TELEMETRY', 'system sensors nominal', 60],
  ];

  class Hud {
    constructor(core) {
      this.core = core;
      this.logEl = $('#log');
      this.waveCanvas = $('#wave');
      this.waveCtx = this.waveCanvas.getContext('2d');
      this.wave = new Array(72).fill(0);
      this.typeTimer = null;
      this.telemetry = {};

      this._sizeWave();
      window.addEventListener('resize', () => this._sizeWave());
      this._clock();
      this._waveLoop();
    }

    /* ------------------------------------------------------------- boot */

    async boot(skip) {
      const out = $('#boot-text');
      if (skip || this._booted) {
        document.body.classList.remove('booting');
        return;
      }
      this._booted = true;
      const write = (html) => { out.innerHTML += html; };
      write(`<span class="dim">J.A.R.V.I.S. v1.0 — Stark Industries interface</span>\n\n`);
      for (const [name, detail, delay] of BOOT_LINES) {
        write(`  ${name.padEnd(11)} <span class="dim">${detail}</span>`);
        await sleep(delay);
        write(`  <span class="ok">[ OK ]</span>\n`);
      }
      write(`\n<span class="dim">all systems nominal. good to see you again, Sir.</span>`);
      await sleep(520);
      document.body.classList.remove('booting');
      this.core.ping();
    }

    /* ------------------------------------------------------------ chips */

    chip(id, on, warn) {
      const el = document.getElementById('chip-' + id);
      if (!el) return;
      el.classList.toggle('on', !!on && !warn);
      el.classList.toggle('warn', !!warn);
    }

    chipLive(id, live) {
      const el = document.getElementById('chip-' + id);
      if (el) el.classList.toggle('live', !!live);
    }

    /* -------------------------------------------------------- telemetry */

    setTelemetry(data) {
      this.telemetry = data;
      const pct = {
        cpu: data.cpu,
        mem: data.mem_percent,
        disk: data.disk_percent,
        batt: data.battery,
      };
      for (const [key, value] of Object.entries(pct)) {
        const gauge = document.querySelector(`.gauge[data-key="${key}"]`);
        if (!gauge) continue;
        const v = Number.isFinite(value) ? value : 0;
        // Battery reads inverted: a low battery is the alarming end.
        const severity = key === 'batt' ? 100 - v : v;
        gauge.querySelector('.fill').style.strokeDashoffset =
          GAUGE_CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, v)) / 100);
        gauge.querySelector('b').textContent =
          Number.isFinite(value) ? Math.round(v) : '--';
        gauge.classList.toggle('hot', severity >= 70 && severity < 88);
        gauge.classList.toggle('crit', severity >= 88);
      }

      const rows = [
        ['MEMORY', data.mem_used_gb != null ? `${data.mem_used_gb} / ${data.mem_total_gb} GB` : '—'],
        ['STORAGE', data.disk_free_gb != null ? `${data.disk_free_gb} GB free` : '—'],
        ['POWER', data.battery != null
          ? `${data.battery}%${data.charging ? ' ⚡' : ''}${data.battery_hours ? ` · ${data.battery_hours}h` : ''}`
          : 'mains'],
        ['NETWORK', data.wifi && data.wifi !== 'unknown' ? data.wifi : 'wired'],
        ['PROCESSES', data.processes != null ? String(data.processes) : '—'],
        ['UPTIME', data.uptime_h != null ? `${data.uptime_h} h` : '—'],
      ];
      $('#sys-readouts').innerHTML = rows
        .map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');

      $('#optics-fps').textContent = data.vision_online
        ? `${(data.vision_fps || 0).toFixed(0)} FPS` : 'OFFLINE';
      this.chip('cam', data.vision_online);
      this.chip('gesture', data.armed);
      $('#gesture-state').textContent = data.armed ? 'ARMED' : 'SAFE';
      $('#gesture-state').classList.toggle('armed', !!data.armed);
      $('#btn-arm').classList.toggle('active', !!data.armed);
      $('#btn-arm').textContent = data.armed ? 'TURN OFF' : 'TURN ON';

      // A critical reading tints the whole interface amber.
      this.core.alert((data.cpu >= 92) || (data.battery != null && data.battery <= 10 && !data.charging));
    }

    setTracking(payload) {
      const hands = (payload.hands || []).length;
      const pills = document.querySelectorAll('.track-pill');
      const body = payload.pose && Object.keys(payload.pose).length;
      const values = { hands: String(hands), body: body ? 'YES' : '—',
                       face: payload.face ? `${Math.round(payload.face.score * 100)}%` : '—' };
      pills.forEach((pill) => {
        const key = pill.dataset.k;
        pill.querySelector('b').textContent = values[key];
        pill.classList.toggle('live', values[key] !== '—' && values[key] !== '0');
      });

      // Per-finger state: which are extended, and which is meeting the thumb.
      const names = ['I', 'M', 'R', 'P'];
      $('#fingers').innerHTML = (payload.hands || []).map((hand) => {
        const gaps = hand.pinches || [hand.pinch, 9, 9, 9];
        const reach = hand.reaches || [hand.index_reach || 0, 0, 0, 0];
        const dots = names.map((n, i) => {
          const pinching = gaps[i] < 0.30 && reach[i] >= 1.3;
          const cls = pinching ? 'pinch' : (hand.extended[i] ? 'up' : '');
          return `<i class="${cls}" title="${n}"></i>`;
        }).join('');
        const thumb = hand.thumb_out ? 'T' : 't';
        return `<div class="finger-row"><b>${hand.label[0].toUpperCase()}</b>` +
               `${dots}<span>${thumb} · ${hand.n_extended}</span></div>`;
      }).join('') || '';
    }

    /* --------------------------------------------------------- dialogue */

    heard(text, final) {
      const el = $('#heard');
      el.textContent = text;
      el.style.color = final ? '' : 'var(--hud-faint)';
    }

    /* Types the reply out rather than printing it, so the line keeps pace
       with the voice instead of arriving before it. */
    speak(text, instant) {
      const el = $('#spoken');
      clearInterval(this.typeTimer);
      if (instant) {
        el.textContent = text;
        el.classList.remove('typing');
        return;
      }
      el.textContent = '';
      el.classList.add('typing');
      let i = 0;
      this.typeTimer = setInterval(() => {
        el.textContent = text.slice(0, ++i);
        if (i >= text.length) {
          clearInterval(this.typeTimer);
          el.classList.remove('typing');
        }
      }, Math.max(12, Math.min(34, 1400 / Math.max(text.length, 1))));
    }

    voiceState(state) {
      const el = $('#voice-state');
      const labels = { standby: 'STANDBY', listening: 'LISTENING', speaking: 'SPEAKING',
                       thinking: 'PROCESSING', denied: 'MIC BLOCKED' };
      el.textContent = labels[state] || state.toUpperCase();
      el.className = 'voice-state ' + (state === 'denied' ? '' : state);
      this.chipLive('mic', state === 'listening');
      this.core.setState(state === 'denied' ? 'idle' : state);
    }

    /* -------------------------------------------------------------- log */

    log(text, kind) {
      const li = document.createElement('li');
      li.className = kind || '';
      const now = new Date();
      li.innerHTML = `<time>${String(now.getHours()).padStart(2, '0')}:` +
                     `${String(now.getMinutes()).padStart(2, '0')}:` +
                     `${String(now.getSeconds()).padStart(2, '0')}</time>` +
                     `<span>${escapeHtml(text)}</span>`;
      this.logEl.prepend(li);
      while (this.logEl.children.length > 60) this.logEl.lastElementChild.remove();
    }

    /* The command index is built from the server's action registry, so the
       panel can never drift from what J.A.R.V.I.S. can actually do. */
    commandIndex(actions, onPick) {
      this.actions = actions;
      const list = $('#index-list');
      const filter = $('#index-filter');
      $('#index-count').textContent = `${actions.length}`;

      const render = () => {
        const q = filter.value.trim().toLowerCase();
        const hits = actions.filter((a) =>
          !q || a.name.includes(q) || a.description.toLowerCase().includes(q) ||
          a.category.includes(q));
        const groups = {};
        for (const a of hits) (groups[a.category] ||= []).push(a);
        list.innerHTML = Object.entries(groups).map(([cat, items]) =>
          `<div class="index-group">${cat.toUpperCase()}</div>` +
          items.map((a) =>
            `<div class="index-item${a.confirm ? ' gated' : ''}" data-name="${a.name}" ` +
            `title="${escapeHtml(a.description)}">${a.name.replace(/_/g, ' ')}</div>`
          ).join('')).join('');
        $('#index-count').textContent = `${hits.length}/${actions.length}`;
      };

      filter.oninput = render;
      list.onclick = (event) => {
        const item = event.target.closest('.index-item');
        if (item) onPick(item.dataset.name);
      };
      render();
    }

    bindings(map) {
      // A gesture may be deliberately bound to nothing, so the value can be null.
      const pretty = (s) => String(s == null ? '—' : s).replace(/_/g, ' ');
      $('#bindings').innerHTML = Object.entries(map)
        .map(([gesture, act]) => `<li><span>${pretty(gesture)}</span><b>${pretty(act)}</b></li>`)
        .join('');
    }

    /* ---------------------------------------------------------- confirm */

    confirm(text, onYes, onNo) {
      const box = $('#confirm');
      $('#confirm-text').textContent = text;
      box.hidden = false;
      this.core.alert(true);
      const close = (fn) => () => {
        box.hidden = true;
        this.core.alert(false);
        this._confirmClose = null;
        fn && fn();
      };
      this._confirmYes = close(onYes);
      this._confirmNo = close(onNo);
      $('#confirm-yes').onclick = this._confirmYes;
      $('#confirm-no').onclick = this._confirmNo;
      this._confirmClose = this._confirmNo;
    }

    dismissConfirm() { if (this._confirmClose) this._confirmClose(); }
    get confirmOpen() { return !$('#confirm').hidden; }

    /* --------------------------------------------------------- waveform */

    setLevel(v) {
      this.wave.push(v);
      if (this.wave.length > 72) this.wave.shift();
    }

    _sizeWave() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = this.waveCanvas.getBoundingClientRect();
      this.waveCanvas.width = Math.max(1, Math.floor(rect.width * dpr));
      this.waveCanvas.height = Math.max(1, Math.floor(rect.height * dpr));
      this.waveCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.waveW = rect.width;
      this.waveH = rect.height;
    }

    _waveLoop() {
      const draw = () => {
        const ctx = this.waveCtx;
        const w = this.waveW, h = this.waveH, mid = h / 2;
        ctx.clearRect(0, 0, w, h);
        const n = this.wave.length;
        const barW = w / n;
        const style = getComputedStyle(document.body);
        const hud = style.getPropertyValue('--hud').trim() || '#64e3ff';

        for (let i = 0; i < n; i++) {
          // Taper the ends so the trace reads as a band, not a bar chart.
          const taper = Math.sin((i / (n - 1)) * Math.PI) ** 0.6;
          const amp = this.wave[i] * taper * (h * 0.46);
          const x = i * barW + barW * 0.5;
          ctx.strokeStyle = hud;
          ctx.globalAlpha = 0.25 + 0.65 * this.wave[i];
          ctx.lineWidth = Math.max(1, barW * 0.5);
          ctx.beginPath();
          ctx.moveTo(x, mid - Math.max(0.7, amp));
          ctx.lineTo(x, mid + Math.max(0.7, amp));
          ctx.stroke();
        }
        ctx.globalAlpha = 0.28;
        ctx.strokeStyle = hud;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, mid);
        ctx.lineTo(w, mid);
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Decay toward a low idle shimmer rather than a dead flat line, so the
        // trace still reads as a live instrument between utterances.
        const idle = 0.03 + Math.random() * 0.035;
        this.wave.push(Math.max(idle, this.wave[this.wave.length - 1] * 0.82));
        if (this.wave.length > 72) this.wave.shift();
        requestAnimationFrame(draw);
      };
      draw();
    }

    _clock() {
      const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
      const tick = () => {
        const d = new Date();
        $('#clock-time').textContent =
          [d.getHours(), d.getMinutes(), d.getSeconds()]
            .map((n) => String(n).padStart(2, '0')).join(':');
        $('#clock-date').textContent =
          `${String(d.getDate()).padStart(2, '0')} ${months[d.getMonth()]} ${d.getFullYear()}`;
        setTimeout(tick, 1000 - (Date.now() % 1000));
      };
      tick();
    }
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  global.Hud = Hud;
})(window);
