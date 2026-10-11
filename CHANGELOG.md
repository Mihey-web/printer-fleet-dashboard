# Changelog

All notable changes to this project are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

Releases after `1.0.0` are dated sync points rather than semver bumps; they
match the `Sync to internal v<date>` commits in this repository's history.

## [Unreleased]

### Changed
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

### Added
- Russian labels for Bambu print stages 36–77 (H2 / P2 series).
- AMS HT units (ids 128+) on the card and in the AMS history.
- Opt-in TLS certificate verification for Bambu printers against Bambu's CAs:
  `FORGE_BAMBU_TLS_VERIFY` (`*` or a comma list of models; off by default).

### Fixed
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
