"""
learning.py — Cog giúp bot tự nhớ user + học skill từ người dùng.

Lệnh:
- /remember <nội dung>        : bot lưu 1 fact vào memory (theo user)
- /teach <tiêu đề> <nội dung> : bot lưu kiến thức/skill (cần embedding)
- /recall <từ khóa>           : bot tìm lại kiến thức đã học (semantic search)
- /my_memory                  : xem bot nhớ gì về bạn

Ghi chú:
- Embedding tính bởi LLM provider (OpenAI/Gemini) ở nơi gọi; cog này chỉ lưu/truy vấn.
- Nếu chưa có embedding service, /teach vẫn lưu body (search sẽ fallback text LIKE).
"""
import discord
from discord import app_commands
from discord.ext import commands

import logging
from typing import Optional

from core.memory_store import (
    save_memory, get_memories, save_knowledge, search_knowledge, list_knowledge
)

logger = logging.getLogger("bot.cogs.learning")


class LearningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----- /remember -----
    @app_commands.command(name="remember", description="Dạy bot 1 fact để nhớ về bạn")
    @app_commands.describe(content="Nội dung bot cần nhớ (vd: 'tôi thích tiếng Việt')")
    async def remember(self, interaction: discord.Interaction, content: str):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        data, err = save_memory(uid, content, kind="fact")
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        await interaction.followup.send("✅ Đã ghi nhớ rồi!", ephemeral=True)

    # ----- /teach -----
    @app_commands.command(name="teach", description="Dạy bot 1 skill / kiến thức")
    @app_commands.describe(title="Tiêu đề ngắn", body="Nội dung chi tiết")
    async def teach(self, interaction: discord.Interaction, title: str, body: str):
        await interaction.response.defer(ephemeral=True)
        # TODO: nếu có embedding service, tính embedding rồi truyền vào.
        # Hiện tại lưu body không embedding (search fallback LIKE).
        data, err = save_knowledge(title, body, guild_id=str(interaction.guild_id) if interaction.guild else None)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Đã học skill **{title}**!", ephemeral=True)

    # ----- /recall -----
    @app_commands.command(name="recall", description="Tìm lại kiến thức đã dạy bot")
    @app_commands.describe(keyword="Từ khóa cần tìm")
    async def recall(self, interaction: discord.Interaction, keyword: str):
        await interaction.response.defer(ephemeral=True)
        # Fallback: list + filter LIKE (khi chưa có embedding)
        rows = list_knowledge(limit=100)
        matched = [r for r in rows if keyword.lower() in (r.get("title") or "").lower()]
        if not matched:
            await interaction.followup.send("🤔 Chưa học gì khớp từ khóa đó.", ephemeral=True)
            return
        lines = [f"**{r['title']}**" for r in matched[:10]]
        await interaction.followup.send("📚 Đã học:\n" + "\n".join(lines), ephemeral=True)

    # ----- /my_memory -----
    @app_commands.command(name="my_memory", description="Xem bot nhớ gì về bạn")
    async def my_memory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        rows = get_memories(uid, limit=30)
        if not rows:
            await interaction.followup.send("🤷 Chưa nhớ gì về bạn cả.", ephemeral=True)
            return
        lines = [f"• {r['content']}" for r in rows[:20]]
        await interaction.followup.send("🧠 Những gì tôi nhớ về bạn:\n" + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LearningCog(bot))
