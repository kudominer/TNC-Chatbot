"""
item_albion.py — Lệnh /iteminfo: tra cứu chi tiết item trang bị Albion (stat/skill/passive).

Dữ liệu: blob Supabase tnc_albion_item_v1.json (schema v2) qua core.data.albion_item
(lazy cache 6h). Ngôn ngữ: ưu tiên tiếng Việt nếu có (name_vi), fallback EN.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.data.albion_item import DISCORD_MSG_LIMIT, format_item_full, search_items

SLOT_HINTS = {
    "mainhand": "vũ khí tay chính", "offhand": "tay phụ", "head": "mũ",
    "armor": "giáp", "shoes": "giày", "cape": "áo choàng", "bag": "túi",
}


class ItemAlbionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="iteminfo", description="Tra cứu item trang bị Albion (stat/skill/passive)")
    @app_commands.describe(name="Tên item (EN hoặc VI), VD: greataxe, rìu, sword")
    async def cmd_iteminfo(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)
        query = name.strip()
        results = search_items(query, limit=3)
        if not results:
            await interaction.followup.send(
                f"❌ Không tìm thấy item nào khớp `{query}`.\n"
                "Thử tên tiếng Anh đầy đủ (vd `Great Axe`, `Dual Sword`) hoặc tên tiếng Việt."
            )
            return

        uid, item = results[0]
        # Tính hints trước để dành budget cho nội dung item chính
        hints = ""
        if len(results) > 1:
            hints = ("\n\n• Còn có thể bạn muốn: "
                     + ", ".join(f"`{data.get('name_vi') or data.get('name','')}` ({u})"
                                 for u, data in results[1:]))
        msg = format_item_full(uid, item, max_chars=DISCORD_MSG_LIMIT - len(hints))
        msg += hints
        # Ép dưới giới hạn Discord (phòng emoji ngoài BMP / cắt thừa)
        if len(msg) > DISCORD_MSG_LIMIT:
            msg = msg[:DISCORD_MSG_LIMIT - 3].rstrip("\n") + "\n…"
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(ItemAlbionCog(bot))