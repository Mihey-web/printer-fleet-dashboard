# Changelog

All notable changes to this project are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

Releases after `1.0.0` are dated sync points rather than semver bumps; they
match the `Sync to internal v<date>` commits in this repository's history.

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
