import discord
from discord.ext import commands, tasks
from core.database import execute
import datetime

class ChatLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.known_channels = set()
        self.cleanup_old_messages.start()

    def cog_unload(self):
        self.cleanup_old_messages.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        # Bỏ qua các tin nhắn rỗng (ví dụ chỉ có ảnh mà không có chữ)
        if not message.content.strip():
            return
            
        # Bỏ qua các tin nhắn gọi lệnh (bắt đầu bằng /, !, .) để tiết kiệm dung lượng
        if message.content.startswith(("/", ".", "!")):
            return

        if not message.guild:
            return

        try:
            guild_id = str(message.guild.id)
            channel_id = str(message.channel.id)
            
            # Upsert guild and channel if not cached
            if channel_id not in self.known_channels:
                try:
                    # Upsert Guild Config (to ensure FK is valid)
                    _, err = execute(lambda c: c.table("guild_config").upsert(
                        {"guild_id": guild_id}, on_conflict="guild_id"))
                    if err:
                        print(f"Lỗi Upsert Guild: {err}")
                        return
                    
                    # Upsert Discord Channels (to ensure FK is valid)
                    _, err2 = execute(lambda c: c.table("discord_channels").upsert({
                        "id": channel_id,
                        "guild_id": guild_id,
                        "name": getattr(message.channel, "name", "unknown"),
                        "type": str(getattr(message.channel, "type", "text"))
                    }, on_conflict="id"))
                    if err2:
                        print(f"Lỗi Upsert Channel: {err2}")
                        return
                    
                    self.known_channels.add(channel_id)
                except Exception as e:
                    print(f"Lỗi khi Upsert Channel/Guild trong chat_logger: {e}")
                    return # Ngừng log tin nhắn này nếu không gán được channel

            data = {
                "id": str(message.id),
                "user_id": str(message.author.id),
                "author_name": message.author.display_name,
                "channel_id": channel_id,
                "channel_name": getattr(message.channel, "name", "unknown"),
                "content": message.content,
                "created_at": message.created_at.isoformat()
            }
            # Chạy ngầm việc insert để không làm đứng bot
            self.bot.loop.create_task(self._insert_log(data))
        except Exception as e:
            print(f"Lỗi khi chuẩn bị log chat: {e}")

    async def _insert_log(self, data):
        try:
            # Insert log âm thầm
            _, err = execute(lambda c: c.table("chat_history").insert(data))
            if err:
                pass  # Bỏ qua lỗi ngầm để tránh spam console khi DB lỗi
        except Exception:
            pass  # Bỏ qua lỗi ngầm để tránh spam console khi DB lỗi

    @tasks.loop(hours=24)
    async def cleanup_old_messages(self):
        """Tự động xoá tin nhắn cũ hơn 7 ngày để tiết kiệm dung lượng."""
        try:
            # Lấy mốc thời gian 7 ngày trước
            seven_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
            
            # Thực thi xoá
            _, err = execute(lambda c: c.table("chat_history").delete().lte("created_at", seven_days_ago))
            if err:
                print(f"Lỗi khi dọn dẹp tin nhắn cũ: {err}")
                return
            print(f"🧹 Đã chạy tác vụ dọn dẹp tin nhắn chat cũ hơn 7 ngày trên Supabase.")
        except Exception as e:
            print(f"Lỗi khi dọn dẹp tin nhắn cũ: {e}")

    @cleanup_old_messages.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(ChatLogger(bot))
