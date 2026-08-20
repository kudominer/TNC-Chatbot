import os
import sys
import signal
import asyncio
import traceback

print(f"🚀 [STARTUP] bot/main.py loaded. Python {sys.version}")

# python -m bot.main chạy từ repo root → cần thêm bot/ vào sys.path
# để import core.* và cogs.* hoạt động
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print(f"🚀 [STARTUP] sys.path set. dir={os.path.dirname(os.path.abspath(__file__))}")

try:
    import discord
    from discord.ext import commands
    print(f"🚀 [STARTUP] discord.py imported OK")
except Exception as e:
    print(f"❌ [STARTUP] Failed to import discord: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    from core.config import BOT_SESSION_ID, GUILD_ID, TOKEN
    from core.webserver import keep_alive
    from core.system_logger import SystemLogger
    from core.heartbeat import start as start_heartbeat
    print(f"🚀 [STARTUP] core.* imported OK")
except Exception as e:
    print(f"❌ [STARTUP] Failed to import core: {e}")
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
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix=["!", "."], intents=intents, help_command=None)

    async def setup_hook(self):
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
        synced = await self.tree.sync(guild=guild)
        print(f"✅ Đã sync {len(synced)} slash commands vào guild!")

        # Bật heartbeat đập tim lên Supabase
        start_heartbeat(self)


bot = TNCChatbot()


# ==============================================================================
# WATCHDOG — TỰ ĐỘNG RESTART NẾU GATEWAY CHẾT QUÁ LÂU
# ==============================================================================
GATEWAY_DEAD_THRESHOLD = 180  # giây


@bot.event
async def on_disconnect():
    print("⚠️ [Watchdog] Mất kết nối gateway Discord. Bắt đầu đếm ngược tự restart...")


@bot.event
async def on_resume():
    print("✅ [Watchdog] Đã kết nối lại gateway. Hủy đếm ngược.")


async def _gateway_watchdog():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(15)
        if bot.is_closed():
            break
        if bot.latency == float("inf"):
            if not hasattr(bot, "_dead_since"):
                bot._dead_since = asyncio.get_event_loop().time()
            dead_for = asyncio.get_event_loop().time() - bot._dead_since
            if dead_for >= GATEWAY_DEAD_THRESHOLD:
                print(f"🔥 [Watchdog] Gateway chết {dead_for:.0f}s — TỰ ĐỘNG RESTART!")
                os.kill(os.getpid(), signal.SIGTERM)
        else:
            bot._dead_since = None


@bot.event
async def on_ready():
    print(f"✅ Chatbot đã hoạt động: {bot.user} | ID: {bot.user.id}")
    bot_name = os.getenv("BOT_NAME", "NDZ")
    print(f"✅ {bot_name} Chatbot v1.0 [AI Chat + Wiki + Items + Learning] Online! Session: {BOT_SESSION_ID}")
    # Khởi chạy watchdog sau khi bot sẵn sàng
    bot.loop.create_task(_gateway_watchdog())


if __name__ == "__main__":
    keep_alive(bot)
    bot.run(TOKEN)
