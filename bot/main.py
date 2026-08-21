import os
import sys
import signal
import threading
import traceback

print(f"🚀 [STARTUP] bot/main.py loaded. Python {sys.version}")

# python -m bot.main chạy từ repo root → cần thêm bot/ vào sys.path
# để import core.* và cogs.* hoạt động
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print(f"🚀 [STARTUP] sys.path set. dir={os.path.dirname(os.path.abspath(__file__))}")

# Boot logger phải lên sóng sớm nhất có thể (chỉ cần os + urllib)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))
from core.boot_logger import boot_log  # noqa: E402

boot_log(f"Process start — Python {sys.version.split()[0]}, pid={os.getpid()}")

try:
    import discord
    from discord.ext import commands
    boot_log("discord.py imported OK")
except Exception as e:
    boot_log(f"Failed to import discord: {e}", "ERROR")
    traceback.print_exc()
    sys.exit(1)

try:
    from core.config import BOT_SESSION_ID, GUILD_ID, TOKEN
    from core.webserver import keep_alive
    from core.system_logger import SystemLogger
    from core.heartbeat import start as start_heartbeat
    boot_log("core.* imported OK")
except Exception as e:
    boot_log(f"Failed to import core: {e}", "ERROR")
    traceback.print_exc()
    sys.exit(1)

# ==============================================================================
# TNC CHATBOT — AI Chat Only
# ==============================================================================
EXTENSIONS = [
    "cogs.chat_ai",
    "cogs.chat_logger",
    "cogs.learning",
    "cogs.item_albion",
    "cogs.wiki",
]


class TNCChatbot(commands.Bot):
    _watchdog_started: bool = False

    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix=["!", "."], intents=intents, help_command=None)

    async def setup_hook(self):
        boot_log("setup_hook bắt đầu — Discord login THÀNH CÔNG")
        # SystemLogger MUST start trước khi load cogs
        # để bắt được lỗi import/cog load
        SystemLogger.start(self)

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
            except Exception as e:
                print(f"❌ Failed to load {extension}: {e}")
                traceback.print_exc()

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            print(f"✅ Đã sync {len(synced)} slash commands vào guild!")
        except Exception as e:
            # Sync fail (thường do rate-limit khi deploy liên tục) không được
            # giết bot — lệnh cũ trong guild vẫn hoạt động.
            print(f"⚠️ Sync slash commands thất bại (bot vẫn chạy): {e}")

        # Bật heartbeat đập tim lên Supabase
        start_heartbeat(self)


bot = TNCChatbot()


# ==============================================================================
# WATCHDOG — TỰ ĐỘNG RESTART NẾU GATEWAY CHẾT QUÁ LÂU
# ==============================================================================
GATEWAY_DEAD_THRESHOLD = 180  # giây

_watchdog_stop = threading.Event()


@bot.event
async def on_disconnect():
    print("⚠️ [Watchdog] Mất kết nối gateway Discord. Bắt đầu đếm ngược tự restart...")


@bot.event
async def on_resume():
    print("✅ [Watchdog] Đã kết nối lại gateway. Hủy đếm ngược.")


def _gateway_watchdog_thread():
    """Watchdog chạy ở OS thread RIÊNG — không phụ thuộc event loop.

    Phiên bản cũ chạy trên event loop, nên khi loop bị block bởi lệnh
    mạng đồng bộ treo (Supabase không timeout), chính watchdog cũng chết
    theo → bot ngưng rep vĩnh viễn trong khi Flask vẫn trả lời 200.
    Thread này đo "nhịp" của event loop qua callback call_soon_threadsafe:
    nếu loop không xử lý callback trong GATEWAY_DEAD_THRESHOLD giây,
    coi như loop đã treo → kill process để Render restart.
    """
    import time as _time

    heartbeat_tick = {"t": None}

    def _tick():
        heartbeat_tick["t"] = _time.monotonic()

    while not _watchdog_stop.is_set():
        try:
            bot.loop.call_soon_threadsafe(_tick)
        except RuntimeError:
            break  # loop đã đóng
        _time.sleep(15)
        last = heartbeat_tick["t"]
        if last is not None and (_time.monotonic() - last) > GATEWAY_DEAD_THRESHOLD:
            print(f"🔥 [Watchdog] Event loop treo quá {GATEWAY_DEAD_THRESHOLD}s "
                  f"— TỰ ĐỘNG RESTART!")
            os.kill(os.getpid(), signal.SIGTERM)
            return


@bot.event
async def on_ready():
    boot_log(f"on_ready — {bot.user} SẴN SÀNG, gateway hoạt động")
    print(f"✅ Chatbot đã hoạt động: {bot.user} | ID: {bot.user.id}")
    bot_name = os.getenv("BOT_NAME", "NDZ")
    print(f"✅ {bot_name} Chatbot v1.0 [AI Chat + Wiki + Items + Learning] Online! Session: {BOT_SESSION_ID}")
    # Khởi chạy watchdog ở thread riêng (chỉ 1 lần)
    if not getattr(bot, "_watchdog_started", False):
        bot._watchdog_started = True
        threading.Thread(target=_gateway_watchdog_thread,
                         name="gateway-watchdog", daemon=True).start()


if __name__ == "__main__":
    keep_alive(bot)
    boot_log("Đang gọi bot.run() — chờ Discord login...")
    try:
        bot.run(TOKEN)
    except BaseException as e:
        print(f"❌ [FATAL] bot.run() thất bại: {e!r}")
        traceback.print_exc()
        boot_log(f"bot.run() THẤT BẠI: {e!r}", "ERROR")
        # BẮT BUỘC hard-exit: thread Flask non-daemon sẽ giữ process sống
        # thành zombie (/health vẫn 200) → Render thấy healthy và KHÔNG BAO GIỜ
        # restart dù bot đã chết từ lâu. Exit code != 0 buộc Render restart.
        os._exit(1)
    # bot.run() trả về bình thường (shutdown sạch) cũng phải thoát,
    # tránh trở thành zombie như trên.
    boot_log("bot.run() đã kết thúc sạch — thoát process để Render restart.")
    os._exit(0)
