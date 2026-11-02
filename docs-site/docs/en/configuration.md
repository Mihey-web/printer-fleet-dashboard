# Settings and environment variables

Kuznitsa has two kinds of settings:

1. **Settings on the web page**, stored in the database — the printer list, Telegram, proxies,
   automation. Most things are set here.
2. **Environment variables** — a few start-up settings: the address and port, security, polling,
   how long history is kept. All of them are optional; the dashboard starts with safe defaults.

## How to set an environment variable

| Installed with | How |
| --- | --- |
| Windows | `set NAME=value` in the Command Prompt window, then `python main.py` in the same window — see [Windows](install/windows.md) |
| Linux / systemd | an `Environment=NAME=value` line in the `[Service]` section of the unit — see [Linux / Raspberry Pi](install/linux.md#add-settings-environment-variables) |
| Docker | a `- NAME=value` line under `environment:` in `docker-compose.yml` — see [Docker](install/docker.md#settings) |

!!! info "About `.env.example`"
    The repository has `.env.example` with the main variables and comments. The program itself does
    **not** read a `.env` file; use it as a reference, or let your service manager load it (for
    example systemd's `EnvironmentFile=` or Docker Compose's `env_file:`).

Restart the dashboard after changing a variable.

## All environment variables

Links in the last column point to the line of code (version 2.0.0) where the variable is read.

### Web server and security

| Variable | Default | What it does | Read in |
| --- | --- | --- | --- |
| `WEB_HOST` | `127.0.0.1` | The address the dashboard listens on. `127.0.0.1` — only this computer (use behind nginx or a tunnel). `0.0.0.0` — every network interface, so other devices can open it. The Docker image sets `0.0.0.0`. | [config.py:7](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L7), [main.py:52](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L52) |
| `WEB_PORT` | `8000` | The port. Must be a whole number. | [config.py:8](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L8), [main.py:53](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L53) |
| `COOKIE_SECURE` | `1` | `1` — login cookies are sent only over HTTPS (right behind an HTTPS proxy). `0`, `false`, `False` or an empty value — also over plain HTTP; needed if you open the dashboard by `http://` from another device. The Docker files set `0`. | [config.py:41](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L41) |
| `FORGE_JWT_SECRET` | generated on the first start and stored in `users.db` | The key that signs login sessions. Set it to choose the key yourself; changing it logs everybody out. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. | [config.py:34](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L34), [app/api/main.py:818](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/api/main.py#L818), [app/api/auth.py:178](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/api/auth.py#L178) |
| `FORGE_ADMIN_USERNAME` | `admin` | Login of the administrator created on the first start. Used only while there are no users. | [config.py:46](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L46) |
| `FORGE_ADMIN_PASSWORD` | empty — a random password is printed to the log once | Password of that first administrator. Used only while there are no users. | [config.py:47](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L47) |

### Polling the printers

| Variable | Default | What it does | Read in |
| --- | --- | --- | --- |
| `POLL_INTERVAL` | `5`, but **never less than 30 s** | Seconds between full polls of all printers (history, print journal, notifications). Values under 30 are raised to 30, so the effective default is 30 s. | [config.py:3](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L3), [main.py:49](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L49) |
| `MAX_WORKERS` | `10` | How many printers are polled at the same time. The dashboard always uses at least as many workers as there are printers. | [config.py:4](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L4), [main.py:50](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L50) |
| `FORGE_LIVE_REFRESH_S` | `2` | Seconds between live card updates from the printers' own data, between the full polls. `0` turns it off — cards then change only on each full poll. | [app/services/printer_commands.py:1339](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/printer_commands.py#L1339) |
| `FORGE_BAMBU_TLS_VERIFY` | empty (off) | Check each Bambu printer's TLS certificate against Bambu's certificates bundled with the dashboard. Empty — the connection is encrypted but the certificate is not checked. `*` — every Bambu printer. Or a comma list of models as chosen in the form, e.g. `X1C,P2S`. A model whose certificate is not covered cannot connect (the log shows `SSLCertVerificationError`) and is retried every second, so enable models one at a time. Needs a correct system clock. | [config.py:13](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L13), [app/collectors/bambu_collector.py:53](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/collectors/bambu_collector.py#L53), [main.py:77](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L77) |

### History and automation

These three are not in `.env.example` but are read by the code.

| Variable | Default | What it does | Read in |
| --- | --- | --- | --- |
| `HISTORY_RETENTION_DAYS` | `90` | Days to keep the state history (timeline, statistics), printer notification records and the AMS history. | [app/services/state_store.py:21](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/state_store.py#L21), [app/services/notification_tracker.py:37](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/notification_tracker.py#L37), [app/services/ams_store.py:21](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/ams_store.py#L21) |
| `JOBS_RETENTION_DAYS` | `730` | Days to keep finished prints in the print journal (at least 1). | [app/services/job_journal.py:939](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/job_journal.py#L939) |
| `FORGE_TZ_OFFSET_MIN` | `180` | The farm's time zone for the auto-drying start window, in minutes from UTC (`180` = UTC+3, Moscow). For example `120` for UTC+2, `-300` for UTC−5. | [app/services/ams_autodry_engine.py:55](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/ams_autodry_engine.py#L55) |

## Settings on the web page

These live in `printer_history.db` and are changed only on the web page (by an administrator):

| What | Where |
| --- | --- |
| Printers: names, addresses, access codes, serial numbers | [Admin → Fleet](printers.md) |
| Creality light control | the *Allow light control* switch in the Creality printer form |
| AMS auto-drying rules | [Admin → Automation](interface/admin.md#ams-auto-drying) |
| Error codes for auto-skip | [Admin → Automation](interface/admin.md#auto-skip-errors) |
| Hidden notifications | the **Hide** / **Restore** buttons on printer notifications |
| Users | [Admin → Access](security.md#users-and-roles) (stored in `users.db`) |
| Telegram: token, chat, interval, events, templates | [Admin → System](telegram.md) |
| Proxies for Telegram and their check interval | [Admin → System](telegram.md#6-proxy-for-telegram) |

Telegram and proxy changes apply at once. Changes to the printer list need **Apply and restart**.

Settings in **👤 This browser** (theme, text size, browser notifications) are kept only in that
browser.
