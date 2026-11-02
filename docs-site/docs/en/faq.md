# FAQ and troubleshooting

## Where is the log?

Most answers start with the log.

| Installed with | Command / place |
| --- | --- |
| Windows | the Command Prompt window running `python main.py` |
| Linux / systemd | `sudo journalctl -u bambudiagnostic.service -f` |
| Docker | `docker compose logs -f` |

## Login and access

### After logging in, the page asks for the password again {#login-loops}

The browser dropped the login cookie. By default (`COOKIE_SECURE=1`) the cookie is sent only over
HTTPS. If you open the dashboard by `http://<address>:8000` from another device, either:

- set `COOKIE_SECURE=0` (only in a trusted local network), or
- set up [HTTPS with nginx](security.md#https-with-nginx) and keep `COOKIE_SECURE=1`.

The Docker files already set `COOKIE_SECURE=0`.

### The dashboard does not open from my phone or another computer

By default it listens only on the computer it runs on (`WEB_HOST=127.0.0.1`). Set
`WEB_HOST=0.0.0.0` (plus `COOKIE_SECURE=0` for plain HTTP), or put nginx in front of it. Also check
the computer's firewall for port 8000.

### I lost the first-run password

Look in the log again — it is printed on the first start (see the table above). If the log is gone
and there is no other administrator, there is no reset button. The administrator is created only
when the user database is empty, so you can:

1. stop the dashboard;
2. move `users.db` out of the project folder (keep it as a copy);
3. start the dashboard — a new `admin` is created and its password printed to the log.

This removes **all** user accounts, the action log and the session key (everyone is logged out).
The printers, settings and history are in `printer_history.db` and are not affected.

### Login says `Too many login attempts. Try again later.`

Too many wrong passwords within 15 minutes (5 from one address, or 10 for one login). Wait 15
minutes. See [Sessions and login protection](security.md#sessions-and-login-protection).

### Some messages are in English

The interface is in Russian, but some messages come straight from the server in English — on the
login page and in some admin forms (for example `Password must be at least 8 characters`).

## Printers

### A printer I added does not appear

Changes to the printer list take effect only after **Apply and restart** («Применить и
перезапустить») in *Admin → Fleet*. Until then the yellow bar «Есть непримененные изменения»
stays.

### After "Apply and restart" the dashboard is gone (Windows)

The button makes the program exit so that a service manager restarts it. On Windows there is none —
run `python main.py` again. If the page says «Сервис не поднялся за 90 сек — проверь вручную»
("the service did not come up in 90 s — check by hand") on Linux or Docker, look at the log.

### A printer is "Offline" («Офлайн»)

- Can the dashboard's computer reach the printer's IP? Check the port: Bambu 8883, Creality 9999,
  Moonraker 7125, MKS WiFi 8080 (or what you entered).
- **Bambu:** is the printer in LAN Mode, and are the access code and serial number right? The log
  shows connection errors.
- **Klipper:** the form has no API key field, so Moonraker must accept the dashboard's computer
  without a key.
- Short drop-outs are hidden: after a failed poll the card keeps the last data for about
  3 minutes before it turns offline.

### A Bambu card has only the light button and a lock 🔒

The printer refuses unsigned control commands. The lock's tip gives the reason; «прошивка
блокирует (нет Developer Mode)» means new firmware accepts such commands only in Developer Mode.
Monitoring keeps working. See [Adding printers → Bambu Lab](printers.md#bambu-lab).

### There are no control buttons at all

- You are logged in as a viewer («Зритель») — only administrators see controls.
- The printer is offline.
- It is an MKS WiFi printer — those are monitored only.

### My Bambu printers resume by themselves after a "build plate marker" error

That is **auto-skip errors**: out of the box the dashboard presses *Ignore and resume* for codes
0500_8062 and 0C00_8009 on every Bambu printer. Remove the codes in
[Admin → Automation](interface/admin.md#auto-skip-errors) if you do not want it.

## Notifications

### Telegram is silent

Fill in the **Allowed chat ID** — in version 2.0.0 the bot ignores every chat when it is empty.
See [Telegram → If the bot is silent](telegram.md#if-the-bot-is-silent).

### Browser notifications do not work

They work only over HTTPS and only while the dashboard is open in a browser tab, and you must allow
them when the browser asks. See [Interface overview](interface/index.md#this-browser-panel).

### I cannot install the dashboard as an app

The browser offers installation only on an HTTPS address. See
[Phone and app](interface/mobile-pwa.md#installing-as-an-app-pwa).

## AMS

### Auto-drying never starts drying

- Out of the box it is in **trial mode** and only writes the journal. Turn trial mode off in
  [Admin → Automation](interface/admin.md#ams-auto-drying).
- Check the **Start window** and the unit's **Auto** switch.
- Remote drying is for AMS 2 Pro and AMS HT; printers in maintenance are skipped.
- The **Drying journal** on the AMS screen says why it decided so.

### The auto-drying night window uses the wrong hours

The farm time is Moscow time (UTC+3) unless you set `FORGE_TZ_OFFSET_MIN` — see
[Settings](configuration.md#history-and-automation).

### The AMS history disappeared after a Docker update

`docker-compose.yml` does not keep `ams_history.db` outside the container. See
[Docker](install/docker.md#settings) for the extra line to add.

## Data

### How do I back up or move the dashboard?

Copy `printer_history.db`, `users.db` and `ams_history.db` from the project folder while the
dashboard is stopped, and put them into the new installation's folder. On Linux there is a ready
[nightly backup](install/linux.md#nightly-database-backup-optional).

### The page does not load when I start `main.py` from another folder

The program loads its web files from the `app/web` folder relative to the current folder. Always
start it from the project folder (`cd kuznitsa` first). The systemd unit and Docker already do.
