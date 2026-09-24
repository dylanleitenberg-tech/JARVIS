/* ===========================================================================
   Voice.

   Recognition and synthesis both live in the page, which solves the problem
   that sinks most assistants: J.A.R.V.I.S. transcribing his own voice. Because
   one object owns both, recognition is suspended for the exact span of every
   spoken reply and resumed after it.

   Chrome's SpeechRecognition also stops on its own every minute or so; `_arm`
   restarts it, rate-limited so a permission failure cannot become a hot loop.
   =========================================================================== */
(function (global) {
  'use strict';

  const SR = global.SpeechRecognition || global.webkitSpeechRecognition;

  class Speech {
    constructor(opts) {
      this.cfg = Object.assign({
        wake_words: ['jarvis'],
        sleep_words: ['that will be all'],
        always_listening: true,
        command_timeout: 6,
        voice_hint: 'Daniel',
        rate: 1.02, pitch: 0.85, volume: 1.0,
        locale: 'en-US',
      }, opts || {});

      this.onCommand = () => {};
      this.onHeard = () => {};
      this.onState = () => {};
      this.onLevel = () => {};
      this.onError = () => {};

      this.supported = !!SR;
      this.listening = false;   // recognition is running
      this.awake = false;       // a wake word has been heard recently
      this.addressed = false;   // the utterance in progress began while awake
      this.speaking = false;
      this.wantListening = false;
      this.lastStart = 0;
      this.wakeTimer = null;
      this.recognition = null;
      this.voice = null;

      this._pickVoice();
      if (global.speechSynthesis) {
        global.speechSynthesis.onvoiceschanged = () => this._pickVoice();
      }
    }

    /* ------------------------------------------------------------- voice */

    /* The voice is most of the character, and the difference between the
       voices on a stock Mac is large. Ranked rather than found by first
       match, because "the first en-GB voice" is how you end up with a novelty
       voice that happens to sort early, and because the good ones are not
       installed by default — a name-based search silently settles for Daniel
       forever once it finds him.

       Order: an explicit voice_hint always wins. Then Google's network voices,
       which are markedly better than anything local. Then a "premium" or
       "enhanced" British male, which is what you get after downloading them
       in System Settings > Accessibility > Spoken Content > Manage Voices.
       Then plain Daniel, then any British voice, then any English one. */
    _score(v) {
      const name = v.name.toLowerCase();
      const hint = (this.cfg.voice_hint || '').toLowerCase().trim();
      if (hint && name === hint) return 1000;
      if (hint && name.includes(hint)) return 900;

      // Novelty voices — Bells, Bubbles, Bad News, Jester and the rest — are
      // installed on every Mac and must never be chosen by accident.
      if (/bells|bubbles|boing|jester|wobble|organ|cellos|bahh|good news|bad news|superstar|albert|junior|grandma|grandpa|trinoids|whisper|zarvox/.test(name)) return -100;

      let score = 0;
      if (/^google/.test(name)) score += 400;             // network, best quality
      if (/premium|enhanced|siri/.test(name)) score += 300;
      if (v.lang === 'en-GB') score += 120;
      else if (v.lang && v.lang.startsWith('en')) score += 40;
      if (/daniel|arthur|oliver|malcolm|jamie|male/.test(name)) score += 60;
      if (v.localService === false) score += 20;          // network voices
      return score;
    }

    _pickVoice() {
      if (!global.speechSynthesis) return;
      const voices = global.speechSynthesis.getVoices();
      if (!voices.length) return;
      const ranked = voices
        .map((v) => [this._score(v), v])
        .sort((a, b) => b[0] - a[0]);
      this.voice = ranked[0][1];
      this.voiceRanking = ranked.slice(0, 5)
        .map(([s, v]) => `${v.name} (${v.lang}) ${s}`);
    }

    /* Speak the same line in each of the best candidates, announcing each by
       name. Choosing a voice from a written list is guesswork — the names say
       nothing about how they sound — and I cannot hear them on your behalf.
       This is the only way to settle it: play them, pick one. */
    async audition(sample, limit = 6) {
      if (!global.speechSynthesis) return [];
      const ranked = global.speechSynthesis.getVoices()
        .map((v) => [this._score(v), v])
        .filter(([s]) => s > 0)
        .sort((a, b) => b[0] - a[0])
        .slice(0, limit)
        .map(([, v]) => v);

      const held = this.voice;
      const names = [];
      this.auditioning = true;
      try {
        for (const v of ranked) {
          if (!this.auditioning) break;        // interrupted: stop cleanly
          names.push(v.name);
          this.voice = v;
          this.heard = v;                      // what "keep this one" means
          await this.say(`${v.name}.`);
          if (!this.auditioning) break;
          await this.say(sample || 'Good evening, Sir. All systems are nominal.');
        }
      } finally {
        // Always put the old voice back. Without this, stopping the audition
        // partway left whichever voice happened to be playing in place —
        // unsaved, so it vanished at the next restart and looked like the
        // choice had simply been ignored.
        this.voice = held;
        this.auditioning = false;
      }
      return names;
    }

    stopAudition() { this.auditioning = false; }

    /* The last voice heard during an audition, for "keep that one" — which is
       how anyone actually chooses, rather than by recalling the name. */
    lastHeard() { return this.heard || null; }

    /* Keep one. Returns the voice actually chosen, which may not be the one
       asked for if the name was misheard — so the caller can say which. */
    useVoice(name) {
      if (!global.speechSynthesis) return null;
      this.stopAudition();
      const want = String(name || '').toLowerCase().trim();
      // "keep this one", said while listening to it — no name needed, and no
      // name remembered either.
      if (!want || /^(this|that|it|this one|that one|the last one)$/.test(want)) {
        const heard = this.lastHeard();
        if (heard) { this.voice = heard; this.cfg.voice_hint = heard.name; }
        return heard;
      }
      const voices = global.speechSynthesis.getVoices();
      const found = voices.find((v) => v.name.toLowerCase() === want)
        || voices.find((v) => v.name.toLowerCase().includes(want))
        || voices.find((v) => want.includes(v.name.toLowerCase()));
      if (found) {
        this.voice = found;
        this.cfg.voice_hint = found.name;
      }
      return found || null;
    }

    listVoices() {
      return (global.speechSynthesis ? global.speechSynthesis.getVoices() : [])
        .map((v) => `${v.name} (${v.lang})`);
    }

    /* ------------------------------------------------------- recognition */

    async start() {
      this.wantListening = true;
      await this._startMeter();
      this._arm();
    }

    stop() {
      this.wantListening = false;
      if (this.recognition) {
        try { this.recognition.stop(); } catch (_) {}
      }
      this.listening = false;
      this.onState('standby');
    }

    toggle() { this.wantListening ? this.stop() : this.start(); }

    _arm() {
      if (!this.supported || !this.wantListening || this.speaking) return;
      const now = Date.now();
      if (now - this.lastStart < 320) {          // never hot-loop on an error
        setTimeout(() => this._arm(), 400);
        return;
      }
      this.lastStart = now;

      const rec = new SR();
      this.addressed = false;       // an unfinished utterance ends with its session
      rec.lang = this.cfg.locale;
      rec.continuous = true;
      rec.interimResults = true;
      rec.maxAlternatives = 3;

      rec.onstart = () => {
        this.listening = true;
        this.onState(this.awake ? 'listening' : 'standby');
      };

      rec.onresult = (event) => {
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i];
          const text = result[0].transcript.trim();
          if (!text) continue;
          if (result.isFinal) {
            const alts = Array.from(result).map((a) => a.transcript.trim());
            const addressed = this.addressed;
            this.addressed = false;
            this._handle(text, alts, addressed);
          } else {
            this.onHeard(text, false);
            // Waking on an interim result buys ~300 ms of perceived latency.
            if (!this.awake && this._findWake(text) !== null) this._wake();
            // Whether he was talking to J.A.R.V.I.S. is decided when the words
            // start, not when Chrome finishes them. "Jarvis", a pause, then a
            // long command arrives as two finals, and the second one lands
            // after the wake window has closed: replaying his recorded voice,
            // "Jarvis ... make the rear bell nozzle a cone instead of a set of
            // tubings" finalised 11 s after the wake word and was dropped here,
            // before the server could log it.
            if (this.awake) this.addressed = true;
          }
        }
      };

      rec.onerror = (event) => {
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
          this.wantListening = false;
          this.onError('the microphone was refused — press SETUP to allow it');
          this.onState('denied');
        } else if (event.error !== 'no-speech' && event.error !== 'aborted') {
          this.onError(event.error);
        }
      };

      rec.onend = () => {
        this.listening = false;
        if (this.wantListening && !this.speaking) setTimeout(() => this._arm(), 250);
        else this.onState(this.speaking ? 'speaking' : 'standby');
      };

      this.recognition = rec;
      try { rec.start(); } catch (_) { /* already running */ }
    }

    /* Returns the text after the wake word, '' if the utterance was only the
       wake word, or null if no wake word is present. */
    _findWake(text) {
      const lower = text.toLowerCase();
      for (const word of this.cfg.wake_words) {
        const at = lower.indexOf(word.toLowerCase());
        if (at === -1) continue;
        return text.slice(at + word.length).replace(/^[\s,.:;!?-]+/, '').trim();
      }
      return null;
    }

    _wake() {
      this.awake = true;
      this.onState('listening');
      this._resetWakeTimer();
    }

    _resetWakeTimer() {
      clearTimeout(this.wakeTimer);
      this.wakeTimer = setTimeout(() => {
        this.awake = false;
        this.onState('standby');
      }, this.cfg.command_timeout * 1000);
    }

    _handle(text, alts, addressed) {
      this.onHeard(text, true);

      const lower = text.toLowerCase();
      if (this.cfg.sleep_words.some((w) => lower.includes(w.toLowerCase()))) {
        this.awake = false;
        clearTimeout(this.wakeTimer);
        this.onState('standby');
        this.onCommand('that will be all', { sleep: true });
        return;
      }

      const after = this._findWake(text);
      if (after !== null) {
        this._wake();
        if (after) {                    // "Jarvis, open Safari" — one utterance
          this.onCommand(after, { alts });
          this._resetWakeTimer();
        }
        return;
      }

      if (this.awake || addressed) {
        this.onCommand(text, { alts });
        this._wake();
      }
    }

    /* ---------------------------------------------------------- synthesis */

    say(text) {
      if (!text || !global.speechSynthesis) return Promise.resolve();
      return new Promise((resolve) => {
        // Hold recognition down for the whole utterance, or he answers himself.
        const wasListening = this.wantListening;
        this.speaking = true;
        if (this.recognition) { try { this.recognition.abort(); } catch (_) {} }
        this.onState('speaking');

        const utter = new SpeechSynthesisUtterance(text);
        if (this.voice) utter.voice = this.voice;
        utter.rate = this.cfg.rate;
        utter.pitch = this.cfg.pitch;
        utter.volume = this.cfg.volume;
        utter.lang = this.voice ? this.voice.lang : 'en-GB';

        // A question is waiting on an answer, and the answer comes without his
        // name on it. The wake window was opened by his command, so by the time
        // the model has thought and the question has been spoken it has
        // usually closed: open a fresh one when he can actually reply.
        const asked = /\?["')\s]*$/.test(text);

        let envelope = null;
        const finish = () => {
          clearInterval(envelope);
          this.onLevel(0);
          this.speaking = false;
          if (asked) { this.awake = true; this._resetWakeTimer(); }
          this.onState(this.awake ? 'listening' : 'standby');
          if (wasListening) setTimeout(() => this._arm(), 160);
          resolve();
        };

        utter.onstart = () => {
          // speechSynthesis output cannot be analysed, so the waveform is
          // driven by a plausible envelope for the duration of the utterance.
          let phase = 0;
          envelope = setInterval(() => {
            phase += 0.35;
            const value = 0.34 + 0.3 * Math.abs(Math.sin(phase))
                               + 0.18 * Math.abs(Math.sin(phase * 2.7))
                               + Math.random() * 0.1;
            this.onLevel(Math.min(1, value));
          }, 45);
        };
        utter.onend = finish;
        utter.onerror = finish;

        global.speechSynthesis.cancel();
        global.speechSynthesis.speak(utter);
      });
    }

    shutUp() {
      if (global.speechSynthesis) global.speechSynthesis.cancel();
    }

    /* Hand the microphone back. Called on power-down and on unload, because
       neither recognition nor the meter releases the device on its own: the
       indicator stays lit, and the honest reading of that is that something
       is still listening. */
    releaseMic() {
      this.wantListening = false;
      if (this.recognition) {
        try { this.recognition.abort(); } catch (_) {}
        try { this.recognition.stop(); } catch (_) {}
        this.recognition = null;
      }
      if (this.micStream) {
        for (const track of this.micStream.getTracks()) {
          try { track.stop(); } catch (_) {}
        }
        this.micStream = null;
      }
      if (this.audioCtx) {
        try { this.audioCtx.close(); } catch (_) {}
        this.audioCtx = null;
      }
      this.analyser = null;
      this.micOk = false;
      this.shutUp();
    }

    /* --------------------------------------------------------- mic meter */

    async _startMeter() {
      if (this.analyser) return;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        const ctx = new (global.AudioContext || global.webkitAudioContext)();
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.72;
        source.connect(analyser);
        this.audioCtx = ctx;
        this.analyser = analyser;
        // Held so it can be given back. A MediaStream is only released when
        // its tracks are stopped — closing the AudioContext does not do it,
        // and neither does dropping the reference, so without this the
        // microphone stays live for as long as the page does.
        this.micStream = stream;
        this.buffer = new Uint8Array(analyser.frequencyBinCount);
        this.micOk = true;
        this._meterLoop();
      } catch (err) {
        this.micOk = false;
        // The error NAME is the diagnosis, and the two cases need different
        // fixes: NotAllowedError with "system" in the message means macOS is
        // refusing Chrome outright, and no amount of in-page permission helps.
        this.micError = { name: err.name || 'Error', message: err.message || '' };
        const systemLevel = /system|denied by system/i.test(this.micError.message);
        this.onError(`${this.micError.name}: ${this.micError.message}`,
                     systemLevel ? 'macos-denied' : 'blocked');
      }
    }

    _meterLoop() {
      const tick = () => {
        if (this.analyser && !this.speaking) {
          this.analyser.getByteFrequencyData(this.buffer);
          let sum = 0;
          for (let i = 0; i < this.buffer.length; i++) sum += this.buffer[i] * this.buffer[i];
          const rms = Math.sqrt(sum / this.buffer.length) / 255;
          this.onLevel(Math.min(1, rms * 3.4));
          this.spectrum = this.buffer;
        }
        requestAnimationFrame(tick);
      };
      tick();
    }
  }

  global.Speech = Speech;
})(window);
