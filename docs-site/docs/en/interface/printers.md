# Printers screen («Принтеры»)

The main screen: one card per printer and, above them, the farm row.

## The farm row

### State filters

The chips **Printing** («Печатает»), **Paused** («Пауза»), **Error** («Ошибка»), **Finished**
(«Завершено»), **Cancelled** («Отменено»), **Idle** («Простой»), **Offline** («Офлайн») and
**Maintenance** («Обслуживание», «Обсл.» on a phone) show how many printers are in each state.
They are also filters:

- click a chip — only those printers are shown;
- click it again — all printers;
- ++ctrl++ + click — add a state to the selection.

The choice is remembered in this browser.

### Order

**Order** («Порядок»): **Name** («Имя» — by name, P2 before P10) or **Finish** («Финиш» — printing
printers by time left, the rest by name). A printer that needs a decision is always first.

### Farm commands (administrators)

- **Light:** **on** / **off** («Свет: вкл / выкл») — on every online printer that has a controllable
  light.
- **Pause all…** («Пауза всех…») — opens a dialog where you tick which printing printers to pause,
  then **Pause selected (N)** («Пауза выбранных (N)»).
- **Resume N** («Продолжить N») — appears only when something is paused; you choose which to
  resume, then **Resume selected (N)** («Продолжить выбранные (N)»).

If some printers did not confirm, a line says so with a **Retry** («Повторить») button. Printers in
maintenance mode are skipped. On a phone these controls are in the **Farm ▾** («Ферма ▾») sheet.

### Finishes

Under the farm row: **Ready:** («Готовы:») — finished printers whose plate is waiting to be taken
off (or «нет — снимать нечего», "none — nothing to take off"), and **Next:** («Дальше:») — the next
finish times. The **Window 12 h ▾** («Окно 12 ч ▾») button unfolds a timeline of upcoming finishes
with a **Horizon** («Горизонт») of **12 h** or **24 h**.

Printers in maintenance and printers offline for a day or more are folded into **Not working · N**
(«Не в работе · N»).

## A printer card

A card shows the name and model, the state, progress, time left, the current job, temperature
chips (nozzle, bed, chamber), AMS slots and humidity, speed and fans where the printer reports
them, and the Wi-Fi signal as an icon (dBm in its tip).

### Temperature charts

Click a temperature chip to see a chart of nozzle, bed or chamber temperature for **6ч / 24ч / 7д**
(6 h / 24 h / 7 days): the current temperature, the target as a dashed line and min / average /
max. For administrators the same pop-up has the setter on top: preset buttons (nozzle: PLA 220,
PETG 250, ABS 260, Off («Выкл»); bed: 60 / 80 / 100 / Off; chamber: 40 / 50 / 60 / 65 / Off — only
on printers with an actively heated chamber, H2 series) and **OK**.

Administrators also have:

- **Speed** chip — **Print speed** («Скорость печати»): Silent · 50 % («Тихий»), Standard · 100 %
  («Стандарт»), Sport · 124 % («Спорт»), Ludicrous · 166 % («Безумный»). On Creality only while
  printing.
- **Fans** — **Fans, %** («Вентиляторы, %»): sliders Part («Деталь»), Auxiliary («Вспом.»), Chamber
  («Камера») and **Apply** («Применить»).
- **AMS** chip on Bambu — choose a slot and **Load** («Загрузить»), **Unload** («Выгрузить»),
  **Dry…** («Сушить…») or **Stop drying** («Остановить сушку»). Load and unload work only on an
  idle printer and ask for confirmation, explaining what the printer will do. A printer without AMS
  shows «внешняя катушка» (external spool) with **Unload filament** («Выгрузить филамент»).

Viewers see the same chips, with charts only.

### The control row (administrators)

Online printers have a row of buttons under the card. A button appears only if this printer can do
it (see [Adding printers](../printers.md) for each brand):

