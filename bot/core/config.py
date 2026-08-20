import os
import random

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("DISCORD_TOKEN", "")
GIT_URL = os.getenv("GITHUB_GIT_URL", "")
# Guild mặc định (fallback khi thiếu env). Đổi tại .env DISCORD_GUILD_ID.
DEFAULT_GUILD_ID = "712258265769050164"
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", DEFAULT_GUILD_ID))
GUILD_NAME = os.getenv("GUILD_NAME", "The Northern Constellations")
GUILD_TAG = os.getenv("GUILD_TAG", "TNC")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
# Key backend (service_role) — dùng cho bot server-side, bypass RLS.
# Ưu tiên key này vì bảng json_storage chỉ có policy cho service_role.
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
# Khóa chính dùng để kết nối Supabase từ backend bot (service_role > anon).
SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY

# Thư mục bot/ — dùng cho file config/template và file tạm
DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Thư mục bot/Storage/ — nơi chứa toàn bộ file dữ liệu JSON
STORAGE_DIR = os.path.join(DATA_DIR, "Storage")

BOT_SESSION_ID = random.randint(1000, 9999)
