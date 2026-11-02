# Installing with Docker

The repository has a `Dockerfile` and a `docker-compose.yml`, so on a computer with Docker and
Docker Compose the dashboard starts with a few commands. The image is built on your computer from
the source code.

## Start

```bash
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
touch printer_history.db users.db      # so Docker mounts them as files
docker compose up -d --build
docker compose logs -f                 # read the first-run admin password once
```

The `touch` line matters: it creates two empty database files, so that Docker connects them into
the container as files and your data stays on the host.

Open `http://<host>:8000` and log in as `admin` with the password from the log
(see [First run](first-run.md)). Press ++ctrl+c++ to stop watching the log — the dashboard keeps
running.

## What `docker-compose.yml` sets up

- The container is called `kuznitsa` and restarts by itself (`restart: unless-stopped`) — this is
  also how **Apply and restart** («Применить и перезапустить») brings it back after you change the
  printer list.
- Port `8000` of the computer is the dashboard.
- `COOKIE_SECURE=0`, because over plain HTTP in your network the login cookie would otherwise be
  dropped. If you put the dashboard behind HTTPS (nginx, Caddy), set it to `1` and open the
  dashboard only through the proxy.
- `printer_history.db` and `users.db` in the project folder are mounted into the container, so the
  printer list, settings, history and users survive updates.

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

!!! warning "AMS history is not kept outside the container"
    `docker-compose.yml` mounts only `printer_history.db` and `users.db`. The AMS humidity and
    temperature history lives in `ams_history.db`, which stays **inside** the container and is lost
    when the container is re-created (for example after `docker compose up -d --build`). The
    downloaded Bambu error texts (`data/hms/`) are also inside, but they are simply downloaded
    again. If you want to keep the AMS history, create the file and add one more line under
    `volumes:` the same way as the other two:

    ```bash
    touch ams_history.db
    ```

    ```yaml
          - ./ams_history.db:/app/ams_history.db
    ```

## Updating

```bash
cd kuznitsa
git pull
docker compose up -d --build
```

## Useful commands

| What | Command |
| --- | --- |
| Watch the log | `docker compose logs -f` |
| Stop | `docker compose down` |
| Start again | `docker compose up -d` |
