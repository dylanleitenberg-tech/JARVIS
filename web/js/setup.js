/* ===========================================================================
   Setup: what the machine has to allow, and a button for each.

   The server knows the state of every permission on this operating system
   (jarvis/permissions.py); this draws it and forwards the buttons. It opens on
   load until the required ones are granted, re-reads the state every second
   and a half while open — a grant made in System Settings shows up here
   without anyone pressing refresh — and stays out of the way after that.
   =========================================================================== */
(function (global) {
  'use strict';

  const BUTTON = {
    ask_os: 'ASK', open_settings: 'OPEN SETTINGS', restart: 'RESTART',
    get_browser: 'GET CHROME', pull_model: 'DOWNLOAD', get_ollama: 'GET OLLAMA',
    ask_page: 'ALLOW', add_folder: 'ADD',
  };
  const STATE = {
    ok: 'READY', waiting: 'WAITING', needed: 'NEEDED', limited: 'LIMITED', optional: 'OPTIONAL',
  };

  class Setup {
    constructor() {
      this.el = document.getElementById('setup');
      this.list = document.getElementById('setup-list');
      this.note = document.getElementById('setup-note');
      this.done = document.getElementById('setup-continue');
      this.timer = null;
      this.busy = {};
      this.onAskMic = () => {};          // the page asks for the microphone itself
      this.done.onclick = () => this.close(true);
    }

    get open() { return !this.el.hidden; }

    async boot() {
      const data = await this.refresh();
      if (data && (!data.seen || !data.ready)) this.show();
    }

    show() {
      this.el.hidden = false;
      this.refresh();
      clearInterval(this.timer);
      this.timer = setInterval(() => this.refresh(), 1500);
    }

    close(seen) {
      this.el.hidden = true;
      clearInterval(this.timer);
      if (seen) this._post({ seen: true });
    }

    toggle() { this.open ? this.close(false) : this.show(); }

    async refresh() {
      try {
        const res = await fetch('/api/permissions', { cache: 'no-store' });
        const data = await res.json();
        this.render(data);
        return data;
      } catch (err) {
        return null;
      }
    }

    render(data) {
      // Rebuilt only when something changed, so a half-typed folder path in
      // the input survives the poll.
      const key = JSON.stringify(data.rows) + JSON.stringify(this.busy);
      if (key === this._last) return;
      this._last = key;
      const focused = document.activeElement && document.activeElement.id === 'setup-folder'
        ? document.activeElement.value : null;

      this.list.textContent = '';
      for (const row of data.rows) this.list.appendChild(this._row(row));
      if (focused !== null) {
        const input = document.getElementById('setup-folder');
        if (input) { input.value = focused; input.focus(); }
      }
      this.done.textContent = data.ready ? 'CONTINUE' : 'CONTINUE ANYWAY';
      this.done.classList.toggle('danger', !data.ready);
    }

    _row(row) {
      const li = document.createElement('li');
      li.className = `setup-row ${row.state}`;

      const state = document.createElement('span');
      state.className = 'setup-state';
      state.textContent = STATE[row.state] || row.state.toUpperCase();

      const text = document.createElement('div');
      text.className = 'setup-text';
      const label = document.createElement('b');
      label.textContent = row.label;
      const why = document.createElement('small');
      why.textContent = row.why;
      const detail = document.createElement('em');
      detail.textContent = this.busy[row.id] || row.detail;
      text.append(label, why, detail);

      li.append(state, text);

      if (row.action === 'add_folder') {
        const form = document.createElement('form');
        form.className = 'setup-folder';
        const input = document.createElement('input');
        input.id = 'setup-folder';
        input.placeholder = 'folder path, e.g. ~/CAD';
        input.spellcheck = false;
        input.autocomplete = 'off';
        const add = document.createElement('button');
        add.className = 'ghost';
        add.textContent = BUTTON.add_folder;
        form.append(input, add);
        form.onsubmit = (e) => {
          e.preventDefault();
          if (input.value.trim()) this._act(row, input.value.trim());
          input.value = '';
        };
        li.appendChild(form);
      } else if (row.action && row.state !== 'ok') {
        const btn = document.createElement('button');
        btn.className = 'ghost';
        btn.textContent = BUTTON[row.action] || 'FIX';
        btn.onclick = () => this._act(row);
        li.appendChild(btn);
      }
      return li;
    }

    async _act(row, arg) {
      if (row.action === 'ask_page') { this.onAskMic(); return; }
      this.busy[row.id] = 'asking…';
      this._last = null;
      const out = await this._post({ id: row.id, arg: arg || '' });
      this.busy[row.id] = out && out.said ? out.said : '';
      this._last = null;
      this.note.textContent = out && out.said ? out.said : '';
      setTimeout(() => { delete this.busy[row.id]; this._last = null; this.refresh(); }, 6000);
      this.refresh();
    }

    async _post(body) {
      try {
        const res = await fetch('/api/permissions', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        return await res.json();
      } catch (err) {
        return { said: 'could not reach J.A.R.V.I.S.' };
      }
    }
  }

  global.Setup = Setup;
})(window);
