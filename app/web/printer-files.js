/* «Файлы на принтере» (admins): the print files stored on a printer and starting one.
   The server (app/services/printer_files.py) lists, reads a Bambu file's plates and
   checks every start again; «Печать» asks on the button, the second press starts. */
(function () {
  'use strict';
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const valid = key => /^[a-f0-9]{64}$/.test(key || '');
  const previewUrl = key => '/api/job-previews/' + key + '.png';
  const hex = c => { const m = /^#?([0-9A-F]{6})/i.exec(c || ''); return m ? '#' + m[1] : ''; };
  // the interface's language (app/web/i18n.js); Russian where there is none (node tests)
  const locale = () => window.I18N ? window.I18N.locale() : 'ru-RU';
  const num = (v, d) => Number(v).toLocaleString(locale(), {maximumFractionDigits: d == null ? 1 : d});
  // whole minutes first: 1 h 59 min 50 s is «2 ч 0 мин», never «1 ч 60 мин»
  const duration = s => { const t = Math.round(s / 60), h = Math.floor(t / 60), m = t % 60; return (h ? h + ' ч ' : '') + m + ' мин'; };
  const size = b => b >= 1048576 ? num(b / 1048576) + ' МБ' : num(Math.max(1, Math.round(b / 1024)), 0) + ' КБ';
  const date = t => new Date(t * 1000).toLocaleString(locale(), {day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit'});
  const EXTERNAL = 254;
  // listRun: the list asked for last — a late answer of the printer opened before is dropped;
  // warning: the server's word on that list (the USB stick unread, the internal files shown)
  let dialog, api, printer, base, files = [], view = null, armTimer = null, thumbRun = 0, listRun = 0, warning = '';

  function close() { if (dialog && dialog.open) dialog.close(); }
  async function call(url, opts) {
    const r = await api(url, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) throw new Error((body && body.detail) || ('Ошибка ' + r.status));
    return body;
  }
  function shell(title, body) {
    dialog.innerHTML = '<div class="preview-panel pf-panel"><header class="preview-header">' +
      (view ? '<button type="button" class="pf-back" data-pf-back aria-label="К списку файлов">‹</button>' : '') +
      '<span>' + esc(title) + '</span><span class="preview-printer">' + esc(printer.label || printer.id) + '</span>' +
      '<button type="button" data-pf-close aria-label="Закрыть" autofocus>×</button></header>' + body + '</div>';
  }
  function meta(f) {
    return [f.time_s ? duration(f.time_s) : '', f.grams ? num(f.grams) + ' г' : '', f.size ? size(f.size) : '',
            f.mtime ? date(f.mtime) : ''].filter(Boolean).join(' · ');
  }
  function thumbHtml(f) {
    if (f.thumbKey) return '<img src="' + previewUrl(f.thumbKey) + '" alt="" loading="lazy">';
    if (f.thumb) return '<img src="' + base + '/files/thumb?path=' + encodeURIComponent(f.path) + '" alt="" loading="lazy">';
    return '';
  }
  function renderList(note) {
    view = null;
    const rows = files.map((f, i) => '<li><button type="button" class="pf-row" data-pf-file="' + i + '">' +
      '<span class="pf-thumb" data-pf-thumb="' + i + '">' + thumbHtml(f) + '</span>' +
      '<span class="pf-copy"><span class="pf-name">' + esc(f.title || f.name) + (f.demo ? ' <small class="pf-demo">образец</small>' : '') + '</span>' +
      '<span class="pf-meta">' + esc(meta(f)) + '</span></span><span class="pf-go" aria-hidden="true">›</span></button></li>').join('');
    shell('Файлы на принтере', '<div class="pf-body">' + (warning ? '<p class="pf-note" role="status">' + esc(warning) + '</p>' : '') +
      (note ? '<p class="pf-note" role="status">' + esc(note) + '</p>' : '') +
      (files.length ? '<ul class="pf-list">' + rows + '</ul>' : '<p class="pf-empty">На принтере нет файлов для печати</p>') + '</div>');
  }
  // Bambu thumbnails come from each file's own plate picture: read one file at a time,
  // the newest first; two failures in a row (P2S keeps its files to itself) stop it.
  async function fillBambuThumbs(run) {
    let failures = 0;
    for (let i = 0; i < files.length && i < 24; i++) {
      const f = files[i];
      if (run !== thumbRun || !dialog.open || failures >= 2) return;
      if (f.demo || f.thumbKey || f.unreadable) continue;
      try {
        const d = await call(base + '/files/detail?path=' + encodeURIComponent(f.path));
        f.detail = d; failures = 0;
        const p = (d.plates || [])[0];
        if (p) {
          f.thumbKey = valid(p.thumb) ? p.thumb : null;
          if (!f.time_s) f.time_s = (d.plates || []).reduce((s, x) => s + (x.time_s || 0), 0) || null;
          if (!f.grams) f.grams = (d.plates || []).reduce((s, x) => s + (x.grams || 0), 0) || null;
        }
      } catch (e) { f.unreadable = e.message; failures++; }
      if (run !== thumbRun || view) continue;
      const cell = dialog.querySelector('[data-pf-thumb="' + i + '"]');
      if (cell) {
        cell.innerHTML = thumbHtml(f);
        const m = cell.parentNode.querySelector('.pf-meta');
        if (m) m.textContent = meta(f);
      }
    }
  }
  async function load() {
    const run = ++listRun;
    shell('Файлы на принтере', '<div class="pf-body"><p class="pf-empty" role="status">Получаю список файлов с принтера…</p></div>');
    try {
      const r = await call(base + '/files');
      if (run !== listRun) return;      // the dialog shows another printer (or a newer list) now
      files = r.files || [];
      warning = typeof r.warning === 'string' ? r.warning : '';
      renderList();
      if (r.kind === 'bambu') fillBambuThumbs(++thumbRun);
    } catch (e) {
      if (run !== listRun) return;
      shell('Файлы на принтере', '<div class="pf-body"><p class="pf-error" role="alert">' + esc(e.message) + '</p>' +
        '<button type="button" class="pf-secondary" data-pf-reload>Повторить</button></div>');
    }
  }
  function slotLabel(s) {
    return s.slot === EXTERNAL ? 'Внешняя катушка' : 'AMS ' + s.name + (s.empty ? ' — пусто' : ' — ' + (s.type || '?'));
  }
  function filamentRows(plate, slots, mapping) {
    const choices = slots.filter(s => !s.empty).concat([{slot: EXTERNAL}]);
    return (plate.filaments || []).map((f, i) => '<li class="pf-fil"><span class="pj-swatch"' + (hex(f.color) ? ' style="background:' + hex(f.color) + '"' : '') + '></span>' +
      '<span class="pf-fil-name"><strong>' + esc(f.type || 'Пластик') + '</strong>' + (f.grams ? '<small>' + num(f.grams) + ' г</small>' : '') + '</span>' +
      '<select data-pf-map="' + i + '" aria-label="Слот для пластика ' + (i + 1) + '">' +
      choices.map(s => '<option value="' + s.slot + '"' + (s.slot === mapping[i] ? ' selected' : '') + '>' + esc(slotLabel(s)) + '</option>').join('') +
      '</select>' + (choices.some(s => s.slot !== EXTERNAL && s.color) ? '' : '') + '</li>').join('');
  }
  function renderDetail() {
    const f = view.file, d = view.detail || {}, plates = d.plates || [];
    const plate = plates.find(p => p.index === view.plate) || plates[0];
    const kind = view.kind;
    let pic = '', body = '';
    if (kind === 'bambu' && plate) {
      pic = valid(plate.thumb) ? previewUrl(plate.thumb) : '';
      const tabs = plates.length > 1 ? '<div class="pf-plates" role="group" aria-label="Стол">' + plates.map(p =>
        '<button type="button" data-pf-plate="' + p.index + '" class="' + (p.index === plate.index ? 'on' : '') + '">Стол ' + p.index + '</button>').join('') + '</div>' : '';
      const facts = [plate.title !== f.title ? plate.title : '', plate.time_s ? duration(plate.time_s) : '', plate.grams ? num(plate.grams) + ' г' : '',
                     plate.nozzle_mm ? 'сопло ' + num(plate.nozzle_mm, 2) + ' мм' : ''].filter(Boolean).join(' · ');
      body = tabs + '<p class="pf-facts">' + esc(facts) + '</p><h3>Пластик → слот</h3><ul class="pf-fils">' +
        filamentRows(plate, d.slots || [], view.mapping[plate.index]) + '</ul>' +
        '<label class="pf-opt"><input type="checkbox" data-pf-opt="bed_leveling"' + (view.bed_leveling ? ' checked' : '') + '> Калибровка стола</label>' +
        '<label class="pf-opt"><input type="checkbox" data-pf-opt="timelapse"' + (view.timelapse ? ' checked' : '') + '> Таймлапс</label>';
    } else if (kind === 'creality') {
      pic = f.thumb ? base + '/files/thumb?path=' + encodeURIComponent(f.path) : '';
      body = '<p class="pf-facts">' + esc(meta(f)) + '</p>' + ((f.filaments || []).length ? '<h3>Пластик</h3><ul class="pf-fils">' +
        f.filaments.map(x => '<li class="pf-fil"><span class="pj-swatch"' + (hex(x.color) ? ' style="background:' + hex(x.color) + '"' : '') + '></span>' +
          '<span class="pf-fil-name"><strong>' + esc(x.type || 'Пластик') + '</strong>' + (x.grams ? '<small>' + num(x.grams) + ' г</small>' : '') + '</span></li>').join('') + '</ul>' : '') +
        (/k2/i.test(printer.device_type || printer.model || '') ? '<p class="pf-hint">Слоты CFS принтер подберёт сам, как при запуске с экрана</p>' : '');
    }
    const blocked = view.error || d.refusal || (kind === 'bambu' && !plate ? 'Принтер не отдаёт содержимое этого файла — запустите его с экрана принтера' : '');
    const status = view.result ? '<p class="' + (view.result.ok ? 'pf-ok' : 'pf-error') + '" role="status">' + esc(view.result.text) + '</p>' : '';
    shell('Печать файла', '<div class="pf-stage">' + (pic ? '<img src="' + esc(pic) + '" alt="Превью стола">' : '<span class="pf-nopic">Нет картинки</span>') + '</div>' +
      '<div class="pf-body"><h2 class="pf-title">' + esc(f.title || f.name) + '</h2>' + body +
      (blocked ? '<p class="pf-error" role="alert">' + esc(blocked) + '</p>' : '') + status +
      '<div class="pf-actions"><button type="button" class="pf-print' + (view.armed ? ' armed' : '') + '" data-pf-print' + (blocked || view.busy ? ' disabled' : '') + '>' +
      (view.busy ? 'Отправляю…' : view.armed ? 'Стол чистый? Нажмите ещё раз — печать начнётся' : 'Печать') + '</button>' +
      // a file the printer's Klipper API listed (K2 Plus) can be deleted: the first press asks, the second deletes
      (f.mr ? '<button type="button" class="pf-secondary pf-delete' + (view.delArmed ? ' armed' : '') + '" data-pf-delete' + (view.busy ? ' disabled' : '') + '>' +
        (view.delArmed ? 'Удалить с принтера насовсем? Нажмите ещё раз' : 'Удалить с принтера') + '</button>' : '') + '</div></div>');
  }
  async function remove() {
    if (!view || view.busy || !view.file.mr) return;
    if (!view.delArmed) {
      view.delArmed = true; view.armed = false; view.result = null; renderDetail();
      armTimer = setTimeout(disarm, 6000);
      return;
    }
    clearTimeout(armTimer);
    const f = view.file;
    view.delArmed = false; view.busy = true; renderDetail();
    try {
      await call(base + '/files?path=' + encodeURIComponent(f.path), {method: 'DELETE'});
      files = files.filter(x => x !== f);
      renderList('Файл удалён с принтера');
      return;
    } catch (e) {
      view.result = {ok: false, text: e.message};
    }
    view.busy = false; renderDetail();
  }
  async function openFile(i) {
    const f = files[i];
    if (!f) return;
    thumbRun++;
    view = {file: f, kind: printer.kind, detail: f.detail || null, plate: null, mapping: {}, bed_leveling: true, timelapse: false};
    shell('Печать файла', '<div class="pf-body"><p class="pf-empty" role="status">Читаю файл…</p></div>');
    try {
      const d = await call(base + '/files/detail?path=' + encodeURIComponent(f.path));
      if (view && view.file === f) view.detail = d;
    } catch (e) {
      if (view && view.file === f) view.error = e.message;
    }
    if (!view || view.file !== f) return;
    const plates = (view.detail && view.detail.plates) || [];
    view.plate = plates.length ? plates[0].index : null;
    plates.forEach(p => { view.mapping[p.index] = (p.mapping || []).slice(); });
    renderDetail();
  }
  function disarm() { clearTimeout(armTimer); armTimer = null; if (view && (view.armed || view.delArmed)) { view.armed = view.delArmed = false; renderDetail(); } }
  async function print() {
    if (!view || view.busy) return;
    if (!view.armed) {             // the first press only asks, on the button itself
      view.armed = true; view.delArmed = false; view.result = null; renderDetail();
      armTimer = setTimeout(disarm, 6000);
      return;
    }
    clearTimeout(armTimer);
    const body = {path: view.file.path};
    if (view.kind === 'bambu') Object.assign(body, {plate: view.plate, mapping: view.mapping[view.plate] || [],
                                                     bed_leveling: !!view.bed_leveling, timelapse: !!view.timelapse});
    view.armed = false; view.busy = true; renderDetail();
    try {
      const r = await call(base + '/print', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      view.result = {ok: true, text: r.status === 'confirmed' ? 'Принтер принял задание' : 'Задание отправлено, ждём принтер'};
    } catch (e) {
      view.result = {ok: false, text: e.message};
    }
    view.busy = false; renderDetail();
  }
  function onClick(e) {
    const t = e.target;
    if (t === dialog || t.closest('[data-pf-close]')) { close(); return; }
    if (t.closest('[data-pf-back]')) { disarm(); renderList(); fillBambuThumbs(++thumbRun); return; }
    if (t.closest('[data-pf-reload]')) { load(); return; }
    const row = t.closest('[data-pf-file]');
    if (row) { openFile(+row.dataset.pfFile); return; }
    const plate = t.closest('[data-pf-plate]');
    if (plate && view) { view.plate = +plate.dataset.pfPlate; view.armed = false; renderDetail(); return; }
    if (t.closest('[data-pf-print]')) print();
    else if (t.closest('[data-pf-delete]')) remove();
  }
  function onChange(e) {
    if (!view) return;
    const t = e.target;
    if (t.matches('[data-pf-map]')) view.mapping[view.plate][+t.dataset.pfMap] = +t.value;
    else if (t.matches('[data-pf-opt]')) view[t.dataset.pfOpt] = t.checked;
    if (view.armed) disarm();
  }
  function open(p, fetcher) {
    if (!p || (p.kind !== 'bambu' && p.kind !== 'creality')) return;
    api = fetcher || ((u, o) => fetch(u, o));
    printer = p; base = '/api/admin/printers/' + encodeURIComponent(p.id); files = []; view = null; warning = '';
    if (!dialog) {
      dialog = document.createElement('dialog'); dialog.className = 'preview-dialog pf-dialog';
      dialog.setAttribute('aria-label', 'Файлы на принтере');
      document.body.append(dialog);
      dialog.addEventListener('click', onClick);
      dialog.addEventListener('change', onChange);
      dialog.addEventListener('close', () => { thumbRun++; clearTimeout(armTimer); if (window.ForgeSheet) window.ForgeSheet.closed(close); });
    }
    if (!dialog.open) { dialog.showModal(); if (window.ForgeSheet) window.ForgeSheet.opened(close); }
    load();
  }
  window.ForgeFiles = {open: open};
})();
