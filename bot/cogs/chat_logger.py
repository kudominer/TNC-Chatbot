import asyncio
import discord
from discord.ext import commands, tasks
from core.database import execute
from core.config import GUILD_ID
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
            # Upsert log âm thầm (dùng id làm khóa — chống trùng khi backfill + realtime)
            _, err = execute(lambda c: c.table("chat_history").upsert(
                data, on_conflict="id"))
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

    @commands.Cog.listener()
    async def on_ready(self):
        """Tự động nạp lịch sử các kênh 1 lần khi bot sẵn sàng (hướng B)."""
        if getattr(self, "_backfilled", False):
            return
        self._backfilled = True
        guild = self.bot.get_guild(GUILD_ID) or (
            self.bot.guilds[0] if self.bot.guilds else None)
        if not guild:
            print("⚠️ [backfill] Không tìm thấy guild để nạp lịch sử.")
            return
        self.bot.loop.create_task(self._backfill_guild_history(guild))

    async def _backfill_guild_history(self, guild):
        """Quét lịch sử các kênh text bot đọc được, upsert vào chat_history.

        Chạy 1 lần mỗi phiên. Upsert theo id nên chạy lại vẫn an toàn (không trùng).
        Giới hạn 1000 tin/kênh + throttle 1s để tránh 429. Đồng thời upsert
        discord_channels để bot nhận diện được tên kênh khi tóm tắt.
        """
        print("🔄 [backfill] Bắt đầu nạp lịch sử các kênh...")
        total = 0
        chan_ok = 0
        try:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me) if guild.me else None
                if not perms or not perms.read_message_history:
                    continue
                # Đảm bảo channel có mặt trong discord_channels (nhận diện tên kênh)
                _, e2 = execute(lambda c: c.table("discord_channels").upsert({
                    "id": str(channel.id),
                    "guild_id": str(guild.id),
                    "name": getattr(channel, "name", "unknown"),
                    "type": str(getattr(channel, "type", "text")),
                }, on_conflict="id"))
                if e2:
                    print(f"[backfill] Lỗi upsert discord_channels {channel.name}: {e2}")
                try:
                    rows = []
                    async for msg in channel.history(limit=1000, oldest_first=False):
                        if msg.author.bot:
                            continue
                        if not msg.content or not msg.content.strip():
                            continue
                        if msg.content.startswith(("/", ".", "!")):
                            continue
                        rows.append({
                            "id": str(msg.id),
                            "user_id": str(msg.author.id),
                            "author_name": msg.author.display_name,
                            "channel_id": str(channel.id),
                            "channel_name": getattr(channel, "name", "unknown"),
                            "content": msg.content,
                            "created_at": msg.created_at.isoformat(),
                        })
                    if rows:
                        BATCH = 500
                        for i in range(0, len(rows), BATCH):
                            _, err = execute(
                                lambda c, b=rows[i:i+BATCH]: c.table("chat_history")
                                .upsert(b, on_conflict="id"))
                            if err:
                                print(f"[backfill] Lỗi upsert {channel.name}: {err}")
                        total += len(rows)
                        chan_ok += 1
                        print(f"[backfill] ✅ {channel.name}: {len(rows)} tin")
                    await asyncio.sleep(1.0)  # throttle giữa các kênh
                except discord.errors.Forbidden:
                    continue
                except discord.errors.HTTPException as e:
                    if "429" in str(e):
                        print(f"[backfill] Rate limit kênh {channel.name}, nghỉ 5s...")
                        await asyncio.sleep(5)
                        continue
                    print(f"[backfill] Lỗi HTTP {channel.name}: {e}")
                except Exception as e:
                    print(f"[backfill] Lỗi {channel.name}: {e}")
            print(f"🎉 [backfill] Xong: {chan_ok} kênh, {total} tin nhắn.")
        except Exception as e:
            print(f"❌ [backfill] Lỗi tổng: {e}")

async def setup(bot):
    await bot.add_cog(ChatLogger(bot))
