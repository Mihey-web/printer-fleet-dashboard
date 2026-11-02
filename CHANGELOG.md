# Changelog

All notable changes to this project are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

Tagged releases are `1.0.0`, `2.0.0` and `2.1.0`. The dated entries between
them are sync points; they match the `Sync to internal v<date>` commits in
this repository's history.

## [Unreleased]

## [2.1.0] — 2026-11-02

The panel speaks English, and the project has a documentation site.

### Added
- Russian / English switch for the web panel: in the 👤 menu («This browser»)
  and on the login page, applied at once without a reload and kept per
  browser. With no choice yet the browser's languages decide: Russian for
  ru, uk, be, kk, ky, uz, tg, hy, az, English for everything else. Russian
  stays the source language; English is a dictionary keyed by the Russian
  text (`app/web/i18n/en.js`, ~1500 strings including the server's command
  replies and errors), so a third language is one more file. Bambu's HMS and
  print-error texts are shown in Bambu's own English. Browser
  notifications, the tab title, the timeline canvas, dates and the
  installable app's manifest follow the language. The Telegram bot stays in
  Russian for now.
- Documentation site (MkDocs Material, English and Russian): install on
  Linux / Raspberry Pi, Windows and Docker, first run, adding each printer
  brand, every screen, Telegram and proxy, users and security,
  configuration, FAQ. Built and published to GitHub Pages by
  `.github/workflows/docs.yml`. The 👤 menu links to it (in the panel's
  language) and to the project, next to the version.

### Fixed
- One NaN or Infinity in one printer's telemetry no longer breaks
  `/api/printers` for the whole farm.
- An AMS tray whose type or colour is not a string no longer takes the
  printer offline.
- «Resume» and «Resume all» refuse a laser or cutter job, which Bambu Studio
  only lets you continue from the printer's screen.
- Heating to 0 during a print is refused like «Cool down»; «Cool down»,
  «Eject» and heating to 0 wait out the ~20 s after an AMS load or unload.
- Parallel login attempts can no longer exceed the 5-per-IP limit; behind a
  proxy that only sets `X-Forwarded-For` the address the proxy appended is
  used, not the one the client wrote.
- `/api/auth/me` rejects a token issued before a password reset or role
  change, like every other route.
- A malformed `printer_capability` setting is refused by the admin API and
  ignored at start-up instead of crash-looping the service.
- The nightly database backup works when the path contains `#` (it used to
  back up an empty file and report success).
- A failed AMS history insert no longer leaves the transaction open.
- Tests: the stub MQTT broker shuts its sockets down properly on Linux.

## [2.0.0] — 2026-11-01

A new interface and a lot underneath it: four screens instead of seven tabs
and a phone layout, printer notifications with Bambu Studio's actions, a print
journal, maintenance mode, an installable app, temperature charts, Creality
controls, and cards that follow live telemetry every 2 s.

### Added
- Printer notifications with actions. Every HMS message and print error of
  each printer is listed on its card (badge «⚠ N» in the colour of the worst
  level, a plate under the progress for a pause-error or a fatal one, a
  popover / bottom sheet on phones, maintenance notes collapsed but never
  hidden), with the time it appeared and a wiki link; a bell in the header
  lists the printers that need attention. The buttons per code are the ones
  Bambu Studio shows, from Bambu's own action and Russian text tables,
  downloaded at runtime once a day (only the 3-character model prefix leaves
  the server; the cache is not in git). Every action is checked against the
  live printer right before sending (same error still active, still paused),
  admin only, «Стоп» / «Прервать» / firmware restart ask for confirmation,
  verdict and audit like every other command. «Сбросить ошибки» at the foot
  of the bell with a dry run. The «Ошибки в карточках» switch is gone.
- A cancelled Bambu job is its own state «ОТМЕНЕНО» (grey) instead of a red
  «ОШИБКА»; «История → Печати» shows why a printer paused.
- Klipper stays online in «ОШИБКА» with the reason when klippy shuts down
  (e.g. «mcu: Unable to connect») and offers «Перезапуск прошивки»;
  Creality shows its error code.
