# Installation

Kuznitsa is one program (a Python web server) that you run on a computer that is always on and
sits in the same local network as the printers — for example a Raspberry Pi, a mini-PC or a home
server. You then open the dashboard in a browser on any device.

## Choose a way to install

| Way | Good for | Page |
| --- | --- | --- |
| **Docker** | The simplest way on a Linux server or NAS that already has Docker. Recommended by the README. | [Docker](docker.md) |
| **Linux / Raspberry Pi** with systemd | An always-on Raspberry Pi or Linux box: the dashboard starts at boot and restarts by itself. | [Linux / Raspberry Pi](linux.md) |
| **Windows** | Trying the dashboard out on a Windows PC. | [Windows](windows.md) |

## What you need

- **A computer** with Linux, macOS or Windows and **Python 3.11 or newer** — or just Docker.
- **Network access to every printer.** The dashboard connects to the printers itself, so the
  computer must reach each printer's IP address on these ports:

    | Printer | Port the dashboard connects to |
    | --- | --- |
    | Bambu Lab | 8883 (8884 is tried as well) |
    | Creality | 9999 |
    | Klipper / Moonraker | 7125 unless you enter another one |
    | MKS WiFi | 8080 unless you enter another one |

    The easiest setup is to keep the computer and the printers in the same network (subnet / VLAN).
- **Internet access is optional.** It is used for Telegram (if you turn it on) and, once a day, to
  download Bambu's tables of error texts and buttons from `e.bambulab.com` (only the first three
  characters of a printer's serial number are sent). Without them the dashboard falls back to
  built-in texts.

## Where the dashboard keeps its data

Everything is stored in files next to the program, in the project folder:

| File / folder | What is inside |
| --- | --- |
| `printer_history.db` | The printer list, settings (Telegram, proxies, automation), state history, print journal |
| `users.db` | User accounts, the action log, the key that signs login sessions |
| `ams_history.db` | AMS humidity and temperature history |
| `data/hms/` | Bambu's downloaded error texts and buttons (downloaded again if missing) |

Back up the three `.db` files to keep your farm's setup and history. On Linux there is a ready
nightly backup job — see [Linux / Raspberry Pi](linux.md#nightly-database-backup-optional).

## After installing

Go to [First run](first-run.md): read the administrator password, log in and change it, then
[add your printers](../printers.md).
