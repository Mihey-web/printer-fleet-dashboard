# Installing on Windows

On Windows the dashboard runs from the source code with Python. The repository has no Windows
installer and no ready autostart for Windows, so this way is best for trying the dashboard out or
for a PC that you keep a console window open on.

## 1. Install Python and Git

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/). In the
   installer, tick **"Add python.exe to PATH"**.
2. Install **Git** from [git-scm.com](https://git-scm.com/download/win) — or download the
   repository as a ZIP from GitHub (**Code → Download ZIP**) and unpack it.

## 2. Download the dashboard and its libraries

Open **Command Prompt** (`cmd`) and run, one by one:

```bat
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The `.venv` folder is a private copy of Python for the dashboard, so its libraries do not mix with
other programs.

## 3. Start it

```bat
python main.py
```

The window shows the log. Keep it open — closing it stops the dashboard. On the very first start
the log prints the administrator's password once; see [First run](first-run.md).

Open <http://127.0.0.1:8000> in a browser on the same PC.

## Opening the dashboard from other devices

By default the dashboard listens only on this PC (`127.0.0.1`) and expects HTTPS for its login
cookies. To open it from a phone or another computer over plain HTTP in your local network, set
two variables **in the same window before starting**:

```bat
set WEB_HOST=0.0.0.0
set COOKIE_SECURE=0
python main.py
```

Then open `http://<this PC's IP address>:8000`. Windows may ask whether to allow Python through
the firewall — allow it for private networks.

!!! warning "Plain HTTP is for a trusted home network only"
    `COOKIE_SECURE=0` sends the login cookie without encryption. Do not expose such a setup to the
    internet. See [Users and security](../security.md).

!!! info "The `.env` file is not read automatically"
    The project has a `.env.example` file with all variables, but the program itself does not load
    a `.env` file. On Windows set the variables with `set NAME=value` in the window before
    `python main.py`, as shown above. All variables are listed in
    [Settings and environment variables](../configuration.md).

## Every next start

```bat
cd kuznitsa
.venv\Scripts\activate
python main.py
```

## "Apply and restart" stops the program on Windows

When you change the printer list, the dashboard asks you to press **Apply and restart**
(«Применить и перезапустить»). This button makes the program exit so that a service manager starts
it again with the new list. On Linux (systemd) and in Docker this happens automatically. **On
Windows nothing restarts it** — after pressing the button, run `python main.py` again in the
window.

## Updating

```bat
cd kuznitsa
git pull
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Your data (`printer_history.db`, `users.db`, `ams_history.db`) stays in the folder.