- Print journal («История → Печати»): one row per print — start and end,
  slicer plan vs actual, pauses and their reasons, result (finished,
  cancelled, failed, power loss, stopped, lost), success rate per printer,
  top failure and pause reasons, prints per day, CSV export (admin only).
  Past prints are rebuilt once from the existing history.
- Maintenance mode per printer (admin, from the card's ⋯, optional note):
  the card keeps its real status with a «обслуживание · с …» line and folds
  into «Не в работе»; the printer leaves the state counts, load,
  «Финиши», notifications, browser pushes, fleet commands (the dry run says
  why), AMS auto-drying, auto-ignore and the command probe; its time shows as
  a hatched «обслуживание» segment on the timeline and is left out of the
  printing share. Single commands from the card still work.
- Installable app (PWA) from the dashboard's HTTPS address with icons and a
  theme colour per theme; the service worker caches static files only. The
  browser tab shows «(N⚠) P% · Кузница» and a progress ring on the favicon.
- Temperature chart in the chip popover (nozzle, bed, chamber): 6 h / 24 h /
  7 d, temperature and dashed target, gaps where there was no data, min / avg /
  max, hover or tap for a value, the live end follows the chip. Admins keep
  the setter above it; viewers and chips that cannot be set get the chart
  only. Chamber temperature and the three targets are recorded since 19.10,
  `GET /api/printers/{id}/temps?hours=1..168`.
- «Скрыть» for any printer notification (admin): a hidden one no longer
  colours or counts in the badge, lifts the card, shows in the bell or the tab
  title or names a browser push; hidden ones sit in a collapsed «скрытые»
  group with «Вернуть»; kept on the server for every device.
- Creality K1 series / Ender-5 Max controls on the card, over the printer's
  own WebSocket `:9999` `set`: the light (`lightSw`), nozzle and bed targets
  (`nozzleTempControl`, `bedTempControl`; up to 300 / 100 °C, or the lower
  `maxNozzleTemp` / `maxBedTemp` the printer reports), the four speed modes
  as `setFeedratePct` 50 / 100 / 124 / 166 % (only while printing or
  paused), the part / auxiliary / case fans (`M106 P0` / `P2` / `P1`, only
  the fans the printer reports) and «Остудить всё» (nozzle and bed to 0,
  not mid-print, nor on a pause that only a `materialStatus` other than the
  runout value 1 reports). Creality never acknowledges a command: the result
  is «отправлено», never «подтверждено» (a multi-frame command cut short says
  how many frames went out). A K1C's `state` 4 after a cancelled print is
  «Отменено», not «неизвестно», so «Остудить» works there.
- Cards follow live telemetry every 2 s (`FORGE_LIVE_REFRESH_S`, `0` = off):
  a started print, a pause or a temperature change shows in 1–4 s instead of
  up to the next 30 s poll. Only the store is refreshed; history, the job
  journal and notifications keep the poll's cadence.

### Fixed
- A lasting «fatal» HMS on a printer that keeps printing (e.g. a dirty lidar lens)
  no longer puts it on top of the cards / the «Финиш» order, in the bell or
  the tab title; only a paused or errored printer is lifted.

### Changed
- The dashboard is laid out anew (information architecture redesign): four
  screens — «Принтеры · AMS · История · Админ», three for a viewer — instead
  of seven tabs; «Сводка», «Окно», «Настройки» and «Администрирование» are
  gone and everything on them has a new home. The screen lives in the address
  (`#/printers`, `#/ams`, `#/history/<sub>?period=…&printer=…`,
  `#/admin/<sub>`), so a reload, the automatic reload after a deploy, Back /
  Forward and shared links keep it; the remembered tab and «История» sub-tab
  (`forge-ops-active-view`, `forge-history-sub`) are read once, turned into an
  address and removed. A sticky 48 px header: the bell is the one loud
  counter, a dot with the poll time (a red «нет связи · 2 мин» on a failure),
  👤 with the role in words.
  - «Принтеры»: one farm row — the state chips with their counts are the
    filters (click, click again for all, Ctrl+click to add; remembered),
    «Порядок: Имя | Финиш» (a printer that needs a decision is always first;
    the «Проблемные сверху» switch and the «Онлайн» filter are gone) and the
    farm's commands (light, «Пауза всех…», «Продолжить N» only when there is
    any). A «Финиши» row — «Готовы» (finished, the plate waits) and «Дальше»
    (the next finishes) — unfolds the former «Окно» timeline in place. «Не в
    работе · N» folds the printers in maintenance and those offline for a day
    or more. Cards: five buttons, one ⋯ in the head (maintenance, and links
    to the printer's prints, timeline and settings), the model always next to
    the name, a coloured notification badge only when a decision is needed or
    a fatal is under 30 min old (a grey «ⓘ N» otherwise). The bell lists
    «Нужно решение» first and the rest folded, «Сбросить ошибки (N)» at its
    foot.
  - «AMS»: an auto-drying strip on top with «Сушить все…», «Остановить все»
    and «Правила ›» (admin), one drying line per unit, the units by printer
    name, humidity zones from the auto-drying goal, the drying journal at the
    bottom (5 lines, up to 50 in place).
  - «История»: one sticky head for every sub-tab (period 24 h · 7 d · 30 d ·
    90 d · «Свой…», one printer choice, «⇩ Выгрузить» for admins) over
    «Печати» (result chips with counts, 50 rows a page, a row unfolds into
    every pause with its time and reason, the auto-ignore presses and how the
    print ended), «Хронология» (up to 7 days, the help under «?»),
    «Статистика» (the former «Сводка» statistics and the per-printer rows in
    one table, prints per day, top reasons) and «Действия» (admin: the audit
    log in groups — commands, automation, park and settings, access — with
    the token refreshes hidden). The events log is gone: prints, pauses and
    outages are in «Печати» and «Хронология».
  - «Админ» (admin only): «Парк» (the printer table; serial number and access
    code are in the form, a read-only «Управление» column, the maintenance
    status with «к карточке ›», the Creality light switch in the form),
    «Автоматика» (the auto-drying rules and units, moved from the AMS tab,
    and the auto-ignore codes with Bambu's texts — no longer API-only),
    «Доступ» (users; role, password and delete in the row), «Система»
    (Telegram and its proxies collapsed, the on/off switch in view).
    «Этот браузер» (👤) holds what only this browser keeps: the account and
    «Выйти», theme and text size, browser notifications, the app install,
    the version. The login page shows this browser's theme.
  - Phones (under 900 px): a bottom navigation, a 44 px top line, the farm's
    commands in a «Ферма ▾» sheet, the finishes as a list, every popover as a
    bottom sheet that Back closes, AMS units as one-line rows, the admin
    tables as cards, pinch and tap on «Хронология»; PWA shortcuts «Нужно
    решение», «Финиши» and «AMS»; a browser push opens its printer.
  - One rule for confirmations, tips and answers: no native confirm / alert /
    prompt is left — one object is asked in place (stop drying, load or
    unload filament, end maintenance, delete a printer or a proxy, apply or
    discard the park's edits), group actions keep their dialogs; tips are
    `data-tip` only (one floating tooltip for the whole page, a tap on a
    phone), no native `title`; a command's answer shows where it was pressed,
    a toast only when that place is gone.
  - The «Годно / Брак» marks and «Брак %» are removed, the «Оценка» column
    of the print journal's CSV too (the
    `PUT /api/admin/history/jobs/{id}/verdict` route stays, unused). The
    print results read the same in the list, its chips and the CSV:
    «Успешно · Отменено · Ошибка · Питание · Нет связи · Остановлено · Идёт».
  - Server: `GET /api/history/jobs.csv` is admin-only (403 for a viewer);
    `GET /api/admin/audit` takes `prefix` / `exclude` (action starts),
    `printer` and `fr` / `to`, next to the new `/api/admin/audit/counts` and
    `/api/admin/audit.csv`; `GET /api/admin/print-error-texts?codes=…` names
    auto-ignore codes; a print row of `/api/history/jobs` carries
    `pause_list` (every pause with its time).
- Setting `printer_light_disabled` replaces `printer_light_enabled`: every
  Creality has its light button unless its id is listed (a model without a
  lamp).
- A Creality's speed chip opens its modes only while a print runs (a
  Bambu's in any state, as before); the modes show the feed rate they set
  (Тихий · 50 % … Безумный · 166 %).
- The card's ⋯ menu no longer repeats the card: speed is the speed chip,
  «Остудить всё» and «Опустить стол» are the row's ❄ / ⤓ buttons; the menu
  holds the maintenance section (admin) and the printer's links.
- Lesser HMS (maintenance reminders) are called «плановые».

### Fixed
- The card head stays one line at every width: a long name with its model no
  longer wraps the status pills under it and pushes that card's progress and
  chips below its neighbours'; short of room the model is left out whole, then
  the name is cut. Wi-Fi is an icon on every card up to 400 px (dBm in its tip).
- A printer with two or more AMS units (e.g. AMS 2 Pro + AMS HT) no longer
  runs the card's AMS row past the edge: each unit's temperature moves into
  its humidity's tooltip (the drying time left stays), and the row is clipped
  inside the card whatever the number of units.
- «История → Печати»: the result badge sits level with the progress text next
  to it.
- AMS auto-drying keeps drying: a run the printer switched off on its own is
  restarted after 5 min instead of holding the unit for 24 h and then
  switching it off; while printing the Bambu print preset is used (PETG
  55 °C — at 65 °C P2S printers kept switching the drying off).
- Auto «Ignore and resume» counted tries per job, but network prints report
  job id "0", so after 3 tries a printer was never waved through again until
  a restart; tries are now per pause. A listed pause that is not waved
  through is written to the audit log with the reason.

## [2026-10-15]

### Added
- Automatic «Ignore and resume» for build-plate marker errors: a Bambu printer
  paused on `0500_8062` (marker not detected / strong light on the plate,
  P2S / H2 family) or `0C00_8009` (localization marker not detected, X1 and
  others) gets the command Bambu Studio sends for that dialog's «Ignore this
  and Resume» button — once per job, again after 2 min if still paused, at
  most 3 times; every one is written to the audit log. The codes are the
  `auto_ignore_print_errors` setting (worn plate markers on plates that move
  between printers).

### Fixed
- The command-capability probe no longer stops a running AMS drying right
  after a service restart: it waits for the printer's first full report
  (before it, a drying is invisible) and starts 3 min after launch.
- «Статистика принтеров» no longer stretches the last known state over a gap
  in the history (days without records were shown as "printing" or
  "offline"); a state lasts at most 10 min past its record, the rest shows as
  «нет данных», and the printing share counts only the time with data.
- AMS auto-drying: a warm AMS reads drier than it is, so a reading already
  above the target starts the next run at once instead of waiting 1–3 h for
  the AMS to cool; "try again in a few seconds" refusals (a printer just
  switched on) are retried in 2 min without a growing back-off; units whose
  slots read empty because the filament is not fed in are dried too, at the
  PLA-safe 45 °C unless the unit has its own mode.

### Changed
- AMS auto-drying form: one main field «Держать влажность не выше» (5–55 %;
  the start threshold is derived one point above it), «Повторять прогоны» as
  a list with «пока не дойдёт до нужной» (no limit, no 48 h back-off), the
  rest under «Дополнительно»; the save bar with its error stays on screen.
- Compact header: the fleet bar sits in the sort / filter row, one
  «на связи» status chip, Russian labels instead of English terms.
- Temperature chips show the target only while really heating or cooling
  (more than 3 °C off); chips try a denser variant before balanced rows.

### Changed (earlier in this sync)
- Bambu telemetry now runs on the `pybambu` library vendored from the Home
  Assistant integration [ha-bambulab](https://github.com/greghesp/ha-bambulab)
  (MIT, `app/vendor/pybambu`, see its `VENDORED.md`) instead of PyPI
  `pybambu==1.0.1`, which has not been updated since 2024. Nothing imports the
  PyPI package any more.
- Bambu HMS errors on the card and in Telegram come from Bambu's own Russian
  tables and read `HMS_<code>: <text> [<severity>]`. A code missing from the
  tables still reads `HMS_<code>: unknown [<severity>]` with a link to the
  code's own wiki page, as before. Bambu's tables have an empty text for 90
  codes, which the library drops: the fatal (11) and serious (67) ones are
  still shown that way (a P2S, for one, keeps reporting the serious
  `0500_0600_0002_0070`), the common (10) and unclassified (2) ones no
  longer are.
- Fan percentages round up to the next 10 % (raw PWM 14 of 15 now reads 100 %,
  was 90 %), as Bambu Studio shows them.
- A Bambu printer that refuses the MQTT login (wrong access code, LAN /
  Developer mode switched off) is retried with the MQTT client's back-off
  (1 → 30 s) instead of every second, also on a flaky link where some attempts
  get no answer at all. Its error reads "MQTT login refused" until it logs in,
  or until two attempts in a row get no login answer (switched off or
  rebooting): with the attempts up to 30 s apart by then, that can take up to
  about a minute after the printer is switched off.
- A Bambu printer whose MQTT broker answers but turns the session away
  (CONNACK 1–3; 3 = server unavailable, e.g. still booting) reads "MQTT broker
  on the printer answered but turned the session away (CONNACK …)" instead of
  "powered off or unreachable".
- After a lost connection (printer powered off or rebooting, network drop) a
  Bambu printer is retried with a back-off of 1 → 5 s instead of every second,
  so it is back online within about 5 s of becoming reachable again. That
  includes a printer still booting, which turns logins away as "server
  unavailable" for a while.
- A Bambu printer that stays connected but sends no data is marked offline
  after 60 s (was 30 s). Power-off is still noticed within about 5–10 s.
- Empty AMS slots report remaining filament as unknown (−1) instead of 0, and
  the packed `device.bed` temperatures win over the flat ones (they agree on
  real printers). Elapsed print time appears once a print start has been seen.
- The nozzle temperature of printers that report per-extruder values
  (`device.extruder`: P2S, H2 series, X1C on current firmware) comes from that
  packed value instead of the flat `nozzle_temper`; the two can differ by ±1 °C
  (seen on an H2S).
- AMS `dry_time` is `null` instead of `0` while the unit is not drying, on the
  card and in the AMS history (`ams_history.dry_time`).
- A Bambu `print_error` reads `Ошибка печати 0300_400C: <text>`, with the text
  from Bambu's own (Russian) tables like the HMS errors, instead of
  `Код ошибки печати: 0x0300400C`. Codes the tables have no text for keep only
  the code. The library's own lookup lost the text of every code from
  `0x10000000` up; the code is formatted here instead.
- Whether a Bambu printer accepts the dashboard's commands (`print_cmds`) is
  read from its reports (`print.fun` bit 29: signed commands required) on the
  printers that send it (X1, P2S, H2 series), so the controls appear with the
  first poll. The background probe — now Bambu Studio's AMS drying stop, sent
  only while no AMS of that printer is drying — remains for printers without
  `fun` (P1 series), and takes over again for a printer whose real reply says
  "verify failed" against a `fun` verdict.
- AMS load and unload moved from the service popover (`AMS выгрузка`, a slot
  number field) to the AMS popover; on a printer without an AMS the card's
  AMS button offers the external-spool unload.
- Commands show the printer's verdict: on the card (the button and a status
  line, see the control row below) and in the AMS tab's toast — confirmed by
  the printer, sent without an answer, or refused with the reason.
- `POST /api/admin/printers/{id}/command` answers `{success, status, detail,
  replied}`, `status` being `confirmed`, `sent`, `rejected` (the printer or the
  transport said no) or `refused` (not sent: capability, parameters, state);
  a failure is a 400 `{detail, status}`. The audit line ends with the outcome
  (`printer_command:<action>:<id>:[params,]st=<status>`).
- Cool down and bed down are sent only while the printer's live state shows it
  standing — cooling a paused print is allowed, the bed moves only when the
  printer is idle, finished or failed — and never during a filament change;
  pause, resume and stop are checked against the live state as well. A second
  bed down within 20 s is refused: each one moves the bed 30 mm.
- The light switches both chamber lamps on H2 / X2 printers and reads lit when
  either lamp is on; the answer says so when the second lamp does not confirm
  (no answer, or refused with the printer's reason).
- Cool down also switches the H2 chamber heater off (`M141 S0`).
- After any command the printer's card follows it from live telemetry every
  2 s until the printer reaches the expected state (30 s at most), not only
  after AMS commands.

### Added
- AMS auto-drying («Автосушка AMS», AMS tab) for AMS 2 Pro / AMS HT units.
  A unit whose humidity at rest (not drying, not right after a drying) stays
  at or above the start threshold for the set hours (default 25 % for 2 h)
  gets a drying run by time, at the temperature of its most heat-sensitive
  spool (Bambu's presets, capped by the unit and by printing from it). After
  the pause the humidity is measured once the AMS has cooled to within 3 °C
  of its temperature before the run (at most pause + 2 h later); above the
  target (default 20 %) the series goes on, up to the run limit (default 2,
  also the cap of starts in any 24 h). A series that runs out of runs above
  the target keeps the unit off for 48 h. Safety: the engine never stops a
  drying on its own; a drying it did not start (Handy, Studio, a schedule) is
  tracked, counted toward the limit and never touched, and when someone stops
  one early — or stops a run of ours — the unit is left alone for 24 h; two
  consecutive interruptions of its own runs switch the unit's automation off
  until an admin re-enables it. A stop followed by a new start within 15 min
  (a schedule restarting its program) is a restart, not a stop, and a drying
  already going when the engine first sees it is dated from the AMS history.
  A short restart of the dashboard (under 15 min) does not lose track of a
  drying: one stopped meanwhile still counts as stopped.
  Units without spools are skipped. It ships in
  shadow mode: it only writes what it would start to its journal until an
  admin presses «выключить пробный режим». The section (visible to everyone,
  editable by admins) holds the rules, a per-unit table with the next run's
  plan and status, «Сушить все сейчас» / «Остановить все», and the journal;
  every AMS card gets an «Авто: …» line, and the humidity chart the start and
  target lines and the drying periods. No Telegram messages.
- A control row on every printer card (admins): light, pause / resume, stop,
  cool down, bed down, AMS and a ⋯ menu (cool down, bed down, print speed).
  A control appears only where the printer can carry it out and is greyed out
  with the reason while its state forbids it; the answer shows on the button
  and in a status line under the row. Stop, and cooling a paused print, ask
  inline instead of a browser `confirm()`.
- Fleet bar «Вся ферма» above the cards (admins): lights on / off on every
  online printer with a switchable light, and pause / resume of chosen
  printers through a dialog that lists who is included, who is not and why;
  a resume pre-selects user pauses only (a pause the printer made itself —
  filament runout, nozzle clog — or a G-code pause is listed unselected).
  `POST /api/admin/printers/bulk-command` (`light_on`, `light_off`, `pause`,
  `resume`, `dry_run`) sends to all chosen printers at once and answers within
  12 s with a result per printer.
- Klipper / Moonraker control: pause, resume, cancel and the light — an
  `output_pin` whose name has the word "light", "led" or "lamp" (`caselight`
  first; `motors_enabled` or `flight_mode` is no light), found automatically
  when klippy becomes ready — each answered by Moonraker.
- `/api/printers` rows carry `caps` (light, pause, resume, stop, cooldown,
  eject, tune, ams: `true` / `false` / `null`) and `caps_note` (why print
  control is not available), and `ctl_ready` / `light_ctl` for Klipper.
- Setting `printer_light_enabled` (a list of printer ids, set through
  `PUT /api/admin/settings`): enables the Creality light switch (`lightSw`),
  not verified on a real printer yet, per printer.
- Card chips (temperatures, fan, speed, nozzle, door, SD card) stay on one row
  whenever they fit — compact on a narrow card — and otherwise wrap into
  balanced rows of equal cells, never leaving one chip alone on a line.
- AMS drying (AMS 2 Pro, AMS HT): the unit's drying status and phase, time
  left, temperature, hours and material, and why drying cannot start right
  now, on the card, in the AMS popover and on the AMS tab. Admins start it
  (material presets, temperature and hours) and stop it from the AMS popover
  or the AMS tab. The temperature is limited the way Bambu Studio limits it:
  45–65 °C (AMS 2 Pro) / 45–85 °C (AMS HT), lower when a loaded spool would
  deform, and lower again while that AMS feeds a print. Drying is allowed while
  printing.
- AMS popover (click the AMS chip on a printer card, admins): load any slot,
  unload the extruder, dry or stop drying per unit.
- Filament-change progress on the card while a Bambu printer loads or unloads
  filament: action and step (heating the nozzle, cutting, pulling back,
  feeding, purging, checking).
- Card indicators: door open (models with a door sensor), SD card missing or
  faulty (a missing card is not flagged on printers that print from internal
  storage), and the nozzle — diameter and type in the nozzle temperature
  tooltip, plus a chip for a non-standard nozzle (not 0.4 mm, high flow, TPU).
- After an AMS command the printer's card follows the change live: it is
  refreshed from the printer's telemetry every 2 s until the change has
  settled (3 minutes at most) instead of the 30 s poll.
- `/api/debug/printers/{id}` (admin) returns `raw`: the report bit fields the
  Bambu library does not keep (AMS units, extruder, filament change), in any
  printer state.
- Russian labels for Bambu print stages 36–77 (H2 / P2 series).
- AMS HT units (ids 128+) on the card and in the AMS history.
- Opt-in TLS certificate verification for Bambu printers against Bambu's CAs:
  `FORGE_BAMBU_TLS_VERIFY` (`*` or a comma list of models; off by default).

### Removed
- Object skip (`skip_objects`, the `obj_list` request field): without the
  printer's list of objects the ids were typed blind and could skip the wrong
  part.
- The «Сервис» popover; its actions are in the control row and the ⋯ menu.
- The chamber temperature of models without a chamber sensor (P1P, P1S, A1,
  A1 mini): they report a bogus value (a constant 5° on the P1S), so the card
  shows no chamber chip for them.

### Fixed
- Printer cards overflowed the screen on phones narrower than 394 px: the grid
  column was at least 300 px wide.
- After a light command the card could show the old state for minutes on
  printers that report the lamp only rarely (P1S): a confirmed command (Bambu
  or Klipper) now updates the printer's state before the answer comes back.
- AMS load / unload sent `ams_change_filament` without `ams_id` / `slot_id`,
  which current Bambu firmware rejects. It is now sent as Bambu Studio sends
  it — the AMS unit and slot, the slot address older firmware expects, and
  nozzle temperatures from the spools' profiles — and only after the printer's
  live telemetry shows it idle (not printing, paused or preparing), no
  filament change running and the firmware able to take the command. Unload
  needs filament in the extruder. A second change within 20 s of the first is
  refused until the printer has run the first one, also when both are sent at
  once. The external spool's profile sets its temperature too, and a refusal
  because the chamber or the AMS is too hot for the filament says so.
  Printers with two extruders or a filament track switch are refused: they
  need a feed direction the dashboard does not send.
- The part-cooling fan speed of Bambu printers was never read, so the card
  showed no part fan and the fan editor pre-filled it as 0 % — "Apply" after
  changing only the aux or chamber fan sent `M106 P1 S0` and switched part
  cooling off mid-print.
- An AMS HT unit in the payload broke the whole AMS update on the card.
- `requirements.txt` now lists what the Bambu library imports (`paho-mqtt`,
  `requests`, `python-dateutil`). The PyPI package declared no dependencies,
  so a clean install such as the Docker image could fail at import.
- Telegram error notifications escape the printer's error text: some HMS
  texts contain `>`, which would make Telegram reject the whole HTML message.
  The printer's name is escaped in the finish, pause and error notifications
  too (a name such as `X1C <test>` had the same effect).

## [2026-09-02]

### Added
- Chamber temperature control on Bambu models with an actively heated chamber
  (H2 series): the chamber chip on the printer card is now editable and sends
  `M141` with presets 40/50/60/65 °C and off. Models without a chamber heater
  (P series, X1C) keep a read-only chip, and the API rejects the command for
  them instead of sending gcode that would do nothing.

### Fixed
- Chamber temperature went stale exactly when the chamber was being heated.
  `device.ctc.info.temp` packs two values as `(target << 16) | current`; with
  the heater off the high bits are zero, so the raw number looked like a plain
  temperature and passed the sanity filter, but with the heater on it was
  discarded and the card kept showing the last value from before. The reading is
  now unpacked, and the target is shown next to it like nozzle and bed.

## [2026-09-01]

### Added
- Print control for **Creality** printers (K1 / Ender WebSocket API): pause,
  resume, and stop straight from the printer card. Actions the Creality
  channel cannot carry (light, speed, fans, AMS) are rejected before they
  reach the printer instead of failing silently.

### Fixed
- A paused Creality printer showed as printing. Ender firmware reports the
  paused state as numeric code `5`, which fell through to `idle` and was then
  promoted back to `printing` by the activity heuristic — so a pause was
  invisible on the card no matter where it came from. Unknown state codes now
  map to `unknown` rather than `idle`, so a future unrecognised code cannot
  hide a pause the same way.

## [2026-08-11]

### Added
- Hover on the AMS humidity and temperature charts: tooltip with time and
  value, plus a marker on the hovered point.

## [2026-08-01]

### Added
- New **Cosmos** theme (void-palette HUD).

### Fixed
- AMS cards were unreadable in the light theme — hardcoded dark colours
  replaced with theme variables.

## [2026-07-23]

### Added
- Firmware-update badge, derived by comparing versions across the fleet.

### Fixed
- Bambu job names now come from `subtask_name`, so cards show the real job
  instead of a placeholder.

## [2026-07-17]

### Fixed
- Reliability and security audit fixes across the collectors, API, and web UI.

## [1.0.0] — 2026-07-14

First public release (AGPL-3.0).

### Added
- Unified live dashboard for a mixed printer fleet: **Bambu Lab** (MQTT),
  **Creality** / **Klipper / Moonraker** (WebSocket), and **MKS WiFi**.
- Per-printer state, progress, ETA, nozzle/bed/chamber temps, current job,
  Wi-Fi signal, and AMS status on one page.
- State-timeline history and state-duration analytics backed by SQLite.
- Telegram notifications (finish / pause / error) with an optional outbound
  proxy pool, all editable from the UI.
- Remote control (pause / resume / stop, temperature) for capability-probed
  firmware.
- JWT cookie auth with `admin` / `viewer` roles, login rate limiting,
  refresh-token rotation, and an audit log.
- Fleet managed entirely from the admin UI — no code edits to add printers.
- Docker / `docker compose` support and systemd + nginx deploy examples.

### Security
- No hardcoded secrets: JWT key resolves env → persisted DB secret →
  auto-generated; Telegram token, proxy list, and printer access codes live in
  the database, never in source.
- Telegram bot fails closed when no `chat_id` is configured (was fail-open).
- Notification templates rendered with `str.replace`, not `str.format`
  (blocks attribute-traversal data leaks via admin-edited templates).
- Exact-match public API paths in the auth middleware (no accidental prefix
  exposure of future routes).
- HTML-escaped proxy URLs in the admin UI (defense-in-depth).
