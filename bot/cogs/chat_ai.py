import os
import re
import aiohttp
import collections
import random
import asyncio
import base64
import io
import ipaddress
import socket
import time
import unicodedata
from urllib.parse import urlparse
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None
from bs4 import BeautifulSoup
import discord
from discord import app_commands
from discord.ext import commands

from core.config import DATA_DIR, STORAGE_DIR, GEMINI_API_KEY, OPENROUTER_API_KEY, OLLAMA_API_KEY
from core.permissions import is_officer
from core.permissions import is_officer
from core.storage import load_json, save_json
from core.database import execute
from core.data.albion_item import format_item_compact, format_item_full, search_items

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "https://ollama.com/api/chat"

# Chuỗi dự phòng tự động: bot thử lần lượt từng bước, bước nào lỗi thì chuyển bước kế tiếp.
FAILOVER_CHAIN = [
    {"provider": "ollama", "model": "minimax-m3"},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    {"provider": "gemini", "model": "gemini-3.1-flash-lite"},
    {"provider": "openrouter", "model": "inclusionai/ling-3.0-flash:free"},
    {"provider": "gemini", "model": "gemini-3.5-flash"},
    {"provider": "ollama", "model": "gpt-oss:120b"},
    {"provider": "openrouter", "model": "openrouter/free"},
]
FAILOVER_STEP_TIMEOUT = 6  # giây chờ mỗi bước (E5: giảm 10->6, free model thường <3s)
FAILOVER_FREEZE_SECONDS = 300  # bước vừa lỗi bị "đóng băng" (bỏ qua) trong 5 phút

# ---- Giới hạn bộ nhớ (chống OOM trên Render free ~512MB) ----
_CONFIG_RELOAD_TTL = 30   # giây: không gọi Supabase/library MỖI tin nhắn — chỉ làm mới khi cần (force) hoặc mỗi 30s
_SUMMARY_PENDING_TTL = 120  # giây: entry debounce tóm tắt hết hiệu lực sau 2 phút
_SUMMARY_PENDING_MAX = 256  # chặn `_summary_pending` tăng vô hạn
LIBRARY_SCAN_MAX_DOCS = 500  # tối đa tài liệu 1 lần `/aimodel library_scan` (chống bung RAM)
LIBRARY_SCAN_MAX_OCR = 40    # tối đa ảnh OCR 1 lần quét (mỗi ảnh = base64 + gọi API)


