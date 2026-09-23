/* ===========================================================================
   The reactor.

   Everything that moves is drawn here on one canvas: the concentric rings, the
   radar sweep, drifting particles, the hand reticles that follow you, and the
   radial menu. Rings are declared as data and drawn by a handful of primitives,
   so the composition is tuned by editing the RINGS table rather than the loop.
   =========================================================================== */
(function (global) {
  'use strict';

  const TAU = Math.PI * 2;
  const rad = (deg) => (deg * Math.PI) / 180;

  /* r: fraction of the reactor radius. speed: degrees per second, signed. */
  const RINGS = [
    { r: 1.30, type: 'ticks',    speed:  -2, count: 120, len: 0.018, alpha: 0.16 },
    { r: 1.18, type: 'arcs',     speed:   7, arcs: [[0, 42], [120, 26], [180, 58], [286, 34]],
      width: 1, alpha: 0.42 },
    { r: 1.08, type: 'ring',     speed:   0, width: 1, alpha: 0.20 },
    { r: 1.00, type: 'ticks',    speed:   5, count: 72, len: 0.032, alpha: 0.5, major: 6,
      majorLen: 0.062, labels: true },
    { r: 0.905, type: 'ring',    speed:   0, width: 1.4, alpha: 0.55, glow: true },
    { r: 0.86, type: 'arcs',     speed: -13, arcs: [[10, 72], [130, 72], [250, 72]],
      width: 2.5, alpha: 0.75, glow: true, caps: true },
    { r: 0.76, type: 'segments', speed:   9, count: 36, gap: 0.30, alpha: 0.35 },
    { r: 0.66, type: 'ticks',    speed: -18, count: 144, len: 0.016, alpha: 0.28 },
    { r: 0.58, type: 'bolts',    speed:  11, count: 8, alpha: 0.7 },
    { r: 0.50, type: 'ring',     speed:   0, width: 1, alpha: 0.30, dash: [2, 6] },
    { r: 0.41, type: 'arcs',     speed:  24, arcs: [[0, 128], [180, 128]],
      width: 1.2, alpha: 0.45 },
    { r: 0.355, type: 'ticks',   speed:  16, count: 60, len: 0.012, alpha: 0.30 },
    { r: 0.32, type: 'markers',  speed: -30, count: 3, alpha: 0.8 },
    { r: 0.28, type: 'ring',     speed:   0, width: 1, alpha: 0.5, glow: true },
    { r: 0.245, type: 'segments', speed: -20, count: 24, gap: 0.42, alpha: 0.42 },
    { r: 0.205, type: 'arcs',    speed:  36, arcs: [[40, 96], [220, 96]],
      width: 1.6, alpha: 0.6, caps: true },
    { r: 0.175, type: 'ring',    speed:   0, width: 1, alpha: 0.34, dash: [1.5, 4] },
  ];

  const HAND_EDGES = [
    [0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],
    [9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]
  ];

  class Core {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.t = 0;
      this.dpr = 1;
      this.w = 0;
      this.h = 0;

      this.level = 0;          // live voice level, 0..1
      this.levelSmooth = 0;
      this.state = 'idle';     // idle | listening | thinking | speaking
      this.energy = 0;         // eases toward the state's target intensity
      this.pulse = 0;          // one-shot ring expansion
      this.hue = 0;            // 0 cyan, 1 amber (alert)

      this.hands = [];
      this.face = null;
      this.pose = null;
      this.mirror = true;
      this.radial = null;
      this.cursor = null;

      this.particles = [];
      this.reduceMotion = false;

      this._resize = this._resize.bind(this);
      this._frame = this._frame.bind(this);
      window.addEventListener('resize', this._resize);
      this._resize();
      this._seedParticles(120);
      requestAnimationFrame(this._frame);
    }

    /* ------------------------------------------------------------ inputs */

    setState(state) {
      if (state === this.state) return;
      this.state = state;
      if (state === 'speaking' || state === 'listening') this.pulse = 1;
    }

    setLevel(v) { this.level = Math.max(0, Math.min(1, v)); }
    alert(on) { this.hueTarget = on ? 1 : 0; }
    ping() { this.pulse = 1; }

    setVision(payload) {
      this.hands = payload.hands || [];
      this.face = payload.face || null;
      this.pose = payload.pose || null;
      this.lastVision = performance.now();
    }

    clearVision() { this.hands = []; this.face = null; this.pose = null; }

    setRadial(data) { this.radial = data; }
    setCursor(c) { this.cursor = c; }

    /* ------------------------------------------------------------ layout */

    _resize() {
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.w = window.innerWidth;
      this.h = window.innerHeight;
      this.canvas.width = Math.floor(this.w * this.dpr);
      this.canvas.height = Math.floor(this.h * this.dpr);
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      this.cx = this.w / 2;
      this.cy = this.h / 2;
      this.R = Math.min(this.w, this.h) * 0.27;
    }

    _seedParticles(n) {
      this.particles = Array.from({ length: n }, () => ({
        a: Math.random() * TAU,
        r: this.R * (1.1 + Math.random() * 2.2),
        v: (Math.random() - 0.5) * 0.09,
        s: Math.random() * 1.3 + 0.3,
        o: Math.random() * 0.4 + 0.08,
      }));
    }

    /* -------------------------------------------------------------- frame */

    _frame(now) {
      const dt = Math.min((now - (this._last || now)) / 1000, 0.05);
      this._last = now;
      this.t += dt;

      const targets = { idle: 0.18, listening: 0.55, thinking: 0.75, speaking: 1.0 };
      const target = targets[this.state] ?? 0.18;
      this.energy += (target - this.energy) * Math.min(1, dt * 5);
      this.levelSmooth += (this.level - this.levelSmooth) * Math.min(1, dt * 12);
      this.pulse = Math.max(0, this.pulse - dt * 1.6);
      this.hue += ((this.hueTarget || 0) - this.hue) * Math.min(1, dt * 3);

      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.save();

      this._drawParticles(dt);
      this._drawReactor();
      this._drawSweep();
      this._drawSkeleton();
      this._drawHands();
      this._drawFace();
      this._drawRadial();

      ctx.restore();
      requestAnimationFrame(this._frame);
    }

    /* --------------------------------------------------------- primitives */

    get color() {
      // Interpolate the stroke colour between cyan and alert amber.
      const c = [100, 227, 255], a = [255, 179, 71];
      const k = this.hue;
      return [
        Math.round(c[0] + (a[0] - c[0]) * k),
        Math.round(c[1] + (a[1] - c[1]) * k),
        Math.round(c[2] + (a[2] - c[2]) * k),
      ];
    }

    stroke(alpha, width, glow) {
      const [r, g, b] = this.color;
      const ctx = this.ctx;
      ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
      ctx.lineWidth = width || 1;
      if (glow && !this.reduceMotion) {
        ctx.shadowColor = `rgba(${r},${g},${b},0.85)`;
        ctx.shadowBlur = 12;
      } else {
        ctx.shadowBlur = 0;
      }
    }

    /* ---------------------------------------------------------- the rings */

    _drawReactor() {
      const ctx = this.ctx;
      const breathe = 1 + 0.012 * Math.sin(this.t * 1.1)
                        + 0.05 * this.levelSmooth * this.energy
                        + 0.06 * this.pulse;
      const R = this.R * breathe;
      ctx.save();
      ctx.translate(this.cx, this.cy);

      for (const ring of RINGS) {
        const r = R * ring.r;
        const rot = rad(ring.speed * this.t * (this.reduceMotion ? 0.25 : 1));
        const alpha = ring.alpha * (0.55 + 0.45 * this.energy);
        ctx.save();
        ctx.rotate(rot);
        switch (ring.type) {
          case 'ring':     this._ring(r, ring, alpha); break;
          case 'ticks':    this._ticks(r, ring, alpha, R); break;
          case 'arcs':     this._arcs(r, ring, alpha); break;
          case 'segments': this._segments(r, ring, alpha); break;
          case 'bolts':    this._bolts(r, ring, alpha, R); break;
          case 'markers':  this._markers(r, ring, alpha, R); break;
        }
        ctx.restore();
      }

      this._spokes(R);
      this._coreLight(R);
      ctx.restore();
    }

    _ring(r, ring, alpha) {
      const ctx = this.ctx;
      this.stroke(alpha, ring.width, ring.glow);
      ctx.setLineDash(ring.dash || []);
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, TAU);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.shadowBlur = 0;
    }

    _ticks(r, ring, alpha, R) {
      const ctx = this.ctx;
      for (let i = 0; i < ring.count; i++) {
        const major = ring.major && i % ring.major === 0;
        const len = R * (major ? ring.majorLen : ring.len);
        const a = (i / ring.count) * TAU;
        this.stroke(major ? alpha * 1.5 : alpha, major ? 1.4 : 1, false);
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r);
        ctx.lineTo(Math.cos(a) * (r + len), Math.sin(a) * (r + len));
        ctx.stroke();

        if (ring.labels && major && i % (ring.major * 3) === 0) {
          const [cr, cg, cb] = this.color;
          const lr = r + len + R * 0.035;
          ctx.save();
          ctx.rotate(a);
          ctx.translate(lr, 0);
          ctx.rotate(Math.PI / 2);
          ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha * 0.9})`;
          ctx.font = `${Math.max(7, R * 0.032)}px 'Share Tech Mono', monospace`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(String(Math.round((i / ring.count) * 360)).padStart(3, '0'), 0, 0);
          ctx.restore();
        }
      }
    }

    _arcs(r, ring, alpha) {
      const ctx = this.ctx;
      this.stroke(alpha, ring.width, ring.glow);
      ctx.lineCap = ring.caps ? 'round' : 'butt';
      for (const [start, span] of ring.arcs) {
        ctx.beginPath();
        ctx.arc(0, 0, r, rad(start), rad(start + span));
        ctx.stroke();
      }
      ctx.lineCap = 'butt';
      ctx.shadowBlur = 0;
    }

    _segments(r, ring, alpha) {
      const ctx = this.ctx;
      const step = TAU / ring.count;
      for (let i = 0; i < ring.count; i++) {
        // A travelling wave of brightness around the ring.
        const wave = 0.5 + 0.5 * Math.sin(this.t * 2.2 - i * 0.42);
        const lit = wave > 0.72 ? 1 : 0.28;
        this.stroke(alpha * lit * (0.6 + this.energy), 3.2, wave > 0.85);
        ctx.beginPath();
        ctx.arc(0, 0, r, i * step, (i + 1 - ring.gap) * step);
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
    }

    _bolts(r, ring, alpha, R) {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      for (let i = 0; i < ring.count; i++) {
        const a = (i / ring.count) * TAU;
        const x = Math.cos(a) * r, y = Math.sin(a) * r;
        this.stroke(alpha, 1, false);
        ctx.beginPath();
        ctx.arc(x, y, R * 0.022, 0, TAU);
        ctx.stroke();
        ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha * 0.55 * (0.4 + this.energy)})`;
        ctx.beginPath();
        ctx.arc(x, y, R * 0.009, 0, TAU);
        ctx.fill();
      }
    }

    _markers(r, ring, alpha, R) {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      const size = R * 0.04;
      for (let i = 0; i < ring.count; i++) {
        const a = (i / ring.count) * TAU;
        ctx.save();
        ctx.rotate(a);
        ctx.translate(r, 0);
        ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha})`;
        ctx.beginPath();
        ctx.moveTo(size, 0);
        ctx.lineTo(-size * 0.6, size * 0.62);
        ctx.lineTo(-size * 0.6, -size * 0.62);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }
    }

    _spokes(R) {
      const ctx = this.ctx;
      for (let i = 0; i < 8; i++) {
        const a = rad(i * 45 + this.t * 3);
        this.stroke(0.12 + 0.12 * this.energy, 1, false);
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * R * 1.08, Math.sin(a) * R * 1.08);
        ctx.lineTo(Math.cos(a) * R * 1.30, Math.sin(a) * R * 1.30);
        ctx.stroke();
      }
    }

    /* The arc reactor itself: layered glows plus an octagonal iris. */
    _coreLight(R) {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      const heat = 0.42 + 0.58 * this.energy + 0.35 * this.levelSmooth;
      const r0 = R * 0.20 * (1 + 0.10 * Math.sin(this.t * 2.4) + 0.18 * this.levelSmooth);

      const halo = ctx.createRadialGradient(0, 0, 0, 0, 0, R * 0.95);
      halo.addColorStop(0.0, `rgba(${cr},${cg},${cb},${0.42 * heat})`);
      halo.addColorStop(0.28, `rgba(${cr},${cg},${cb},${0.13 * heat})`);
      halo.addColorStop(1.0, 'rgba(0,0,0,0)');
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(0, 0, R * 0.95, 0, TAU);
      ctx.fill();

      const inner = ctx.createRadialGradient(0, 0, 0, 0, 0, r0);
      inner.addColorStop(0.0, `rgba(255,255,255,${0.88 * heat})`);
      inner.addColorStop(0.45, `rgba(${cr},${cg},${cb},${0.78 * heat})`);
      inner.addColorStop(1.0, `rgba(${cr},${cg},${cb},0)`);
      ctx.fillStyle = inner;
      ctx.beginPath();
      ctx.arc(0, 0, r0, 0, TAU);
      ctx.fill();

      ctx.save();
      ctx.rotate(this.t * 0.35);
      this.stroke(0.5 + 0.4 * this.energy, 1.4, true);
      ctx.beginPath();
      for (let i = 0; i <= 8; i++) {
        const a = (i / 8) * TAU;
        const x = Math.cos(a) * R * 0.135, y = Math.sin(a) * R * 0.135;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.closePath();
      ctx.stroke();
      ctx.restore();
      ctx.shadowBlur = 0;

      // The expanding shock ring on wake, speech start, and confirmations.
      if (this.pulse > 0.01) {
        const p = 1 - this.pulse;
        this.stroke(this.pulse * 0.6, 2, true);
        ctx.beginPath();
        ctx.arc(0, 0, R * (0.3 + p * 1.5), 0, TAU);
        ctx.stroke();
        ctx.shadowBlur = 0;
      }
    }

    /* A comet tail running the outer ring. Built from short arc segments of
       falling alpha rather than a conic gradient, which leaves a hard seam. */
    _drawSweep() {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      const head = this.t * 0.62;
      const R = this.R * 1.24;
      const segments = 26;
      const span = rad(74) / segments;
      ctx.save();
      ctx.translate(this.cx, this.cy);
      ctx.lineWidth = 2.5;
      ctx.lineCap = 'round';
      for (let i = 0; i < segments; i++) {
        const fade = (1 - i / segments) ** 2.2;
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${0.5 * fade * (0.25 + this.energy)})`;
        ctx.beginPath();
        ctx.arc(0, 0, R, head - (i + 1) * span, head - i * span);
        ctx.stroke();
      }
      ctx.lineCap = 'butt';
      ctx.restore();
    }

    _drawParticles(dt) {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      const drift = this.reduceMotion ? 0.2 : 1;
      for (const p of this.particles) {
        p.a += p.v * dt * drift * (0.4 + this.energy);
        const x = this.cx + Math.cos(p.a) * p.r;
        const y = this.cy + Math.sin(p.a) * p.r * 0.72;
        ctx.fillStyle = `rgba(${cr},${cg},${cb},${p.o * (0.35 + this.energy * 0.65)})`;
        ctx.fillRect(x, y, p.s, p.s);
      }
    }

    /* ------------------------------------------------------- tracked body */

    _toScreen(nx, ny) {
      // Vision coordinates are already mirrored by the tracker, so the overlay
      // and the person move the same way.
      return [nx * this.w, ny * this.h];
    }

    _drawSkeleton() {
      if (!this.pose) return;
      const ctx = this.ctx;
      const links = [['l_shoulder','r_shoulder'], ['l_shoulder','l_elbow'], ['l_elbow','l_wrist'],
                     ['r_shoulder','r_elbow'], ['r_elbow','r_wrist'], ['l_shoulder','l_hip'],
                     ['r_shoulder','r_hip'], ['l_hip','r_hip']];
      this.stroke(0.22, 1, false);
      for (const [a, b] of links) {
        const pa = this.pose[a], pb = this.pose[b];
        if (!pa || !pb) continue;
        const [x1, y1] = this._toScreen(pa.x, pa.y);
        const [x2, y2] = this._toScreen(pb.x, pb.y);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      const [cr, cg, cb] = this.color;
      ctx.fillStyle = `rgba(${cr},${cg},${cb},0.4)`;
      for (const key of Object.keys(this.pose)) {
        const [x, y] = this._toScreen(this.pose[key].x, this.pose[key].y);
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, TAU);
        ctx.fill();
      }
    }

    _drawHands() {
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      for (const hand of this.hands) {
        const pts = hand.points.map((p) => this._toScreen(p[0], p[1]));

        this.stroke(0.42, 1.2, true);
        for (const [a, b] of HAND_EDGES) {
          ctx.beginPath();
          ctx.moveTo(pts[a][0], pts[a][1]);
          ctx.lineTo(pts[b][0], pts[b][1]);
          ctx.stroke();
        }
        ctx.shadowBlur = 0;

        ctx.fillStyle = `rgba(${cr},${cg},${cb},0.85)`;
        for (const [x, y] of pts) {
          ctx.beginPath();
          ctx.arc(x, y, 2.2, 0, TAU);
          ctx.fill();
        }

        // Pinch indicator across thumb and index.
        const [tx, ty] = this._toScreen(hand.thumb_tip[0], hand.thumb_tip[1]);
        const [ix, iy] = this._toScreen(hand.index_tip[0], hand.index_tip[1]);
        const tight = hand.pinch < 0.30 && (hand.index_reach || 0) >= 1.3;
        this.stroke(tight ? 0.95 : 0.3, tight ? 2 : 1, tight);
        ctx.setLineDash(tight ? [] : [3, 4]);
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.lineTo(ix, iy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.shadowBlur = 0;

        const [bx, by, bw, bh] = hand.bbox;
        this._bracket(bx * this.w - 14, by * this.h - 14,
                      bw * this.w + 28, bh * this.h + 28, 0.5);

        const label = `${hand.label.toUpperCase()} · ${(hand.depth * 100).toFixed(0)}`;
        ctx.fillStyle = `rgba(${cr},${cg},${cb},0.6)`;
        ctx.font = "9px 'Share Tech Mono', monospace";
        ctx.textAlign = 'left';
        ctx.fillText(label, bx * this.w - 12, by * this.h - 20);
      }
    }

    _drawFace() {
      if (!this.face) return;
      const f = this.face;
      this._bracket(f.x * this.w, f.y * this.h, f.w * this.w, f.h * this.h, 0.35);
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      ctx.fillStyle = `rgba(${cr},${cg},${cb},0.45)`;
      ctx.font = "9px 'Share Tech Mono', monospace";
      ctx.textAlign = 'left';
      ctx.fillText(`FACE ${(f.score * 100).toFixed(0)}%`,
                   f.x * this.w, f.y * this.h - 7);
    }

    _bracket(x, y, w, h, alpha) {
      const ctx = this.ctx;
      const c = Math.max(8, Math.min(w, h) * 0.22);
      this.stroke(alpha, 1.2, false);
      const corners = [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]];
      for (const [px, py, sx, sy] of corners) {
        ctx.beginPath();
        ctx.moveTo(px + sx * c, py);
        ctx.lineTo(px, py);
        ctx.lineTo(px, py + sy * c);
        ctx.stroke();
      }
    }

    /* ------------------------------------------------------- radial menu */

    _drawRadial() {
      if (!this.radial) return;
      const ctx = this.ctx;
      const [cr, cg, cb] = this.color;
      const items = this.radial.items || [];
      if (!items.length) return;

      const age = Math.min(1, (performance.now() - this.radial.at) / 220);
      const [cx, cy] = this._toScreen(this.radial.center[0], this.radial.center[1]);
      const R = Math.min(this.w, this.h) * 0.17 * age;
      const step = TAU / items.length;

      ctx.save();
      ctx.globalAlpha = age;

      this.stroke(0.35, 1, false);
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.34, 0, TAU);
      ctx.stroke();

      items.forEach((item, i) => {
        const a0 = i * step - step / 2 - Math.PI / 2 + step / 2;
        const mid = i * step;
        const selected = this.radial.selection === i;

        this.stroke(selected ? 0.95 : 0.3, selected ? 3 : 1.4, selected);
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.8, mid - step * 0.42, mid + step * 0.42);
        ctx.stroke();
        ctx.shadowBlur = 0;

        if (selected) {
          ctx.fillStyle = `rgba(${cr},${cg},${cb},0.14)`;
          ctx.beginPath();
          ctx.moveTo(cx, cy);
          ctx.arc(cx, cy, R * 0.8, mid - step * 0.42, mid + step * 0.42);
          ctx.closePath();
          ctx.fill();
        }

        const lx = cx + Math.cos(mid) * R * 1.0;
        const ly = cy + Math.sin(mid) * R * 1.0;
        ctx.fillStyle = selected
          ? `rgba(255,255,255,0.95)` : `rgba(${cr},${cg},${cb},0.6)`;
        ctx.font = `${selected ? 12 : 10}px 'Rajdhani', sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(item.label, lx, ly);
      });

      ctx.restore();
    }
  }

  global.JarvisCore = Core;
})(window);
