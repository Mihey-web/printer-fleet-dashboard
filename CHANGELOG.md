# Changelog

All notable changes to this project are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

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
