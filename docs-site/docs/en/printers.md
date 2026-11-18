# Adding printers

The list of printers (the "fleet") is kept in the dashboard's database and edited on the web page.
You do not edit any files to add, change or remove a printer. Only an administrator can do it.

## How to add a printer

1. Open **Admin → Fleet** («Админ → Парк»).
2. Click **+ Add printer** («+ Добавить принтер»).
3. At the top of the form pick the type: **Bambu Lab**, **Creality**, **Klipper** or **MKS WiFi**.
   The form shows only the fields that type needs (see below).
4. Click **Save** («Сохранить»).
5. A bar appears: **There are unapplied changes — N** («Есть непримененные изменения — N»). Until
   you apply them, the dashboard keeps polling the old list. Click **Apply and restart**
   («Применить и перезапустить») and confirm with **Apply** («Применить»). The service restarts —
   the dashboard is unavailable for about 10 seconds and then shows *Service restarted,
   configuration applied* («Сервис перезапущен, конфигурация применена»).
   **Discard edits** («Отменить правки») throws the unsaved list changes away instead.

!!! warning "Windows"
    *Apply and restart* makes the program exit and relies on systemd or Docker to start it again.
    If you run `python main.py` by hand on Windows, start it again yourself after pressing the
    button. See [Windows](install/windows.md#apply-and-restart-stops-the-program-on-windows).

The same table has **Edit** («Изменить») and **Delete** («Удалить») on each row. A deleted printer
keeps its print history; until you apply the changes it can be brought back with **Restore**
(«Вернуть»). The history is tied to an internal ID, so renaming a printer does not lose it.

The server checks the form: the **Name** and **IP address** must not be empty, a port must be
1–65535, and a Bambu printer needs the access code and the serial number.

## Bambu Lab

| Field | What to enter |
| --- | --- |
| **Name** («Название») | Any name you like, e.g. `P1S-1`. |
| **Model** («Модель») | Pick from the list: X1, X1 Carbon, X1E, P1P, P1S, P2S, A1, A1 mini, A2L, H2C, H2D, H2D Pro, H2S, X2D. |
| **IP address** («IP-адрес») | The printer's address in your network. |
| **Access code** («Код доступа») | The code from the printer's screen. The hint under the field says: *printer screen → Settings → Network* («Экран принтера → Настройки → Сеть»). The 👁 button shows what you typed. |
| **Serial number** («Серийный номер») | The printer's serial number. |

What the printer needs:

- **LAN Mode.** The README requires the printer to be in LAN Mode. The dashboard talks to the
  printer's own MQTT server over TLS (port 8883; 8884 is tried as well) with the access code. No
  Bambu cloud account is used.
- **Control may need Developer Mode.** Monitoring works with the access code alone. For pause,
  resume, stop, cool-down, bed-down, temperatures, fans, speed and AMS commands the printer must
  accept commands that are not signed by Bambu. The dashboard checks this by itself; new firmware
  accepts such commands only in Developer Mode. If the printer refuses them, the card shows only
  the light button and a lock with the reason *firmware blocks (no Developer Mode)* («прошивка
  блокирует (нет Developer Mode)»). While the check is still running the reason reads «доступ к
  управлению ещё проверяется» ("access to control is still being checked").
- Optional: the dashboard can verify the printer's TLS certificate — see
  `FORGE_BAMBU_TLS_VERIFY` in [Settings](configuration.md).

Out of the box Kuznitsa automatically presses *Ignore and resume* on two Bambu print errors
(build-plate marker not detected). See [Admin → Automation](interface/admin.md#auto-skip-errors).

What depends on the Bambu model:

| | X1 | P1, A1 | P2S, H2 series, X2D |
| --- | --- | --- | --- |
| [Job card](interface/printers.md#where-the-job-card-comes-from) | from the printer's memory; print settings when the job's file is on the SD card | from the SD card | from the printer's memory; print settings when the job's file is on a USB stick |
| [Files on the printer](interface/printers.md#files-on-the-printer) | listed and started | SD card: listed and started | internal storage listed but started only from the printer's screen; a USB stick listed and started. The H2S keeps its internal storage closed — only a USB stick |
| [Shared files](interface/shared-files.md) | source and target (needs an SD card) | source and target (needs an SD card) | source and target only through a USB stick |
| Air duct mode | — | — | P2S and H2 series ([fans pop-up](interface/printers.md#air-duct)) |
| Chamber heater | — | — | H2 series |

Starting a file needs Developer Mode like every other print command. Printers with two nozzles
(H2D, H2C, X2D) cannot start files from the panel yet, nor load / unload AMS filament. To
[skip parts mid-print](interface/printers.md#skip-parts) the job must be sliced with object
labelling on.

## Creality

| Field | What to enter |
| --- | --- |
| **Name** | Any name. |
| **Model** | Select K1, K1 Max, K1C, K1 SE, K2, K2 Plus, K2 Pro, K2 SE, Ender-3 V3, Ender-3 V3 KE, Ender-3 V3 Plus, Ender-5 Max, CR-10 SE, Hi, SPARKX i7 or enter a model name. Monitoring requires compatible telemetry on port 9999. |
| **IP address** | The printer's address in your network. |
| **Allow light control** («Разрешить управление светом») | On by default. Turn it off for a printer without a controllable lamp: its 💡 button and the farm's light command skip it. This switch is saved at once, without a restart. |

The dashboard connects to the printer's WebSocket on port **9999**; no password or code is needed.
It uses a standalone client copied from [ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws)
(AGPL-3.0, in `app/vendor/ha_creality_ws`). On Creality the card offers:

- pause / resume / stop, light, cool-down, speed modes and fans;
- nozzle and bed temperatures up to the model's limits: 350 / 120 °C on the K2 Plus, 300 / 100 °C
  on the K1, K1C, K1 SE, K1 Max and Ender-5 Max; other models up to what the printer reports;
- the chamber temperature where the printer reports it, and the chamber heater on a K2 Plus /
  K2 Pro that reports a chamber target;
- **bed down** on the K1, K1C, K1 Max, K2 Plus and Ender-5 Max, once the axes are homed («Сначала
  выполните «Домой»» otherwise);
- **home** and **axis jog** ([move pop-up](interface/printers.md#motion)) with the firmware's own
  commands, the ones Creality Print sends. Filament push / retract is not available on Creality —
  use the printer's screen;
- the **CFS**: slots with material and remaining percent, humidity and temperature, load / unload,
  tag re-read, setting a spool by hand and the backup-spool setting
  ([Creality CFS](interface/printers.md#creality-cfs));
- **files on the printer** with a print start ([Files on the printer](interface/printers.md#files-on-the-printer))
  and the [job card](interface/printers.md#where-the-job-card-comes-from) from the printer's file
  list;
- **skipping parts** mid-print where the printer reports the job's parts (K2 Plus, K1C, Ender-5 Max)
  — see [Skipping parts](interface/printers.md#skip-parts);
- the error code in a notification with **Clear the error** («Сбросить ошибку») or **Restart the
  firmware** («Перезапустить прошивку») — see
  [Printer notifications](interface/printers.md#printer-notifications).

Creality does not confirm most commands, so their result says "sent" («отправлено»), not
"confirmed" («подтверждено»). The light is the exception: it is confirmed once the printer reports
the lamp's new state.

### K2 Plus: Klipper's API {#k2-plus}

Some Creality printers, such as the K2 Plus, also run Klipper's API (Moonraker) on port **7125**.
The dashboard finds it by itself; there is nothing to set in the form, but Moonraker has to accept
connections from the dashboard's computer without an API key. The K1C and the Ender-5 Max have no
such API. Through it:

- **files on the printer** are listed with their pictures, times and grams, can be deleted
  (**Delete from the printer**, «Удалить с принтера») and are a source and a target of
  [shared files by model](interface/shared-files.md) without a USB stick;
- the **job card** also gets the print settings from the job's G-code: layer height, infill, walls,
  supports and their weight, bed, filament profiles and the slicer's version;
- a paused print's notification gets **Resume print** («Продолжить печать»);
- **⋯ → Service ›** («Сервис ›») runs the printer's calibrations and a firmware restart — see
  [Service](interface/printers.md#k2-service).

### Ender-5 Max {#ender-5-max}

- **Weight from the length.** Its file list gives the filament's length, not its weight. The
  dashboard works the weight out: the length × the section of 1.75 mm filament × the material's
  density (Creality Print's own: PLA 1.24, PETG 1.27, ABS 1.04, ASA 1.07, TPU 1.21, PC 1.20 g/cm³).
  The material comes from the file name as Creality Print writes it, for example
  `Кронштейн_PETG_2h5m.gcode`; a name without one counts as PETG. So the weight on the card and in
  [History → Filament](interface/history.md#plastic) is close to the slicer's, not exact.
- **Job card:** the picture and that weight only — its file list has no print settings.
- **Fans:** part and case fans are only on or off; it has no auxiliary fan.
- **Bed down** needs homed axes; **Home** uses the firmware's own homing.

## Klipper / Moonraker

| Field | What to enter |
| --- | --- |
| **Name** | Any name. |
| **Model (optional)** («Модель (не обязательно)») | Free text, may be empty. |
| **IP address** | The address of the computer running Moonraker (the printer's host). |
| **Port** («Порт») | Moonraker's port. Leave empty for the default **7125**. |

The dashboard connects to `ws://<IP>:<port>/websocket`. The form has no field for an API key, so
Moonraker has to accept connections from the dashboard's computer without one.

On Klipper the card offers pause / resume / cancel and the [move pop-up](interface/printers.md#motion)
(home, axis jog, filament push / retract) once Klipper reports it is ready, and a light
button if the printer has a light: the dashboard looks for an `output_pin` whose name says
`caselight` / `case_light`, then a name with the word `light`, then `led`, then `lamp`. When Klipper
shuts down (for example *mcu: Unable to connect*) the printer stays online in the error state with
the reason and offers a firmware restart («Перезапуск прошивки»).

## MKS WiFi

| Field | What to enter |
| --- | --- |
| **Name** | Any name. |
| **Model (optional)** | Free text, may be empty. |
| **IP address** | The address of the MKS WiFi module. |
| **Port** | Leave empty for the default **8080**. |

The dashboard opens a TCP connection to the module and reads the state with G-code commands
(M115, M997, M27, M105, M114, M220, then M991, M992, M994). **MKS printers are monitored only** —
the card has no control buttons for them.

## Finding the printer's IP address

Every printer shows its IP address in its network settings; your router's list of connected
devices shows it too. Give the printers fixed addresses (a DHCP reservation in the router), so that
the address in the dashboard stays correct.

## Fleet table columns

**Name** («Название»), **Model** («Модель»), **Type** («Тип»), **IP**, **Firmware** («Прошивка»),
**Maintenance** («Обслуживание»), **Control** («Управление», read-only — what the dashboard can
do on this printer), **Actions** («Действия»). Maintenance mode is switched on the printer's card,
not here — see [Printers screen](interface/printers.md#maintenance-mode).
