# First run and the administrator account

## The administrator is created automatically

When the dashboard starts and its user database is empty, it creates one administrator account:

- The user name is `admin` (or the value of `FORGE_ADMIN_USERNAME`).
- If you set `FORGE_ADMIN_PASSWORD` before the first start, that is the password.
- Otherwise a **random password is generated and printed once to the log**, like this:

```text
============================================================
 FIRST-RUN ADMIN ACCOUNT CREATED
   username: admin
   password: <random password>
 This password is shown ONCE. Log in and change it now.
 (Set FORGE_ADMIN_USERNAME / FORGE_ADMIN_PASSWORD to choose
  your own credentials before first run.)
============================================================
```

Where to find the log:

| Installed with | Where the log is |
| --- | --- |
| Windows | the Command Prompt window where you ran `python main.py` |
| Linux / Raspberry Pi (systemd) | `sudo journalctl -u bambudiagnostic.service` |
| Docker | `docker compose logs` |

!!! note
    The password is printed only on the start that creates the account. `FORGE_ADMIN_USERNAME` and
    `FORGE_ADMIN_PASSWORD` are also used only then — changing them later does nothing to existing
    accounts.

## Log in

Open the dashboard address in a browser (`http://127.0.0.1:8000` on the same computer, or the
address of your server). The login page shows the fields **Login** («Логин») and **Password**
(«Пароль») and the button **Sign in** («Войти»).

Error messages on the login page come from the server and are in English:

| Message | Meaning |
| --- | --- |
| `Invalid username or password` | Wrong login or password. |
| `Too many login attempts. Try again later.` | Too many wrong attempts: 5 from one address or 10 for one login within 15 minutes. Wait and try again. |
| `Username and password required` | A field is empty. |

If the page reloads and asks for the password again after a correct login, the browser dropped the
login cookie — see [FAQ](../faq.md#login-loops).

## Change the generated password right away

1. Open **Admin → Access** («Админ → Доступ»).
2. In the users table, your row is marked «(вы)» ("you").
3. Click **Change password** («Сменить пароль»), type a new password (at least 8 characters) and
   click **Save** («Сохранить»).

A password change ends that user's other sessions, so you may be asked to log in again.

## Next

- [Add your printers](../printers.md).
- Look at the **👤** button in the top right corner: the panel **This browser** («Этот браузер»)
  shows your login and role, has **Log out** («Выйти»), the colour theme, text size, browser
  notifications and the version.
