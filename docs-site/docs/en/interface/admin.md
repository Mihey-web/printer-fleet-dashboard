# Admin screen («Админ»)

Only administrators see this screen. It is the only place where settings for everyone are
changed. It has four sub-tabs: **Fleet** («Парк»), **Automation** («Автоматика»), **Access**
(«Доступ») and **System** («Система»).

## Fleet («Парк»)

The printer list — adding, editing and removing printers, and **Apply and restart**
(«Применить и перезапустить»). Described in detail on [Adding printers](../printers.md).

## Automation («Автоматика»)

### AMS auto-drying {#ams-auto-drying}

**AMS auto-drying** («Автосушка AMS») — the rules live only here; **Dry all** and **Stop all** are
on the [AMS screen](ams.md).

| Setting | Meaning | Default |
| --- | --- | --- |
| **Auto-drying on** («Автосушка включена») | Watches the AMS units and starts drying by the rules. Turning it off does not stop runs already going. | on |
| **Trial mode** («Пробный режим») | On: auto-drying only writes the journal and sends nothing to printers. Off: it starts drying by itself. Use **Turn off…** («Выключить…») / **Turn on** («Включить»). | on |
| **Keep humidity at or below** («Держать влажность не выше») | The goal, %. | 20 % |
| **Repeat runs** («Повторять прогоны») | If it is still too humid after a run: until the goal is reached («пока не дойдёт до нужной»), one run, or at most 2 / 3 / 4 / 6 in a row. | at most 2 in a row |
| **Default mode** («Режим по умолчанию») | **Auto by filament** («Авто по пластику» — for a mix, the temperature of the most delicate filament and the time of the longest), **PLA 45° / 12 ч**, **PETG 65° / 12 ч**, **TPU 65° / 12 ч**, or **Custom…** («Своё…», 45–65 °C, 1–24 h). The AMS 2 Pro maximum is 65 °C. | Auto |
| **Dry while printing** («Сушить во время печати») | The firmware lowers the temperature for a printing AMS by itself (PETG → 55°, PLA → 45°). | on |

Under **More** («Дополнительно»):

| Setting | Meaning | Default |
| --- | --- | --- |
| **Ignore spikes** («Не дёргаться от скачков») | Start only if it has been too humid for longer than this (opening the lid makes humidity jump and fall back). | 2 h |
| **Pause between runs** («Пауза между прогонами») | Cooling down, then measuring. | 1 h |
| **Start window** («Окно запуска») | Any time («Любое время»), at night 22:00–08:00 («Ночью»), or custom. Outside the window a start waits; a running cycle is not interrupted. Farm time — Moscow time (UTC+3) unless `FORGE_TZ_OFFSET_MIN` says otherwise. | any time |
| **After a manual stop** («После ручной остановки») | If drying was stopped in Handy, Bambu Studio or here, auto-drying leaves that unit alone for this many hours. | 24 h |

With a limit on runs, a series that did not reach the goal is followed by the next one no sooner
than 48 h later; drying started by someone else counts too. Without a limit it dries until the goal
is reached.

**Units** («Блоки») — a table with each unit's **Auto** («Авто») switch and **Mode** («Режим»),
so you can exclude a unit or give it its own mode.

**How it works** («Как это работает») unfolds the explanation of the drying cycle — see
[AMS screen](ams.md#how-auto-drying-works).

Changes are saved with **Save** («Сохранить»); **Discard edits** («Отменить правки») throws them
away. Unsaved changes are marked «Есть несохранённые изменения».

### Auto-skip errors {#auto-skip-errors}

**Auto-skip errors** («Автопропуск ошибок») presses **Ignore and resume** («Игнорировать и
продолжить») on **every Bambu printer** when a print pauses with one of the listed error codes —
the same thing Bambu Studio sends for that dialog button. It is meant for build plates with a worn
marker.

- Out of the box the list holds **0500_8062** and **0C00_8009** — "build-plate marker not detected".
  **These two are active from the first start.** Remove them if you do not want this.
- Add a code in the form `0500_8062` with **+ код**, remove one from the list, then **Save**
  («Сохранить»). Up to 32 codes. Each code is shown with Bambu's text.
- For each pause it tries again after 2 minutes if the pause is still there, at most 3 times.
  Printers in maintenance are skipped.
- **Triggers ›** («Срабатывания ›») opens History → Actions, group Automation, where every press
  (and every pause that was not waved through, with the reason) is logged.

## Access («Доступ»)

**Users** («Пользователи») — accounts and roles. See
[Users and security](../security.md#users-and-roles).

## System («Система»)

**Telegram** — the bot, its notifications and proxies. See [Telegram and proxy](../telegram.md).
