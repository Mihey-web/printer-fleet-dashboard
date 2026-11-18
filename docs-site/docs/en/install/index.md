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

Everything is stored in files next to the program, in the project folder (another folder can be set
with [`FORGE_DATA_DIR`](../configuration.md#data-dir)):

| File / folder | What is inside |
| --- | --- |
| `printer_history.db` | The printer list, settings (Telegram, proxies, automation), state history, print journal |
| `users.db` | User accounts, the action log, the key that signs login sessions |
| `ams_history.db` | AMS humidity and temperature history |
| `data/job_previews/` | Job previews and job cards (print settings, the plate map) the print journal refers to |
| `data/file_pool/` | The shared file pool: print files uploaded to the dashboard (up to 8 GB, see [Shared file pool](../configuration.md#file-pool)) |
| `data/hms/` | Bambu's downloaded error texts and buttons (downloaded again if missing) |

Back up the three `.db` files and the `data/job_previews/` folder to keep your farm's setup and
history: without that folder the history's jobs lose their previews and cards. Back up
`data/file_pool/` unless the uploaded files are kept somewhere else; `data/hms/` needs no backup.
On Linux there is a ready nightly backup — see
[Linux / Raspberry Pi](linux.md#nightly-database-backup-optional); it copies the three `.db` files,
`config.py` and `data/job_previews/`, not `data/file_pool/` or `data/hms/`.

## After installing

Go to [First run](first-run.md): read the administrator password, log in and change it, then
[add your printers](../printers.md).
