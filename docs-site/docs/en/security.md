# Users, roles and security

## Users and roles

There are two roles:

| Role | What it can do |
| --- | --- |
| **Administrator** («Админ») | Everything: the control row and chips on the cards, notification actions, farm commands, AMS drying, maintenance mode, the **Admin** screen, CSV export and **History → Actions**. |
| **Viewer** («Зритель») | Watch only: Printers, AMS and History (Prints, Timeline, Statistics), charts included. No buttons that change anything; the server refuses administrator requests from a viewer. |

### Managing users

Open **Admin → Access** («Админ → Доступ»):

- **+ User** («+ Пользователь») — enter a **Login** («Логин», at least 2 characters), a **Password**
  («Пароль», at least 8 characters), pick the role **Viewer** («Зритель») or **Admin** («Админ») and
  click **Add** («Добавить»).
- In a user's row:
    - the **role** list — choose another role and confirm with **Change** («Сменить»); that user's
      sessions end;
    - **Change password** («Сменить пароль») — type a new one and **Save** («Сохранить»); that
      user's sessions end;
    - **Delete** («Удалить») — asks for confirmation.
- You cannot change your own role or delete yourself, so the dashboard always keeps an
  administrator.
- **Last login** («Последний вход») shows the time, the IP address and the device; «не входил»
  means the user has never logged in.

## Sessions and login protection

- Passwords are stored only as bcrypt hashes.
- After login the browser gets two cookies: a short session (15 minutes) that is renewed
  automatically and a renewal cookie valid for 7 days. They are not readable by scripts
  (`HttpOnly`), are not sent by other sites (`SameSite=Strict`) and, unless `COOKIE_SECURE=0`, are
  sent only over HTTPS.
- **Brute-force protection:** after 5 wrong attempts from one address within 15 minutes, logins
  from that address are refused for a while (`Too many login attempts. Try again later.`). After 10
  wrong attempts for one login within 15 minutes — from any addresses — every further attempt with
  that login waits before the password is checked: 1, 2, 4, 8, 16, then 30 seconds. This slows
  guessing from many addresses at once. The right password still gets in after the wait, so nobody
  can lock the real owner out; a wrong one after the wait is answered with the same
  `Too many login attempts`.
- Changing a user's role or password, or deleting the user, ends their sessions at once.
- The sessions are signed with a key that is generated on the first start and stored in
  `users.db`. Setting `FORGE_JWT_SECRET` replaces it; changing it logs everybody out. See
  [Settings](configuration.md).

## The action log

Every login (including failed ones), user change, printer command, fleet or settings change and
every automatic action is written to the log. Administrators see it in **History → Actions**
(«История → Действия») — see [History screen](interface/history.md#actions-administrators).

## HTTPS with nginx {#https-with-nginx}

Why bother:

- the login cookie travels encrypted;
- browser notifications work only over HTTPS;
- browsers offer to install the dashboard as an app only from a secure address.

The recommended layout: the dashboard listens only on `127.0.0.1` (the default `WEB_HOST`),
`COOKIE_SECURE` stays `1` (the default) and **nginx** in front of it handles HTTPS. nginx passes the
visitor's address in `X-Real-IP` / `X-Forwarded-For`; the dashboard trusts these headers only from
`127.0.0.1` / `::1`, so the action log and the login limits see real addresses. If the proxy
connects from another address — Docker behind nginx or Caddy on the host, a proxy on another
machine — list it in `FORGE_TRUSTED_PROXIES` (see
[Docker behind nginx or Caddy](install/docker.md#behind-nginx-or-caddy)).

The `deploy/` folder has ready-to-adapt templates:

| File | What it is |
| --- | --- |
| `nginx-bambu.conf` | nginx site: port 80 redirects to HTTPS on port **8443**; certificate paths of Let's Encrypt for `your-domain.example`; logins limited to 5 per minute per address (burst 3, then `429`); everything proxied to `127.0.0.1:58000`. |
| `setup_https.sh` | For a Raspberry Pi / Debian: installs nginx, creates a self-signed certificate for the computer's LAN IP (valid 10 years) in `/etc/ssl/bambu/`, installs `nginx-bambu.conf` as the site `bambu`, removes nginx's default site and restarts nginx. |
| `bambu-tunnel.service` | An `autossh` reverse SSH tunnel: port `58000` on a public server (VPS) leads to port `8000` of the dashboard computer. |

As shipped, `nginx-bambu.conf` is written for the VPS end of the tunnel (port `58000`, a Let's
Encrypt certificate). Choose one of the two options below and adapt the file.

### Option A: HTTPS inside your local network

Uses `setup_https.sh` and a self-signed certificate.

1. Edit `deploy/nginx-bambu.conf` **before** running the script:
    - in both `location` blocks change `proxy_pass http://127.0.0.1:58000;` to
      `proxy_pass http://127.0.0.1:8000;` (the dashboard's port);
    - change `ssl_certificate` to `/etc/ssl/bambu/fullchain.pem` and `ssl_certificate_key` to
      `/etc/ssl/bambu/privkey.pem` — the files the script creates.
2. Run it from the project folder (the IP is detected automatically or can be given):

    ```bash
    sudo deploy/setup_https.sh 192.168.1.100
    ```

3. Open `https://<IP>:8443` — the config listens on port 8443 (port 80 redirects there). On each
   device accept the certificate warning once (*Advanced → Proceed*), as the script itself
   explains.
4. Make sure the dashboard has `COOKIE_SECURE=1` (remove `COOKIE_SECURE=0` if you added it) and
   `WEB_HOST=127.0.0.1`, so it is reached only through nginx.

!!! note
    A self-signed certificate is not trusted by browsers by default. Some browsers may refuse to
    install the app or to show notifications from such an address; a real certificate (option B)
    avoids that.

### Option B: access from the internet through a VPS

For reaching the farm from outside without opening ports at home: the dashboard computer keeps an
SSH tunnel to a public server, and nginx on that server serves HTTPS with a real certificate.

**On the dashboard computer:**

1. Install `autossh` and set up SSH key login from this computer to the VPS.
2. Copy `deploy/bambu-tunnel.service` to `/etc/systemd/system/`, replace `__USER__`,
   `__VPS_USER__`, `__VPS_HOST__` and `__VPS_SSH_PORT__`, then enable it:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable --now bambu-tunnel.service
    ```

3. Keep `WEB_HOST=127.0.0.1` and `COOKIE_SECURE=1`.

**On the VPS:**

1. Install nginx and put `deploy/nginx-bambu.conf` into its sites, replacing `your-domain.example`
   with your domain.
2. Get a Let's Encrypt certificate for the domain. The config already serves
   `/.well-known/acme-challenge/` from `/var/www/html`, so the certificate tool's *webroot* method
   works with it.
3. Reload nginx and open `https://<your domain>:8443`.

## Hardening checklist

From the project's `SECURITY.md`. The dashboard controls physical printers and stores their access
codes, so:

- keep it bound to `127.0.0.1` behind an HTTPS proxy, or reachable only in your LAN / VPN — **never
  directly on the internet**;
- keep `COOKIE_SECURE=1` when it is served over HTTPS;
- change the first-run administrator password at once;
- where practical, put the printers and the dashboard computer in a separate subnet / VLAN.

## Reporting a vulnerability

Do not open a public issue. Use **Security → Advisories → Report a vulnerability** on the
repository's GitHub page; it creates a private report visible only to the maintainers.