| Button (tip) | What it does | Asks first? |
| --- | --- | --- |
| 💡 Light («Свет горит — выключить» / «Свет выключен — включить») | Toggles the light | no |
| ⏸ Pause («Пауза») / ▶ Resume («Продолжить печать») | Pauses or resumes the print | no |
| ⏹ Stop («Остановить печать») | Stops the print | yes — «Остановить печать?» → **Стоп** |
| ❄ Cool down («Остудить всё: сопло и стол», plus chamber where it can be set) | Sets heaters to 0 | only when paused — «На паузе — остудить?» |
| ⤓ Bed down («Опустить стол на 3 см, чтобы снять деталь») | Lowers the bed 3 cm to remove the part | no |

A button that cannot be used right now is greyed out and its tip says why — for example «Во время
печати остудить нельзя — сначала пауза или стоп» ("cannot cool down while printing — pause or stop
first"). The server refuses commands that the printer's current state forbids.

A **🔒 lock** means the light works but print control does not; its tip gives the reason (for
Bambu, usually «прошивка блокирует (нет Developer Mode)» — see
[Adding printers](../printers.md#bambu-lab)). MKS printers have no control row.

### The ⋯ menu

The **⋯** in the card's head has, for everyone, links to this printer's prints («Печати принтера ›»)
and timeline («Хронология принтера ›»). Administrators also get the printer's settings
(«Параметры принтера ›», opens the form in *Admin → Fleet*) and maintenance mode.

## Maintenance mode {#maintenance-mode}

Use it when a printer is being repaired or serviced. Administrators switch it from the card's
**⋯** menu:

- **In maintenance…** («На обслуживании…») — optional note («что делаем», up to 200 characters),
  then **Set** («Поставить»).
- While in maintenance: **Maintenance note…** («Заметка обслуживания…») and **✓ End maintenance**
  («✓ Снять с обслуживания»), which asks for confirmation.

The card keeps showing the real state, with a line like «обслуживание · с 14:32 · <note>». The
printer is left out of the state counts, load, finishes, notifications (badge, bell, browser
pop-ups, Telegram), farm commands, AMS auto-drying and the automatic error skipping; on the
timeline its time shows as a hatched «обслуживание» segment. Commands from its own card still
work.

## Printer notifications {#printer-notifications}

Every Bambu HMS message and print error is listed on the printer's card; Creality shows its error
code, and Klipper the reason it stopped.

- **The badge** «⚠ N» in the colour of the worst level appears when a decision is needed or a
  critical error is less than 30 minutes old; otherwise a grey «ⓘ N». Click it for the list.
- **The strip** under the progress appears for a critical or pause error while the printer is
  paused or in error. It ends with **Resolve ›** («Решить ›») for an administrator or **Details ›**
  («Подробнее ›»).
- **Levels:** Critical («Критично»), Pause error («Пауза-ошибка»), Serious («Серьёзно»), Notice
  («Уведомление»), Info («Инфо»). Maintenance reminders are folded as «плановые» ("scheduled").
- **Action buttons** are the ones Bambu Studio shows for that code (taken from Bambu's own tables),
  for example «Продолжить печать», «Игнорировать и продолжить», «Остановить печать». Only
  administrators see them. Each action is checked against the live printer right before it is sent;
  dangerous ones ask first (e.g. «Остановить печать? Задание будет потеряно.»).
- **Hide** («Скрыть», administrators) — the notification stops counting in the badge, the bell, the
  tab title and browser pop-ups, for everyone. Hidden ones are kept under «скрытые · N» with
  **Restore** («Вернуть»). Nothing is sent to the printer.

**The bell** in the top bar lists the printers that need a decision («Нужно решение») first and
the rest folded. At its foot administrators have **Clear errors (N)** («Сбросить ошибки (N)»): it
closes the "task cancelled" dialogs on printer screens and first shows which printers will
(«Попадут») and will not («Не попадут») be affected.

## The tab title

The browser tab shows the farm at a glance: «(N⚠) P% · Кузница», and «нет связи · Кузница» when the
server has not answered for 30 seconds. The tab icon shows a progress ring.
