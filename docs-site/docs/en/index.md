# Kuznitsa

**Kuznitsa** (Russian «Кузница», "the forge") is a dashboard for a 3D-printer farm that you run on
your own computer. One web page shows every printer of the farm — of different brands — with its
state, progress, temperatures, AMS, notifications and history, and lets an administrator control
the printers remotely.

- It works inside your local network: the dashboard talks to each printer directly. No cloud
  account is used.
- It is free and open source (AGPL-3.0); a commercial license is also available — see
  [License](license.md).
- This documentation describes **version 2.0.0** (released 1 November 2026).

!!! note "The interface is in Russian"
    The dashboard's interface is currently in Russian. In this guide every button and screen is
    given in English with the **exact Russian label in brackets**, for example
    *Admin → Fleet* («Админ → Парк»), so you can find it on the screen.

## Supported printers

| Brand | Models offered in the "add printer" form | How the dashboard connects |
| --- | --- | --- |
| **Bambu Lab** | X1, X1 Carbon, X1E, P1P, P1S, P2S, A1, A1 mini, H2D, H2S | MQTT over TLS, port 8883 (8884 is tried too) |
| **Creality** | K1 Max, K1C, Ender-5 Max | WebSocket, port 9999 |
| **Klipper / Moonraker** | any — the model is free text | Moonraker WebSocket, port 7125 by default |
| **MKS WiFi** | any — the model is free text | G-code over TCP, port 8080 by default |

The README describes the Creality support as "K1 series / Sonic Pad". What each brand needs and
where to find it is on [Adding printers](printers.md).

## What it can do

- **One live board** — every printer on one page: state, progress, time left, nozzle / bed /
  chamber temperatures, the current job, Wi-Fi signal, AMS. Cards follow live data every 2 s.
- **Four screens** — Printers («Принтеры»), AMS, History («История»), Admin («Админ»). A viewer
  sees three of them (no Admin). On a phone there is a bottom navigation bar; the dashboard can be
  installed as an app (PWA).
- **Printer notifications with actions** — every Bambu HMS message and print error appears on the
  printer's card, with the same buttons Bambu Studio shows.
- **History** — a print journal (one row per print), a state timeline, statistics and, for
  administrators, a log of every action.
- **Maintenance mode** — take a printer out of the farm's counts, alerts and automation without
  losing its live status.
- **Temperature charts** — nozzle, bed and chamber for 6 h / 24 h / 7 days.
- **Telegram notifications** (optional, off by default) — on finish, pause or error, through a
  proxy if needed.
- **Remote control** for administrators — light, pause / resume, stop, cool down, bed down,
  temperatures, fans, speed; AMS filament load / unload and drying on Bambu. Each button appears
  only where the printer supports it.
- **AMS auto-drying** — dries an AMS 2 Pro / AMS HT when its humidity stays high. It ships in a
  trial mode that only writes a journal.
- **Users and roles** — administrator («Админ») and viewer («Зритель»), with login protection and
  an action log.
- **The fleet is managed from the web page** — add, edit and remove printers without touching
  files.

## Screenshots

All screenshots use a synthetic demo farm — no real printers or jobs.

![Printers — a mixed Bambu / Creality / Klipper / MKS farm on one page](https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/dashboard.png)

*Printers («Принтеры») — every printer on one page: state, progress, time left, temperatures, AMS
slots and humidity, the notification that needs a decision, state chips, the next finishes and the
administrator's control row.*

<p align="center"><img src="https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/mobile.png" width="320" alt="The same board on a phone, with the bottom navigation"></p>

*On a phone — one column, bottom navigation, pop-ups as bottom sheets.*

![History → Prints](https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/history-jobs.png)

*History → Prints («История → Печати») — one row per print: start, duration against the slicer's
plan, result, pauses, layers.*

![History → Statistics](https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/history-stats.png)

*History → Statistics («История → Статистика») — prints, success rate, print hours, load, state
shares per printer, prints per day, most frequent errors.*

![History → Timeline](https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/history-chrono.png)

*History → Timeline («История → Хронология») — every printer's state over the chosen period.*

![AMS — humidity and temperature per unit with auto-drying](https://raw.githubusercontent.com/mihey-web/printer-fleet-dashboard/main/docs/screenshots/ams.png)

*AMS — humidity and temperature of every unit with drying periods, and the auto-drying line on
top.*

## Where to start

1. [Install](install/index.md) the dashboard — Docker, Windows or Linux / Raspberry Pi.
2. [First run](install/first-run.md) — read the administrator password and log in.
3. [Add your printers](printers.md).
4. Optionally set up [Telegram](telegram.md) and [HTTPS](security.md#https-with-nginx).
