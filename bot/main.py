import os
import signal
import asyncio

import discord
from discord.ext import commands

from core.config import BOT_SESSION_ID, GUILD_ID, TOKEN
from core.webserver import keep_alive
from core.system_logger import SystemLogger
from core.heartbeat import start as start_heartbeat

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
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"✅ Đã sync {len(synced)} slash commands vào guild!")

        # Bật ghi log hệ thống
        SystemLogger.start(self)
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
