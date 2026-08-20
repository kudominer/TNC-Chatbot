"""Smoke test: kiểm tra tất cả chatbot cog load được không."""
import os
import discord
import pytest

# Set dummy env vars before importing bot modules
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DISCORD_GUILD_ID", "123456789")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon")

from discord.ext import commands

EXTENSIONS = [
    "cogs.chat_ai",
    "cogs.chat_logger",
    "cogs.learning",
    "cogs.item_albion",
    "cogs.wiki",
]


@pytest.fixture
def bot():
    intents = discord.Intents.default()
    return commands.Bot(command_prefix="!", intents=intents)


@pytest.mark.asyncio
async def test_all_cogs_load(bot):
    """Mỗi chatbot cog phải load được mà không lỗi."""
    for ext in EXTENSIONS:
        await bot.load_extension(ext)


@pytest.mark.asyncio
async def test_chat_ai_loaded(bot):
    """chat_ai cog có on_message listener."""
    await bot.load_extension("cogs.chat_ai")
    cog = bot.get_cog("ChatAI")
    assert cog is not None, "ChatAI cog not found after load"
