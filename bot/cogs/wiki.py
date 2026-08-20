import discord
from discord import app_commands
from discord.ext import commands
import asyncio

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

class WikiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _search_wiki_async(self, query: str) -> str:
        if DDGS is None:
            return "[LỖI: Chưa cài thư viện duckduckgo-search]"
        try:
            def _sync_search():
                results = DDGS().text(f"site:wiki.albiononline.com {query}", max_results=3)
                return list(results)
            
            results = await asyncio.to_thread(_sync_search)
            if not results:
                return "[Không tìm thấy thông tin trên Albion Wiki]"
            
            wiki_text = "Dữ liệu cào được từ Albion Wiki:\n"
            for r in results:
                wiki_text += f"- {r.get('title', '')}: {r.get('body', '')}\n"
            return wiki_text
        except Exception as e:
            import logging
            logging.error(f"[Error] Lỗi tra cứu Wiki: {e}")
            return f"[Lỗi tra cứu Wiki: {e}]"

    @app_commands.command(name="wiki", description="Tra cứu kiến thức chuẩn từ Albion Wiki")
    @app_commands.describe(query="Từ khóa cần tra cứu (VD: bloodletter, thetford cape)")
    async def cmd_wiki(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        wiki_data = await self._search_wiki_async(query)
        msg = f"🔍 **Đang tra cứu Wiki cho:** `{query}`\n\n{wiki_data}\n\n*Gợi ý: Tham khảo thông tin này nhé!*"
        await interaction.followup.send(msg)

async def setup(bot: commands.Bot):
    await bot.add_cog(WikiCog(bot))
