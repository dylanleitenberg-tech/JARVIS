/* ===========================================================================
   The in-HUD 3D viewer.

   The model lives inside the interface, and the hand landmarks that already
   stream over the websocket turn it directly. Nothing here synthesises a mouse
   event, so this works with only the camera permission — no Accessibility, and
   no CAD application installed.

   STL is parsed here rather than pulled in as a loader dependency: binary STL
   is an 84-byte header and 50 bytes per triangle, and ASCII STL is one regex.
   =========================================================================== */
(function (global) {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  function parseSTL(buffer) {
    const view = new DataView(buffer);
    // A binary STL's triangle count must exactly match its length; anything
    // else is ASCII, whatever the first bytes happen to say.
    const expected = 84 + view.getUint32(80, true) * 50;
    return expected === buffer.byteLength ? parseBinary(view) : parseASCII(buffer);
  }

  function parseBinary(view) {
    const triangles = view.getUint32(80, true);
    const positions = new Float32Array(triangles * 9);
    const normals = new Float32Array(triangles * 9);
    let offset = 84;
    for (let i = 0; i < triangles; i++) {
      const nx = view.getFloat32(offset, true);
      const ny = view.getFloat32(offset + 4, true);
      const nz = view.getFloat32(offset + 8, true);
      offset += 12;
      for (let v = 0; v < 3; v++) {
        const p = i * 9 + v * 3;
        positions[p] = view.getFloat32(offset, true);
        positions[p + 1] = view.getFloat32(offset + 4, true);
        positions[p + 2] = view.getFloat32(offset + 8, true);
        normals[p] = nx; normals[p + 1] = ny; normals[p + 2] = nz;
        offset += 12;
      }
      offset += 2;                     // attribute byte count
    }
    return { positions, normals };
  }

  function parseASCII(buffer) {
    const text = new TextDecoder().decode(buffer);
    const verts = [];
    const re = /vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      verts.push(+m[1], +m[2], +m[3]);
    }
    return { positions: new Float32Array(verts), normals: null };
  }

  /* Smooth shading. STL carries one normal per triangle, so a curved surface
     tessellated coarsely (a STEP nozzle at 26 degrees a facet) reads as
     flat strips. Each vertex here takes the mean normal of the triangles
     that share its position, but only those within `angle` of its own face,
     so a cylinder goes smooth while a flange edge stays sharp. */
  function creasedNormals(pos, angle) {
    const nTri = pos.length / 9, cosA = Math.cos(angle * Math.PI / 180);
    const fn = new Float32Array(nTri * 3);
    for (let t = 0; t < nTri; t++) {
      const o = t * 9;
      const ax = pos[o + 3] - pos[o], ay = pos[o + 4] - pos[o + 1], az = pos[o + 5] - pos[o + 2];
      const bx = pos[o + 6] - pos[o], by = pos[o + 7] - pos[o + 1], bz = pos[o + 8] - pos[o + 2];
      let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
      const l = Math.hypot(nx, ny, nz) || 1;
      fn[t * 3] = nx / l; fn[t * 3 + 1] = ny / l; fn[t * 3 + 2] = nz / l;
    }
    const q = 1e4, buckets = new Map(), keys = new Array(nTri * 3);
    for (let v = 0; v < nTri * 3; v++) {
      const key = Math.round(pos[v * 3] * q) + ',' + Math.round(pos[v * 3 + 1] * q) + ',' + Math.round(pos[v * 3 + 2] * q);
      keys[v] = key;
      let list = buckets.get(key);
      if (!list) { list = []; buckets.set(key, list); }
      list.push((v / 3) | 0);
    }
    const out = new Float32Array(pos.length);
    for (let v = 0; v < nTri * 3; v++) {
      const t = (v / 3) | 0, tx = fn[t * 3], ty = fn[t * 3 + 1], tz = fn[t * 3 + 2];
      let sx = 0, sy = 0, sz = 0;
      for (const j of buckets.get(keys[v])) {
        const jx = fn[j * 3], jy = fn[j * 3 + 1], jz = fn[j * 3 + 2];
        if (tx * jx + ty * jy + tz * jz >= cosA) { sx += jx; sy += jy; sz += jz; }
      }
      const l = Math.hypot(sx, sy, sz);
      if (l > 1e-9) { out[v * 3] = sx / l; out[v * 3 + 1] = sy / l; out[v * 3 + 2] = sz / l; }
      else { out[v * 3] = tx; out[v * 3 + 1] = ty; out[v * 3 + 2] = tz; }
    }
    return out;
  }

  // The model is scaled so its longest side spans 2 units. A long thin part
  // (the worm string) is only 0.13 thick, so the camera has to get well inside
  // the box before a segment fills the view: 0.2 allows that. 5.5 keeps the
  // whole part about half the frame tall; further out it was a sliver.
  const ZOOM_MIN = 0.2, ZOOM_MAX = 5.5;
  const clampZoom = (d) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, d));

  class ModelViewer {
    constructor() {
      this.open = false;
      this.model = null;
      this.el = $('#model-stage');
      this.rotation = { x: -1.2, y: 0.5 };     // start on a three-quarter view
      this.target = { x: -1.2, y: 0.5 };
      this.distance = 3.2;
      this.targetDistance = 3.2;
      this.pan = { x: 0, y: 0 };
      this.targetPan = { x: 0, y: 0 };
      this.grab = null;                        // hand anchor while dragging
      this.spin = true;                        // idle turntable until you grab it

      // Deliberately NOT set up here. Creating a WebGL context can fail, and
      // this object is constructed during page start-up — a throw here took
      // the whole interface down with it, speech and gestures included.
      this.ready = false;
      this.failed = null;
      this._loop = this._loop.bind(this);
      requestAnimationFrame(this._loop);
    }

    _setup() {
      if (this.ready || this.failed) return this.ready;
      try {
        this._build();
        this.ready = true;
      } catch (err) {
        this.failed = err;
        console.error('[viewer] no WebGL:', err.message);
        this.setStatus('3D view unavailable in this window: ' + err.message);
      }
      return this.ready;
    }

    _build() {
      // preserveDrawingBuffer so the canvas can still be read after the frame
      // is drawn. Without it toDataURL comes back blank except in the instant
      // between render and composite, and J.A.R.V.I.S. cannot look at what he
      // just built. It costs a little fill rate and nothing else.
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true,
                                                 preserveDrawingBuffer: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      this.el.appendChild(renderer.domElement);
      this.renderer = renderer;

      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(42, 1, 0.01, 1000);
      this.group = new THREE.Group();
      this.scene.add(this.group);

      // Light it like the rest of the interface: a cool key, a dim fill, and
      // a rim that separates the silhouette from the black background.
      this.scene.add(new THREE.AmbientLight(0x4a7f99, 0.55));
      const key = new THREE.DirectionalLight(0x9fe6ff, 1.15);
      key.position.set(2, 3, 4);
      this.scene.add(key);
      const rim = new THREE.DirectionalLight(0x64e3ff, 0.9);
      rim.position.set(-3, -1, -2);
      this.scene.add(rim);

      this.grid = new THREE.GridHelper(4, 16, 0x64e3ff, 0x1b4a5e);
      this.grid.material.opacity = 0.22;
      this.grid.material.transparent = true;
      this.grid.position.y = -1;
      this.scene.add(this.grid);

      window.addEventListener('resize', () => this._resize());
    }

    _resize() {
      if (!this.ready) return;
      const rect = this.el.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.renderer.setSize(rect.width, rect.height, false);
      this.camera.aspect = rect.width / rect.height;
      this.camera.updateProjectionMatrix();
    }

    /* ------------------------------------------------------------ loading */

    /* opts.url + opts.keepView: a live-edit rebuild. It keeps the camera and
       the first load's centre and scale, so a dimension made longer actually
       looks longer instead of being re-framed back to the same size. */
    async load(model, opts = {}) {
      if (!this._setup()) return false;
      const rebuild = !!(opts.url && opts.keepView && this._norm && this.model === model);
      if (!rebuild) this.setStatus(`loading ${model.name}…`);
      try {
        const src = opts.url || ('/api/model?path=' + encodeURIComponent(model.path));
        const res = await fetch(src);
        if (!res.ok) throw new Error(await res.text());
        const { positions, normals } = parseSTL(await res.arrayBuffer());

        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        if (normals) geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
        else geometry.computeVertexNormals();

        // Centre on the model's own bounding box and scale it to a unit size,
        // so every part arrives framed the same way whatever units it used.
        geometry.computeBoundingBox();
        const box = geometry.boundingBox;
        const centre = new THREE.Vector3();
        box.getCenter(centre);
        const size = new THREE.Vector3();
        box.getSize(size);
        let scale = 2 / Math.max(size.x, size.y, size.z, 1e-6);
        if (rebuild) { centre.copy(this._norm.centre); scale = this._norm.scale; }
        else this._norm = { centre: centre.clone(), scale };
        geometry.translate(-centre.x, -centre.y, -centre.z);
        geometry.scale(scale, scale, scale);
        // "Make it smooth" sticks for this model, through rebuilds; a new model starts faceted.
        if (!rebuild && this.model !== model) this.smooth = false;
        if (opts.smooth) this.smooth = true;
        if (this.smooth) {
          geometry.setAttribute('normal', new THREE.BufferAttribute(
            creasedNormals(geometry.attributes.position.array, 38), 3));
        }

        // A rebuild cross-fades: the old shape dissolves while the new one
        // comes up with a brief glow, so an edit reads as the part changing,
        // not a flicker. A new model simply replaces the old one.
        const now = performance.now();
        if (this.mesh) {
          if (rebuild) {
            const old = this.mesh;
            old.material.transparent = true;
            old.material.depthWrite = false;
            old.children.forEach((c) => { if (c.material) c.material.transparent = true; });
            this._fading = (this._fading || []).concat([{ mesh: old, t0: now }]);
          } else {
            this.group.remove(this.mesh);
            this.mesh.geometry.dispose();
            (this._fading || []).forEach((f) => { this.group.remove(f.mesh); f.mesh.geometry.dispose(); });
            this._fading = [];
          }
        }
        const material = new THREE.MeshPhongMaterial({
          color: 0x7fd4ef, specular: 0xbfefff, shininess: 28,
          flatShading: !normals && !this.smooth,
          transparent: rebuild, opacity: rebuild ? 0 : 1,
        });
        this.mesh = new THREE.Mesh(geometry, material);
        this.group.add(this.mesh);
        this._arrive = rebuild ? now : null;

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 32),
          new THREE.LineBasicMaterial({ color: 0xb6f2ff, transparent: true, opacity: 0.28 }));
        this.mesh.add(edges);

        this.lastUrl = src;
        const fresh = this.model !== model || !rebuild;
        this.model = model;
        const tris = positions.length / 9;
        this.setStatus('');
        // Tell the assistant what is on screen: a .scad part becomes editable.
        if (fresh && !rebuild && global.bus && global.bus.send) {
          global.bus.send({ type: 'model_loaded', path: model.path });
        }
        $('#model-name').textContent = model.name.replace(/_/g, ' ');
        $('#model-meta').textContent =
          `${model.project} · ${tris.toLocaleString()} triangles · ` +
          `${(size.x / scale * scale).toFixed(0)}×${size.y.toFixed(0)}×${size.z.toFixed(0)} units`;
        this._resize();
        return true;
      } catch (err) {
        this.setStatus('could not load: ' + err.message);
        return false;
      }
    }

    setStatus(text) {
      $('#model-status').textContent = text;
      $('#model-status').hidden = !text;
    }

    toggle(force) {
      this.open = force === undefined ? !this.open : !!force;
      $('#model-view').hidden = !this.open;
      document.body.classList.toggle('model-open', this.open);
      // Tell the gesture engine, so a hand waved at the model is not also read
      // as an app switch or a menu.
      if (global.bus && global.bus.send) global.bus.send({ type: 'model_view', open: this.open });
      // The corner camera: its own MJPEG client, started only while the viewer
      // is up and dropped on close, so it costs nothing the rest of the time.
      const cam = $('#model-cam'), camImg = $('#model-cam-img');
      if (cam && camImg) {
        cam.hidden = !this.open;
        camImg.src = this.open ? '/feed.mjpg?' + Date.now() : '';
      }
      if (this.open) {
        this._setup();
        setTimeout(() => this._resize(), 30);
      }
    }

    /* ---------------------------------------------------------- the hands */

    /* An open hand turns it and pushes or pulls it; pinch to turn, two
       fingers to slide, thumb and finger apart (or both palms apart) to zoom. The
       maths is the same relative-anchor idea CAD mode uses: the grab point is
       remembered and only the delta since then is applied. */
    onVision(payload) {
      if (!this.open || !this.ready) return;
      // Only the hands the tracker attributed to the user drive the model.
      const hands = (payload.hands || []).filter((h) => h.owner !== false);
      if (hands.length >= 2) {
        const span = Math.hypot(hands[0].palm[0] - hands[1].palm[0],
                                hands[0].palm[1] - hands[1].palm[1]);
        if (this._span != null) {
          this.targetDistance = clampZoom(
            this.targetDistance * Math.exp(-(span - this._span) * 6));
        }
        this._span = span;
        this.grab = null;
        this.spin = false;
        return;
      }
      this._span = null;

      const hand = hands[0];
      if (!hand) { this.grab = null; return; }

      const gaps = hand.pinches || [hand.pinch, 9, 9, 9];
      const reach = hand.reaches || [hand.index_reach || 0, 0, 0, 0];
      const pinching = gaps[0] < 0.3 && reach[0] >= 1.3;
      const ext = hand.extended || [];
      const nExt = hand.n_extended != null ? hand.n_extended : ext.filter(Boolean).length;
      const twoFingers = nExt === 2 && ext[0] && ext[1];
      const pointing = !pinching && ext[0] && !ext[1] && !ext[2] && !ext[3];
      const openHand = !pinching && nExt === 4;

      // A dimension picked by voice ("adjust the rim") is dragged with a pinch:
      // up makes it bigger, down smaller, across its whole range in a hand-height.
      if (this.editParam && pinching) {
        this.spin = false;
        const p = this.editParam;
        if (!this.grab || this.grab.mode !== 'param') {
          this.grab = { mode: 'param', y: hand.palm[1], v: +p.value };
          return;
        }
        const span = (p.hi - p.lo) || Math.abs(+p.value) || 1;
        let v = this.grab.v + (this.grab.y - hand.palm[1]) * span * 1.2;
        v = Math.max(p.lo, Math.min(p.hi, v));
        if (p.step) v = Math.round(v / p.step) * p.step;
        v = +v.toFixed(6);
        if (v !== +p.value) this._paramInput(p.name, v, true);
        return;
      }
      if (this.grab && this.grab.mode === 'param') this.grab = null;
      // Openness: the mean thumb-to-finger gap in hand-scales. It falls as the
      // fingers come together, whether they curl or bunch toward the thumb.
      const openness = gaps.slice(0, 4).reduce((a, b) => a + b, 0) / 4;
      const scale = Math.max(hand.scale || 0.1, 1e-4);   // bigger = nearer the lens

      // Open hand: the whole hand is the handle. Moving it turns the model.
      // Pushing it away (the hand gets smaller) or opening the fingers backs
      // the view out; pulling it in or closing the fingers brings it closer.
      // The hold is sticky: once an open hand has the model, closing the
      // fingers stays a zoom-in rather than becoming some other grip, until the
      // hand pinches or leaves the frame.
      const holding = this.grab && this.grab.mode === 'hand';
      if (openHand || (holding && !pinching)) {
        this.spin = false;
        if (!holding) {
          this.grab = { mode: 'hand', x: hand.palm[0], y: hand.palm[1],
                        rx: this.target.x, ry: this.target.y,
                        scale, open: openness, d: this.targetDistance };
          return;
        }
        const g = this.grab;
        // A hand pulled close to the lens runs off the edge of the frame, and
        // the landmarks that remain are guesses: the hand "shrinks", and a zoom
        // read from its size would spring back out. So the zoom accumulates
        // frame to frame, only while the whole hand is in view and the change
        // is one a hand can make in a frame; otherwise it holds where it is.
        const b = hand.bbox;
        const inFrame = !b || (b[0] > 0.02 && b[1] > 0.02 && b[0] + b[2] < 0.98 && b[1] + b[3] < 0.98);
        if (!inFrame) {
          g.x = hand.palm[0]; g.y = hand.palm[1]; g.rx = this.target.x; g.ry = this.target.y;
          g.scale = scale; g.open = openness;
          return;
        }
        const dx = hand.palm[0] - g.x;
        const dy = hand.palm[1] - g.y;
        this.target.y = g.ry + dx * 6.0;
        this.target.x = Math.max(-Math.PI, Math.min(Math.PI, g.rx + dy * 5.0));
        let toward = Math.log(scale / g.scale);       // + as the hand comes nearer
        let opened = openness - g.open;               // + as the fingers spread
        if (Math.abs(toward) < 0.012) toward = 0;     // landmark jitter, not motion
        if (Math.abs(opened) < 0.015) opened = 0;
        // Proportional: a pull that halves the hand's distance quarters the
        // camera's, whatever the current zoom, so one gesture goes from the
        // whole part to a close-up and the control stays fine near the surface.
        if (Math.abs(toward) < 0.35 && Math.abs(opened) < 1.2) {
          this.targetDistance = clampZoom(
            this.targetDistance * Math.exp(-toward * 2.0 + opened * 1.2));
        }
        g.scale = scale; g.open = openness;           // reference rolls forward
        return;
      }

      if (pointing) {
        this.spin = false;
        if (!this.grab || this.grab.mode !== 'zoom') {
          this.grab = { mode: 'zoom', gap: gaps[0], d: this.targetDistance };
          return;
        }
        this.targetDistance = clampZoom(
          this.grab.d * Math.exp(-(gaps[0] - this.grab.gap) * 1.8));
        return;
      }

      if (pinching || twoFingers) {
        this.spin = false;
        const mode = pinching ? 'orbit' : 'pan';
        if (!this.grab || this.grab.mode !== mode) {
          this.grab = { mode, x: hand.palm[0], y: hand.palm[1],
                        rx: this.target.x, ry: this.target.y,
                        px: this.targetPan.x, py: this.targetPan.y };
          return;
        }
        const dx = hand.palm[0] - this.grab.x;
        const dy = hand.palm[1] - this.grab.y;
        if (mode === 'orbit') {
          this.target.y = this.grab.ry + dx * 6.0;
          this.target.x = Math.max(-Math.PI, Math.min(Math.PI,
                                    this.grab.rx + dy * 5.0));
        } else {
          this.targetPan.x = this.grab.px + dx * 4.0;
          this.targetPan.y = this.grab.py - dy * 4.0;
        }
      } else {
        this.grab = null;
      }
    }

    /* ------------------------------------------------------ live editing */

    _panel() {
      let el = $('#model-params');
      if (!el) {
        el = document.createElement('aside');
        el.id = 'model-params';
        el.className = 'model-params';
        el.hidden = true;
        this.el.appendChild(el);
      }
      return el;
    }

    /* The .scad on screen and its dimensions; an empty list hides the panel. */
    setParams(params, name, dirty, edits, mode) {
      this.params = params || [];
      this.editParam = null;
      const el = this._panel();
      if (mode === 'step') {
        // A STEP has no dimensions: the panel lists the edits made so far.
        const list = (edits || []).map((e, i) => `<div class="param edit"><label><span>${i + 1}. ${String(e).replace(/</g, '&lt;')}</span></label></div>`).join('')
          || '<div class="param"><label><span>no edits yet</span></label></div>';
        el.innerHTML = `<header><span>EDIT · ${String(name || '').replace(/_/g, ' ')}</span><i>${dirty ? 'unsaved' : 'original'}</i></header>` +
          `<div class="params">${list}</div><footer>say "make the nozzle 20% longer" · "remove the gimbal" · "undo" · "save"</footer>`;
        el.hidden = false;
        return;
      }
      if (!this.params.length) { el.hidden = true; el.innerHTML = ''; return; }
      const rows = this.params.map((p, i) => {
        const val = p.kind === 'bool'
          ? `<input type="checkbox" data-i="${i}" ${p.value ? 'checked' : ''}>`
          : `<input type="range" data-i="${i}" min="${p.lo}" max="${p.hi}" step="${p.step || 'any'}" value="${p.value}">`;
        return `<div class="param" data-name="${p.name}" title="${(p.comment || '').replace(/"/g, '&quot;')}">
          <label><span>${p.spoken}</span><b>${this._fmt(p)}</b></label>${val}</div>`;
      }).join('');
      el.innerHTML = `<header><span>EDIT · ${String(name || '').replace(/_/g, ' ')}</span><i>${dirty ? 'unsaved' : 'saved'}</i></header>` +
        `<div class="params">${rows}</div><footer>say "set rim to 3" · "make it thicker" · "undo" · "save"</footer>`;
      el.hidden = false;
      el.querySelectorAll('input').forEach((input) => {
        const p = this.params[+input.dataset.i];
        const on = () => this._paramInput(p.name, p.kind === 'bool' ? input.checked : +input.value, false);
        input.addEventListener('input', on);
        input.addEventListener('change', () => { this._lastSend = 0; on(); });
      });
    }

    updateParams(params, dirty) {
      const el = this._panel();
      (params || []).forEach((p) => {
        const mine = (this.params || []).find((q) => q.name === p.name);
        if (!mine) return;
        // A value being dragged right now is the source of truth, not the echo.
        const dragging = this.grab && this.grab.mode === 'param' && this.editParam === mine;
        if (!dragging) mine.value = p.value;
        const row = el.querySelector(`.param[data-name="${p.name}"]`);
        if (!row) return;
        row.querySelector('b').textContent = this._fmt(mine);
        row.classList.toggle('changed', p.value !== p.base);
        const input = row.querySelector('input');
        if (input && document.activeElement !== input && !dragging) {
          if (input.type === 'checkbox') input.checked = !!p.value; else input.value = p.value;
        }
      });
      const tag = el.querySelector('header i');
      if (tag) tag.textContent = dirty ? 'unsaved' : 'saved';
    }

    selectParam(name) {
      this.editParam = (this.params || []).find((p) => p.name === name) || null;
      const el = this._panel();
      el.querySelectorAll('.param').forEach((row) => {
        const on = row.dataset.name === name;
        row.classList.toggle('picked', on);
        if (on) row.scrollIntoView({ block: 'nearest' });
      });
      if (this.editParam) el.hidden = false;
    }

    setBuilding(on) {
      this._panel().classList.toggle('building', !!on);
    }

    _fmt(p) {
      if (p.kind === 'bool') return p.value ? 'on' : 'off';
      const v = +p.value;
      return Math.abs(v) >= 100 ? v.toFixed(1) : +v.toPrecision(4) + '';
    }

    /* Send at most every 120 ms while dragging; the final value always goes. */
    _paramInput(name, value, fromHand) {
      const p = (this.params || []).find((q) => q.name === name);
      if (!p) return;
      p.value = value;
      const row = this._panel().querySelector(`.param[data-name="${name}"]`);
      if (row) {
        row.querySelector('b').textContent = this._fmt(p);
        const input = row.querySelector('input');
        if (fromHand && input && input.type === 'range') input.value = value;
      }
      const now = performance.now();
      clearTimeout(this._sendTimer);
      const send = () => {
        this._lastSend = performance.now();
        if (global.bus && global.bus.send) global.bus.send({ type: 'cad_param', name, value: p.value });
      };
      if (!this._lastSend || now - this._lastSend > 120) send();
      else this._sendTimer = setTimeout(send, 130);
    }

    reset() {
      this.target = { x: -1.2, y: 0.5 };
      this.targetPan = { x: 0, y: 0 };
      this.targetDistance = 3.2;
      this.spin = true;
    }

    /* -------------------------------------------------------------- frame */

    _loop() {
      requestAnimationFrame(this._loop);
      if (!this.open || !this.ready || !this.renderer) return;
      if (!this.renderer.domElement.width) this._resize();

      // Cross-fade after an edit: 450 ms out for the old shape, in for the new,
      // with a cyan glow on the new one that settles over a second.
      const now = performance.now();
      if (this._fading && this._fading.length) {
        this._fading = this._fading.filter((f) => {
          const k = Math.min(1, (now - f.t0) / 450);
          f.mesh.material.opacity = 1 - k;
          f.mesh.children.forEach((c) => { if (c.material) c.material.opacity = 0.28 * (1 - k); });
          if (k >= 1) { this.group.remove(f.mesh); f.mesh.geometry.dispose(); return false; }
          return true;
        });
      }
      if (this._arrive != null && this.mesh) {
        const t = now - this._arrive;
        const m = this.mesh.material;
        m.opacity = Math.min(1, t / 450);
        const glow = Math.max(0, 1 - t / 1100);
        m.emissive.setRGB(0.10 * glow, 0.55 * glow, 0.75 * glow);
        if (t > 1100) {
          m.opacity = 1; m.transparent = false; m.emissive.setRGB(0, 0, 0); m.needsUpdate = true;
          this._arrive = null;
        }
      }

      if (this.spin) this.target.y += 0.0035;

      // Ease toward the target so tracking jitter does not shake the model.
      this.rotation.x += (this.target.x - this.rotation.x) * 0.18;
      this.rotation.y += (this.target.y - this.rotation.y) * 0.18;
      this.distance += (this.targetDistance - this.distance) * 0.12;
      this.pan.x += (this.targetPan.x - this.pan.x) * 0.18;
      this.pan.y += (this.targetPan.y - this.pan.y) * 0.18;

      this.group.rotation.set(this.rotation.x, this.rotation.y, 0);
      this.camera.position.set(this.pan.x, this.pan.y, this.distance);
      this.camera.lookAt(this.pan.x, this.pan.y, 0);
      this.renderer.render(this.scene, this.camera);
    }

    /* Photograph the model from a few angles so J.A.R.V.I.S. can look at what
       he just built. Editing geometry blind is what turned a nozzle into a
       starburst: the numbers all checked out and nobody looked.

       The viewer's own camera is left exactly as the user had it — the shot
       is taken from a temporary one, so capturing never moves the model on
       screen. Angles rather than one view because a change can be invisible
       from the front and obvious from the side. */
    capture(views = 3, size = 640) {
      if (!this.ready || !this.renderer || !this.group) return [];
      const canvas = this.renderer.domElement;
      const oldW = canvas.width, oldH = canvas.height;
      const keep = { x: this.group.rotation.x, y: this.group.rotation.y, z: this.group.rotation.z };
      const cam = this.camera.position.clone();
      const shots = [];
      try {
        this.renderer.setSize(size, size, false);
        this.camera.aspect = 1;
        // Back off a little so a part that grew cannot leave the frame — a
        // shot that crops the failure is worse than no shot.
        const dist = this.distance * 1.35;
        for (let i = 0; i < views; i++) {
          const yaw = (i / views) * Math.PI * 2;
          this.group.rotation.set(0.34, yaw, 0);
          this.camera.position.set(0, 0, dist);
          this.camera.lookAt(0, 0, 0);
          this.camera.updateProjectionMatrix();
          this.renderer.render(this.scene, this.camera);
          shots.push(canvas.toDataURL('image/png'));
        }
      } catch (_) {
        return [];
      } finally {
        this.group.rotation.set(keep.x, keep.y, keep.z);
        this.camera.position.copy(cam);
        this.renderer.setSize(oldW, oldH, false);
        this.camera.aspect = oldW / Math.max(oldH, 1);
        this.camera.updateProjectionMatrix();
        this._resize();
      }
      return shots;
    }
  }

  global.ModelViewer = ModelViewer;
})(window);
