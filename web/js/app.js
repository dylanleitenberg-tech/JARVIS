/* ===========================================================================
   Wiring: the browser half talks to the Python half, and both halves drive
   the same HUD.
   =========================================================================== */
(function () {
  'use strict';

  // Each piece is constructed defensively: one of them failing must not stop
  // the others from wiring up. A WebGL failure in the model viewer once took
  // speech and gesture handling down with it, because it threw before any of
  // the bus subscriptions below were registered.
  function build(name, make, fallback) {
    try {
      return make();
    } catch (err) {
      console.error(`[init] ${name} failed:`, err);
      return fallback;
    }
  }

  const core = new JarvisCore(document.getElementById('core'));
  const hud = new Hud(core);
  const panel = build('hand panel', () => new HandPanel(core, (m) => bus.send(m)),
                      { init() {}, setVision() {}, setArmed() {}, flash() {},
                        applyTuned() {}, toggle() {}, open: false, bindings: {} });
  const viewer = build('model viewer', () => new ModelViewer(),
                       { onVision() {}, toggle() {}, load: async () => false,
                         reset() {}, open: false });
  // Exposed so the drawer can be driven from the console while tuning.
  window.jarvis = { core, hud, panel, bus, viewer };
  let speech = null;
  let config = null;
  let visionSeen = 0;

  /* --------------------------------------------------------------- link */

  bus.on('link', (e) => {
    hud.log(e.up ? 'link established' : 'link lost — reconnecting', e.up ? 'ok' : 'warn');
    document.getElementById('offline').hidden = !!e.up;
    if (!e.up) {
      hud.chip('cam', false);
      hud.chip('mic', false);
      hud.chip('gesture', false);
      core.clearVision();
      core.alert(true);
    } else {
      core.alert(false);
    }
  });

  bus.on('hello', (e) => {
    // Whatever else fails in here, the boot screen must come down — otherwise
    // one bad line leaves a black window with no way to see why.
    try {
      applyHello(e);
    } catch (err) {
      console.error('[hello]', err);
      hud.log('interface error during startup: ' + err.message, 'error');
    } finally {
      hud.boot(!(config && config.hud && config.hud.boot_sequence))
         .then(() => bus.send({ type: 'ready' }));
    }
  });

  function applyHello(e) {
    config = e.config;
    document.body.dataset.theme = config.hud.theme || 'stark';
    document.body.dataset.scanlines = config.hud.scanlines ? 'on' : 'off';
    document.body.dataset.grain = config.hud.grain ? 'on' : 'off';
    if (config.hud.reduce_motion) {
      document.body.dataset.motion = 'reduced';
      core.reduceMotion = true;
    }

    hud.bindings(e.gestures.bindings);
    hud.commandIndex(e.actions, (name) => bus.send({ type: 'action', name }));
    panel.init(e.gestures, e.actions);
    hud.chip('ai', e.ai.status === 'ready', e.ai.status !== 'ready');
    hud.chip('axs', e.accessibility, !e.accessibility);
    hud.log(`cognition: ${e.ai.backend} ${e.ai.detail || ''}`.trim(),
            e.ai.status === 'ready' ? 'ok' : 'warn');
    if (!e.accessibility) {
      hud.log('accessibility permission missing — keyboard, mouse and window ' +
              'control are inert until it is granted', 'error');
    }

    if (!speech) startSpeech(config.speech);
    if (config.vision && config.vision.stream_feed) attachFeed();
  }

  /* ---------------------------------------------------------- telemetry */

  bus.on('telemetry', (e) => {
    hud.setTelemetry(e);
    canAct = !!e.can_control;
    paintSwitch(!!e.armed);
  });

  bus.on('vision_state', (e) => {
    const online = e.state === 'online';
    hud.chip('cam', online, e.state === 'error');
    document.getElementById('feed-off').hidden = online;
    hud.log(`optics ${e.state}${e.error ? ': ' + e.error : ''}`,
            e.state === 'error' ? 'error' : 'ok');
    if (online) attachFeed();
  });

  bus.on('vision', (e) => {
    core.setVision(e);
    panel.setVision(e);
    viewer.onVision(e);
    // Throttle the DOM side; the canvas already runs at frame rate.
    const now = performance.now();
    if (now - visionSeen > 180) {
      visionSeen = now;
      hud.setTracking(e);
    }
  });

  function attachFeed() {
    const img = document.getElementById('feed');
    if (img.dataset.on === '1') return;
    img.dataset.on = '1';
    img.src = '/feed.mjpg?' + Date.now();
    img.onerror = () => { img.dataset.on = '0'; };
  }

  /* ------------------------------------------------------------ speech */

  bus.on('utterance', (e) => {
    if (e.source !== 'voice') hud.heard(e.text, true);
    hud.log(e.text, 'you');
  });

  bus.on('say_partial', (e) => hud.speak(e.text, true));

  bus.on('say', async (e) => {
    hud.speak(e.text);
    hud.log(e.text, 'ok');
    if (speech) await speech.say(e.text);
  });

  bus.on('thinking', (e) => {
    if (e.on) hud.voiceState('thinking');
    else if (speech) hud.voiceState(speech.awake ? 'listening' : 'standby');
  });

  /* ----------------------------------------------------------- actions */

  bus.on('action_result', (e) => {
    const args = e.args && Object.keys(e.args).length
      ? ' ' + JSON.stringify(e.args) : '';
    if (e.ok) {
      hud.log(`${e.action}${args}`, 'ok');
      core.ping();
    } else if (!e.pending) {
      hud.log(`${e.action}${args} — ${e.error}`, 'error');
    }
  });

  bus.on('confirm_required', (e) => {
    const pretty = e.action.replace(/_/g, ' ') +
      (e.args && e.args.name ? ` — ${e.args.name}` : '');
    hud.confirm(pretty,
      () => bus.send({ type: 'confirm', accept: true }),
      () => bus.send({ type: 'confirm', accept: false }));
  });

  bus.on('log', (e) => hud.log(e.text, e.level === 'error' ? 'error'
                                    : e.level === 'warn' ? 'warn' : ''));

  bus.on('error', (e) => hud.log(e.text, 'error'));

  /* ---------------------------------------------------------- gestures */

  bus.on('gesture_armed', (e) => {
    hud.log(`gesture control ${e.armed ? 'on' : 'off'} (${e.why})`,
            e.armed ? 'ok' : 'warn');
    panel.setArmed(e.armed);
    paintSwitch(e.armed);
    core.ping();
  });

  bus.on('gesture_tuned', (e) => {
    panel.applyTuned(e.patch);
    if (e.saved) hud.log(`calibration saved: ${JSON.stringify(e.patch)}`, 'ok');
  });

  bus.on('rebound', (e) => {
    hud.log(`${e.gesture.replace(/_/g, ' ')} → ${(e.action || 'nothing').replace(/_/g, ' ')}`,
            'ok');
    hud.bindings(panel.bindings);
  });

  bus.on('gesture', (e) => {
    panel.flash(e.name);
    if (e.name === 'press' || e.name === 'drag_start' || e.name === 'drag_end') return;
    hud.log(`gesture: ${e.name.replace(/_/g, ' ')}` +
            (e.action ? ` → ${e.action.replace(/_/g, ' ')}` : ''), 'ok');
    core.ping();

    // A thumbs up or down answers an open confirmation without touching anything.
    if (hud.confirmOpen && e.action === 'confirm') hud._confirmYes();
    if (hud.confirmOpen && e.action === 'dismiss') hud._confirmNo();
  });

  bus.on('radial_open', (e) => {
    core.setRadial({ center: e.center, items: e.items, selection: null,
                     at: performance.now() });
  });
  bus.on('radial_update', (e) => {
    if (core.radial) core.radial.selection = e.selection;
  });
  bus.on('radial_close', (e) => {
    core.setRadial(null);
    if (e.chosen) core.ping();
  });

  bus.on('cursor', (e) => core.setCursor(e));

  /* ------------------------------------------------------- speech setup */

  function startSpeech(cfg) {
    speech = new Speech(cfg);
    window.speech = speech;

    if (!speech.supported) {
      bus.send({ type: 'mic', state: 'unsupported browser' });
      hud.log('this browser has no speech recognition — use Chrome, or type below', 'warn');
      hud.chip('mic', false, true);
      hud.voiceState('standby');
      return;
    }

    speech.onState = (state) => hud.voiceState(state);
    speech.onLevel = (v) => { core.setLevel(v); hud.setLevel(v); };
    speech.onHeard = (text, final) => hud.heard(text, final);
    speech.onError = (text, state) => {
      hud.log('voice: ' + text, 'warn');
      hud.chip('mic', false, true);
      bus.send({ type: 'mic', state: state || 'blocked', detail: text });
    };

    speech.onCommand = (text, meta) => {
      if (meta && meta.sleep) {
        bus.send({ type: 'sleep' });
        hud.speak('Standing by.');
        speech.say('Standing by.');
        return;
      }
      if (hud.confirmOpen) {
        if (/^(yes|yeah|confirm|do it|proceed|affirmative)\b/i.test(text)) return hud._confirmYes();
        if (/^(no|cancel|stop|abort|negative)\b/i.test(text)) return hud._confirmNo();
      }
      bus.send({ type: 'utterance', text, source: 'voice' });
    };

    // Chrome will not open a microphone until the page has been interacted
    // with, and macOS will not even list Chrome under Microphone until Chrome
    // has asked. So the prompt below is shown until the mic is genuinely live.
    const prompt = document.getElementById('mic-prompt');
    const why = document.getElementById('mic-prompt-why');

    const request = async () => {
      why.textContent = 'requesting…';
      await speech.start();
      if (speech.micOk === false) {
        const err = speech.micError || {};
        prompt.classList.add('bad');
        why.textContent = /system/i.test(err.message || '')
          ? 'macOS is blocking Chrome — System Settings › Microphone'
          : (err.name ? `${err.name}: ${err.message}` : 'blocked — click to retry');
        hud.chip('mic', false, true);
      } else {
        prompt.hidden = true;
        hud.chip('mic', true);
        bus.send({ type: 'mic', state: 'live' });
        hud.log('microphone live — say "Jarvis"', 'ok');
      }
    };

    prompt.hidden = false;
    prompt.onclick = request;
    // Any first interaction anywhere also counts, so the prompt is a hint
    // rather than a gate.
    const arm = () => {
      window.removeEventListener('pointerdown', arm);
      window.removeEventListener('keydown', arm);
      request();
    };
    window.addEventListener('pointerdown', arm, { once: true });
    window.addEventListener('keydown', arm, { once: true });
    hud.log('click ENABLE MICROPHONE to let J.A.R.V.I.S. hear you', 'warn');
  }

  /* ---------------------------------------------------------- controls */

  const gestureSwitch = document.getElementById('gesture-switch');
  let canAct = false;          // whether control permission is actually there

  function paintSwitch(armed) {
    gestureSwitch.classList.toggle('on', armed && canAct);
    gestureSwitch.classList.toggle('inert', armed && !canAct);
    gestureSwitch.setAttribute('aria-pressed', armed ? 'true' : 'false');
    gestureSwitch.querySelector('.switch-state').textContent = armed ? 'ON' : 'OFF';
    gestureSwitch.title = armed && !canAct
      ? 'Gestures are on, but macOS control permission is missing so nothing will move'
      : 'Gesture control (g)';
  }

  gestureSwitch.onclick = () => {
    const armed = gestureSwitch.classList.contains('on') ||
                  gestureSwitch.classList.contains('inert');
    bus.send({ type: 'arm_gestures', armed: !armed });
  };

  document.getElementById('btn-arm').onclick = () => {
    const armed = document.getElementById('gesture-state').classList.contains('armed');
    bus.send({ type: 'arm_gestures', armed: !armed });
  };

  document.getElementById('btn-mic').onclick = () => {
    if (!speech) return;
    speech.toggle();
    document.getElementById('btn-mic').classList.toggle('active', speech.wantListening);
  };

  document.querySelector('[data-cmd="vision-on"]').onclick = () =>
    bus.send({ type: 'vision', on: true });

  const composer = document.getElementById('composer');
  composer.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const text = composer.value.trim();
    if (!text) return;
    composer.value = '';
    bus.send({ type: 'utterance', text, source: 'text' });
  });

  window.addEventListener('keydown', (event) => {
    const typing = document.activeElement === composer;
    if (event.key === 'Escape') {
      if (panel.open) panel.toggle(false);
      else if (hud.confirmOpen) hud.dismissConfirm();
      else if (speech) speech.shutUp();
      composer.blur();
      return;
    }
    if (typing) return;
    if (event.key === '/') { event.preventDefault(); composer.focus(); }
    if (event.key === 'c') document.body.classList.toggle('hide-cursor');
    if (event.key === 'g') document.getElementById('btn-arm').click();
    if (event.key === 'h') panel.toggle();
    if (event.key === 'm') { viewer.toggle(); if (viewer.open) refreshModels(''); }
    if (event.key === 'Q' && event.shiftKey) powerDown();
    if (event.key === 't') {
      const order = ['stark', 'mark42', 'combat'];
      const now = document.body.dataset.theme || 'stark';
      document.body.dataset.theme = order[(order.indexOf(now) + 1) % order.length];
    }
  });

  /* ------------------------------------------------------- model viewer */

  let modelCache = [];

  async function refreshModels(query) {
    const res = await fetch('/api/models' + (query ? '?q=' + encodeURIComponent(query) : ''));
    const data = await res.json();
    modelCache = data.models || [];
    document.getElementById('model-list').innerHTML = modelCache.slice(0, 80)
      .map((m, i) => `<div class="model-chip" data-i="${i}">` +
           `<b>${m.name.replace(/_/g, ' ')}</b><span>${m.project}</span></div>`).join('');
  }

  document.getElementById('model-list').onclick = (event) => {
    const chip = event.target.closest('.model-chip');
    if (!chip) return;
    document.querySelectorAll('.model-chip').forEach((c) => c.classList.remove('on'));
    chip.classList.add('on');
    viewer.load(modelCache[+chip.dataset.i]);
  };
  document.getElementById('model-search').oninput = (e) => refreshModels(e.target.value.trim());
  document.getElementById('model-reset').onclick = () => viewer.reset();
  document.getElementById('model-close').onclick = () => viewer.toggle(false);

  bus.on('show_model', async (e) => {
    viewer.toggle(true);
    await refreshModels('');
    if (e.model) await viewer.load(e.model);
  });

  // Live CAD edits: the assistant owns the numbers and the builds; the viewer
  // shows the dimensions, sends slider and hand changes, and swaps the mesh.
  bus.on('cad_params', (e) => { if (viewer.setParams) viewer.setParams(e.params, e.name, e.dirty, e.edits, e.mode); });
  bus.on('cad_done', () => { if (viewer.setBuilding) viewer.setBuilding(false); });
  bus.on('cad_panel', () => { const el = document.getElementById('model-params'); if (el && viewer.params && viewer.params.length) el.hidden = false; });
  bus.on('cad_select', (e) => { if (viewer.selectParam) viewer.selectParam(e.name); });
  bus.on('cad_building', () => { if (viewer.setBuilding) viewer.setBuilding(true); });
  bus.on('cad_failed', (e) => {
    if (viewer.setBuilding) viewer.setBuilding(false);   // the reason arrives as a log line
  });
  bus.on('model_update', async (e) => {
    if (!viewer.model) return;
    const url = e.url || viewer.lastUrl ||
      ('/api/model?path=' + encodeURIComponent(viewer.model.path));
    await viewer.load(viewer.model, { url, keepView: true, smooth: !!e.smooth });
    if (viewer.updateParams && e.params) viewer.updateParams(e.params, e.dirty);
    if (viewer.setBuilding) viewer.setBuilding(false);
  });

  /* ------------------------------------------------------- staying open */

  // The HUD is meant to stay up, so closing it asks first. `quitting` is set
  // only by a deliberate power-down, which must not be challenged.
  let quitting = false;

  // Whatever takes this page away — a power-down, the window being closed,
  // the tab being discarded — the microphone goes with it. `pagehide` rather
  // than only `beforeunload`, because a page put into the back/forward cache
  // never fires unload at all and would sit there holding the device.
  function releaseDevices() {
    try { speech.releaseMic(); } catch (_) {}
  }
  window.addEventListener('pagehide', releaseDevices);

  window.addEventListener('beforeunload', (event) => {
    if (quitting) return;
    event.preventDefault();
    event.returnValue = '';
    return '';
  });

  function powerDown() {
    if (quitting) return;
    hud.confirm('POWER DOWN J.A.R.V.I.S.', () => {
      quitting = true;
      hud.speak('Powering down. Good day, Sir.');
      bus.send({ type: 'quit' });
      // Let the last line finish, then let go of the microphone before the
      // window goes — the indicator should be out by the time it closes.
      setTimeout(releaseDevices, 1400);
      setTimeout(() => window.close(), 1600);
    });
  }

  bus.on('power_down', () => {
    quitting = true;
    setTimeout(releaseDevices, 1400);
  });

  document.getElementById('btn-power').onclick = powerDown;

  bus.connect();
})();
