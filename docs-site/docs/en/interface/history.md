# History screen («История»)

History has four sub-tabs: **Prints** («Печати»), **Timeline** («Хронология»), **Statistics**
(«Статистика») and, for administrators, **Actions** («Действия»).

## Filters for every sub-tab

One head is shared by all sub-tabs and stays when you switch between them:

- **Period** («Период»): **24 ч**, **7 дн**, **30 дн**, **90 дн** or **Custom…** («Свой…» — pick
  the dates **from** («С») and **to** («по») and click **Apply** («Применить»)).
- **Printer** («Принтер»): all («Все») or one printer.
- **Export** («Выгрузить», administrators only) — downloads the list for the period with its
  filters as a CSV file for Excel. Available on Prints and Actions.

On a phone the filters fold into a summary line such as «24 ч · все принтеры»; tap it to change
them.

## Prints («Печати») — the print journal

One row per print. Columns: **Printer** («Принтер»), **Start** («Начало»), **Duration (plan, Δ)**
(«Длительность (план, Δ)» — actual time against the slicer's estimate), **Result** («Результат»),
**Pauses** («Паузы»), **Layers** («Слои»), **Job** («Задание»).

The result chips above the table show the totals and filter the list: All («Все»), Succeeded
(«Успешно»), Cancelled («Отменено»), Error («Ошибка»), Power («Питание» — power loss), No
connection («Нет связи»), Stopped («Остановлено»), Running («Идут»).

Click a row to unfold every pause with its time and reason, the automatic error-skip presses and
how the print ended. 50 rows are shown at a time — **Show 50 more** («Показать ещё 50»). The
journal was rebuilt from the earlier state history when version 2.0.0 first started.

## Timeline («Хронология»)

**State timeline** («Хронология состояний») — every printer's state over time as coloured
segments. It shows up to 7 days: for a longer period it shows the last 7 days and says so. The
**?** button explains the controls:

- computer: mouse wheel — zoom at the pointer, drag — move, double-click — reset, hover — details;
- phone: pinch — zoom, swipe sideways — move, tap — details, double-tap — reset.

Maintenance time is drawn as a hatched «обслуживание» segment.

## Statistics («Статистика»)

- The totals: **Prints** («Печатей»), **Succeeded** («Успешно»), **Print hours** («Часы печати»),
  **Load** («Загрузка»), **Actual / plan** («Факт / план»).
- **By printer** («По принтерам»): time shares of each state, printing time, prints, success rate,
  actual vs plan and the most frequent reason.
- **Prints per day** («Печатей в день»).
- **Frequent reasons** («Частые причины») — switch between **Errors** («Ошибки») and **Pauses**
  («Паузы»).

## Actions («Действия», administrators) {#actions-administrators}

The log of who did what — people and the automation. The chips are groups with counts: All
(«Все»), Commands («Команды»), Automation («Автоматика»), Fleet and settings («Парк и настройки»),
Access and logins («Доступ и входы»). **Who** («Кто») filters by user. Technical rows (session
token refreshes) are hidden by default — the **Служебные · скрыты** chip shows them.

Columns: **Time** («Время»), **Who** («Кто»), **What** («Что»), **Printer** («Принтер»), **Device**
(«Устройство»). Click a row to see the IP address, the browser and the raw record.

## How long history is kept

| Data | Kept for | Variable |
| --- | --- | --- |
| State history, timeline, notifications, AMS history | 90 days | `HISTORY_RETENTION_DAYS` |
| Finished prints in the journal | 730 days | `JOBS_RETENTION_DAYS` |

See [Settings and environment variables](../configuration.md).
