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
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
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

    async load(model) {
      if (!this._setup()) return false;
      this.setStatus(`loading ${model.name}…`);
      try {
        const res = await fetch('/api/model?path=' + encodeURIComponent(model.path));
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
        const scale = 2 / Math.max(size.x, size.y, size.z, 1e-6);
        geometry.translate(-centre.x, -centre.y, -centre.z);
        geometry.scale(scale, scale, scale);

        if (this.mesh) {
          this.group.remove(this.mesh);
          this.mesh.geometry.dispose();
        }
        const material = new THREE.MeshPhongMaterial({
          color: 0x7fd4ef, specular: 0xbfefff, shininess: 28,
          flatShading: !normals,
        });
        this.mesh = new THREE.Mesh(geometry, material);
        this.group.add(this.mesh);

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 32),
          new THREE.LineBasicMaterial({ color: 0xb6f2ff, transparent: true, opacity: 0.28 }));
        this.mesh.add(edges);

        this.model = model;
        const tris = positions.length / 9;
        this.setStatus('');
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
      const hands = payload.hands || [];
      if (hands.length >= 2) {
        const span = Math.hypot(hands[0].palm[0] - hands[1].palm[0],
                                hands[0].palm[1] - hands[1].palm[1]);
        if (this._span != null) {
          this.targetDistance = Math.max(1.2, Math.min(9,
            this.targetDistance - (span - this._span) * 9));
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
        const dx = hand.palm[0] - g.x;
        const dy = hand.palm[1] - g.y;
        this.target.y = g.ry + dx * 6.0;
        this.target.x = Math.max(-Math.PI, Math.min(Math.PI, g.rx + dy * 5.0));
        const toward = Math.log(scale / g.scale);     // + as the hand comes nearer
        const opened = openness - g.open;             // + as the fingers spread
        this.targetDistance = Math.max(1.2, Math.min(9,
          g.d - toward * 2.6 + opened * 1.5));
        return;
      }

      if (pointing) {
        this.spin = false;
        if (!this.grab || this.grab.mode !== 'zoom') {
          this.grab = { mode: 'zoom', gap: gaps[0], d: this.targetDistance };
          return;
        }
        this.targetDistance = Math.max(1.2, Math.min(9,
          this.grab.d - (gaps[0] - this.grab.gap) * 2.4));
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
  }

  global.ModelViewer = ModelViewer;
})(window);
