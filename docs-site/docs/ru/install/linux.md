# Установка на Linux / Raspberry Pi

Под этот вариант сделана папка проекта `deploy/`: панель работает как **служба systemd**,
запускается при загрузке компьютера и сама перезапускается после сбоя или после нажатия
**«Применить и перезапустить»**.

Команды ниже — для Debian, Ubuntu и Raspberry Pi OS. Выполняйте их от обычного пользователя с
правами `sudo` (например, `pi`).

## 1. Установите системные пакеты

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
```

Проверьте, что Python 3.11 или новее: `python3 --version`.

## 2. Скачайте панель

```bash
cd ~
git clone https://github.com/mihey-web/printer-fleet-dashboard.git kuznitsa
cd kuznitsa
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Попробуйте запустить вручную

```bash
.venv/bin/python main.py
```

Дождитесь строки `Web dashboard: http://127.0.0.1:8000`. При самом первом запуске в журнале также
один раз печатается пароль администратора — скопируйте его (см. [Первый запуск](first-run.md)).
Остановите программу клавишами ++ctrl+c++.

## 4. Установите службу systemd

В репозитории есть шаблон службы `deploy/bambudiagnostic.service` и установщик
`deploy/install_service.sh`. Установщик подставляет вашего пользователя, группу и папку, копирует
файл в `/etc/systemd/system/bambudiagnostic.service`, включает и запускает службу. Запускайте его
**из папки проекта**:

```bash
cd ~/kuznitsa
sudo deploy/install_service.sh "$PWD"
```

Другого пользователя и группу можно передать 2-м и 3-м аргументами:
`sudo deploy/install_service.sh "$PWD" pi pi`.

### Направьте службу на виртуальное окружение

Шаблон запускает программу **системным** `python3`, в котором нет библиотек, установленных в
`.venv`. Отредактируйте службу:

```bash
sudo systemctl edit --full bambudiagnostic.service
```

Замените строку `ExecStart=`, чтобы использовался Python из `.venv` (подставьте путь к своей папке
проекта):

```ini
ExecStart=/home/pi/kuznitsa/.venv/bin/python -u main.py
```

В службе уже задан `WorkingDirectory=` — папка проекта, она нужна программе.

### Добавьте настройки (переменные окружения) {#env-vars}

Такие настройки, как адрес, на котором слушает панель, — это переменные окружения. В том же
редакторе добавьте строки `Environment=` в раздел `[Service]`, например:

```ini
Environment=WEB_HOST=0.0.0.0
Environment=COOKIE_SECURE=0
```

Эти две строки позволяют открывать панель с других устройств по обычному HTTP — только в
доверенной локальной сети. Про HTTPS см. [Пользователи и безопасность](../security.md#https-nginx).
Все переменные — в разделе [Настройки и переменные окружения](../configuration.md).

Сохраните и перезапустите:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bambudiagnostic.service
```

## 5. Полезные команды

| Что | Команда |
| --- | --- |
| Смотреть журнал (здесь пароль первого запуска) | `sudo journalctl -u bambudiagnostic.service -f` |
| Состояние | `systemctl status bambudiagnostic.service` |
| Перезапуск | `sudo systemctl restart bambudiagnostic.service` |
| Остановить / запустить | `sudo systemctl stop bambudiagnostic.service` / `sudo systemctl start bambudiagnostic.service` |

Служба перезапускает программу через 5 секунд после выхода с ошибкой (`Restart=on-failure`,
`RestartSec=5`). Именно так работает **«Применить и перезапустить»**: программа завершается, а
systemd запускает её снова с новым списком принтеров.

## Обновление

```bash
cd ~/kuznitsa
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart bambudiagnostic.service
```

## Ночное резервное копирование баз (по желанию) {#backup}

В `deploy/` есть и ночное резервное копирование: `forge-db-backup.service`, `forge-db-backup.timer`
и скрипт `forge_db_backup.py`. Каждую ночь около 03:30 он делает целостную копию
`printer_history.db`, `users.db`, `ams_history.db` и `config.py` в папку с датой, проверяет каждую
копию и хранит 14 последних.

Шаблон рассчитан на **отдельный диск, смонтированный в `/mnt/ssd`** (копии идут в
`/mnt/ssd/forge-backups`). Если диск не смонтирован, копирование отказывается работать, чтобы не
забить SD-карту. Если диск у вас в другом месте, сначала замените `/mnt/ssd` в файле службы.

Установка из папки проекта (команды из заголовка самого файла):

```bash
sed "s|__USER__|$USER|g; s|__WORKDIR__|$PWD|g" deploy/forge-db-backup.service | sudo tee /etc/systemd/system/forge-db-backup.service
sudo cp deploy/forge-db-backup.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now forge-db-backup.timer
```

Проверьте один раз вручную: `sudo systemctl start forge-db-backup.service`, затем посмотрите
`journalctl -u forge-db-backup`.

## Дальше

- [Первый запуск](first-run.md) — войдите и смените пароль администратора.
- [HTTPS через nginx](../security.md#https-nginx) — нужен для уведомлений браузера и установки панели
  как приложения.
