import asyncio
from datetime import datetime, timezone
import aiohttp
from twitchio import Client, Message
import re
from aiohttp import web  # 👈 добавлено для анти-сна

# ======= НАСТРОЙКИ =======
TWITCH_NICK = "ikinonesa"
TWITCH_TOKEN = "oauth:m9fjxy56isocq24r4rq7fo5vwpbxg5"
TWITCH_CHANNEL = "uzya"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1428397350174331081/47GVqb8wZyMOAg-AOu22G7aYrj8C1BFPphnC4jzbNG0jQ2RVQEVOg0tgx88DEymkp7PU"
THREAD_ID = "1427256764394639420"  # ваш пост форума
MAX_MONTHS_BACK = 12  # сколько месяцев проверяем назад
BESTLOGS_BASE_URL = "https://bestlogs.supa.codes/channel/uzya/user"
# ===============================

banned_lock = asyncio.Lock()

# --- send discord message to forum post ---
async def send_discord_message(content: str):
    url = f"{DISCORD_WEBHOOK_URL}?thread_id={THREAD_ID}"
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"content": content}
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status in (200, 204):
                    print(f"[Discord] ✅ Сообщение отправлено в пост форума")
                else:
                    text = await resp.text()
                    print(f"[Discord] Ошибка отправки ({resp.status}): {text}")
        except Exception as e:
            print(f"[Discord] Exception: {e}")

# --- fetch user log for a given month ---
async def fetch_user_month_log(nick: str, year: int, month: int):
    url = f"{BESTLOGS_BASE_URL}/{nick}/{year}/{month:02d}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    print(f"[Logs] Не удалось получить {url} — статус {resp.status}")
                    return None
        except Exception as e:
            print(f"[Logs] Ошибка запроса {url}: {e}")
            return None

# --- parse log text for last normal message ---
def find_last_message_in_log_text(log_text: str, nick: str, ban_dt: datetime):
    last_found = None
    nick_l = nick.lower()
    pattern = re.compile(
        r"^\[(?P<ts>[\d\- :]+)\]\s+#\S+\s+(?P<user>\S+):\s*(?P<msg>.*)$"
    )
    for line in log_text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        user = m.group("user").lower()
        if user != nick_l:
            continue
        msg = m.group("msg").strip()
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts > ban_dt:
            continue
        # игнорируем сообщения о бане
        if re.search(r'\b(ban|has been banned|timed out|was timed out)\b', msg, re.IGNORECASE):
            continue
        last_found = (msg, ts)
    return last_found

# --- get last message for nick ---
async def get_last_message_for_nick(nick: str):
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    ban_dt = now

    for _ in range(MAX_MONTHS_BACK):
        log_text = await fetch_user_month_log(nick, year, month)
        if log_text:
            found = find_last_message_in_log_text(log_text, nick, ban_dt)
            if found:
                return found
        # идём на месяц назад
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return (None, None)

# --- handle a ban asynchronously ---
async def handle_ban(nick: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] Бан получен: {nick}, ожидаем 10 секунд для логов...")
    await asyncio.sleep(10)  # ждём 10 секунд перед проверкой логов

    msg_text, msg_ts = await get_last_message_for_nick(nick)
    if msg_text:
        ts_str = msg_ts.strftime("%Y-%m-%d %H:%M:%S") if msg_ts else "?"
        to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение ({ts_str} UTC): {msg_text}"
    else:
        to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение: (нет сообщений в логах)"

    print(f"[{datetime.now(timezone.utc).isoformat()}] Бан обработан: {nick} — last_msg={msg_text}")
    await send_discord_message(to_send)

# --- twitch client ---
class BanWatcher(Client):
    async def event_ready(self):
        print(f"✅ Connected as {TWITCH_NICK}, listening {TWITCH_CHANNEL}")

    async def event_message(self, message: Message):
        return  # не хранится в памяти, ищем по логам

    async def event_raw_data(self, raw: str):
        if " CLEARCHAT " not in raw or f"#{TWITCH_CHANNEL}" not in raw:
            return

        if " :" in raw:
            nick = raw.split(" :")[-1].strip().split()[0].lstrip('@').lower()
        else:
            nick = None

        if not nick:
            return

        asyncio.create_task(handle_ban(nick))

# --- анти-сон вебсервер ---
async def keepalive_server():
    async def handle(request):
        return web.Response(text="Bot is alive and running!")

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("[KeepAlive] Мини вебсервер запущен на порту 8080")

# --- main ---
async def main():
    await keepalive_server()  # анти-сон
    bot = BanWatcher(token=TWITCH_TOKEN, initial_channels=[TWITCH_CHANNEL])
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")

