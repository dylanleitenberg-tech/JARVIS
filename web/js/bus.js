/* Websocket link to the J.A.R.V.I.S. process.
   Reconnects on its own; the HUD keeps rendering while the link is down. */
(function (global) {
  'use strict';

  class Bus {
    constructor() {
      this.ws = null;
      this.handlers = new Map();
      this.connected = false;
      this.queue = [];
      this.backoff = 400;
    }

    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      this.ws = ws;

      ws.onopen = () => {
        this.connected = true;
        this.backoff = 400;
        this.emit('link', { type: 'link', up: true });
        while (this.queue.length) ws.send(this.queue.shift());
      };

      ws.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch (_) { return; }
        this.emit(data.type, data);
        this.emit('*', data);
      };

      ws.onclose = () => {
        this.connected = false;
        this.emit('link', { type: 'link', up: false });
        setTimeout(() => this.connect(), this.backoff);
        this.backoff = Math.min(this.backoff * 1.7, 8000);
      };

      ws.onerror = () => ws.close();
    }

    on(type, fn) {
      if (!this.handlers.has(type)) this.handlers.set(type, []);
      this.handlers.get(type).push(fn);
      return fn;
    }

    emit(type, payload) {
      const list = this.handlers.get(type);
      if (!list) return;
      for (const fn of list) {
        try {
          fn(payload);
        } catch (err) {
          console.error('[bus]', type, err && (err.stack || err.message || err));
        }
      }
    }

    send(obj) {
      const text = JSON.stringify(obj);
      if (this.connected && this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(text);
      } else if (this.queue.length < 40) {
        this.queue.push(text);
      }
    }
  }

  global.bus = new Bus();
})(window);
