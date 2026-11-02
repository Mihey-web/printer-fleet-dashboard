# Interface overview

The dashboard has **four screens**. The tabs are in the top bar on a computer and in the bottom
bar on a phone.

| Screen | What is there | Who sees it |
| --- | --- | --- |
| [Printers](printers.md) («Принтеры») | Every printer's card, the farm row with filters and farm-wide commands, the next finishes | everyone |
| [AMS](ams.md) | Humidity and temperature of every AMS unit, drying, the auto-drying journal | everyone |
| [History](history.md) («История») | Prints, timeline, statistics and (administrators) the action log | everyone |
| [Admin](admin.md) («Админ») | Printer list, automation, users, Telegram | administrators only |

A viewer («Зритель») sees three screens and no control buttons. See
[Users and security](../security.md) for the roles.

## The top bar

- **⚒ Кузница** — the dashboard's name.
- **The screen tabs.**
- **The bell** — farm notifications. It shows a «⚠ N» pill when printers need attention; click it
  for the list. See [Printers screen → Notifications](printers.md#printer-notifications).
- **The live dot** with the time of the last update. If the server stops answering it turns red
  and says how long there has been no connection (e.g. «нет связи · 2 мин», "no connection").
- **👤 and your role** («Админ» or «Зритель») — opens the **This browser** panel.

## The address keeps your place

The current screen, the History sub-tab, its period and printer are part of the page address (for
example `#/history/jobs?period=7d`). A reload, the browser's Back / Forward and a link you send to
someone open the same view.

## "This browser" panel (👤) {#this-browser-panel}

Settings here apply **only to this browser** on this device, for both roles («Этот браузер»):

| Section | What it does |
| --- | --- |
| Your login and **Log out** («Выйти») | |
| **Appearance** («Оформление») | Colour theme: Ocean («Океан»), Classic («Классика»), Light («Светлая»), Night («Ночь»), Neon («Неон»), Cosmos («Космос»). **Text size** («Масштаб текста») 50–150 %. |
| **Browser notifications** («Уведомления браузера») | **Pop-up notifications** («Всплывающие уведомления») with ticks for finish («завершение»), error («ошибка») and pause («пауза»). Off by default; the browser asks for permission. |
| **App** («Приложение») | **Install** («Установить») — appears only when the browser offers to install the dashboard as an app. See [Phone and app](mobile-pwa.md). |
| **Version** («Версия») | The running version. |

!!! info "Browser notifications"
    The pop-ups are shown by the open dashboard page itself: they work only while the dashboard is
    open in a tab and **only over HTTPS** — over plain HTTP the panel says
    «Уведомления не работают через HTTP. Откройте https://…». Printers in maintenance mode never
    send them. To get alerts with the browser closed, use [Telegram](../telegram.md).

## Tips and confirmations

Hover over an icon (or tap it on a phone) to see a tip. Commands that could hurt a print — stop,
cool-down during a pause, loading or unloading filament, ending maintenance, deleting a printer —
ask for confirmation right where you clicked; a question that is not answered disappears by
itself after a few seconds. The printer's answer is shown in the same place: «ждём принтер…»
("waiting for the printer"), then «подтверждено» ("confirmed"), «отправлено, ответа нет» ("sent,
no reply") or the error.
