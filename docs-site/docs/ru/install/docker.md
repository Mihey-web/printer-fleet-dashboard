# Установка через Docker

В репозитории есть `Dockerfile` и `docker-compose.yml`, поэтому на компьютере с Docker и Docker
Compose панель запускается несколькими командами. Образ собирается на вашем компьютере из
исходного кода.

## Запуск

```bash
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
touch printer_history.db users.db      # чтобы Docker подключил их как файлы
docker compose up -d --build
docker compose logs -f                 # один раз прочитать пароль администратора
```

Строка с `touch` важна: она создаёт два пустых файла баз, чтобы Docker подключил их в контейнер
именно как файлы, а данные оставались на компьютере.

Откройте `http://<адрес компьютера>:8000` и войдите как `admin` с паролем из журнала
(см. [Первый запуск](first-run.md)). Нажмите ++ctrl+c++, чтобы перестать смотреть журнал, — панель
продолжит работать.

## Что настраивает `docker-compose.yml`

- Контейнер называется `kuznitsa` и сам перезапускается (`restart: unless-stopped`) — так же
  **«Применить и перезапустить»** возвращает его после изменения списка принтеров.
- Порт `8000` компьютера — это панель.
- `COOKIE_SECURE=0`, потому что по обычному HTTP в вашей сети cookie входа иначе отбрасывается. Если
  поставите панель за HTTPS (nginx, Caddy), задайте `1` и открывайте панель только через прокси.
- Файлы `printer_history.db` и `users.db` из папки проекта подключены в контейнер, поэтому список
  принтеров, настройки, история и пользователи переживают обновления.

## Настройки {#settings}

Добавляйте переменные в список `environment:` в `docker-compose.yml`. В файле уже есть
закомментированные примеры для первого администратора и ключа сеансов:

```yaml
    environment:
      - WEB_HOST=0.0.0.0
      - WEB_PORT=8000
      - COOKIE_SECURE=0
      # - FORGE_ADMIN_USERNAME=admin
      # - FORGE_ADMIN_PASSWORD=change-me-immediately
      # - FORGE_JWT_SECRET=your-64-hex-chars
```

Уберите `#`, чтобы строка заработала, и снова выполните `docker compose up -d`. Все переменные — в
разделе [Настройки и переменные окружения](../configuration.md).

!!! warning "История AMS не сохраняется вне контейнера"
    `docker-compose.yml` подключает только `printer_history.db` и `users.db`. История влажности и
    температуры AMS хранится в `ams_history.db`, который остаётся **внутри** контейнера и
    пропадает при его пересоздании (например, после `docker compose up -d --build`). Скачанные
    тексты ошибок Bambu (`data/hms/`) тоже внутри, но они просто скачаются заново. Чтобы сохранять
    историю AMS, создайте файл и добавьте ещё одну строку в `volumes:` по образцу двух других:

    ```bash
    touch ams_history.db
    ```

    ```yaml
          - ./ams_history.db:/app/ams_history.db
    ```

## Обновление

```bash
cd kuznitsa
git pull
docker compose up -d --build
```

## Полезные команды

| Что | Команда |
| --- | --- |
| Смотреть журнал | `docker compose logs -f` |
| Остановить | `docker compose down` |
| Запустить снова | `docker compose up -d` |
