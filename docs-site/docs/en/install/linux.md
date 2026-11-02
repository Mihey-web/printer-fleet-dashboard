# Installing on Linux / Raspberry Pi

This is the setup the project's `deploy/` folder is made for: the dashboard runs as a **systemd
service**, starts when the computer boots and restarts by itself after a crash or after you press
**Apply and restart** («Применить и перезапустить»).

The commands below are for Debian, Ubuntu and Raspberry Pi OS. Run them as a normal user with
`sudo` rights (for example `pi`).

## 1. Install the system packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
```

Check that Python is 3.11 or newer: `python3 --version`.

## 2. Download the dashboard

```bash
cd ~
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Try it by hand

```bash
.venv/bin/python main.py
```

Wait for the line `Web dashboard: http://127.0.0.1:8000`. On the very first start the log also
prints the administrator's password once — copy it (see [First run](first-run.md)). Stop the
program with ++ctrl+c++.

## 4. Install the systemd service

The repository has a service template, `deploy/bambudiagnostic.service`, and an installer,
`deploy/install_service.sh`. The installer fills in your user, group and folder, copies the unit to
`/etc/systemd/system/bambudiagnostic.service`, enables it and starts it. Run it **from the project
folder**:

```bash
cd ~/kuznitsa
sudo deploy/install_service.sh "$PWD"
```

You can pass a different user and group as the 2nd and 3rd arguments:
`sudo deploy/install_service.sh "$PWD" pi pi`.

### Point the service at the virtual environment

The template starts the program with the **system** `python3`, which does not have the libraries
you installed into `.venv`. Edit the unit:

```bash
sudo systemctl edit --full bambudiagnostic.service
```

Change the `ExecStart=` line so it uses the Python from `.venv` (replace the path with your
project folder):

```ini
ExecStart=/home/pi/kuznitsa/.venv/bin/python -u main.py
```

The unit already has `WorkingDirectory=` set to the project folder, which the program needs.

### Add settings (environment variables)

Settings such as the listening address are environment variables. In the same editor, add
`Environment=` lines under `[Service]`, for example:

```ini
Environment=WEB_HOST=0.0.0.0
Environment=COOKIE_SECURE=0
```

These two let you open the dashboard from other devices over plain HTTP — only in a trusted local
network. For HTTPS see [Users and security](../security.md#https-with-nginx). All variables are in
[Settings and environment variables](../configuration.md).

Save, then restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bambudiagnostic.service
```

## 5. Useful commands

| What | Command |
| --- | --- |
| Watch the log (the first-run password is here) | `sudo journalctl -u bambudiagnostic.service -f` |
| Status | `systemctl status bambudiagnostic.service` |
| Restart | `sudo systemctl restart bambudiagnostic.service` |
| Stop / start | `sudo systemctl stop bambudiagnostic.service` / `sudo systemctl start bambudiagnostic.service` |

The unit restarts the program 5 seconds after it exits with an error (`Restart=on-failure`,
`RestartSec=5`). This is exactly how **Apply and restart** works: the program exits and systemd
starts it again with the new printer list.

## Updating

```bash
cd ~/kuznitsa
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart bambudiagnostic.service
```

## Nightly database backup (optional)

`deploy/` also has a nightly backup: `forge-db-backup.service`, `forge-db-backup.timer` and the
script `forge_db_backup.py`. Every night at about 03:30 it makes a consistent copy of
`printer_history.db`, `users.db`, `ams_history.db` and `config.py` into a dated folder, checks each
copy and keeps the newest 14.

The template is written for a **separate disk mounted at `/mnt/ssd`** (backups go to
`/mnt/ssd/forge-backups`). If that disk is not mounted the backup refuses to run, so it never fills
the SD card. If your disk is elsewhere, change `/mnt/ssd` in the service file first.

Install it from the project folder (commands from the file's own header):

```bash
sed "s|__USER__|$USER|g; s|__WORKDIR__|$PWD|g" deploy/forge-db-backup.service | sudo tee /etc/systemd/system/forge-db-backup.service
sudo cp deploy/forge-db-backup.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now forge-db-backup.timer
```

Run it once by hand to test: `sudo systemctl start forge-db-backup.service`, then look at
`journalctl -u forge-db-backup`.

## Next steps

- [First run](first-run.md) — log in and change the administrator password.
- [HTTPS with nginx](../security.md#https-with-nginx) — needed for browser notifications and for
  installing the dashboard as an app.
