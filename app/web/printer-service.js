/* «Сервис» (admins): calibrations and a firmware restart of a Creality with Klipper's API
   (K2 Plus), from the card's ⋯ menu. The server (app/services/klipper_service.py) runs one
   macro of the printer's own config through Moonraker and checks the printer again; each
   button asks on itself first, the second press sends. The printer's last console lines
   show while it runs. */
(function () {
  'use strict';
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // The buttons: klipper_service.ACTIONS as /service sends them ({key, title, about}), one list.
  const FREE = ['idle', 'finished', 'cancelled'];
  const STATE = {running: 'идёт', done: 'готово', failed: 'не вышло'};
  // opened: which open() this is — an answer for the printer shown before is dropped;
  // fault: why /service did not answer while there is no answer yet
  let dialog, api, printer, base, armed = null, armTimer = null, pollTimer = null, line = null, sending = false, last = null,
    opened = 0, fault = '';

  function close() { if (dialog && dialog.open) dialog.close(); }
  async function call(url, opts) {
    const r = await api(url, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) throw new Error((body && body.detail) || ('Ошибка ' + r.status));
    return body;
  }
  function blocked() {
    if (!printer.online || printer.state === 'offline') return 'Принтер не на связи';
    if (FREE.indexOf(printer.state) < 0) return 'Сервис — только когда принтер свободен: не печатает, не на паузе, без ошибки';
    return '';
  }
  function jobHtml() {
    const job = last && last.job;
    if (!job) return '';
    const lines = (last.lines || []).map(t => '<li>' + esc(t) + '</li>').join('');
    return '<div class="ps-job ps-' + esc(job.state) + '" role="status"><strong>' + esc(job.title) + ' — ' + esc(STATE[job.state] || job.state) + '</strong>' +
      (job.state !== 'running' && job.detail ? '<span>' + esc(job.detail) + '</span>' : '') +
      (lines ? '<ul class="ps-lines">' + lines + '</ul>' : '') + '</div>';
  }
  function render() {
    const why = blocked(), busy = !!(last && last.job && last.job.state === 'running');
    const rows = ((last && last.actions) || []).map(a => {
      const on = armed === a.key;
      return '<li><button type="button" class="ps-btn' + (on ? ' armed' : '') + '" data-ps="' + esc(a.key) + '"' + (why || busy || sending ? ' disabled' : '') + '>' +
        '<span class="ps-name">' + esc(on ? 'Запустить «' + a.title + '»? Нажмите ещё раз' : a.title) + '</span>' +
        '<span class="ps-sub">' + esc(a.about) + '</span></button></li>';
    }).join('');
    dialog.innerHTML = '<div class="preview-panel pf-panel"><header class="preview-header"><span>Сервис</span>' +
      '<span class="preview-printer">' + esc(printer.label || printer.id) + '</span>' +
      '<button type="button" data-ps-close aria-label="Закрыть" autofocus>×</button></header><div class="pf-body">' +
      (why ? '<p class="pf-error" role="alert">' + esc(why) + '</p>' : '') + jobHtml() +
      (line ? '<p class="' + (line.ok ? 'pf-ok' : 'pf-error') + '" role="status">' + esc(line.text) + '</p>' : '') +
      (last ? '' : fault ? '<p class="pf-error" role="alert">' + esc(fault) + '</p>' :
        '<p class="pf-empty" role="status">Спрашиваю принтер…</p>') +
      (rows ? '<ul class="ps-list">' + rows + '</ul>' : '') +
      '<p class="pf-hint">Здесь нет ручной калибровки стола, калибровки оси Z, печати калибровки и ножа: они идут с экрана принтера.</p>' +
      '</div></div>';
  }
  async function refresh() {
    clearTimeout(pollTimer);
    if (!dialog || !dialog.open) return;
    const run = opened;
    try { const r = await call(base + '/service'); if (run === opened) { last = r; fault = ''; } }
    catch (e) { if (run === opened) fault = e.message; }   // the last answer stays; none yet: the reason shows
    if (run !== opened || !dialog.open) return;            // the dialog shows another printer now
    if (!armed) render();
    if (last && last.job && last.job.state === 'running') pollTimer = setTimeout(refresh, 3000);
  }
  function disarm() { clearTimeout(armTimer); armTimer = null; if (armed) { armed = null; render(); } }
  async function press(action) {
    if (sending) return;
    if (armed !== action) {             // the first press only asks, on the button itself
      armed = action; line = null; render();
      clearTimeout(armTimer); armTimer = setTimeout(disarm, 5000);
      return;
    }
    clearTimeout(armTimer); armed = null; sending = true; render();
    const run = opened;
    let answer;
    try {
      const r = await call(base + '/command', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: action})});
      answer = {ok: true, text: r.detail || 'Отправлено'};
    } catch (e) {
      answer = {ok: false, text: e.message};
    }
    if (run !== opened) return;          // the answer of the printer shown before: not this one's line
    line = answer;
    sending = false;
    await refresh();
    render();
  }
  function onClick(e) {
    const t = e.target;
    if (t === dialog || t.closest('[data-ps-close]')) { close(); return; }
    const b = t.closest('[data-ps]');
    if (b && !b.disabled) press(b.dataset.ps);
  }
  function open(p, fetcher) {
    if (!p || p.kind !== 'creality' || p.klipper_api !== true) return;
    api = fetcher || ((u, o) => fetch(u, o));
    printer = p; base = '/api/admin/printers/' + encodeURIComponent(p.id); armed = null; line = null; last = null; sending = false;
    opened++; fault = ''; clearTimeout(pollTimer);
    if (!dialog) {
      dialog = document.createElement('dialog'); dialog.className = 'preview-dialog pf-dialog';
      dialog.setAttribute('aria-label', 'Сервис принтера');
      document.body.append(dialog);
      dialog.addEventListener('click', onClick);
      dialog.addEventListener('close', () => { clearTimeout(pollTimer); clearTimeout(armTimer); if (window.ForgeSheet) window.ForgeSheet.closed(close); });
    }
    render();
    if (!dialog.open) { dialog.showModal(); if (window.ForgeSheet) window.ForgeSheet.opened(close); }
    refresh();
  }
  window.ForgeService = {open: open};
})();
