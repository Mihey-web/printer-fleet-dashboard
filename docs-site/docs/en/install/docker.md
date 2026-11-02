# Installing with Docker

The repository has a `Dockerfile` and a `docker-compose.yml`, so on a computer with Docker and
Docker Compose the dashboard starts with a few commands. The image is built on your computer from
the source code.

## Start

```bash
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
touch printer_history.db users.db ams_history.db   # so Docker mounts them as files
docker compose up -d --build
docker compose logs -f                 # read the first-run admin password once
```

The `touch` line matters: it creates three empty database files, so that Docker connects them into
the container as files and your data stays on the host.

Open `http://<host>:8000` and log in as `admin` with the password from the log
(see [First run](first-run.md)). Press ++ctrl+c++ to stop watching the log — the dashboard keeps
running.

## What `docker-compose.yml` sets up

- The container is called `kuznitsa` and restarts by itself (`restart: unless-stopped`) — this is
  also how **Apply and restart** («Применить и перезапустить») brings it back after you change the
  printer list.
- Port `8000` of the computer is the dashboard. It is open on **all** network interfaces, and
  Docker adds its own firewall rules, so **ufw / firewalld do not close it**. That is fine in a
  home network; to keep the port on this computer only see
  [Behind nginx or Caddy](#behind-nginx-or-caddy).
- `COOKIE_SECURE=0`, because over plain HTTP in your network the login cookie would otherwise be
  dropped. If you put the dashboard behind HTTPS (nginx, Caddy), set it to `1` and open the
  dashboard only through the proxy.
- `printer_history.db`, `users.db` and `ams_history.db` in the project folder are mounted into the
  container, so the printer list, settings, history, users and the AMS humidity history (auto-drying
  decides on it) survive updates.

## Settings

Add variables to the `environment:` list in `docker-compose.yml`. The file already has commented
examples for the first administrator and the session key:

```yaml
    environment:
      - WEB_HOST=0.0.0.0
      - WEB_PORT=8000
      - COOKIE_SECURE=0
      # - FORGE_ADMIN_USERNAME=admin
      # - FORGE_ADMIN_PASSWORD=change-me-immediately
      # - FORGE_JWT_SECRET=your-64-hex-chars
```

Remove the `#` to use a line, then run `docker compose up -d` again. All variables are in
[Settings and environment variables](../configuration.md).

## Behind nginx or Caddy on the same computer {#behind-nginx-or-caddy}

If nginx or Caddy on this computer serves the dashboard over HTTPS, change three things in
`docker-compose.yml`:

1. Publish the port on this computer only, so the dashboard is reached only through the proxy:

    ```yaml
        ports:
          - "127.0.0.1:8000:8000"
    ```

2. Set `COOKIE_SECURE=1`.
3. Trust the proxy's address. The proxy connects to the published port, so inside the container
   every request comes from the Docker network's gateway (for example `172.18.0.1`), not from
   `127.0.0.1`. Find it in the log — the address at the start of each request line:

    ```bash
    docker compose logs | grep '"GET /'
    ```

    and add it:

    ```yaml
          - FORGE_TRUSTED_PROXIES=172.18.0.1
    ```

Without step 3 the dashboard ignores the visitor's address the proxy passes and sees everyone as one
address: five wrong passwords from anybody block logging in for everybody for 15 minutes, and the
action log shows the gateway instead of real addresses. Trust only the address the proxy really
comes from: whoever connects from a trusted address can claim any visitor address. A whole network
such as `172.16.0.0/12` also trusts every other container on the computer.

## Updating

```bash
cd kuznitsa
git pull
touch printer_history.db users.db ams_history.db   # existing files are kept as they are
docker compose up -d --build
```

Versions before this one did not mount `ams_history.db`. Without the `touch` line Docker creates a
*folder* with that name, and the dashboard stops with `… a directory, not a database file` in the
log: stop it, delete the empty folder, run `touch ams_history.db` and start again. To keep the AMS
history of the old container, copy it out **before** `docker compose up -d --build`, instead of the
`touch` for that file (the last few minutes may be missing):

```bash
docker compose stop
docker cp kuznitsa:/app/ams_history.db ./ams_history.db
```

## Useful commands

| What | Command |
| --- | --- |
| Watch the log | `docker compose logs -f` |
| Stop | `docker compose down` |
| Start again | `docker compose up -d` |
