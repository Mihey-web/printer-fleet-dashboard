# Настройки и переменные окружения

В Кузнице два вида настроек:

1. **Настройки на веб-странице**, хранятся в базе — список принтеров, Telegram, прокси, автоматика.
   Почти всё настраивается здесь.
2. **Переменные окружения** — несколько настроек запуска: адрес и порт, безопасность, опрос, срок
   хранения истории. Все они не обязательны; панель стартует с безопасными значениями по умолчанию.

## Как задать переменную окружения

| Способ установки | Как |
| --- | --- |
| Windows | `set ИМЯ=значение` в окне Командной строки, затем `python main.py` в том же окне — см. [Windows](install/windows.md) |
| Linux / systemd | строка `Environment=ИМЯ=значение` в разделе `[Service]` службы — см. [Linux / Raspberry Pi](install/linux.md#env-vars) |
| Docker | строка `- ИМЯ=значение` в `environment:` файла `docker-compose.yml` — см. [Docker](install/docker.md#settings) |

!!! info "Про `.env.example`"
    В репозитории есть `.env.example` с основными переменными и комментариями. Сама программа файл
    `.env` **не** читает; используйте его как справку или поручите загрузку диспетчеру служб
    (например, `EnvironmentFile=` в systemd или `env_file:` в Docker Compose).

После изменения переменной перезапустите панель.

## Все переменные окружения

Ссылки в последнем столбце ведут на строку кода (версия 2.0.0), где переменная читается.

### Веб-сервер и безопасность

| Переменная | По умолчанию | Что делает | Где читается |
| --- | --- | --- | --- |
| `WEB_HOST` | `127.0.0.1` | Адрес, на котором слушает панель. `127.0.0.1` — только этот компьютер (за nginx или туннелем). `0.0.0.0` — все сетевые интерфейсы, панель откроется с других устройств. В образе Docker задано `0.0.0.0`. | [config.py:7](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L7), [main.py:52](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L52) |
| `WEB_PORT` | `8000` | Порт. Должен быть целым числом. | [config.py:8](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L8), [main.py:53](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L53) |
| `COOKIE_SECURE` | `1` | `1` — cookie входа передаются только по HTTPS (правильно за HTTPS-прокси). `0`, `false`, `False` или пустое значение — и по обычному HTTP; нужно, если открываете панель по `http://` с другого устройства. В файлах Docker задано `0`. | [config.py:41](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L41) |
| `FORGE_JWT_SECRET` | создаётся при первом запуске и хранится в `users.db` | Ключ, которым подписываются сеансы входа. Задайте, чтобы выбрать ключ самостоятельно; при смене ключа выходят все. Сгенерировать: `python -c "import secrets; print(secrets.token_hex(32))"`. | [config.py:34](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L34), [app/api/main.py:818](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/api/main.py#L818), [app/api/auth.py:178](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/api/auth.py#L178) |
| `FORGE_ADMIN_USERNAME` | `admin` | Логин администратора, создаваемого при первом запуске. Используется, только пока пользователей нет. | [config.py:46](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L46) |
| `FORGE_ADMIN_PASSWORD` | пусто — случайный пароль один раз печатается в журнал | Пароль этого первого администратора. Используется, только пока пользователей нет. | [config.py:47](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L47) |

### Опрос принтеров

| Переменная | По умолчанию | Что делает | Где читается |
| --- | --- | --- | --- |
| `POLL_INTERVAL` | `5`, но **не меньше 30 с** | Секунды между полными опросами всех принтеров (история, журнал печатей, уведомления). Значения меньше 30 поднимаются до 30, так что фактически по умолчанию 30 с. | [config.py:3](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L3), [main.py:49](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L49) |
| `MAX_WORKERS` | `10` | Сколько принтеров опрашивается одновременно. Панель всегда берёт не меньше потоков, чем принтеров. | [config.py:4](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L4), [main.py:50](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L50) |
| `FORGE_LIVE_REFRESH_S` | `2` | Секунды между живыми обновлениями карточек по данным самих принтеров, между полными опросами. `0` — выключить: карточки будут меняться только при полном опросе. | [app/services/printer_commands.py:1339](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/printer_commands.py#L1339) |
| `FORGE_BAMBU_TLS_VERIFY` | пусто (выкл) | Проверять TLS-сертификат каждого принтера Bambu по сертификатам Bambu, встроенным в панель. Пусто — соединение шифруется, но сертификат не проверяется. `*` — все принтеры Bambu. Или список моделей через запятую, как выбрано в форме, например `X1C,P2S`. Модель, чей сертификат не покрыт, не подключится (в журнале `SSLCertVerificationError`) и будет повторять попытку каждую секунду, поэтому включайте модели по одной. Нужны верные системные часы. | [config.py:13](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/config.py#L13), [app/collectors/bambu_collector.py:53](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/collectors/bambu_collector.py#L53), [main.py:77](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/main.py#L77) |

### История и автоматика {#history-automation}

Этих трёх переменных нет в `.env.example`, но код их читает.

| Переменная | По умолчанию | Что делает | Где читается |
| --- | --- | --- | --- |
| `HISTORY_RETENTION_DAYS` | `90` | Сколько дней хранить историю состояний (хронология, статистика), записи уведомлений принтеров и историю AMS. | [app/services/state_store.py:21](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/state_store.py#L21), [app/services/notification_tracker.py:37](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/notification_tracker.py#L37), [app/services/ams_store.py:21](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/ams_store.py#L21) |
| `JOBS_RETENTION_DAYS` | `730` | Сколько дней хранить завершённые печати в журнале (не меньше 1). | [app/services/job_journal.py:939](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/job_journal.py#L939) |
| `FORGE_TZ_OFFSET_MIN` | `180` | Часовой пояс фермы для окна запуска автосушки, в минутах от UTC (`180` = UTC+3, Москва). Например, `120` для UTC+2, `300` для UTC+5. | [app/services/ams_autodry_engine.py:55](https://github.com/mihey-web/printer-fleet-dashboard/blob/313ccaec83e4a3a05e632db1127df39faeed967e/app/services/ams_autodry_engine.py#L55) |

## Настройки на веб-странице

Они хранятся в `printer_history.db` и меняются только на веб-странице (администратором):

| Что | Где |
| --- | --- |
| Принтеры: имена, адреса, коды доступа, серийные номера | [«Админ → Парк»](printers.md) |
| Управление светом Creality | галочка «Разрешить управление светом» в форме принтера Creality |
| Правила автосушки AMS | [«Админ → Автоматика»](interface/admin.md#ams-auto-drying) |
| Коды ошибок для автопропуска | [«Админ → Автоматика»](interface/admin.md#auto-skip-errors) |
| Скрытые уведомления | кнопки «Скрыть» / «Вернуть» у уведомлений принтера |
| Пользователи | [«Админ → Доступ»](security.md#users-and-roles) (хранятся в `users.db`) |
| Telegram: токен, чат, интервал, события, шаблоны | [«Админ → Система»](telegram.md) |
| Прокси для Telegram и интервал их проверки | [«Админ → Система»](telegram.md#proxy) |

Изменения Telegram и прокси применяются сразу. Изменения списка принтеров требуют **«Применить и
перезапустить»**.

Настройки в **👤 «Этот браузер»** (тема, масштаб текста, уведомления браузера) хранятся только в
этом браузере.
