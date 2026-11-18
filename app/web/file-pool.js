/* «Общие файлы по моделям» (Админ → Парк): every print file of a model's printers and the
   panel's own uploads (app/services/file_pool.py); «Синхронизировать» shows the plan first,
   «Копировать» asks on the button, then the copy runs with progress per printer. */
(function () {
  'use strict';
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // the interface's language (app/web/i18n.js); Russian where there is none (node tests)
  const locale = () => window.I18N ? window.I18N.locale() : 'ru-RU';
  const num = (v, d) => Number(v).toLocaleString(locale(), {maximumFractionDigits: d == null ? 1 : d});
  const size = b => b == null ? '' : b >= 1073741824 ? num(b / 1073741824) + ' ГБ' : b >= 1048576 ? num(b / 1048576) + ' МБ' : num(Math.max(1, Math.round(b / 1024)), 0) + ' КБ';
  const BASE = '/api/admin/file-pool';
  const POLL_GIVE_UP = 20;     // the progress unread 20 times in a row (~30 s): the poll stops and says so
  // groupsError / syncError: why the list / the copy's progress could not be read (shown, not swallowed)
  let root, api, groups = [], open = {}, pools = {}, plans = {}, notes = {}, picked = {}, armed = null, armTimer = null, poll = null, sync = null,
    groupsError = '', syncError = '', syncFails = 0;

  async function call(url, opts) {
    const r = await api(url, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) throw new Error((body && body.detail) || ('Ошибка ' + r.status));
    return body;
  }

  function poolHtml(g) {
    const pool = pools[g.key];
    if (pool === 'loading') return '<p class="fp-note" role="status">Собираю списки файлов с принтеров…</p>';
    if (!pool) return '';
    if (pool.error) return '<p class="fp-err" role="alert">' + esc(pool.error) + '</p>';
    const why = pool.printers.filter(p => !p.listed).map(p => p.label + ': ' + (p.why || 'нет списка'));
    const sel = picked[g.key] || {};
    const rows = pool.files.map(f => '<tr><td class="fp-pick">' + (f.readable ? '<input type="checkbox" data-fp-pick="' + esc(g.key) + '" value="' + esc(f.id) + '"' +
      (sel[f.id] ? ' checked' : '') + ' aria-label="Выбрать для синхронизации">' : '') + '</td><td class="fp-name">' + esc(f.title) + (f.pi ? ' <span class="fp-tag">на Кузнице</span>' : '') + (f.sample ? ' <span class="fp-tag fp-dim">образец</span>' : '') +
      (!f.readable && !f.sample ? ' <span class="fp-tag fp-dim" title="' + esc(f.why || 'Ни с одного принтера этой модели файл не прочитать') + '">не копируется</span>' +
        (f.why ? '<small class="fp-why">' + esc(f.why) + '</small>' : '') : '') + '</td>' +
      '<td class="fp-size">' + esc(size(f.size)) + '</td><td class="fp-on">' + pool.printers.map(p =>
        '<span class="fp-chip' + (f.on.indexOf(p.id) >= 0 ? ' on' : '') + '" title="' + esc(p.label + (f.on.indexOf(p.id) >= 0 ? ': есть' : ': нет')) + '">' + esc(p.label) + '</span>').join('') + '</td>' +
      '<td>' + (f.pi ? '<button type="button" class="fp-x" data-fp-del="' + esc(g.key) + '" data-name="' + esc(f.name) + '" aria-label="Убрать из хранилища Кузницы">×</button>' : '') + '</td></tr>').join('');
    return (why.length ? '<p class="fp-note">Без списка: ' + esc(why.join(' · ')) + '</p>' : '') +
      (pool.files.length ? '<div class="adm-table-wrap"><table class="fp-table"><thead><tr><th></th><th>Файл</th><th>Размер</th><th>Где лежит</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<p class="fp-note">Файлов нет</p>');
  }

  function planHtml(g) {
    const plan = plans[g.key];
    if (!plan) return '';
    if (plan === 'loading') return '<p class="fp-note" role="status">Считаю план…</p>';
    if (plan.error) return '<p class="fp-err" role="alert">' + esc(plan.error) + '</p>';
    const targets = plan.targets.map(t => '<li><strong>' + esc(t.label) + '</strong> — ' +
      (t.files.length ? t.files.length + ' файл.' + ', ' + esc(size(t.bytes)) +
        '<details><summary>какие</summary>' + esc(t.files.map(f => f.title).join(', ')) + '</details>' : 'копировать нечего') +
      (t.conflicts.length ? '<br><small>Не тронем (там другой файл с тем же именем): ' + esc(t.conflicts.join(', ')) + '</small>' : '') + '</li>').join('');
    const skipped = plan.skipped.map(s => esc(s.label) + ' — ' + esc(s.why)).join('<br>');
    const lost = plan.unavailable.map(u => esc(u.name)).join(', ');
    const isArmed = armed === g.key;
    return '<div class="fp-plan"><h4>План: ' + plan.copies + ' копий, ' + esc(size(plan.total_bytes)) + '</h4>' +
      (targets ? '<ul>' + targets + '</ul>' : '<p class="fp-note">Все доступные принтеры уже со всеми файлами</p>') +
      (skipped ? '<p class="fp-note"><b>Пропущены:</b><br>' + skipped + '</p>' : '') +
      (lost ? '<details class="fp-note"><summary><b>Не скопировать</b> (не прочитать ни с одного принтера): ' + plan.unavailable.length + '</summary>' + lost + '</details>' : '') +
      '<p class="fp-note">Свободное место принтеры не сообщают. Файлы на принтерах только добавляются, ничего не удаляется.</p>' +
      '<div class="fp-actions"><button type="button" class="pill-btn' + (isArmed ? ' fp-armed' : '') + '" data-fp-go="' + esc(g.key) + '"' +
      (plan.copies ? '' : ' disabled') + '>' + (isArmed ? 'Нажмите ещё раз — начнём копировать' : 'Копировать') + '</button>' +
      '<button type="button" class="discard-btn" data-fp-cancel="' + esc(g.key) + '">Отмена</button></div></div>';
  }

  function syncHtml(g) {
    if (!sync || sync.group !== g.key || !sync.printers) return '';
    const label = {waiting: 'ждёт', copying: 'копирую', done: 'готово', failed: 'ошибка', skipped: 'пропущен'};
    return '<div class="fp-sync"><h4>' + (sync.running ? 'Копирую…' : 'Копирование закончено') + '</h4>' +
      (syncError ? '<p class="fp-err" role="alert">' + esc('Ход копирования не обновляется: ' + syncError) + '</p>' +
        (poll ? '' : '<button type="button" class="pill-btn fp-light" data-fp-watch>Проверить снова</button>') : '') + sync.printers.map(r => {
      const pct = r.bytes_total ? Math.min(100, Math.round(r.bytes_done / r.bytes_total * 100)) : (r.state === 'done' ? 100 : 0);
      return '<div class="fp-row"><span class="fp-lbl">' + esc(r.label) + '</span><span class="fp-bar"><span style="width:' + pct + '%"></span></span>' +
        '<span class="fp-st">' + esc((label[r.state] || r.state) + ' · ' + r.files_done + '/' + r.files_total + (r.current ? ' · ' + r.current : '') + (r.why ? ' · ' + r.why : '')) + '</span>' +
        (r.errors && r.errors.length ? '<small class="fp-err">' + esc(r.errors.map(e => e.name + ': ' + e.why).join('; ')) + '</small>' : '') + '</div>';
    }).join('') + '</div>';
  }

  function render() {
    if (!root) return;
    const body = groups.map(g => {
      const accept = g.kind === 'bambu' ? '.3mf' : '.gcode';
      return '<div class="fp-group"><div class="fp-head"><div><strong>' + esc(g.title) + '</strong>' +
        '<small>' + esc(g.printers.map(p => p.label).join(', ')) + (g.pi_files ? ' · на Кузнице: ' + g.pi_files : '') +
        (g.syncable ? '' : ' · копирование недоступно: прошивка не принимает файлы по сети') + '</small></div>' +
        '<div class="fp-btns"><button type="button" class="pill-btn fp-light" data-fp-open="' + esc(g.key) + '">' + (open[g.key] ? 'Скрыть' : 'Файлы') + '</button>' +
        (g.syncable ? '<button type="button" class="pill-btn fp-light" data-fp-plan="' + esc(g.key) + '"' + (sync && sync.running ? ' disabled' : '') + '>' +
          (count(g.key) ? 'Синхронизировать выбранные: ' + count(g.key) : 'Синхронизировать всё') + '</button>' : '') +
        (g.syncable ? '<label class="pill-btn fp-light fp-upload">Загрузить файл<input type="file" accept="' + accept + '" data-fp-upload="' + esc(g.key) + '" hidden></label>' : '') +
        '</div></div>' +
        (notes[g.key] ? '<p class="' + (notes[g.key].ok ? 'fp-ok' : 'fp-err') + '" role="status">' + esc(notes[g.key].text) + '</p>' : '') +
        planHtml(g) + syncHtml(g) + (open[g.key] ? poolHtml(g) : '') + '</div>';
    }).join('');
    root.innerHTML = '<div class="panel-head"><div class="panel-title-group"><h3>Общие файлы по моделям</h3>' +
      '<p>Файлы печати со всех принтеров одной модели. «Синхронизировать» докладывает недостающие файлы на свободные принтеры этой модели — сначала показывает план. Между моделями файлы не копируются, с принтеров ничего не удаляется.</p></div></div>' +
      (groupsError ? '<p class="fp-err" role="alert">' + esc('Список не загрузился: ' + groupsError) + '</p>' +
        '<button type="button" class="pill-btn fp-light" data-fp-reload>Повторить</button>' : '') +
      (groups.length ? body : groupsError ? '' : '<p class="fp-note">Нет принтеров Bambu или Creality</p>');
  }

  async function loadPool(key) {
    pools[key] = 'loading'; render();
    try {
      const pool = pools[key] = await call(BASE + '/' + encodeURIComponent(key));
      // a ticked file that is gone (removed from the panel, no printer has it now) is no pick any more
      const sel = picked[key] || {};
      Object.keys(sel).forEach(id => { if (!pool.files.some(f => f.id === id && f.readable)) delete sel[id]; });
    }
    catch (e) { pools[key] = {error: e.message}; }
    render();
  }
  async function loadGroups() {
    try {
      const r = await call(BASE);
      groups = r.groups || []; groupsError = '';
      sync = r.sync && r.sync.group ? r.sync : null;
      if (sync && sync.running) watch();
    } catch (e) { groupsError = e.message; }      // not «no printers»: the groups shown stay, the reason above them
    render();
  }
  function watch() {
    if (poll) return;
    syncFails = 0;
    poll = setInterval(async () => {
      try { sync = await call(BASE + '/sync'); syncError = ''; syncFails = 0; }
      catch (e) {                   // the bar would stand still without a word: say why, give up after a while
        syncError = e.message;
        if (++syncFails >= POLL_GIVE_UP) { clearInterval(poll); poll = null; }
        render();
        return;
      }
      render();
      if (!sync.running) {
        clearInterval(poll); poll = null;
        Object.keys(open).forEach(k => { if (open[k]) loadPool(k); });
      }
    }, 1500);
  }
  function upload(key, file) {
    notes[key] = {ok: true, text: 'Загружаю ' + file.name + '…'}; render();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', BASE + '/' + encodeURIComponent(key) + '/upload?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => { if (e.lengthComputable) { notes[key] = {ok: true, text: 'Загружаю ' + file.name + ' · ' + Math.round(e.loaded / e.total * 100) + '%'}; render(); } };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { body = null; }
      notes[key] = xhr.status === 200 ? {ok: true, text: 'Загружено на Кузницу: ' + file.name} :
        {ok: false, text: (body && body.detail) || (xhr.status === 401 ? 'Сессия истекла — обновите страницу' : 'Ошибка ' + xhr.status)};
      loadGroups(); if (open[key]) loadPool(key);
    };
    xhr.onerror = () => { notes[key] = {ok: false, text: 'Связь прервалась'}; render(); };
    xhr.send(file);
  }
  async function onClick(e) {
    const t = e.target;
    let key;
    if (t.closest('[data-fp-reload]')) { loadGroups(); return; }
    if (t.closest('[data-fp-watch]')) { syncError = ''; watch(); render(); return; }
    if ((key = t.closest('[data-fp-open]') && t.closest('[data-fp-open]').dataset.fpOpen)) {
      open[key] = !open[key];
      if (open[key]) loadPool(key); else render();
    } else if ((key = t.closest('[data-fp-plan]') && t.closest('[data-fp-plan]').dataset.fpPlan)) {
      plans[key] = 'loading'; armed = null; render();
      const ids = Object.keys(picked[key] || {}).filter(id => picked[key][id]);
      try { plans[key] = await call(BASE + '/' + encodeURIComponent(key) + '/plan', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                                  body: JSON.stringify(ids.length ? {files: ids} : {})}); }
      catch (err) { plans[key] = {error: err.message}; }
      render();
    } else if ((key = t.closest('[data-fp-cancel]') && t.closest('[data-fp-cancel]').dataset.fpCancel)) {
      delete plans[key]; armed = null; render();
    } else if ((key = t.closest('[data-fp-go]') && t.closest('[data-fp-go]').dataset.fpGo)) {
      if (armed !== key) {                       // the first press only asks, on the button itself
        armed = key; render();
        clearTimeout(armTimer); armTimer = setTimeout(() => { armed = null; render(); }, 6000);
        return;
      }
      armed = null; clearTimeout(armTimer);
      try {
        sync = await call(BASE + '/' + encodeURIComponent(key) + '/sync', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                           body: JSON.stringify({plan: plans[key].id})});
        delete plans[key]; watch();
      } catch (err) { notes[key] = {ok: false, text: err.message}; }
      render();
    } else if ((key = t.closest('[data-fp-del]') && t.closest('[data-fp-del]').dataset.fpDel)) {
      const name = t.closest('[data-fp-del]').dataset.name;
      try {
        await call(BASE + '/' + encodeURIComponent(key) + '/stored?name=' + encodeURIComponent(name), {method: 'DELETE'});
        notes[key] = {ok: true, text: 'Убрано из хранилища Кузницы: ' + name};
      } catch (err) { notes[key] = {ok: false, text: err.message}; }
      loadGroups(); loadPool(key);
    }
  }
  function count(key) { return Object.values(picked[key] || {}).filter(Boolean).length; }
  function onChange(e) {
    const pick = e.target.closest('[data-fp-pick]');
    if (pick) {
      const key = pick.dataset.fpPick;
      (picked[key] = picked[key] || {})[pick.value] = pick.checked;
      delete plans[key]; armed = null;
      const btn = root.querySelector('[data-fp-plan="' + key + '"]');
      if (btn) btn.textContent = count(key) ? 'Синхронизировать выбранные: ' + count(key) : 'Синхронизировать всё';
      const plan = btn && btn.closest('.fp-group').querySelector('.fp-plan');
      if (plan) plan.remove();
      return;
    }
    const input = e.target.closest('[data-fp-upload]');
    if (input && input.files && input.files[0]) upload(input.dataset.fpUpload, input.files[0]);
  }
  function mount(el, fetcher) {
    if (!el) return;
    api = fetcher || ((u, o) => fetch(u, o));
    if (root !== el) {
      root = el;
      el.addEventListener('click', onClick);
      el.addEventListener('change', onChange);
    }
    loadGroups();
  }
  window.ForgeFilePool = {mount: mount};
})();
