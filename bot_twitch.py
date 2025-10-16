import asyncio
from datetime import datetime, timedelta
import aiohttp
from twitchio import Client, Message
import re

# ======= НАСТРОЙКИ (вставь свои данные) =======
TWITCH_NICK = "ikinonesa"   # имя аккаунта который подключается (он же бот)
TWITCH_TOKEN = "oauth:m9fjxy56isocq24r4rq7fo5vwpbxg5"  # oauth token формата "oauth:...." (пользовательский)
TWITCH_CHANNEL = "uzya"                 # канал для прослушки (без #)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1428025518514638910/DV2F1msT3xpOQJDM4Gu4RViS52-yyzasbzufQSXKnC52s90C6w1tLzydQtQx9GXysUXd"
CHECK_INTERVAL_SECONDS = 30 * 60    



CHECK_INTERVAL_SECONDS = 30 * 60  # 30 минут (для периодических уведомлений)
LOG_LOOKBACK_DAYS = 3  # сколько предыдущих дней логов проверять для поиска последнего сообщения
BESTLOGS_BASE = "https://bestlogs.supa.codes/channel"  # пример: /channel/uzya/2025/10/16
# ===============================

banned_in_window = set()
banned_lock = asyncio.Lock()
latest_messages = {}  # { username: (message_text, timestamp_iso) }  - локальная память

# --- helper: send discord message ---
async def send_discord_message(content: str):
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"content": content}
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    print(f"[Discord] Ошибка отправки ({resp.status}): {text}")
                else:
                    print(f"[Discord] ✅ Отправлено: {content}")
        except Exception as e:
            print(f"[Discord] Exception: {e}")

# --- helper: fetch and parse bestlogs for a single date ---
async def fetch_bestlogs_for_date(session: aiohttp.ClientSession, channel: str, date: datetime):
    """
    Получает plain-text лог с bestlogs.supa.codes для канала и даты.
    Возвращает текст или None.
    """
    url = f"{BESTLOGS_BASE}/{channel}/{date.year}/{date.month:02d}/{date.day:02d}"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                return text
            else:
                print(f"[Logs] Не удалось получить {url} — статус {resp.status}")
                return None
    except Exception as e:
        print(f"[Logs] Ошибка запроса {url}: {e}")
        return None

# --- helper: find last message by nick in logs text before a cutoff time ---
def find_last_message_in_log_text(log_text: str, nick: str, cutoff_dt: datetime):
    """
    Парсим лог (plain text с timestamp в начале каждой записи),
    ищем последние вхождения " #uzya nick: message" (учитываем возможные варианты 'nick has been banned' и т.д.)
    Возвращаем строку сообщения и временную метку, либо None.
    Пример строки: "[2025-10-16 10:27:13] #uzya splinterzpm: Привет"
    """
    nick_l = nick.lower()
    pattern = re.compile(r"^\[(?P<ts>[\d\- :]+)\]\s+#" + re.escape(TWITCH_CHANNEL) + r"\s+(?P<nick>\S+?):\s*(?P<msg>.*)$")
    last_found = None
    for line in log_text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        ln = m.group("nick").lower()
        if ln != nick_l:
            continue
        # parse timestamp
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except Exception:
            # если формат другой — пропускаем
            continue
        if ts <= cutoff_dt:
            last_found = (m.group("msg"), ts)
    return last_found  # None or (msg, ts)

# --- try to get last message for nick by checking memory + external logs ---
async def get_last_message_for_nick(nick: str, ban_arrival_dt: datetime):
    """
    1) Сначала проверяет latest_messages (в памяти).
    2) Если нет — делает запросы к BESTLOGS_BASE для текущей даты и LOG_LOOKBACK_DAYS назад.
    Возвращает (message_text, timestamp) или (None, None).
    """
    nick_l = nick.lower()
    # 1) in-memory
    entry = latest_messages.get(nick_l)
    if entry:
        return entry  # (msg, ts)

    # 2) try external logs
    async with aiohttp.ClientSession() as session:
        for delta in range(0, LOG_LOOKBACK_DAYS + 1):
            date = (ban_arrival_dt - timedelta(days=delta)).date()
            text = await fetch_bestlogs_for_date(session, TWITCH_CHANNEL, datetime(date.year, date.month, date.day))
            if not text:
                continue
            found = find_last_message_in_log_text(text, nick, ban_arrival_dt)
            if found:
                return found  # (msg, ts)

    return (None, None)

# --- periodic report (same as before, can be left or removed) ---
async def periodic_report():
    global banned_in_window
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        async with banned_lock:
            current_bans = set(banned_in_window)
            banned_in_window.clear()
        if current_bans:
            msg = "\n".join(current_bans)
            await send_discord_message(f"Баны за последние 30 минут:\n{msg}")
        else:
            await send_discord_message("За последние 30 минут банов не было.")

# --- twitch client ---
class BanWatcher(Client):
    async def event_ready(self):
        print(f"✅ Connected as {TWITCH_NICK}, listening {TWITCH_CHANNEL}")

    async def event_message(self, message: Message):
        if message.echo:
            return
        username = message.author.name.lower()
        latest_messages[username] = (message.content, datetime.utcnow())
        # optionally trim dict to limit memory
        if len(latest_messages) > 2000:
            # простой удалитель старых: удаляем случайный/первый ключ (можно сделать OrderedDict)
            k = next(iter(latest_messages))
            del latest_messages[k]

    async def event_raw_data(self, raw: str):
        if " CLEARCHAT " not in raw:
            return
        if f"#{TWITCH_CHANNEL}" not in raw:
            return

        if " :" in raw:
            nick = raw.split(" :")[-1].strip().split()[0].lstrip('@').lower()
        else:
            nick = None

        if not nick:
            return

        ban_dt = datetime.utcnow()  # момент получения события (приближённо время бана)
        async with banned_lock:
            banned_in_window.add(nick)

        # найдем последнее сообщение
        msg_text, msg_ts = await get_last_message_for_nick(nick, ban_dt)

        if msg_text:
            # форматируем время если есть
            ts_str = msg_ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(msg_ts, datetime) else "?"
            to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение ({ts_str} UTC): {msg_text}"
        else:
            to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение: (нет данных)"

        print(f"[{ban_dt.isoformat()}] Бан: {nick} — last_msg={msg_text}")
        await send_discord_message(to_send)

# --- main ---
async def main():
    bot = BanWatcher(token=TWITCH_TOKEN, initial_channels=[TWITCH_CHANNEL])
    asyncio.create_task(periodic_report())
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")