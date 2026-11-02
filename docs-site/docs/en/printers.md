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
| **Model** («Модель») | Pick from the list: X1, X1 Carbon, X1E, P1P, P1S, P2S, A1, A1 mini, H2D, H2S. |
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

## Creality

| Field | What to enter |
| --- | --- |
| **Name** | Any name. |
| **Model** | K1 Max, K1C or Ender-5 Max. |
| **IP address** | The printer's address in your network. |
| **Allow light control** («Разрешить управление светом») | On by default. Turn it off for a printer without a controllable lamp: its 💡 button and the farm's light command skip it. This switch is saved at once, without a restart. |

The dashboard connects to the printer's WebSocket on port **9999**; no password or code is needed.
On Creality the card offers pause / resume / stop, light, cool-down, nozzle and bed temperatures,
speed modes and fans. Creality never confirms a command, so the result says "sent" («отправлено»),
never "confirmed" («подтверждено»).

## Klipper / Moonraker

| Field | What to enter |
| --- | --- |
| **Name** | Any name. |
| **Model (optional)** («Модель (не обязательно)») | Free text, may be empty. |
| **IP address** | The address of the computer running Moonraker (the printer's host). |
| **Port** («Порт») | Moonraker's port. Leave empty for the default **7125**. |

The dashboard connects to `ws://<IP>:<port>/websocket`. The form has no field for an API key, so
Moonraker has to accept connections from the dashboard's computer without one.

On Klipper the card offers pause / resume / cancel once Klipper reports it is ready, and a light
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
