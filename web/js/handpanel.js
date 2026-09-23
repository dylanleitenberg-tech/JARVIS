/* ===========================================================================
   The hand control drawer.

   Three jobs: show the hand the tracker is actually seeing, show what every
   gesture is wired to, and let the thresholds be tuned against a real hand
   rather than guessed. Tuning applies live — the slider talks to the running
   recogniser, and SAVE writes it to jarvis.json.
   =========================================================================== */
(function (global) {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const TAU = Math.PI * 2;

  const HAND_EDGES = [
    [0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],
    [9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]
  ];
  const TIPS = [4, 8, 12, 16, 20];
  const DIGITS = [
    { key: 'thumb',  label: 'THUMB' },
    { key: 'index',  label: 'INDEX' },
    { key: 'middle', label: 'MIDDLE' },
    { key: 'ring',   label: 'RING' },
    { key: 'pinky',  label: 'PINKY' },
  ];

  // Gestures in the order they are worth reading, with a plain-language cue.
  const GESTURE_HELP = {
    point: 'index finger out',
    pinch: 'thumb + index',
    pinch_drag: 'hold the index pinch and move',
    pinch_middle: 'thumb + middle',
    pinch_ring: 'thumb + ring',
    pinch_pinky: 'thumb + pinky',
    two_hand_spread: 'both hands pinched, moving apart',
    swipe_left: 'open palm, sweep left',
    swipe_right: 'open palm, sweep right',
    swipe_up: 'open palm, sweep up',
    swipe_down: 'open palm, sweep down',
    fist: 'closed fist',
    open_palm_hold: 'open palm, held still',
    thumbs_up: 'thumb up',
    thumbs_down: 'thumb down',
    peace: 'index + middle',
  };

  const TUNE_HELP = {
    pinch_on: 'pinch closes',
    pinch_off: 'pinch releases',
    cooldown: 'gesture gap',
    arm_hold: 'palm to arm',
    min_hand_scale: 'min hand size',
    swipe_velocity: 'swipe speed',
    cursor_smoothing: 'cursor easing',
    disarm_after_idle: 'idle disarm',
  };

  class HandPanel {
    constructor(core, send) {
      this.core = core;
      this.send = send;
      this.open = false;
      this.hands = [];
      this.tuning = {};
      this.baseline = {};
      this.limits = {};

      this.canvas = $('#hand-canvas');
      this.ctx = this.canvas.getContext('2d');
      this._resize = this._resize.bind(this);
      window.addEventListener('resize', this._resize);

      $('#drawer-tab').onclick = () => this.toggle();
      $('#drawer-arm').onclick = () => {
        this.send({ type: 'arm_gestures', armed: !this.armed });
      };
      $('#tune-save').onclick = () => {
        this.send({ type: 'gesture_tune', patch: this.tuning, save: true });
      };
      $('#tune-reset').onclick = () => {
        this.tuning = Object.assign({}, this.baseline);
        this.renderTuning();
        this.send({ type: 'gesture_tune', patch: this.tuning });
      };

      this._draw = this._draw.bind(this);
      requestAnimationFrame(this._draw);
    }

    toggle(force) {
      this.open = force === undefined ? !this.open : !!force;
      $('#drawer').classList.toggle('open', this.open);
      document.body.classList.toggle('drawer-open', this.open);
      if (this.open) this._resize();
    }

    /* ------------------------------------------------------------ setup */

    init(gestures, actions) {
      this.actions = actions;
      this.bindings = Object.assign({}, gestures.bindings);
      this.tuning = Object.assign({}, gestures.tuning || {});
      this.baseline = Object.assign({}, this.tuning);
      this.limits = gestures.limits || {};
      this.setArmed(gestures.armed);
      this.renderBindings();
      this.renderTuning();
      this.renderDigits(null);
    }

    setArmed(armed) {
      this.armed = !!armed;
      const el = $('#drawer-state');
      el.textContent = armed ? 'ARMED' : 'SAFE';
      el.classList.toggle('armed', !!armed);
      $('#drawer-arm').textContent = armed ? 'DISARM' : 'ARM';
      $('#drawer-arm').classList.toggle('active', !!armed);
    }

    /* --------------------------------------------------------- bindings */

    renderBindings() {
      // Reserved targets are handled inside the recogniser as modes, not as
      // registry actions, so they are offered alongside the real actions.
      const reserved = ['cursor', 'drag', 'zoom', 'radial_menu', 'confirm',
                        'dismiss', 'next_app', 'prev_app'];
      const options = reserved.concat((this.actions || []).map((a) => a.name));

      $('#bind-list').innerHTML = Object.keys(this.bindings).map((gesture) => {
        const current = this.bindings[gesture] || '';
        const opts = ['<option value="">— nothing —</option>'].concat(
          options.map((name) =>
            `<option value="${name}"${name === current ? ' selected' : ''}>` +
            `${name.replace(/_/g, ' ')}</option>`)
        ).join('');
        const help = GESTURE_HELP[gesture] || gesture.replace(/_/g, ' ');
        return `<div class="bind-row" data-gesture="${gesture}" title="${help}">` +
               `<span>${help}</span><select>${opts}</select></div>`;
      }).join('');

      $('#bind-list').onchange = (event) => {
        const row = event.target.closest('.bind-row');
        if (!row) return;
        const gesture = row.dataset.gesture;
        const action = event.target.value || null;
        this.bindings[gesture] = action;
        this.send({ type: 'rebind', gesture, action, save: true });
      };
    }

    /* Flash the row for a gesture that just fired, so the panel doubles as a
       way to find out which gesture you actually made. */
    flash(gesture) {
      const row = $(`.bind-row[data-gesture="${gesture}"]`);
      if (!row) return;
      row.classList.remove('fired');
      void row.offsetWidth;            // restart the animation
      row.classList.add('fired');
      setTimeout(() => row.classList.remove('fired'), 700);
    }

    /* ------------------------------------------------------ calibration */

    renderTuning() {
      $('#tune-list').innerHTML = Object.keys(this.tuning).map((key) => {
        const [low, high] = this.limits[key] || [0, 1];
        const value = this.tuning[key];
        const step = (high - low) / 100;
        return `<label class="tune" data-key="${key}">` +
               `<span>${TUNE_HELP[key] || key.replace(/_/g, ' ')}</span>` +
               `<input type="range" min="${low}" max="${high}" step="${step}" ` +
               `value="${value}">` +
               `<output>${fmt(value)}</output></label>`;
      }).join('');

      $('#tune-list').oninput = (event) => {
        const label = event.target.closest('.tune');
        if (!label) return;
        const key = label.dataset.key;
        const value = parseFloat(event.target.value);
        this.tuning[key] = value;
        label.querySelector('output').textContent = fmt(value);
        // Live, unsaved: move the slider and the next pinch already behaves.
        this.send({ type: 'gesture_tune', patch: { [key]: value } });
      };
    }

    applyTuned(patch) {
      Object.assign(this.tuning, patch);
      for (const [key, value] of Object.entries(patch)) {
        const label = $(`.tune[data-key="${key}"]`);
        if (!label) continue;
        label.querySelector('input').value = value;
        label.querySelector('output').textContent = fmt(value);
      }
    }

    /* ----------------------------------------------------------- digits */

    setVision(payload) {
      this.hands = payload.hands || [];
      this.fps = payload.fps;
    }

    renderDigits(hand) {
      const on = this.tuning.pinch_on ?? 0.30;
      const grid = $('#digit-grid');
      grid.innerHTML = DIGITS.map((digit, i) => {
        if (!hand) return `<div class="digit"><b>—</b>${digit.label}</div>`;
        if (i === 0) {
          return `<div class="digit${hand.thumb_out ? ' up' : ''}">` +
                 `<b>${hand.thumb_out ? 'OUT' : 'IN'}</b>${digit.label}</div>`;
        }
        const f = i - 1;
        const gaps = hand.pinches || [hand.pinch, 9, 9, 9];
        const reach = hand.reaches || [hand.index_reach || 0, 0, 0, 0];
        const pinching = gaps[f] < on && reach[f] >= 1.3;
        const cls = pinching ? 'pinch' : (hand.extended[f] ? 'up' : '');
        return `<div class="digit ${cls}"><b>${gaps[f].toFixed(2)}</b>${digit.label}</div>`;
      }).join('');
    }

    /* ------------------------------------------------------- live hand */

    _resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = this.canvas.getBoundingClientRect();
      if (!rect.width) return;
      this.canvas.width = Math.floor(rect.width * dpr);
      this.canvas.height = Math.floor(rect.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.w = rect.width;
      this.h = rect.height;
    }

    _draw() {
      requestAnimationFrame(this._draw);
      if (!this.open) return;
      if (!this.w) this._resize();
      if (!this.w) return;

      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.h);

      const hand = this.hands[0] || null;
      $('#hand-empty').hidden = !!hand;
      $('#drawer-fps').textContent = this.fps ? `${this.fps.toFixed(0)} FPS` : '—';
      this.renderDigits(hand);
      if (!hand) return;

      // Fit the hand's own bounding box to the stage, so it stays large and
      // centred however far away it is from the camera.
      const [bx, by, bw, bh] = hand.bbox;
      const pad = 0.16;
      const scale = Math.min(this.w / (bw * (1 + pad * 2)),
                             this.h / (bh * (1 + pad * 2)));
      const ox = this.w / 2 - (bx + bw / 2) * scale;
      const oy = this.h / 2 - (by + bh / 2) * scale;
      const pts = hand.points.map((p) => [p[0] * scale + ox, p[1] * scale + oy]);

      const style = getComputedStyle(document.body);
      const hud = style.getPropertyValue('--hud').trim() || '#64e3ff';
      const amber = style.getPropertyValue('--amber').trim() || '#ffb347';
      const on = this.tuning.pinch_on ?? 0.30;
      const gaps = hand.pinches || [hand.pinch, 9, 9, 9];
      const reach = hand.reaches || [hand.index_reach || 0, 0, 0, 0];

      ctx.strokeStyle = hud;
      ctx.lineWidth = 1.6;
      ctx.shadowColor = hud;
      ctx.shadowBlur = 8;
      for (const [a, b] of HAND_EDGES) {
        ctx.beginPath();
        ctx.moveTo(pts[a][0], pts[a][1]);
        ctx.lineTo(pts[b][0], pts[b][1]);
        ctx.stroke();
      }
      ctx.shadowBlur = 0;

      // A line from the thumb to each fingertip, brightening as it closes —
      // this is literally the quantity the threshold is compared against.
      for (let f = 0; f < 4; f++) {
        const pinching = gaps[f] < on && reach[f] >= 1.3;
        const near = Math.max(0, 1 - gaps[f] / 1.2);
        ctx.strokeStyle = pinching ? amber : hud;
        ctx.globalAlpha = pinching ? 1 : 0.12 + near * 0.4;
        ctx.lineWidth = pinching ? 2.4 : 1;
        ctx.setLineDash(pinching ? [] : [2, 4]);
        ctx.beginPath();
        ctx.moveTo(pts[4][0], pts[4][1]);
        ctx.lineTo(pts[TIPS[f + 1]][0], pts[TIPS[f + 1]][1]);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      for (let i = 0; i < pts.length; i++) {
        const tipIndex = TIPS.indexOf(i);
        const pinching = tipIndex > 0 &&
          gaps[tipIndex - 1] < on && reach[tipIndex - 1] >= 1.3;
        ctx.fillStyle = pinching ? amber : hud;
        ctx.beginPath();
        ctx.arc(pts[i][0], pts[i][1], tipIndex >= 0 ? 3.4 : 2, 0, TAU);
        ctx.fill();
      }

      ctx.fillStyle = hud;
      ctx.font = "9px 'Share Tech Mono', monospace";
      ctx.globalAlpha = 0.65;
      ctx.fillText(`${hand.label.toUpperCase()}  size ${hand.scale.toFixed(3)}`, 8, 14);
      ctx.globalAlpha = 1;
    }
  }

  function fmt(v) {
    return Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(2);
  }

  global.HandPanel = HandPanel;
})(window);
