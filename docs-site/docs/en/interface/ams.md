# AMS screen

The AMS screen shows every AMS unit of every Bambu printer: humidity, temperature and drying. If no
printer has an AMS, the screen says «Нет принтеров с AMS.» ("No printers with AMS").

## The auto-drying strip

The line on top sums up auto-drying, for example «Автосушка: вкл · сушит 1 из 4 · цель ≤ 20 % ·
пробный режим вкл» ("auto-drying on · drying 1 of 4 · goal ≤ 20 % · trial mode on").

Administrators also have:

- **Dry all…** («Сушить все…») — opens **Dry now** («Сушить сейчас»): tick the units and click
  **Start (N)** («Запустить (N)»).
- **Stop all** («Остановить все») — opens **Stop drying** («Остановить сушку»): tick the drying
  units and click **Stop (N)** («Остановить (N)»).
- **Rules ›** («Правила ›») — goes to the auto-drying rules in
  [Admin → Automation](admin.md#ams-auto-drying).

## Period

**Period** («Период»): **6 ч**, **24 ч**, **7 дн**, **30 дн** (6 hours to 30 days) for the charts.

## A unit card

Each card is titled «<printer> · AMS N» (plus «AMS 2 Pro» or «AMS HT» for those models) and shows:

- humidity 💧 and temperature 🌡, the filament type in each slot;
- a chart **Humidity, %** («Влажность, %») — the green zone is the auto-drying goal;
- a chart **Temperature, °C** («Температура, °C») — stripes mark drying periods;
- one **drying line**.

### The drying line

- While a unit is drying it says who started it — auto-drying run N («авто · прогон N»), by hand
  («вручную») or *someone else's drying — leave it alone* («чужая сушка — не трогаем», started from
  the printer, Bambu Studio or Handy) — the temperature and the time left («ещё H:MM»). An
  administrator can press **Stop** («Стоп»), which asks «Остановить сушку?».
- Otherwise it shows what auto-drying is doing for this unit, or why drying is unavailable
  («Сушка недоступна: …»), and an administrator can press **Dry…** («Сушить…») to choose a material
  preset, temperature and hours and **Start drying** («Начать сушку»).

Remote drying works on AMS 2 Pro and AMS HT units. A printer in maintenance mode is shown as «на
обслуживании» and is not dried automatically.

## Drying journal

**Drying journal** («Журнал сушки») at the bottom explains why auto-drying decided what it did:
«RH — до сушки → после остывания» (humidity before drying → after cooling down). Entries are
marked Start («Старт»), Repeat («Повтор»), Finish («Финиш»), Someone else's drying («Чужая
сушка»), Failure («Срыв»), Trial («Пробный») and so on. 5 lines are shown; **All N ›** («Все N ›»)
unfolds up to 50.

## How auto-drying works

The AMS humidity sensor is unreliable while drying: heat lowers the reading by itself, and moisture
from the spools raises it. So auto-drying, like Bambu itself, dries **by time** in a cycle:

1. **Rest:** the unit's humidity stays at or above the start threshold for the set number of
   hours (25 % for 2 h by default).
2. **Run** a drying cycle by time.
3. **Cool down** for at least the set pause (1 h by default).
4. **Measure** humidity again once the unit has cooled almost to its pre-drying temperature.

If the humidity is at or below the goal (20 % by default) the unit is done; if not, another run
follows, up to the set limit (2 in a row by default). A drying stopped by hand pauses auto-drying
for that unit (24 h by default). Auto-drying never stops a drying itself and leaves drying started
elsewhere alone.

!!! note "Out of the box it only writes the journal"
    Auto-drying is switched on in **trial mode** («Пробный режим»): it evaluates the rules and
    writes what it *would* do («пробный режим: запустили бы сейчас») but sends no commands. An
    administrator turns trial mode off in [Admin → Automation](admin.md#ams-auto-drying).