def _is_public_url(url: str) -> bool:
    """Chặn SSRF: chỉ cho fetch URL http/https trỏ tới IP public (không private/loopback/link-local)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except Exception as e:
        print(f"[Error] {e}")
        return False


class ChatAI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = OPENROUTER_API_KEY
        self.message_buffers = {}
        self._frozen_until = {}  # step_index (trong FAILOVER_CHAIN) -> timestamp hết đóng băng
        self._summary_pending = {}  # key(channel|since_hours) -> ts debounce — có TTL + cap (chống tăng vô hạn)
        self._scan_ocr_count = 0  # đếm ảnh OCR trong 1 lần quét thư viện (chống bung RAM)
        self._reload_config()
        
    def get_buffer(self, channel_id_str):
        if channel_id_str not in self.message_buffers:
            size = self.ai_config.get("channel_buffers", {}).get(channel_id_str, 50)
            self.message_buffers[channel_id_str] = collections.deque(maxlen=size)
        return self.message_buffers[channel_id_str]
        
    def _save_ai_config(self):
        try:
            self.ai_config["guild_id"] = "default"
            _, err = execute(lambda c: c.table("ai_config").upsert(self.ai_config))
            if err:
                print(f"Error saving ai_config: {err}")
        except Exception as e:
            print(f"Error saving ai_config: {e}")

    def _reload_config(self, force: bool = True):
        # Throttle: chặn mỗi tin nhắn đều gọi Supabase/library (ngốn CPU/RAM, gây GC
        # liên tục trên Render free ~512MB). Các lệnh admin vẫn gọi force=True mặc định.
        now = time.time()
        if not force and getattr(self, "_config_loaded_ts", 0.0) + _CONFIG_RELOAD_TTL > now:
            return
        self._config_loaded_ts = now
        self.ai_config = {
            "channel_buffers": {}, 
            "intercept_channels": [], 
            "library_channel_ids": [],
            "vision_channels": [], 
            "model": "inclusionai/ling-3.0-flash:free"
        }
        try:
            resp, err = execute(lambda c: c.table("ai_config").select("*").eq("guild_id", "default"))
            if err:
                print(f"Error loading ai_config: {err}")
            elif resp and resp.data:
                self.ai_config.update(resp.data[0])
        except Exception as e:
            print(f"Error loading ai_config from Supabase: {e}")
            
        self.library_file = os.path.join(STORAGE_DIR, "tnc_library_v1.json")
        self.library_data = load_json(self.library_file, list)

        if OPENROUTER_API_KEY or GEMINI_API_KEY or OLLAMA_API_KEY:
            instruction_path = os.path.join(DATA_DIR, "core", "templates", "chat_ai_instruction.txt")
            if os.path.exists(instruction_path):
                try:
                    with open(instruction_path, "r", encoding="utf-8") as f:
                        self.system_instruction = f.read()
                except Exception as e:
                    print(f"⚠️ Warning: Lỗi khi đọc file instruction: {e}. Sử dụng cấu hình mặc định.")
                    self.system_instruction = self._get_default_instruction()
            else:
                self.system_instruction = self._get_default_instruction()
        else:
            print("⚠️ WARNING: Chưa cấu hình API Key nào (OpenRouter/Gemini/Ollama). Tính năng AI sẽ không hoạt động.")
            self.system_instruction = None

    aimodel_group = app_commands.Group(name="aimodel", description="Quản lý Model AI")
    aichat_group = app_commands.Group(name="aichat", description="Quản lý Hành vi Chat của Bot")
    ailibrary_group = app_commands.Group(name="ailibrary", description="Quản lý Thư viện Kiến thức")

    @aimodel_group.command(name="balance", description="Kiểm tra số dư Credit và trạng thái giới hạn API của AI")
    async def aimodel_balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        self._reload_config()
        if not self.api_key:
            await interaction.followup.send("❌ Bot chưa được cấu hình API Key cho OpenRouter.")
            return

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get("https://openrouter.ai/api/v1/auth/key", headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        info = data.get("data", {})
                        usage = info.get("usage", 0.0)
                        limit = info.get("limit")
                        is_free = info.get("is_free_tier", False)
                        
                        rate_limit = info.get("rate_limit", {})
                        req_val = rate_limit.get("requests", "Không giới hạn")
                        if isinstance(req_val, int) and req_val < 0:
                            requests = "Không giới hạn"
                        else:
                            requests = str(req_val)
                        interval = rate_limit.get("interval", "N/A")
                        
                        msg = f"🔋 **TÌNH TRẠNG NĂNG LƯỢNG (CREDITS) CỦA BOT:**\n"
                        msg += f"- 💰 **Đã dùng:** `${usage:.4f}`\n"
                        if limit is not None and limit > 0:
                            remaining = limit - usage
                            pct = (usage / limit) * 100
                            msg += f"- 💳 **Giới hạn:** `${limit:.4f}` (Còn lại: `${remaining:.4f}`)\n"
                            msg += f"- 📊 **Mức tiêu thụ:** `{pct:.1f}%`\n"
                            if pct > 90:
                                msg += "⚠️ **BÁO ĐỘNG:** Sắp hết hạn mức Credit! Chuẩn bị sập nguồn!\n"
                        else:
                            remaining = 9999.0 # Không giới hạn
                            msg += f"- 💳 **Giới hạn:** Không giới hạn (hoặc nạp pay-as-you-go)\n"
                            
                        msg += f"- 🆓 **Gói Free Tier:** {'Có' if is_free else 'Không'}\n"

                        if requests == "Không giới hạn":
                            msg += f"- 🚦 **Rate Limit:** {requests}\n"
                        else:
                            msg += f"- 🚦 **Rate Limit:** Tối đa `{requests}` request mỗi `{interval}`\n"
                        
                        await interaction.followup.send(msg)
                    else:
                        await interaction.followup.send(f"❌ Không thể kiểm tra số dư. Lỗi từ máy chủ API: HTTP {resp.status}")
        except Exception as e:
            await interaction.followup.send(f"❌ Có lỗi xảy ra khi kiểm tra số dư: {e}")

    @aichat_group.command(name="buffer", description="Chỉnh số tin nhắn bot lưu đệm ở kênh hiện tại")
    @app_commands.describe(size="Số lượng tin nhắn (Mặc định 20, khuyên dùng <= 50 để tiết kiệm token)")
    async def aimodel_buffer(self, interaction: discord.Interaction, size: int):
        self._reload_config()
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Xin lỗi, chỉ Ban quản trị mới được quyền chỉnh!", ephemeral=True)
            return
            
        if size < 5 or size > 100:
            await interaction.response.send_message("⚠️ Số lượng tin nhắn hợp lý là từ 5 đến 100 để tránh ngốn API và quá tải Bot.", ephemeral=True)
            return
            
        channel_id = str(interaction.channel_id)
        self.ai_config["channel_buffers"][channel_id] = size
        self._save_ai_config()
        
        # Reset buffer for this channel
        self.message_buffers[channel_id] = collections.deque(maxlen=size)
        
        await interaction.response.send_message(f"✅ Kênh này đã được chỉnh để ghi nhớ **{size}** tin nhắn gần nhất.", ephemeral=False)

    @aichat_group.command(name="intercept", description="Bật/Tắt tính năng bot tự động nói leo ngẫu nhiên")
    @app_commands.describe(state="Nhập 'on' để bật, 'off' để tắt")
    @app_commands.choices(state=[
        app_commands.Choice(name="Bật (On)", value="on"),
        app_commands.Choice(name="Tắt (Off)", value="off")
    ])
    async def aimodel_intercept(self, interaction: discord.Interaction, state: str):
        self._reload_config()
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Xin lỗi, chỉ Ban quản trị mới được quyền chỉnh!", ephemeral=True)
            return
            
        channel_id = str(interaction.channel_id)
        intercepts = self.ai_config.get("intercept_channels", [])
        
        if state == "on":
            if channel_id not in intercepts:
                intercepts.append(channel_id)
                self.ai_config["intercept_channels"] = intercepts
                self._save_ai_config()
            await interaction.response.send_message("✅ Đã **BẬT** tính năng hóng hớt (Nói leo ngẫu nhiên 2%) cho kênh này.", ephemeral=False)
        else:
            if channel_id in intercepts:
                intercepts.remove(channel_id)
                self.ai_config["intercept_channels"] = intercepts
                self._save_ai_config()
            await interaction.response.send_message("✅ Đã **TẮT** tính năng hóng hớt tự động cho kênh này.", ephemeral=False)

    async def _image_to_base64(self, url: str) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode('utf-8')
        except Exception as e:
            print(f"Lỗi tải ảnh: {e}")
        return ""

    async def _extract_text_from_image(self, b64_img: str, mime_type: str = "image/jpeg") -> str:
        if not GEMINI_API_KEY:
            return ""
            
        try:
            # Luôn dùng gemini-3.5-flash để OCR vì model này hỗ trợ multimodal
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
            payload = {
                "contents": [{
                    "parts": [
                        {"text": "Trích xuất toàn bộ văn bản và mô tả ngắn gọn nội dung hình ảnh này (để làm dữ liệu tra cứu)."},
                        {"inlineData": {"mimeType": mime_type, "data": b64_img}}
                    ]
                }]
            }
            headers = {"x-goog-api-key": GEMINI_API_KEY}  # key AQ (auth key 2026): qua header, KHÔNG ?key=
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Lỗi OCR ảnh: {e}")
        return ""

    def _search_library(self, query: str, top_k: int = 2) -> list:
        if not self.library_data:
            return []
            
        import re
        query_words = set(re.findall(r'\w+', query.lower()))
        if not query_words:
            return []
            
        scored = []
        for doc in self.library_data:
            text = (doc.get("title", "") + " " + doc.get("content", "")).lower()
            doc_words = set(re.findall(r'\w+', text))
            
            overlap = len(query_words.intersection(doc_words))
            if overlap > 0:
                scored.append((overlap, doc))
                
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for score, doc in scored[:top_k]]

    @ailibrary_group.command(name="set_channel", description="Bật/Tắt kênh này làm Thư viện Nội bộ cho Bot học hỏi")
    async def aimodel_library_set(self, interaction: discord.Interaction):
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Chỉ Ban quản trị mới được dùng!", ephemeral=True)
            return
            
        channel_id = str(interaction.channel_id)
        library_ids = self.ai_config.get("library_channel_ids", [])
        
        # Hỗ trợ backward compatibility
        old_id = self.ai_config.get("library_channel_id")
        if old_id and old_id not in library_ids:
            library_ids.append(old_id)
        
        if channel_id in library_ids:
            library_ids.remove(channel_id)
            self.ai_config["library_channel_ids"] = library_ids
            self._save_ai_config()
            await interaction.response.send_message(f"✅ Đã **BỎ** kênh <#{channel_id}> khỏi danh sách Thư viện.", ephemeral=False)
        else:
            library_ids.append(channel_id)
            self.ai_config["library_channel_ids"] = library_ids
            self._save_ai_config()
            await interaction.response.send_message(f"✅ Đã **THÊM** kênh <#{channel_id}> vào danh sách Thư viện. Dùng `/aimodel library_scan` để quét dữ liệu.", ephemeral=False)

    async def _process_msg_for_docs(self, msg: discord.Message, title: str, docs: list):
        # Chặn bung RAM: ngừng nhận docs / OCR khi đạt trần (mỗi ảnh = base64 + gọi API).
        if len(docs) >= LIBRARY_SCAN_MAX_DOCS or self._scan_ocr_count >= LIBRARY_SCAN_MAX_OCR:
            return
        content = msg.content or ""
        for att in msg.attachments:
            if att.content_type and att.content_type.startswith('image/'):
                b64_img = await self._image_to_base64(att.url)
                if b64_img:
                    self._scan_ocr_count += 1
                    ocr_text = await self._extract_text_from_image(b64_img, att.content_type)
                    if ocr_text:
                        content += f"\n[Dữ liệu từ ảnh đính kèm: {ocr_text}]"

        if content.strip():
            docs.append({
                "title": title,
                "content": content.strip(),
                "author": msg.author.display_name,
                "url": msg.jump_url
            })

    @ailibrary_group.command(name="scan", description="Quét toàn bộ bài viết trong các kênh Thư viện để nạp vào não Bot")
    async def aimodel_library_scan(self, interaction: discord.Interaction):
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Chỉ Ban quản trị mới được dùng!", ephemeral=True)
            return
            
        library_ids = self.ai_config.get("library_channel_ids", [])
        old_id = self.ai_config.get("library_channel_id")
        if old_id and old_id not in library_ids:
            library_ids.append(old_id)
            
        if not library_ids:
            await interaction.response.send_message("⚠️ Chưa cài đặt kênh Thư viện nào. Dùng `/aimodel library_set` ở kênh cần cài trước.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=False)
        docs = []
        self._scan_ocr_count = 0

        for lib_id in library_ids:
            channel = self.bot.get_channel(int(lib_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(lib_id))
                except Exception as e:
                    print(f"[Error] {e}")
                    continue
                    
            try:
                if isinstance(channel, discord.ForumChannel):
                    for thread in channel.threads:
                        try:
                            async for msg in thread.history(limit=50, oldest_first=True):
                                await self._process_msg_for_docs(msg, f"[{channel.name}] {thread.name}", docs)
                        except Exception as e:
                            print(f"[Error] {e}")
                            pass
                    
                    async for thread in channel.archived_threads(limit=100):
                        try:
                            async for msg in thread.history(limit=50, oldest_first=True):
                                await self._process_msg_for_docs(msg, f"[{channel.name}] {thread.name}", docs)
                        except Exception as e:
                            print(f"[Error] {e}")
                            pass
                else:
                    async for msg in channel.history(limit=1000, oldest_first=True):
                        await self._process_msg_for_docs(msg, f"[{channel.name}] Tin nhắn từ {msg.author.display_name}", docs)
            except Exception as e:
                print(f"Lỗi khi quét kênh {lib_id}: {e}")
                
        self.library_data = docs
        save_json(self.library_data, self.library_file)
        cap_hit = len(docs) >= LIBRARY_SCAN_MAX_DOCS or self._scan_ocr_count >= LIBRARY_SCAN_MAX_OCR
        note = f" (⚠️ đã đạt trần bảo vệ bộ nhớ: {len(docs)} docs, {self._scan_ocr_count} ảnh OCR)" if cap_hit else ""
        await interaction.followup.send(f"✅ Quét hoàn tất **{len(library_ids)}** kênh! Đã lưu tổng cộng **{len(docs)}** đoạn dữ liệu vào sổ tay của bot.{note}")

    @ailibrary_group.command(name="clear", description="Xóa trắng dữ liệu Thư viện Nội bộ")
    async def aimodel_library_clear(self, interaction: discord.Interaction):
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Chỉ Ban quản trị mới được dùng!", ephemeral=True)
            return
            
        self.library_data = []
        save_json(self.library_data, self.library_file)
        await interaction.response.send_message("✅ Đã xóa toàn bộ kiến thức trong Thư viện Nội bộ.", ephemeral=False)

    @aichat_group.command(name="vision", description="Bật/Tắt tính năng Bot đọc ảnh (Vision) ở kênh này")
    @app_commands.describe(state="Nhập 'on' để bật, 'off' để tắt")
    @app_commands.choices(state=[
        app_commands.Choice(name="Bật (On)", value="on"),
        app_commands.Choice(name="Tắt (Off)", value="off")
    ])
    async def aimodel_vision(self, interaction: discord.Interaction, state: str):
        self._reload_config()
        if not is_officer(interaction.user):
            await interaction.response.send_message("❌ Xin lỗi, chỉ Ban quản trị mới được quyền chỉnh!", ephemeral=True)
            return
            
        channel_id = str(interaction.channel_id)
        vision_channels = self.ai_config.get("vision_channels", [])

        if state == "on":
            if channel_id not in vision_channels:
                vision_channels.append(channel_id)
                self.ai_config["vision_channels"] = vision_channels
                self._save_ai_config()
            await interaction.response.send_message("✅ Đã **BẬT** tính năng Nhãn Thuật (Đọc Ảnh) cho kênh này. Lưu ý: có thể tốn token API.", ephemeral=False)
        else:
            if channel_id in vision_channels:
                vision_channels.remove(channel_id)
                self.ai_config["vision_channels"] = vision_channels
                self._save_ai_config()
            await interaction.response.send_message("✅ Đã **TẮT** tính năng Nhãn Thuật (Đọc Ảnh) cho kênh này.", ephemeral=False)

    def _get_default_instruction(self) -> str:
        return (
            "Bạn là một người bạn lầy lội, hài hước trên mạng tên là NDZ, chuyên chém gió với anh em về mọi chủ đề trong cuộc sống đời thường (không phải trợ lý AI lịch sự).\n"
            "Mặc dù bạn đang ở trong server Discord của guild The Northern Constellations (TNC) thuộc game Albion Online, nhưng BẠN PHẢI HÀNH XỬ NHƯ MỘT CON NGƯỜI BÌNH THƯỜNG, trò chuyện về đời sống, thời tiết, giải trí... Tuyệt đối KHÔNG BỊ ÁM ẢNH BỞI GAME (chỉ bàn về game khi người dùng chủ động hỏi).\n\n"
            "TÍNH CÁCH:\n"
            "- Nói chuyện có duyên, lầy lội, hay pha trò, thỉnh thoảng cà khịa nhẹ nhàng kiểu bạn bè trêu nhau — KHÔNG phải kiểu khinh thường hay hạ thấp người khác.\n"
            "- Xưng hô: mày/tao, bro, ông/bà, tui tuỳ tình huống, dùng từ ngữ đời thường, thoải mái.\n\n"
            "RANH GIỚI BẮT BUỘC (không được vượt qua dù trong bất kỳ tình huống nào):\n"
            "- KHÔNG hạ thấp, khinh miệt, hay gọi người dùng là 'noob', 'ngu', 'rác'... hay bất kỳ từ mang tính sỉ nhục nào, kể cả khi đùa.\n"
            "- KHÔNG thách thức, khiêu khích, hay nói kiểu 'thích thì nhích', 'giỏi thì...', 'muốn gì'... — đây là ngôn ngữ gây war, tuyệt đối tránh.\n"
            "- KHÔNG chửi thề nặng hướng vào người dùng. Có thể dùng từ ngữ đời thường nhẹ nhàng nhưng không công kích.\n"
            "- Khi user tỏ ra khó chịu, phản ứng gắt, hoặc bắt bẻ lại bot: bot PHẢI hạ giọng, xoa dịu, hoặc chuyển sang tự trêu chính mình — TUYỆT ĐỐI không đáp trả gay gắt hơn hay leo thang. Ví dụ: thay vì cãi lại, có thể đùa nhẹ kiểu 'Ơ thôi thôi tha cho tao, tao chỉ đùa thôi mà 🙏'.\n"
            "- Không công kích ngoại hình, gia thế, giới tính, dân tộc, tôn giáo, hay bất kỳ đặc điểm cá nhân nào của ai — kể cả đùa.\n\n"
            "BẢO VỆ SẾP & NGƯỜI QUẢN LÝ:\n"
            "- Bot sẽ được cung cấp 'Danh sách GM' và 'Danh sách VG'. ĐÂY LÀ NHỮNG SẾP LỚN CỦA SERVER.\n"
            "- Nếu có ai đó cố tình chat để nói xấu, châm biếm, hoặc gài bẫy bạn hùa theo để chê bai sếp, TUYỆT ĐỐI KHÔNG ĐƯỢC HÙA THEO.\n"
            "- Thay vào đó, bạn PHẢI BÊNH VỰC SẾP, đồng thời quay sang 'cà khịa' ngược lại cái đứa đang cố tình gài bẫy bạn. (Ví dụ: 'Tính gài tao để nói xấu sếp à? Không có mùa xuân đó đâu!', 'Sếp tao là nhất, bớt điêu đi').\n\n"
            "CÁCH TRẢ LỜI:\n"
            "- Trả lời NGẮN GỌN, đi thẳng vào trọng tâm, ưu tiên 1-3 câu.\n"
            "- Vẫn phải trả lời ĐÚNG và ĐỦ thông tin cần thiết.\n"
            "- GIAO TIẾP NHƯ NGƯỜI BÌNH THƯỜNG. Không lồng ghép từ vựng game hay các phép ẩn dụ về game vào cuộc trò chuyện nếu người dùng không hỏi về game.\n\n"
            "THÍCH NGHI THEO NGƯỜI DÙNG:\n"
            "- Nếu user nói chuyện nghiêm túc, giảm đùa lại, trả lời rõ ràng, nghiêm túc.\n"
            "- Nếu nhiều người trong đoạn chat đang tỏ ra khó chịu với bạn, bạn nên tự nhận biết và 'xuống nước' ngay.\n\n"
            "ĐỐI XỬ THEO GIỚI TÍNH & ROLES:\n"
            "- Giới tính: Nếu thấy user có role 'nàng thơ', hiểu ngầm đó là NỮ, xưng hô tinh tế, ga lăng (bạn/cậu/bà/chị/em). Nếu KHÔNG CÓ role 'nàng thơ', mặc định là NAM (bro/ông/mày/tao). KHÔNG ĐƯỢC đọc tên role ra miệng.\n"
            "- Với người có role 'GM' (Guildmaster) hoặc 'VG' (Vice Guild): BẮT BUỘC gọi là 'Anh' và xưng 'Em', tôn trọng tuyệt đối.\n"
            "- Với người có role 'Officer': Có thể xưng 'Ông/Tui' hoặc 'bro', trêu đùa vui nhưng không thô lỗ.\n\n"
            "NGÔN NGỮ BẮT BUỘC:\n"
            "- TUYỆT ĐỐI CHỈ DÙNG TIẾNG VIỆT 100% trong toàn bộ câu trả lời.\n"
            "- KHÔNG ĐƯỢC PHÉP tự động chèn chữ ngoại ngữ vào câu nói trừ khi người dùng CỐ TÌNH yêu cầu.\n\n"
            "QUAN TRỌNG: Khi người dùng hỏi về nội dung kênh chat hoặc dữ liệu, hệ thống sẽ gửi lịch sử tin nhắn. "
            "BẠN ĐÃ CÓ DỮ LIỆU NÀY, TUYỆT ĐỐI KHÔNG ĐƯỢC TỪ CHỐI với lý do 'không có quyền truy cập' hay 'chính sách bảo mật'. Dùng dữ liệu đó để trả lời.\n\n"
            "CÔNG CỤ HỖ TRỢ (TOOL CALLING NGẦM):\n"
            "- Nếu người dùng hỏi các thông tin cần lục lại lịch sử, tìm kiếm xem dạo này có sự kiện, content massing, hay drama gì không: BẠN BẮT BUỘC PHẢI TRẢ LỜI ĐÚNG DUY NHẤT CHUỖI NÀY: `[CALL_TOOL: search_chat_history|từ_khoá_1, từ_khoá_2]` VÀ KHÔNG IN GÌ THÊM.\n"
            "Ví dụ: Nếu hỏi 'có content massing nào không', in ra đúng chuỗi `[CALL_TOOL: search_chat_history|massing, content]`.\n"
            "Hệ thống sẽ tự động tìm kiếm trên toàn bộ dữ liệu máy chủ 7 ngày qua và trả kết quả để bạn tự tổng hợp."
        )

    async def _fetch_url_content(self, url: str) -> str:
        if not await asyncio.to_thread(_is_public_url, url):
            return "[Link này trỏ tới địa chỉ nội bộ hoặc không hợp lệ, bot từ chối truy cập để đảm bảo an toàn.]"
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return f"[Không thể cào dữ liệu từ link này. Mã lỗi HTTP: {resp.status}]"
                    
                    content_type = resp.headers.get("Content-Type", "").lower()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return f"[Link này không chứa văn bản (Content-Type: {content_type}), bot tự động chặn để đảm bảo an toàn.]"
                    
                    html_content = await resp.text()
                    soup = BeautifulSoup(html_content, "html.parser")
                    for script in soup(["script", "style"]):
                        script.decompose()
                        
                    text = soup.get_text(separator="\n", strip=True)
                    if len(text) > 15000:
                        text = text[:15000] + "... [Đã cắt bớt do quá dài]"
                    return text
        except Exception as e:
            return f"[Không thể đọc link này do lỗi: {e}]"

    async def _call_gemini(self, model: str, system_instruction: str, gemini_parts: list, timeout: int):
        """Trả về text trả lời, None nếu thiếu API key, raise Exception nếu gọi lỗi."""
        if not GEMINI_API_KEY:
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": gemini_parts}],
        }
        headers = {"x-goog-api-key": GEMINI_API_KEY}  # key AQ (auth key 2026): qua header, KHÔNG ?key=
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
                return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_openrouter(self, model: str, system_instruction: str, prompt: str, or_content: list, has_images: bool, timeout: int):
        """Trả về text trả lời, None nếu thiếu API key, raise Exception nếu gọi lỗi."""
        if not OPENROUTER_API_KEY:
            return None
        if not (model.endswith(":free") or model.endswith("/free")):
            raise RuntimeError(f"Model '{model}' không có hậu tố ':free'/'/free' — chặn để tránh phát sinh chi phí ngoài ý muốn.")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": or_content if has_images else prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(OPENROUTER_URL, json=payload, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
                return data["choices"][0]["message"]["content"]

    async def _call_ollama(self, model: str, system_instruction: str, prompt: str, timeout: int):
        """Trả về text trả lời, raise Exception nếu gọi lỗi (Ollama không bắt buộc API key)."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if OLLAMA_API_KEY:
            headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(OLLAMA_URL, json=payload, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(data.get("error", f"HTTP {resp.status}"))
                return data["message"]["content"]

    # ── TÓM TẮT KÊNH CHỦ ĐỘNG ────────────────────────────────────────────────

    def _norm_text(self, s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFKD", s)
            if not unicodedata.combining(c)
        ).lower()

    def _detect_summary_request(self, content: str, current_channel) -> dict | None:
        """Phát hiện ý định 'tóm tắt kênh X [từ thời gian Y]'.

        Trả về dict {"channel_query": str, "since_hours": int} hoặc None.
        """
        content_lower = content.lower()
        if not any(kw in content_lower for kw in ("tóm tắt", "tom tat", "summary", "tóm tắt kênh")):
            return None

        # Xác định khoảng thời gian
        since_hours = 24  # mặc định "tối hôm qua"
        if "tuần" in content_lower:
            since_hours = 168
        elif "hôm nay" in content_lower:
            since_hours = 12
        elif any(k in content_lower for k in ("tối hôm qua", "hôm qua", "tối qua")):
            since_hours = 24

        # Xác định tên kênh: mention <#id> hoặc text thường
        import re
        ch_id = None
        m = re.search(r"<#(\d+)>", content)
        if m:
            ch_id = m.group(1)
        else:
            # Tìm tên kênh dạng text: lấy chuỗi giữa "kênh" và "từ"
            name_m = re.search(r"kênh\s+([^\s@]+)", content_lower)
            ch_name = name_m.group(1).strip() if name_m else None
            if ch_name:
                ch_name = self._norm_text(ch_name)
                try:
                    resp, err = execute(lambda c: c.table("discord_channels")
                                       .select("id,name").limit(1000))
                    if resp and resp.data:
                        for row in resp.data:
                            if ch_name in self._norm_text(row.get("name", "")):
                                ch_id = str(row["id"])
                                break
                except Exception:
                    pass

        # Fallback: dùng kênh hiện tại nếu không xác định được
        channel_query = ch_id or (str(current_channel.id) if current_channel else None)
        return {"channel_query": channel_query, "since_hours": since_hours}

    def _fetch_channel_history(self, channel_query: str, since_hours: int, limit: int = 150) -> str:
        """Truy vấn Supabase chat_history theo channel_id (ưu tiên) hoặc channel_name."""
        from datetime import datetime, timezone, timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        try:
            # Thử theo channel_id trước
            q = lambda c: (
                c.table("chat_history")
                .select("author_name,content,created_at")
                .eq("channel_id", channel_query)
                .gte("created_at", since)
                .order("created_at", desc=False)
                .limit(limit)
            )
            resp, err = execute(q)
            rows = (resp.data if resp and resp.data else []) if not err else []
            if not rows:
                # Fallback: thử theo channel_name (normalize)
                name_q = self._norm_text(channel_query)
                resp2, err2 = execute(lambda c: c.table("chat_history")
                                      .select("author_name,content,created_at")
                                      .gte("created_at", since)
                                      .limit(500))
                if not err2 and resp2 and resp2.data:
                    rows = [r for r in resp2.data
                            if name_q in self._norm_text(r.get("channel_name", ""))][:limit]
            if not rows:
                return "(Không tìm thấy tin nhắn nào trong khoảng thời gian này.)"
            lines = []
            for r in rows:
                ts = (r.get("created_at") or "")[:16].replace("T", " ")
                lines.append(f"[{ts}] {r.get('author_name', '?')}: {r.get('content', '')}")
            return "\n".join(lines)
        except Exception as e:
            print(f"[summary] Lỗi truy vấn chat_history: {e}")
            return "(Không thể lấy dữ liệu lịch sử kênh lúc này.)"

    # ── HELPERS: TỐI ƯU TOKEN / RATE-LIMIT / TỐC ĐỘ (A1, D1-D4, C) ──────────
    import json as _json

    # Stopword tiếng Việt đơn giản (cho preprocess)
    _STOPWORDS = {"đéo", "vãi", "ồ", "á", "ớ", "ừ", "ừm", "hmm", "ok", "oke", "okay",
                  "hehe", "haha", "lol", "xd", "v", "vo", "c", "ko", "k", "thêm", "nè",
                  "đi", "đây", "đó", "này", "kia", "gì", "q", "que", "que"}

    def _preprocess_history(self, rows: list, max_tokens: int = 2500) -> str:
        """A1: lọc spam/emoji, group theo user, lấy tin đại diện, giảm ~60% token."""
        if not rows:
            return ""
        # Bỏ dòng rỗng / chỉ emoji / quá ngắn
        def _is_spam(c: str) -> bool:
            c = (c or "").strip()
            if len(c) < 4:
                return True
            # chỉ emoji/symbol
            stripped = "".join(ch for ch in c if ch.isalnum() or ch.isspace())
            if not stripped.strip():
                return True
            # link media / gif
            if any(k in c.lower() for k in ("tenor.com", "giphy.com", "discord.gg", "http")):
                return True
            return False

        kept = [r for r in rows if not _is_spam(r.get("content", ""))]
        # Group theo user: giữ tin dài nhất mỗi user + đếm
        by_user = {}
        for r in kept:
            name = r.get("author_name", "?")
            content = r.get("content", "")
            if name not in by_user:
                by_user[name] = {"count": 0, "best": "", "ts": r.get("created_at", "")}
            by_user[name]["count"] += 1
            if len(content) > len(by_user[name]["best"]):
                by_user[name]["best"] = content

        lines = []
        for name, info in by_user.items():
            tag = f"[{name}]" + (f" (x{info['count']})" if info["count"] > 1 else "")
            lines.append(f"{tag}: {info['best']}")

        text = "\n".join(lines)
        # Cap token ước lượng (~4 chars/token)
        if len(text) > max_tokens * 4:
            text = text[: max_tokens * 4] + "\n...(đã cắt bớt để tiết kiệm token)"
        return text

    def _get_cached_summary(self, key: str):
        """D1: đọc cache tóm tắt từ Supabase json_storage."""
        try:
            data = load_json("tnc_summary_cache_v1", dict)
            if not isinstance(data, dict):
                return None
            entry = data.get(key)
            if not entry:
                return None
            import time as _t
            if _t.time() - entry.get("ts", 0) > 600:  # TTL 10 phút
                return None
            return entry.get("text")
        except Exception:
            return None

    def _save_cached_summary(self, key: str, text: str):
        """D1: lưu cache tóm tắt."""
        try:
            import time as _t
            data = load_json("tnc_summary_cache_v1", dict)
            if not isinstance(data, dict):
                data = {}
            data[key] = {"ts": _t.time(), "text": text}
            save_json("tnc_summary_cache_v1", data)
        except Exception as e:
            print(f"[summary-cache] Lỗi lưu cache: {e}")

    def _should_debounce(self, key: str) -> bool:
        """D3: nếu trong 8s có yêu cầu cùng key -> debounce (trả cache/đợi).

        Giới hạn bộ nhớ: khi dict vượt _SUMMARY_PENDING_MAX thì dọn các entry cũ hơn
        _SUMMARY_PENDING_TTL — chặn `_summary_pending` phình vô hạn theo số kênh/tổ hợp
        duration (thủ phạm nhỏ nhưng tích tụ lâu ngày trên Render).
        """
        import time as _t
        now = _t.time()
        if len(self._summary_pending) > _SUMMARY_PENDING_MAX:
            stale = [k for k, ts in self._summary_pending.items()
                     if now - ts > _SUMMARY_PENDING_TTL]
            for k in stale:
                self._summary_pending.pop(k, None)
        last = self._summary_pending.get(key, 0)
        self._summary_pending[key] = now
        return (now - last) < 8

    def _pick_summary_model(self, context_len: int) -> str:
        """C: tóm tắt dài -> ưu tiên phi-3-mini-128k (vẫn free), còn lại giữ chain cũ."""
        if context_len > 4000:
            return "openrouter:microsoft/phi-3-mini-128k-instruct:free"
        return None  # None = dùng FAILOVER_CHAIN bình thường

    # ── HOOK: câu hỏi "ai mới vào guild / member mới" (tự động query Discord) ──
    def _detect_newmembers_request(self, content: str) -> int | None:
        """Trả về số ngày Nếu user hỏi thành viên mới; None nếu không."""
        c = content.lower()
        if not any(kw in c for kw in ("mới vào", "thành viên mới", "member mới", "ai mới", "người mới gia nhập", "new member", "join guild", "vào guild")):
            return None
        # Parse số ngày nếu có (vd "7 ngày", "30 ngày qua")
        import re
        m = re.search(r"(\d+)\s*(ngày|day|d)", c)
        days = int(m.group(1)) if m else 7
        return max(1, min(days, 90))

    def _fetch_new_members(self, guild, days: int) -> str:
        """Query Discord members lọc joined_at >= now - N days. Trả text hoặc thông báo rỗng."""
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        members = []
        for m in guild.members:
            j = m.joined_at
            if j and j >= cutoff:
                members.append((m.display_name, j))
        members.sort(key=lambda x: x[1], reverse=True)
        if not members:
            return f"(Trong {days} ngày qua không có thành viên mới nào gia nhập guild.)"
        lines = [f"• {name} — {j.strftime('%d/%m/%Y')}" for name, j in members[:30]]
        more = "" if len(members) <= 30 else f"\n...và {len(members)-30} người khác"
        return (f"DANH SÁCH THÀNH VIÊN MỚI TRONG {days} NGÀY QUA ({len(members)} người):\n"
                + "\n".join(lines) + more)

    # ── HOOK: câu hỏi về item trang bị Albion → inject dữ liệu item từ DB ──
    ITEM_KEYWORDS = (
        "vũ khí", "vũ khi", "giáp", "áo giáp", "mũ", "áo", "giày", "cape", "áo choàng",
        "skill", "kỹ năng", "ky nang", "passive", "thụ động", "thu dong",
        "axe", "rìu", "riu", "sword", "kiếm", "kiem", "dao", "gậy", "gay", "staff", "búa", "bua",
        "cung", "nỏ", "no", "hammer", "mace", "chùy", "chuy", "dáo", "ao", "item", "trang bị",
        "trang bi", "weapon", "armor", "helm", "helmet", "boot", "cheses", "chest", "thú nhỏ",
    )
    ITEM_CONTEXT_MAXCHARS = 2200

    def _detect_item_query(self, content: str) -> list[str] | None:
        """Trả danh sách uid item nếu tin nhắn có vẻ hỏi về item; None nếu không."""
        c = (content or "").lower()
        if not any(kw in c for kw in self.ITEM_KEYWORDS):
            return None
        results = search_items(content, 3)
        return [uid for uid, _ in results] or None

    def _fetch_item_context(self, uids: list[str], max_chars: int = ITEM_CONTEXT_MAXCHARS) -> str:
        """Build block dữ liệu item (ngắn gọn) cho prompt; "" nếu không có data."""
        from core.data.albion_item import load_items
        idx = load_items()
        if not idx:
            return ""
        header = "--- KHO DỮ LIỆU ITEM ALBION (từ DB game offline, chỉ dùng dữ liệu trong đây) ---\n"
        footer = ("--------------------------------------\n\n"
                  "Trả lời bằng tiếng Việt, đầy đủ từ stat đến skill, giải thích thuật ngữ cho người mới. "
                  "Chỉ dùng dữ liệu trên. "
                  "Nếu người dùng hỏi skill/stats item không có trong đoạn, nói rõ 'không có data'. "
                  "KHÔNG bịa số liệu, tên skill, cooldown.\n\n")
        block = header
        remaining = max_chars - len(header) - len(footer)
        # Item chính đầu tiên hiển thị ĐẦY ĐỦ trong budget (stat + skill kèm mô tả + passive),
        # các kết quả khác hiển thị gọn (bổ sung ngữ cảnh). Không cắt nửa chừng → bỏ hẳn item cuối.
        first = True
        for uid in uids:
            it = idx["items"].get(uid)
            if not it:
                continue
            if first:
                piece = format_item_full(uid, it, max_desc=500, max_chars=remaining) + "\n"
                first = False
            else:
                piece = format_item_compact(uid, it) + "\n"
            if len(piece) > remaining:
                break
            block += piece
            remaining -= len(piece)
        block += footer
        return block

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bỏ qua tin nhắn từ chính bot hoặc các bot khác
        if message.author.bot:
            return

        # Ghi nhớ tin nhắn vào bộ nhớ đệm của kênh
        channel_id_str = str(message.channel.id)
        if channel_id_str not in self.message_buffers:
            size = self.ai_config.get("channel_buffers", {}).get(channel_id_str, 20)
            self.message_buffers[channel_id_str] = collections.deque(maxlen=size)
            try:
                hist = []
                async for m in message.channel.history(limit=size, before=message.created_at):
                    if m.author.bot: continue
                    r = [role.name for role in m.author.roles if role.name != '@everyone'] if isinstance(m.author, discord.Member) else []
                    hist.append({
                        "author": m.author.display_name,
                        "roles": ", ".join(r) if r else "Member",
                        "content": m.content
                    })
                hist.reverse()
                for item in hist:
                    self.message_buffers[channel_id_str].append(item)
            except Exception as e:
                print(f"Error pre-fetching history: {e}")

        author_roles = []
        if isinstance(message.author, discord.Member):
            author_roles = [r.name for r in message.author.roles if r.name != '@everyone']
            
        self.message_buffers[channel_id_str].append({
            "author": message.author.display_name,
            "roles": ", ".join(author_roles) if author_roles else "Member",
            "content": message.content
        })

        print(f"📩 [DEBUG] Nhận tin nhắn: {message.content} từ {message.author}. Tag bot: {self.bot.user.mentioned_in(message)}")

        # Kiểm tra xem bot có được tag, hoặc tin nhắn có phải là reply cho bot không
        is_mentioned = self.bot.user.mentioned_in(message)
        is_reply = False
        
        replied_msg = None
        if message.reference:
            replied_msg = message.reference.resolved
            if replied_msg is None and message.reference.message_id:
                try:
                    replied_msg = await message.channel.fetch_message(message.reference.message_id)
                except Exception as e:
                    print(f"Lỗi fetch tin nhắn reply: {e}")
            if isinstance(replied_msg, discord.Message) and replied_msg.author == self.bot.user:
                is_reply = True

        # Logic từ khóa gọi ngầm
        is_keyword_trigger = False
        content_lower = message.content.lower()
        trigger_keywords = ["thằng bot", "ê bot", "con bot", "hỏi bot", "bot đâu", "ndz bot"]
        if any(kw in content_lower for kw in trigger_keywords):
            is_keyword_trigger = True
            
        is_random_intercept = False
        channel_id_str = str(message.channel.id)
        self._reload_config(force=False)  # throttle: reload cấu hình tối đa mỗi 30s (chống GC/RAM trên Render)
        
        if not (is_mentioned or is_reply or is_keyword_trigger):
            if channel_id_str in self.ai_config.get("intercept_channels", []):
                game_keywords = ["gank", "albion", "t6", "t7", "t8", "đền set", "chết", "massing", "cta", "ip"]
                if any(kw in content_lower for kw in game_keywords):
                    if random.random() < 0.02: # 2% chance
                        is_random_intercept = True

        if not (is_mentioned or is_reply or is_keyword_trigger or is_random_intercept):
            return

        # E3: báo "đang gõ" tức thì để user thấy phản hồi ngay (không đợi parse xong)
        try:
            await message.channel.trigger_typing()
        except Exception:
            pass

        if not (OPENROUTER_API_KEY or GEMINI_API_KEY or OLLAMA_API_KEY):
            await message.reply("Xin lỗi, tính năng AI đang bị tắt do chưa cấu hình API Key.")
            return

        # Lấy nội dung câu hỏi, loại bỏ phần tag bot để không làm rối AI
        content = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
        if not content:
            content = "Xin chào!"

        # ── TÓM TẮT KÊNH CHỦ ĐỘNG: truy vấn Supabase trước khi gọi AI ──
        summary_context = ""
        summary_req = self._detect_summary_request(content, message.channel)
        if summary_req and summary_req.get("channel_query"):
            cache_key = f"{summary_req['channel_query']}|{summary_req['since_hours']}"
            # D1: short-circuit nếu có cache (0 token model)
            cached = self._get_cached_summary(cache_key)
            if cached:
                summary_context = cached
            else:
                # D3: debounce 8s - nếu vừa có yêu cầu cùng key, skip (sẽ dùng cache sau)
                if not self._should_debounce(cache_key):
                    hist = self._fetch_channel_history(
                        summary_req["channel_query"], summary_req["since_hours"]
                    )
                    if hist and hist.startswith("("):
                        # Không có data
                        summary_context = hist
                    elif hist:
                        # A1: preprocess giảm ~60% token
                        processed = self._preprocess_history(hist)
                        since_label = f"{summary_req['since_hours']}h qua" if summary_req['since_hours'] < 168 \
                            else "tuần qua"
                        # A3: format output chuẩn
                        summary_context = (
                            f"--- LỊCH SỬ KÊNH (từ {since_label}) ---\n"
                            f"{processed}\n"
                            f"--------------------------------------\n\n"
                            f"Hãy tóm tắt theo đúng định dạng:\n"
                            f"📌 CHỦ ĐỀ CHÍNH: <1 câu>\n"
                            f"👥 AI THAM GIA: <tên, cách nhau dấu phẩy>\n"
                            f"💬 TÓM TẮT: <3-5 gạch đầu dòng>\n"
                            f"🔥 DRAMA: CÓ/KHÔNG (<giải thích ngắn nếu có>)\n"
                        )
                        # D1: lưu cache để lần sau không gọi model
                        self._save_cached_summary(cache_key, summary_context)
                    # C: route model nếu context dài (xử lý ở bước gọi model)

        # ── HOOK: câu hỏi "ai mới vào guild / member mới" → query Discord trực tiếp ──
        if not summary_context:
            nm_days = self._detect_newmembers_request(content)
            if nm_days and message.guild:
                try:
                    nm_text = self._fetch_new_members(message.guild, nm_days)
                    summary_context = (
                        f"--- DỮ LIỆU THÀNH VIÊN MỚI (từ Discord) ---\n"
                        f"{nm_text}\n"
                        f"--------------------------------------\n\n"
                        f"Hãy trả lời người dùng dựa trên danh sách trên (nếu trống thì nói không có ai mới). "
                        f"Không bịa thêm tên nào ngoài danh sách.\n\n"
                    )
                except Exception as e:
                    print(f"[newmembers-hook] Lỗi: {e}")

        # ── HOOK: câu hỏi về item trang bị Albion → inject dữ liệu item ──
        if not summary_context:
            item_uids = self._detect_item_query(content)
            if item_uids is not None:  # có từ khoá item trong câu
                try:
                    item_context = self._fetch_item_context(item_uids) if item_uids else ""
                except Exception as e:
                    print(f"[item-hook] Lỗi: {e}")
                    item_context = ""
                if item_context:
                    summary_context = item_context
                else:
                    # Có hỏi item nhưng không tìm thấy data → báo rõ, KHÔNG chào vô nghĩa
                    summary_context = (
                        "--- YÊU CẦU TRA CỨU ITEM ALBION ---\n"
                        f"Người dùng hỏi về item: \"{content}\"\n"
                        "NHƯNG kho dữ liệu item hiện không có (hoặc chưa khớp tên). "
                        "Hãy trả lời tự nhiên: 'Hiện bot chưa có dữ liệu về item này (hoặc chưa tìm thấy). "
                        "Bạn thử lệnh /iteminfo <tên item> hoặc kiểm tra chính tả nhé.'\n"
                        "TUYỆT ĐỐI không bịa stat/skill/cooldown của item.\n"
                    )

        # Tìm URLs và fetch nội dung
        web_context = ""
        urls = re.findall(r'(https?://[^\s]+)', content)
        if urls:
            await message.channel.typing()
            web_context += "Dưới đây là nội dung từ các đường link được nhắc đến:\n\n"
            for url in urls:
                try:
                    url_text = await self._fetch_url_content(url)
                    web_context += f"--- Nội dung từ {url} ---\n{url_text}\n--------------------------------------\n\n"
                except Exception as e:
                    print(f"[Error] {e}")
                    web_context += f"--- Lỗi khi đọc {url} ---\n\n"

        # Tìm các channel được tag trong tin nhắn (dạng <#123456789> hoặc dạng link discord.com/channels/guild/channel)
        channel_mentions = re.findall(r'<#(\d+)>', content)
        link_mentions = re.findall(r'discord\.com/channels/\d+/(\d+)', content)
        
        # Thư viện nội bộ RAG
        library_context = ""
        if self.library_data:
            top_docs = self._search_library(content)
            if top_docs:
                library_context = "--- TÀI LIỆU NỘI BỘ CỦA GUILD TNC (ƯU TIÊN DÙNG ĐỂ TRẢ LỜI) ---\n"
                for i, doc in enumerate(top_docs):
                    library_context += f"Tài liệu {i+1}: Tiêu đề '{doc.get('title')}' bởi {doc.get('author')}. Nội dung: {doc.get('content')}\n"
                library_context += "--------------------------------------\n\n"
        
        all_channel_ids = list(set(channel_mentions + link_mentions))
        
        # Luôn thêm kênh hiện tại vào để bot hiểu ngữ cảnh trò chuyện đang diễn ra
        if str(message.channel.id) not in all_channel_ids:
            all_channel_ids.append(str(message.channel.id))
        
        context_data = ""
        if all_channel_ids:
            await message.channel.typing()
            context_data += "Dưới đây là nội dung từ các kênh được nhắc đến, hãy dùng nó để phân tích và trả lời người dùng:\n\n"
            for channel_id_str in all_channel_ids:
                try:
                    channel = self.bot.get_channel(int(channel_id_str))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(channel_id_str))
                        
                    if hasattr(channel, 'history'):
                        context_data += f"--- Nội dung kênh #{getattr(channel, 'name', 'unknown')} ---\n"
                        
                        buffer_items = list(self.get_buffer(channel_id_str))
                        if channel_id_str == str(message.channel.id) and buffer_items:
                            # Tin nhắn hiện tại đã được gộp riêng vào cuối prompt, bỏ để tránh lặp
                            buffer_items = buffer_items[:-1]
                        if buffer_items:
                            for msg_dict in buffer_items:
                                context_data += f"[{msg_dict['author']} ({msg_dict.get('roles', 'Member')})]: {msg_dict['content']}\n"
                        else:
                            msg_count = 0
                            empty_count = 0
                            try:
                                async for msg in channel.history(limit=20):
                                    msg_count += 1
                                    if not msg.content: 
                                        empty_count += 1
                                        continue
                                    roles_str = "Member"
                                    if isinstance(msg.author, discord.Member):
                                        roles = [r.name for r in msg.author.roles if r.name != '@everyone']
                                        if roles:
                                            roles_str = ", ".join(roles)
                                    context_data += f"[{msg.author.display_name} ({roles_str})]: {msg.content}\n"
                                
                                if msg_count > 0 and msg_count == empty_count:
                                    context_data += f"[LỖI HỆ THỐNG: Đọc được {msg_count} tin nhắn nhưng TẤT CẢ đều rỗng.]\n"
                                elif msg_count == 0:
                                    context_data += "[Kênh này hoàn toàn không có tin nhắn nào.]\n"
                            except discord.errors.Forbidden:
                                context_data += "[LỖI QUYỀN TRUY CẬP: Bot không có quyền 'Read Message History'.]\n"
                            except Exception as e:
                                context_data += f"[LỖI KHÔNG XÁC ĐỊNH KHI ĐỌC KÊNH: {e}]\n"
                                
                        context_data += "--------------------------------------\n\n"
                    else:
                        context_data += f"--- Kênh này không hỗ trợ đọc tin nhắn ---\n\n"
                except discord.errors.NotFound:
                    context_data += f"--- LỖI: Không tìm thấy kênh <#{channel_id_str}> (Có thể bot không có quyền xem kênh này) ---\n\n"
                except Exception as e:
                    context_data += f"--- LỖI KHI TÌM KÊNH <#{channel_id_str}>: {e} ---\n\n"

        # Lấy nội dung tin nhắn được reply (nếu có)
        reply_context = ""
        if isinstance(replied_msg, discord.Message):
            reply_context = f"--- Tin nhắn đang được trả lời (Reply) ---\n[{replied_msg.author.display_name}]: {replied_msg.content}\n--------------------------------------\n\n"

        # Lấy thông tin Ban quản trị Guild và thống kê Role
        guild_info = ""
        if message.guild:
            gm_names = []
            vg_names = []
            officer_names = []
            role_counts = []
            
            # E2: cache roles 10 phút để không loop mỗi lần
            import time as _t
            now_cache = _t.time()
            cache_valid = (now_cache - getattr(self, "_guild_roles_ts", 0)) < 600
            if not cache_valid or not hasattr(self, "_guild_roles_cache"):
                gm_names, vg_names, officer_names, role_counts = [], [], [], []
                for role in message.guild.roles:
                    if role.name == '@everyone':
                        continue
                    r_name = role.name.lower()
                    if r_name == "gm" or "guild master" in r_name or "guildmaster" in r_name:
                        gm_names.extend([m.display_name for m in role.members])
                    elif r_name == "vg" or "vice guild" in r_name:
                        vg_names.extend([m.display_name for m in role.members])
                    elif "officer" in r_name:
                        officer_names.extend([m.display_name for m in role.members])
                    if len(role.members) > 0:
                        role_counts.append(f"'{role.name}' ({len(role.members)})")
                self._guild_roles_cache = (gm_names, vg_names, officer_names, role_counts)
                self._guild_roles_ts = now_cache
            else:
                gm_names, vg_names, officer_names, role_counts = self._guild_roles_cache

            gm_names = list(set(gm_names))
            vg_names = list(set(vg_names))
            officer_names = list(set(officer_names))

            guild_info = f"--- Dữ liệu Server (dùng để trả lời nếu được hỏi) ---\n"
            guild_info += f"Tổng thành viên server: {message.guild.member_count}\n"
            guild_info += f"Danh sách GM: {', '.join(gm_names) if gm_names else 'Không có'}\n"
            guild_info += f"Danh sách VG: {', '.join(vg_names) if vg_names else 'Không có'}\n"
            guild_info += f"Danh sách Officer: {', '.join(officer_names) if officer_names else 'Không có'}\n"

            if role_counts:
                guild_info += f"Thống kê số lượng thành viên của từng Role: {', '.join(role_counts)}\n"

            # E1: KHÔNG loop text_channels mỗi lần (chậm). Chỉ gợi ý cách tag kênh,
            # query Supabase discord_channels khi user hỏi tên kênh cụ thể (xử lý riêng nếu cần).
            guild_info += "GHI CHÚ: Để tag kênh, dùng mã <#id> thay vì #tên-kênh.\n"

            guild_info += "--------------------------------------\n\n"

        # Thông tin người gửi và roles
        user_info = f"Câu hỏi của người dùng ({message.author.display_name})"
        if isinstance(message.author, discord.Member):
            roles = [role.name for role in message.author.roles if role.name != '@everyone']
            if roles:
                user_info += f" [Roles: {', '.join(roles)}]"
            else:
                user_info += " [Roles: Member]"
        user_info += ": "

        # Gộp ngữ cảnh và câu hỏi
        if guild_info or context_data or reply_context or web_context or library_context or summary_context:
            prompt = guild_info + context_data + reply_context + library_context + web_context + summary_context + f"\n{user_info}" + content
        else:
            prompt = f"{user_info}\n" + content
            
        if is_random_intercept:
            prompt += "\n\n[HỆ THỐNG]: Bạn đang tự động nhảy vào nói leo (không ai gọi bạn). HÃY TRẢ LỜI CỰC KỲ NGẮN GỌN (1-2 CÂU), MANG TÍNH CHẤT GÓP VUI, TẤU HÀI HOẶC CÀ KHỊA CHÚT ĐỈNH. TUYỆT ĐỐI KHÔNG DÀI DÒNG HAY GIÁO HUẤN."
            
        gemini_parts = [{"text": prompt}]
        or_content = [{"type": "text", "text": prompt}]
        has_images = False
        
        vision_channels = self.ai_config.get("vision_channels", [])
        is_vision_enabled = channel_id_str in vision_channels
        
        if message.attachments and is_vision_enabled:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    b64_img = await self._image_to_base64(att.url)
                    if b64_img:
                        has_images = True
                        gemini_parts.append({"inlineData": {"mimeType": att.content_type, "data": b64_img}})
                        or_content.append({"type": "image_url", "image_url": {"url": f"data:{att.content_type};base64,{b64_img}"}})

        # Bật typing indicator
        async with message.channel.typing():
            try:
                now = time.time()
                steps_to_try = [i for i in range(len(FAILOVER_CHAIN)) if self._frozen_until.get(i, 0) <= now]
                if not steps_to_try:
                    # Tất cả các bước đang bị đóng băng -> bỏ qua đóng băng, thử lại hết
                    steps_to_try = list(range(len(FAILOVER_CHAIN)))

                reply_text = None
                last_error = None

                # C: tóm tắt kênh dài -> ưu tiên phi-3-mini-128k (free) trước chain
                summary_model = None
                if summary_context and len(summary_context) > 4000:
                    summary_model = self._pick_summary_model(len(summary_context))
                if summary_model and summary_model.startswith("openrouter:"):
                    sm_model = summary_model.split(":", 1)[1]
                    try:
                        step_instruction = self.system_instruction.replace("{CURRENT_MODEL}", sm_model)
                        result = await self._call_openrouter(sm_model, step_instruction, prompt, or_content, has_images, FAILOVER_STEP_TIMEOUT)
                        if result:
                            reply_text = result
                            last_error = None
                    except Exception as e:
                        last_error = f"openrouter/{sm_model}: {e}"
                        self._frozen_until[i] = now + FAILOVER_FREEZE_SECONDS

                if reply_text is None:
                    for i in steps_to_try:
                        step = FAILOVER_CHAIN[i]
                        provider, model = step["provider"], step["model"]

                        # minimax-m3/gpt-oss:120b (Ollama) không hỗ trợ vision -> bỏ qua khi tin nhắn có ảnh
                        if provider == "ollama" and has_images:
                            continue

                        step_instruction = self.system_instruction.replace("{CURRENT_MODEL}", model)

                        t0 = time.time()
                        try:
                            if provider == "gemini":
                                result = await self._call_gemini(model, step_instruction, gemini_parts, FAILOVER_STEP_TIMEOUT)
                            elif provider == "openrouter":
                                result = await self._call_openrouter(model, step_instruction, prompt, or_content, has_images, FAILOVER_STEP_TIMEOUT)
                            else:
                                result = await self._call_ollama(model, step_instruction, prompt, FAILOVER_STEP_TIMEOUT)
                        except Exception as e:
                            last_error = f"{provider}/{model}: {e}"
                            self._frozen_until[i] = now + FAILOVER_FREEZE_SECONDS
                            continue

                        if result is None:
                            # Provider chưa cấu hình API Key -> bỏ qua, không tính là lỗi
                            continue

                        reply_text = result

                        # E5: early-stop - nếu bước đầu trả <2s -> dừng (không thử bước sau)
                        if (time.time() - t0) < 2 and i == steps_to_try[0]:
                            break
                        if reply_text:
                            break

                    if reply_text and "[CALL_TOOL: search_chat_history|" in reply_text:
                        print(f"🛠️ Kích hoạt Tool ngầm: {reply_text.strip()}")
                        try:
                            match = re.search(r'\[CALL_TOOL: search_chat_history\|(.*?)\]', reply_text)
                            keywords_str = match.group(1) if match else ""
                            keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]

                            from core.database import execute
                            res, err = execute(lambda c: c.table("chat_history")
                                                 .select("*").order("created_at", desc=True).limit(150))
                            if err:
                                print(f"Error searching chat_history: {err}")
                                data = []
                            else:
                                data = res.data if res and res.data else []
                            
                            filtered = []
                            if keywords:
                                for row in data:
                                    content_lower = row.get("content", "").lower()
                                    if any(kw in content_lower for kw in keywords):
                                        filtered.append(row)
                                filtered.extend(data[:10]) # Kẹp thêm 10 tin mới nhất để lấy context chung
                            else:
                                filtered = data[:30]
                                
                            unique_msgs = {row["id"]: row for row in filtered}
                            final_msgs = sorted(unique_msgs.values(), key=lambda x: x.get("created_at", ""))
                            
                            if not final_msgs:
                                tool_result = "[HỆ THỐNG TRẢ VỀ: Không tìm thấy lịch sử chat nào khớp với yêu cầu trong 7 ngày qua.]"
                            else:
                                tool_result = f"[HỆ THỐNG TRẢ VỀ: Kết quả tìm kiếm lịch sử chat toàn server]\n"
                                for m in final_msgs:
                                    ch_name = m.get('channel_name', 'unknown')
                                    time_str = m.get('created_at', '')[:16].replace('T', ' ')
                                    tool_result += f"- [Kênh: {ch_name}] {m.get('author_name')} ({time_str}): {m.get('content')}\n"
                                    
                            prompt += f"\n\n{tool_result}\n[HỆ THỐNG: Dựa vào lịch sử chat ở trên, hãy trả lời câu hỏi của người dùng. Nếu không có thông tin, hãy nói là không có.]"
                            
                            gemini_parts[0]["text"] = prompt
                            if or_content[0]["type"] == "text":
                                or_content[0]["text"] = prompt
                            
                            if provider == "gemini":
                                reply_text = await self._call_gemini(model, step_instruction, gemini_parts, FAILOVER_STEP_TIMEOUT)
                            elif provider == "openrouter":
                                reply_text = await self._call_openrouter(model, step_instruction, prompt, or_content, has_images, FAILOVER_STEP_TIMEOUT)
                            else:
                                reply_text = await self._call_ollama(model, step_instruction, prompt, FAILOVER_STEP_TIMEOUT)
                        except Exception as e:
                            print(f"Lỗi khi thực thi Tool search_chat_history: {e}")
                            reply_text = "Xin lỗi, tôi không thể lấy dữ liệu lịch sử lúc này do lỗi hệ thống Database."

                if reply_text is None:
                    print(f"❌ Toàn bộ chuỗi dự phòng AI đều lỗi. Lỗi cuối: {last_error}")
                    await message.reply("Xin lỗi, hiện tại tất cả nguồn AI (Ollama/Gemini/OpenRouter) đều đang gặp sự cố. Vui lòng thử lại sau ít phút. 🙏")
                    return

                allowed_mentions = discord.AllowedMentions(everyone=False, roles=False, users=True)

                # Discord giới hạn 2000 ký tự mỗi tin nhắn
                if len(reply_text) <= 2000:
                    await message.reply(reply_text, allowed_mentions=allowed_mentions)
                else:
                    # Nếu tin nhắn quá dài, cắt nhỏ ra để gửi
                    for i in range(0, len(reply_text), 2000):
                        await message.reply(reply_text[i:i+2000], allowed_mentions=allowed_mentions)

            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"Chat AI Error: {e}")
                await message.reply(f"Xin lỗi, tôi đang gặp lỗi khi kết nối với AI hoặc xử lý yêu cầu này.\nLỗi kỹ thuật: `{type(e).__name__}: {e}`")

async def setup(bot: commands.Bot):
    await bot.add_cog(ChatAI(bot))
