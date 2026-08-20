"""
database.py — Backward-compatible shim.

Mọi logic Supabase giờ nằm ở db.py (retry/timeout/logging).
File này giữ export `supabase` để các cog cũ không break, nhưng KHUYẾN NGHỊ
chuyển sang dùng helpers trong db.py (safe_select/safe_upsert/...) để có resilience.
"""
from .db import get_client, safe_select, safe_upsert, safe_insert, safe_update, execute, DBError

# Lazy singleton — trả client đã init (có thể None nếu thiếu env).
supabase = get_client()

__all__ = [
    "supabase",
    "get_client",
    "safe_select",
    "safe_upsert",
    "safe_insert",
    "safe_update",
    "DBError",
]
