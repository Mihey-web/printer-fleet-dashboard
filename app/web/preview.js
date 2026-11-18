/* Slicer previews: immutable local images, shared by printer cards and job history. */
(function () {
  'use strict';
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const valid = key => /^[a-f0-9]{64}$/.test(key || '');
  const url = key => '/api/job-previews/' + key + '.png';
  function button(data, compact) {
    if (!valid(data.key)) return '';
    const attrs = Object.entries(data).map(([k,v]) => ' data-preview-' + k + '="' + escape(v) + '"').join('');
    return '<button type="button" class="job-preview' + (compact ? ' job-preview-compact' : '') + '"' + attrs + ' aria-label="Превью: ' + escape(data.name || 'Задание') + '">' +
      '<span class="job-preview-thumb"><img src="' + url(data.key) + '" alt="" loading="lazy" decoding="async"></span>' +
      '<span class="job-preview-copy"><span class="job-preview-name">' + escape(data.name || 'Задание') + '</span>' +
      (!compact && data.grams ? '<span class="job-preview-meta">' + escape(data.grams) + '</span>' : '') + '</span>' +
      '<span class="job-preview-open" aria-hidden="true">↗</span></button>';
  }
  window.ForgePreview = {button: button, markup: projectMarkup};   // markup: the card's body (tests)
  // The job card: what a person needs from the slicer project (app/services/project_summary.py).
  const GENERIC = /(^|\/)(Metadata\/plate_\d+\.gcode|project_file(\.3mf)?)$/i;
  // the interface's language (app/web/i18n.js); Russian where there is none (node tests)
  const locale = () => window.I18N ? window.I18N.locale() : 'ru-RU';
  const num = (v, digits) => Number(v).toLocaleString(locale(), {maximumFractionDigits: digits == null ? 1 : digits});
  // whole minutes first: 1 h 59 min 50 s is «2ч 0м», never «1ч 60м»
  const duration = s => { const t = Math.round(s / 60), h = Math.floor(t / 60), m = t % 60; return (h ? h + 'ч ' : '') + m + 'м'; };
  const color = c => /^#[0-9A-F]{6}$/i.test(c || '') ? c : '';
  function fact(label, value, wide) {
    return value == null || value === '' ? '' :
      '<div class="pj-fact' + (wide ? ' pj-wide' : '') + '"><small>' + escape(label) + '</small><span>' + escape(value) + '</span></div>';
  }
  function plural(n, one, few, many) {
    const a = n % 10, b = n % 100;
    return n + ' ' + (a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many);
  }
  // metres in words: «24 м» beside «8ч 23м» read as minutes (QA 17.11, interface 9)
  function meters(v) {
    const r = Math.round(Number(v) * 10) / 10;
    return Number.isInteger(r) ? plural(r, 'метр', 'метра', 'метров') : num(r) + ' метра';
  }
  // A slicer profile without the printer it was made for: «Bambu PETG Basic @BBL X1C» → «Bambu PETG Basic».
  const profileName = value => String(value || '').replace(/\s*@.*$/, '').trim();
  // Each fact once: the total grams stand by «Пластик» (and, in the history, the planned time in the
  // card's head); a single filament's row then has no grams of its own, the nozzle's temperature lives in the
  // filament row, the bed's next to the bed, a part named like the job is not listed again.
  function filamentRow(f, single, nozzleTemp) {
    const profile = profileName(f.profile);
    const name = profile && f.type && profile.toLowerCase().includes(String(f.type).toLowerCase()) ? profile : f.type || profile || 'Пластик';
    const amount = [!single && f.grams ? num(f.grams) + ' г' : '', f.meters ? meters(f.meters) : ''].filter(Boolean).join(' · ');
    const temp = f.nozzle_temp || nozzleTemp;
    return '<li class="pj-filament"><span class="pj-swatch"' + (color(f.color) ? ' style="background:' + color(f.color) + '"' : '') + '></span>' +
      '<span class="pj-filament-name"><strong>' + escape(name) + '</strong>' +
      (profile && profile !== name ? '<small>' + escape(profile) + '</small>' : '') + '</span>' +
      (temp ? '<span class="pj-filament-temp">' + num(temp, 0) + '°</span>' : '') +
      '<span class="pj-filament-amount">' + escape(amount) + '</span></li>';
  }
  function plasticHead(grams, any) {
    return grams || any ? '<h3>Пластик' + (grams ? ' <small>' + escape(grams) + '</small>' : '') + '</h3>' : '';
  }
  // grams: the printer card's text («≈ 352,5 г»), else the slicer's total
  function projectMarkup(p, title, planned, grams) {
    const filaments = p.filaments || [];
    const single = filaments.length === 1;
    let html = plasticHead(grams || (p.grams ? num(p.grams) + ' г' : ''), filaments.length);
    if (filaments.length) html += '<ul class="pj-filaments">' +
      filaments.map(f => filamentRow(f, single, single ? p.nozzle_temp : null)).join('') + '</ul>';
    const layer = p.layer_mm ? num(p.layer_mm, 2) + ' мм' + (p.first_layer_mm && p.first_layer_mm !== p.layer_mm ? ', первый ' + num(p.first_layer_mm, 2) : '') : '';
    const bed = [p.bed, p.bed_temp ? num(p.bed_temp, 0) + '°' : ''].filter(Boolean).join(', ');
    const settings = fact('Время по расчёту', p.time_s && !planned ? duration(p.time_s) : '') +
      fact('Высота слоя', layer) +
      fact('Заполнение', [p.infill, p.infill_pattern].filter(Boolean).join(', ')) +
      fact('Стенки', p.walls != null ? String(p.walls) : '') +
      fact('Поддержки', [p.supports, p.support_grams != null ? num(p.support_grams) + ' г' : ''].filter(Boolean).join(' · ')) +
      fact('Стол', bed) +
      fact('Температура сопла', !filaments.length && p.nozzle_temp ? num(p.nozzle_temp, 0) + '°' : '') +
      fact('Сопло', p.nozzle_mm ? num(p.nozzle_mm, 2) + ' мм' : '') +
      fact('Размер детали', p.size_mm ? p.size_mm.map(v => num(v, 0)).join(' × ') + ' мм' : '') +
      fact('Пауз по плану', p.pauses ? String(p.pauses) : '') +
      fact('Порядок', p.by_object ? 'по объектам' : '') +
      fact('Разглаживание', p.ironing ? 'есть' : '') +
      fact('Таймлапс', p.timelapse ? 'есть' : '') +
      fact('Слайсер', p.slicer);
    if (settings) html += '<h3>Печать</h3><div class="pj-facts">' + settings + '</div>';
    const objects = p.objects || [];
    const map = valid(p.pick) && (p.parts || []).length ? mapMarkup(p) : '';
    const total = objects.reduce((n, o) => n + (o.count || 1), 0);
    const named = String(title || '').toLowerCase();
    const lone = objects.length === 1 ? String(objects[0].name) : '';   // «1»: a number, not a name
    const listed = objects.length > 1 || objects.some(o => o.skipped) ||
      (lone !== '' && !/^\d+$/.test(lone) && !named.includes(lone.toLowerCase()));
    if (map || listed || total > 1) {
      html += '<h3>Детали на столе <small>' + plural(total, 'штука', 'штуки', 'штук') + '</small></h3>' + map +
        (listed ? '<ul class="pj-objects">' +
          objects.map(o => '<li><span>' + escape(o.name) + '</span>' + (o.count > 1 ? '<b>×' + o.count + '</b>' : '') +
            (o.skipped ? '<em>пропущено ' + o.skipped + '</em>' : '') + '</li>').join('') + '</ul>' : '');
    }
    return html;
  }
  let dialog, previousFocus, activeKey, activeProject, requestNumber = 0;

  // ── Plate map: Bambu's pick image fills each part with its id (pixel = id as B,G,R).
  // A click selects a part of the running job, «Пропустить» (pressed twice) skips them.
  function cssColor(name) {
    const probe = document.createElement('span');
    probe.style.color = 'var(' + name + ')';
    document.body.append(probe);
    const m = getComputedStyle(probe).color.match(/[\d.]+/g) || [128, 128, 128];
    probe.remove();
    return m.slice(0, 3).map(Number);
  }
  function mapBar() {
    return '<div class="pj-map-bar"><span class="pj-map-note" role="status"></span>' +
      '<button type="button" class="pj-skip" hidden disabled>Пропустить</button></div>';
  }
  function mapMarkup(p) {
    return '<div class="pj-map"><canvas aria-label="Схема стола: детали задания"></canvas><span class="pj-map-tip" hidden></span></div>' + mapBar();
  }
  // The bar under a plate map: the note, «Пропустить» that asks on itself, the send.
  // o: {live, skippable, can, total, skipped: Set, selected: Set, redraw(), pid}
  function skipBar(root, o) {
    const note = root.querySelector('.pj-map-note'), skipBtn = root.querySelector('.pj-skip');
    const jobs = window.ForgeJobs;
    let armed = false, armTimer = 0;
    function say(text) { note.textContent = text; }
    function idle() {
      if (!o.live) say('');
      else if (!o.skippable) say('Маркировка деталей не включена в слайсере — пропуск недоступен');
      else if (!o.can) say(o.skipped.size ? 'Пропущено: ' + o.skipped.size : '');
      else say((o.skipped.size ? 'Пропущено: ' + o.skipped.size + ' (серые). ' : '') +
        (o.selected.size ? 'Выбрано: ' + o.selected.size : 'Нажмите на деталь, чтобы выбрать её для пропуска'));
      skipBtn.hidden = !o.can;
      skipBtn.disabled = !o.selected.size;
      skipBtn.classList.remove('armed');
      skipBtn.textContent = o.selected.size ? 'Пропустить ' + plural(o.selected.size, 'деталь', 'детали', 'деталей') : 'Пропустить';
      armed = false;
    }
    skipBtn.addEventListener('click', async () => {
      if (!o.selected.size || skipBtn.classList.contains('is-wait')) return;
      if (o.selected.size + o.skipped.size >= o.total) { say('Пропустить все детали нельзя — для этого есть «Стоп»'); return; }
      if (!armed) {   // the first press asks on the button itself, the second sends
        armed = true;
        skipBtn.classList.add('armed');
        skipBtn.textContent = 'Точно пропустить? Нажмите ещё раз';
        clearTimeout(armTimer);
        armTimer = setTimeout(() => { if (armed) idle(); }, 4000);
        return;
      }
      clearTimeout(armTimer);
      const chosen = Array.from(o.selected);
      skipBtn.classList.add('is-wait'); skipBtn.disabled = true; skipBtn.textContent = 'Отправляю…';
      let res = {ok: false, line: {t: 'Нет связи с сервером'}};
      try { res = await jobs.skip(o.pid, chosen); } catch (error) { /* keeps the failure line */ }
      skipBtn.classList.remove('is-wait');
      if (res.ok) { chosen.forEach(id => { o.skipped.add(id); o.selected.delete(id); }); o.redraw(); }
      idle();
      say(res.line && res.line.t || '');
    });
    return {idle: idle, say: say};
  }
  function setupMap(root, p, pid) {
    const box = root.querySelector('.pj-map');
    if (!box) return;
    const canvas = box.querySelector('canvas'), tip = box.querySelector('.pj-map-tip');
    const names = new Map((p.parts || []).map(part => [part.id, part.name]));
    const jobs = window.ForgeJobs, printer = pid && jobs ? jobs.printer(pid) : null;
    // only the job the card shows now: the history's and a finished job's map is a picture
    const live = !!printer && printer.project_key === activeProject;
    const skipped = new Set(live && Array.isArray(printer.skipped_objects) ? printer.skipped_objects : []);
    const can = live && p.skippable && jobs.canSkip(printer);
    const selected = new Set();
    let ids = null, w = 0, h = 0, hover = 0;
    const colors = {base: cssColor('--info'), pick: cssColor('--danger'), done: cssColor('--text-muted'), edge: cssColor('--bg-elev')};
    const bar = skipBar(root, {live: live, skippable: !!p.skippable, can: can, total: names.size, skipped: skipped,
      selected: selected, pid: pid, redraw: () => draw()});
    function draw() {
      const ctx = canvas.getContext('2d'), out = ctx.createImageData(w, h), d = out.data;
      for (let i = 0; i < ids.length; i++) {
        const id = ids[i];
        if (!id) continue;
        const x = i % w, y = (i / w) | 0;
        const edge = x === 0 || y === 0 || x === w - 1 || y === h - 1 || ids[i - 1] !== id || ids[i + 1] !== id || ids[i - w] !== id || ids[i + w] !== id;
        let c = skipped.has(id) ? colors.done : selected.has(id) ? colors.pick : colors.base;
        let a = skipped.has(id) ? 90 : id === hover && can ? 255 : 215;
        if (edge) { c = colors.edge; a = 255; }
        d[i * 4] = c[0]; d[i * 4 + 1] = c[1]; d[i * 4 + 2] = c[2]; d[i * 4 + 3] = a;
      }
      ctx.putImageData(out, 0, 0);
    }
    function idAt(event) {
      const r = canvas.getBoundingClientRect();
      const x = Math.floor((event.clientX - r.left) / r.width * w), y = Math.floor((event.clientY - r.top) / r.height * h);
      return x >= 0 && y >= 0 && x < w && y < h ? ids[y * w + x] : 0;
    }
    const img = new Image();
    img.onload = () => {
      w = img.naturalWidth; h = img.naturalHeight;
      const scratch = document.createElement('canvas');
      scratch.width = w; scratch.height = h;
      const sctx = scratch.getContext('2d');
      sctx.drawImage(img, 0, 0);
      const px = sctx.getImageData(0, 0, w, h).data;
      ids = new Uint32Array(w * h);
      for (let i = 0; i < ids.length; i++) {
        const id = px[i * 4 + 3] ? (px[i * 4 + 2] << 16 | px[i * 4 + 1] << 8 | px[i * 4]) >>> 0 : 0;
        ids[i] = names.has(id) ? id : 0;
      }
      canvas.width = w; canvas.height = h;
      canvas.classList.toggle('pj-map-live', can);
      draw(); bar.idle();
    };
    img.onerror = () => { box.remove(); bar.say(''); };
    img.src = url(p.pick);
    canvas.addEventListener('mousemove', e => {
      const id = ids ? idAt(e) : 0;
      if (id !== hover) { hover = id; draw(); }
      tip.hidden = !id;
      if (id) {
        const r = box.getBoundingClientRect();
        tip.textContent = names.get(id) + (skipped.has(id) ? ' · пропущена' : selected.has(id) ? ' · выбрана' : '');
        tip.style.left = (e.clientX - r.left) + 'px'; tip.style.top = (e.clientY - r.top) + 'px';
      }
    });
    canvas.addEventListener('mouseleave', () => { hover = 0; tip.hidden = true; if (ids) draw(); });
    canvas.addEventListener('click', e => {
      const id = ids && can ? idAt(e) : 0;
      if (!id || skipped.has(id)) return;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      draw(); bar.idle();
    });
  }

  // ── Creality plate map: the printer's own part outlines (Klipper exclude_object,
  // GET /api/printers/{id}/parts), drawn as SVG; parts without outlines as a checkbox list.
  function outlineSvg(v) {
    const pts = [].concat(...v.parts.map(part => part.polygon || []));
    // the parts' area with a margin (small parts on a big bed stay clickable), within the bed
    const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]);
    const m = Math.max(15, 0.2 * Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)));
    let x0 = Math.min(...xs) - m, x1 = Math.max(...xs) + m, y0 = Math.min(...ys) - m, y1 = Math.max(...ys) + m;
    if (v.bed) { x0 = Math.max(0, x0); y0 = Math.max(0, y0); x1 = Math.min(v.bed[0], x1); y1 = Math.min(v.bed[1], y1); }
    const by = v.bed ? v.bed[1] : y1 + y0;      // flip: the bed's back on top (Y grows away from the viewer)
    const fy = y => (by - y).toFixed(2);
    const paths = v.parts.map((part, i) => part.polygon ? '<path class="pj-part" data-i="' + i + '" d="' +
      part.polygon.map((q, n) => (n ? 'L' : 'M') + q[0].toFixed(2) + ' ' + fy(q[1])).join('') + 'Z"/>' : '').join('');
    const top = by - y1, bed = v.bed ? [0, 0, v.bed[0], v.bed[1]] : [x0, top, x1 - x0, y1 - y0];
    return '<svg class="pj-outline" viewBox="' + [x0, top, x1 - x0, y1 - y0].map(n => n.toFixed(2)).join(' ') +
      '" role="img" aria-label="Схема стола: детали задания">' +
      '<rect class="pj-bed" x="' + bed[0] + '" y="' + bed[1] + '" width="' + bed[2] + '" height="' + bed[3] + '" rx="4"/>' + paths + '</svg>';
  }
  async function loadParts(pid, key) {
    const jobs = window.ForgeJobs, printer = pid && jobs ? jobs.printer(pid) : null;
    const target = dialog.querySelector('.preview-parts');
    // only the job the card shows now: its parts come live from the printer
    if (!printer || printer.kind !== 'creality' || printer.preview_key !== key || !target) return;
    let v;
    try {
      const response = await fetch('/api/printers/' + encodeURIComponent(pid) + '/parts');
      if (!response.ok) return;
      v = await response.json();
    } catch (error) { return; }
    if (!dialog.open || activeKey !== key || !v || !Array.isArray(v.parts) || v.parts.length < 2) return;
    const parts = v.parts, outlined = parts.some(part => part.polygon);
    const skipped = new Set(v.skipped || []), selected = new Set();
    const can = jobs.canSkip(printer);
    target.innerHTML = '<h3>Детали на столе <small>' + plural(parts.length, 'штука', 'штуки', 'штук') + '</small></h3>' +
      (outlined ? '<div class="pj-map">' + outlineSvg(v) + '<span class="pj-map-tip" hidden></span></div>' + mapBar() +
        '<ul class="pj-objects">' + parts.map(part => '<li><span>' + escape(part.name) + '</span></li>').join('') + '</ul>'
      : mapBar() + '<ul class="pj-parts-list">' + parts.map((part, i) => '<li><label><input type="checkbox" data-i="' + i + '"> <span>' +
        escape(part.name) + '</span></label></li>').join('') + '</ul>');
    const box = target.querySelector('.pj-map'), tip = box && box.querySelector('.pj-map-tip');
    if (box) box.classList.toggle('pj-map-live', can);
    function redraw() {
      target.querySelectorAll('[data-i]').forEach(el => {
        const id = parts[+el.dataset.i].id;
        if (el.tagName === 'INPUT') { el.checked = selected.has(id) || skipped.has(id); el.disabled = !can || skipped.has(id); }
        else { el.classList.toggle('sel', selected.has(id)); el.classList.toggle('done', skipped.has(id)); }
      });
    }
    const bar = skipBar(target, {live: true, skippable: true, can: can, total: parts.length, skipped: skipped,
      selected: selected, pid: pid, redraw: redraw});
    function toggle(i) {
      const id = parts[i] && parts[i].id;
      if (!can || !id || skipped.has(id)) return;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      redraw(); bar.idle();
    }
    target.addEventListener('click', e => {
      const el = e.target.closest('path[data-i]');
      if (el) toggle(+el.dataset.i);
    });
    target.addEventListener('change', e => { if (e.target.matches('input[data-i]')) toggle(+e.target.dataset.i); });
    if (box) {
      box.addEventListener('mousemove', e => {
        const el = e.target.closest('path[data-i]');
        tip.hidden = !el;
        if (!el) return;
        const part = parts[+el.dataset.i], r = box.getBoundingClientRect();
        tip.textContent = part.name + (skipped.has(part.id) ? ' · пропущена' : selected.has(part.id) ? ' · выбрана' :
          part.id === v.current ? ' · печатается сейчас' : '');
        tip.style.left = (e.clientX - r.left) + 'px'; tip.style.top = (e.clientY - r.top) + 'px';
      });
      box.addEventListener('mouseleave', () => { tip.hidden = true; });
    }
    redraw(); bar.idle();
  }
  async function loadProject(key, name, pid, planned, grams) {
    const request = ++requestNumber, target = dialog.querySelector('.preview-project');
    activeProject = key;
    if (!valid(key)) return;
    try {
      const response = await fetch('/api/job-projects/' + key + '.json');
      if (!response.ok) return;
      const project = await response.json();
      if (request !== requestNumber || !dialog.open) return;
      const generic = project.title && GENERIC.test(String(name || '').trim());
      target.innerHTML = projectMarkup(project, generic ? project.title : name, planned, grams);
      setupMap(target, project, pid);
      if (generic) dialog.querySelector('#preview-title').textContent = project.title;
      if (project.plate) dialog.querySelector('.preview-printer').textContent += ' · пластина ' + project.plate;
    } catch (error) { /* the card simply stays without project details */ }
  }
  function close() { if (dialog && dialog.open) dialog.close(); }
  function open(button) {
    const d = button.dataset, key = d.previewKey;
    if (!valid(key)) return;
    previousFocus = button; activeKey = key;
    if (!dialog) {
      dialog = document.createElement('dialog'); dialog.className = 'preview-dialog';
      dialog.setAttribute('aria-labelledby','preview-title');
      document.body.append(dialog);
      dialog.addEventListener('click', e => { if (e.target === dialog || e.target.closest('[data-preview-close]')) close(); });
      dialog.addEventListener('close', () => {
        if (window.ForgeSheet) window.ForgeSheet.closed(close);
        const target = previousFocus && previousFocus.isConnected ? previousFocus : document.querySelector('[data-preview-key="' + activeKey + '"]');
        if (target) target.focus({preventScroll:true});
      });
    }
    dialog.innerHTML = '<div class="preview-panel"><header class="preview-header"><span>Превью задания</span><span class="preview-printer">' + escape(d.previewPrinter) + '</span>' +
      '<button type="button" data-preview-close aria-label="Закрыть превью" autofocus>×</button></header>' +
      '<div class="preview-stage"><img alt="Превью детали" src="' + url(key) + '"><span class="preview-loading" role="status">Загрузка превью…</span></div>' +
      '<div class="preview-body"><h2 id="preview-title">' + escape(d.previewName || 'Задание') + '</h2>' +
      // the head: the history's planned time only (the grams stand by «Пластик», owner 17.11)
      (d.previewTime ? '<div class="preview-facts"><div><small>' + escape(d.previewTimecaption || 'Время') + '</small><strong>' +
        escape(d.previewTime) + '</strong></div></div>' : '') +
      '<section class="preview-project">' + plasticHead(d.previewGrams || '', 0) + '</section><section class="preview-parts"></section></div></div>';
    const img = dialog.querySelector('img'), status = dialog.querySelector('.preview-loading');
    img.onload = () => {status.hidden=true;};
    img.onerror = () => {img.hidden=true;status.textContent='Превью недоступно';};
    if (img.complete && img.naturalWidth) status.hidden=true;
    if (!dialog.open) { dialog.showModal(); if (window.ForgeSheet) window.ForgeSheet.opened(close); }
    loadProject(d.previewProject, d.previewName, d.previewPid, d.previewPlanned === '1', d.previewGrams || '');
    loadParts(d.previewPid, key);
  }
  document.addEventListener('click', e => {
    const target = e.target.closest('.job-preview');
    if (!target) return;
    e.preventDefault(); open(target);
  });
  document.addEventListener('error', e => {
    if (e.target instanceof HTMLImageElement && e.target.closest('.job-preview')) e.target.closest('.job-preview').classList.add('preview-missing');
  }, true);
})();
