# Telegram notifications and proxy

Kuznitsa can run a Telegram bot that keeps a live summary of the farm in your chat and sends
messages when a print finishes, pauses or fails. It is off by default. Everything is set on the
web page — the token, chat and proxies are stored in the dashboard's database, not in files.

## 1. Create a bot

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a name and a user name for the bot.
3. BotFather replies with a **token** (a long string with a colon). Keep it secret.

## 2. Enter the token

1. Open **Admin → System** («Админ → Система»), section **Telegram**.
2. Turn on **Telegram bot** («Телеграм-бот»).
3. Unfold **Token, chat, templates and proxies** («Токен, чат, шаблоны и прокси»).
4. Paste the token into **Bot token** («Токен бота»). Changing the token restarts the bot.

Settings in this section are saved as you change them (a «Сохранено» mark appears) — no restart is
needed.

## 3. Allow your chat

The field **Allowed chat ID** («Разрешённый ID чата») is the number of the Telegram chat the bot
works with.

!!! warning "Fill in the chat ID — the bot does not work without it"
    The hint under the field says that an empty field means "no restriction", but in version 2.0.0
    the bot answers `/start` **only** from the chat whose ID is entered here. With the field empty
    every chat is refused and no notifications are sent.

How to find your chat ID with the dashboard itself:

1. With the token entered and the bot on, open your bot in Telegram and send `/start`.
2. The bot refuses an unknown chat and writes its number to the dashboard's log:
   `Denied chat 123456789`.
3. Enter that number into **Allowed chat ID**.
4. Send `/start` again.

Where the log is: see [First run](install/first-run.md#the-administrator-is-created-automatically).

## 4. The live summary

After `/start` the bot sends a message with the state of every printer and keeps **editing the
same message** every **Update interval, sec** («Интервал обновления, сек», 2 by default, 1–3600).
Every new `/start` sends a fresh message. You can pin it in the chat yourself.

The summary lists printers in this order: errors, pauses, finished, printing (soonest finish
first), idle, the rest; printers in maintenance are last. For example:

```text
🔴 K1C — ошибка: <the printer's error text>
🟡 X1C — 80% · case
✅ A1 mini — завершено · clip
🟢 P1S-1 — 47% · bracket x4
⏱2ч10м · ⏳1ч55м · 🌡220/60°C
⬛ Ghost — офлайн
🔧 H2D — на обслуживании
```

⏱ is the time left, ⏳ the time printed, 🌡 nozzle / bed. The words are in Russian, as in the
interface.

## 5. Event notifications

Under **Event notifications** («Уведомления о событиях») each event has a switch and a message
template:

| Event | Default template | Sent when |
| --- | --- | --- |
| **Print finished** («Завершение печати») | `✅ Печать завершена: {label}` | a printer goes from printing to finished |
| └ **Repeat until taken off** («Повторять, пока не забрали») | — | while the printer stays finished, repeat every **N** minutes («Интервал повтора, мин», 30 by default, 1–1440) |
| **Printer error** («Ошибка принтера») | `🔴 Ошибка: {label}` | a printer goes into an error; the printer's error text is added |
| **Print paused** («Пауза печати») | `⚠️ Пауза: {label}` | a printing printer pauses |

- All three events are **off** by default; the repeat switch is on, so it works once you turn on
  *Print finished*.
- `{label}` is replaced by the printer's name. It is the only placeholder — any other `{...}` is
  rejected. Messages are sent with Telegram's HTML formatting, so you can use tags such as
  `<b>…</b>` in a template.
- Printers in maintenance mode send nothing.

## 6. Proxy for Telegram

If Telegram is blocked in your network, add proxies under **Proxy for Telegram** («Прокси для
Telegram»). The section is shown only while the bot is on.

- Enter an address in the form `http://login:password@host:port` and click **+ Add** («+ Добавить»).
  Only `http://` and `https://` proxies are accepted; login and password are optional.
- If the login or password contains `/`, `?`, `#` or `@`, write them encoded: `%2F`, `%3F`, `%23`,
  `%40`.
- Every proxy is checked regularly by a request to `api.telegram.org`; the table shows **Address**
  («Адрес»), **Status** («Статус») and **Latency** («Задержка»). The bot always uses the fastest
  working proxy and switches automatically.
- **Check interval, sec** («Интервал проверки, сек») — 600 by default, 30–86400. **Check now**
  («Проверить сейчас») checks at once; **Last check** («Последняя проверка») shows when.
- With no proxies in the list the bot connects to Telegram directly.
- Proxy logins and passwords are masked in the dashboard's logs.

## If the bot is silent

- Is **Telegram bot** switched on and the token correct? The log says `Telegram bot started` when
  the bot starts.
- Is **Allowed chat ID** filled in with your chat's number (see step 3)?
- Did you send `/start` to the bot at least once? Telegram does not let a bot write to someone who
  never started it.
- Does the dashboard's computer reach `api.telegram.org`? If not, add a proxy.
