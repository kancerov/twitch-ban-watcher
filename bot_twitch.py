import asyncio
from datetime import datetime, timezone
import aiohttp
from twitchio import Client, Message
import re
from aiohttp import web # 👈 добавлено для анти-сна

# ======= НАСТРОЙКИ =======
TWITCH_NICK = "ikinonesa"
TWITCH_TOKEN = "oauth:m9fjxy56isocq24r4rq7fo5vwpbxg5"
TWITCH_CHANNEL = "uzya"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1428397350174331081/47GVqb8wZyMOAg-AOu22G7aYrj8C1BFPphnC4jzbNG0jQ2RVQEVOg0tgx88DEymkp7PU"
THREAD_ID = "1427256764394639420"  # ваш пост форума
MAX_MONTHS_BACK = 24  # сколько месяцев проверяем назад
BESTLOGS_BASE_URL = "https://bestlogs.supa.codes/channel/uzya/user"
STREAM_CHECK_INTERVAL = 120  # каждые 2 минуты проверяем стрим
STREAM_PREVIEW_IMAGE = "https://cdn.discordapp.com/attachments/1428397665384927262/1429105246566748301/image.png?ex=68f4ed7a&is=68f39bfa&hm=679549648866f47d654ce21a56fcd260bf73da85013e64211f7f4ca10bdfb4d1&"
# ===============================

banned_lock = asyncio.Lock()
stream_live = False  # состояние эфира

# --- отправка сообщения в Discord ---
async def send_discord_message(content: str = None, embed_image: str = None):
    url = f"{DISCORD_WEBHOOK_URL}?thread_id={THREAD_ID}"
    async with aiohttp.ClientSession() as session:
        payload = {}
        if content:
            payload["content"] = content
        if embed_image:
            payload["embeds"] = [{"image": {"url": embed_image}}]
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status in (200, 204):
                    print(f"[Discord] ✅ Сообщение отправлено")
                else:
                    text = await resp.text()
                    print(f"[Discord] Ошибка ({resp.status}): {text}")
        except Exception as e:
            print(f"[Discord] Exception: {e}")

# --- получить лог пользователя за месяц ---
async def fetch_user_month_log(nick: str, year: int, month: int):
    url = f"{BESTLOGS_BASE_URL}/{nick}/{year}/{month:02d}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.text()
                return None
        except Exception as e:
            print(f"[Logs] Ошибка запроса {url}: {e}")
            return None

# --- поиск последнего сообщения пользователя ---
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
        if re.search(r'\b(ban|has been banned|timed out|was timed out)\b', msg, re.IGNORECASE):
            continue
        last_found = (msg, ts)
    return last_found

# --- получить последнее сообщение ---
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
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return (None, None)

# --- обработка бана ---
async def handle_ban(nick: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] Бан получен: {nick}")
    await asyncio.sleep(10)
    msg_text, msg_ts = await get_last_message_for_nick(nick)
    if msg_text:
        ts_str = msg_ts.strftime("%Y-%m-%d %H:%M:%S") if msg_ts else "?"
        to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение ({ts_str} UTC): {msg_text}"
    else:
        to_send = f"Пользователь забанен: **{nick}**\nПоследнее сообщение: (нет сообщений в логах)"
    await send_discord_message(to_send)

# --- Twitch клиент ---
class BanWatcher(Client):
    async def event_ready(self):
        print(f"✅ Connected as {TWITCH_NICK}, listening {TWITCH_CHANNEL}")

    async def event_message(self, message: Message):
        return

    async def event_raw_data(self, raw: str):
        if " CLEARCHAT " not in raw or f"#{TWITCH_CHANNEL}" not in raw:
            return
        # --- игнорируем таймауты ---
        if "ban-duration=" in raw:
            return
        # --- извлекаем ник ---
        if " :" in raw:
            nick = raw.split(" :")[-1].strip().split()[0].lstrip('@').lower()
        else:
            nick = None
        if not nick:
            return
        asyncio.create_task(handle_ban(nick))

# --- проверка начала стрима (через GQL) ---
async def check_stream_loop():
    global stream_live
    headers = {"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"}
    query = {
        "query": "query($login: String!){ user(login:$login){ stream{ id type } } }",
        "variables": {"login": TWITCH_CHANNEL},
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                async with session.post("https://gql.twitch.tv/gql", json=query, timeout=10) as resp:
                    data = await resp.json()
                    stream = data.get("data", {}).get("user", {}).get("stream")
                    if stream and not stream_live:
                        stream_live = True
                        print(f"[Stream] 🚀 Стрим '{TWITCH_CHANNEL}' начался!")
                        await send_discord_message(embed_image=STREAM_PREVIEW_IMAGE)
                    elif not stream and stream_live:
                        stream_live = False
                        print(f"[Stream] ⛔ Стрим '{TWITCH_CHANNEL}' завершён")
            except Exception as e:
                print(f"[Stream] Ошибка: {e}")
            await asyncio.sleep(STREAM_CHECK_INTERVAL)

# --- анти-сон сервер ---
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
    await keepalive_server()
    bot = BanWatcher(token=TWITCH_TOKEN, initial_channels=[TWITCH_CHANNEL])
    await asyncio.gather(bot.start(), check_stream_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")

