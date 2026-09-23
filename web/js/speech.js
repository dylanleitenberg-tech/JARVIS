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

    _pickVoice() {
      if (!global.speechSynthesis) return;
      const voices = global.speechSynthesis.getVoices();
      if (!voices.length) return;
      const hint = (this.cfg.voice_hint || '').toLowerCase();
      this.voice =
        voices.find((v) => v.name.toLowerCase() === hint) ||
        voices.find((v) => v.name.toLowerCase().includes(hint)) ||
        // A British male is the closest match to the character.
        voices.find((v) => v.lang === 'en-GB' && /daniel|arthur|oliver|male/i.test(v.name)) ||
        voices.find((v) => v.lang === 'en-GB') ||
        voices.find((v) => v.lang.startsWith('en')) ||
        voices[0];
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
            this._handle(text, alts);
          } else {
            this.onHeard(text, false);
            // Waking on an interim result buys ~300 ms of perceived latency.
            if (!this.awake && this._findWake(text) !== null) this._wake();
          }
        }
      };

      rec.onerror = (event) => {
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
          this.wantListening = false;
          this.onError('microphone permission denied — click the mic icon in the address bar');
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

    _handle(text, alts) {
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

      if (this.awake) {
        this.onCommand(text, { alts });
        this._resetWakeTimer();
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

        let envelope = null;
        const finish = () => {
          clearInterval(envelope);
          this.onLevel(0);
          this.speaking = false;
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
