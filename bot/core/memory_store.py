"""
memory_store.py — Lớp nhớ dài hạn + kiến thức tự học cho bot.

Chức năng:
- Lưu/đọc memory (user profile, fact, preference) vào bảng `memory`.
- Lưu/đọc knowledge (skill user dạy) vào bảng `knowledge` (có embedding pgvector).
- Semantic search knowledge qua cosine distance.

Thiết kế:
- Dùng lại helper an toàn từ `core.db` (safe_select/safe_insert/safe_upsert/execute).
- Không crash nếu thiếu env (Supabase client lazy).
- Backend tính embedding bên ngoài rồi truyền vào (không gọi API trong SQL).
"""
from __future__ import annotations

import logging
from typing import Optional

from .db import safe_insert, safe_select, execute

logger = logging.getLogger("bot.memory_store")


# ---------- MEMORY ----------
def save_memory(user_id: str, content: str, kind: str = "fact") -> tuple[Optional[dict], Optional[str]]:
    """Lưu 1 mẩu memory. user_id='global' nếu chung cho mọi người."""
    row = {"user_id": user_id, "kind": kind, "content": content}
    data = safe_insert("memory", row)
    if data is None:
        return None, "Lỗi lưu memory (DB null)"
    return data, None


def get_memories(user_id: str, kind: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Lấy memory của user (và global). Merge global luôn để bot nhớ chung."""
    rows: list[dict] = []
    base = "memory" if kind is None else "memory"
    # user-specific
    q_user = {"user_id": user_id}
    if kind:
        q_user["kind"] = kind
    d1, e1 = safe_select(base, columns="*", filters=q_user)
    if d1:
        rows.extend(d1)
    # global (chỉ khi user_id != global)
    if user_id != "global":
        d2, _ = safe_select("memory", columns="*", filters={"user_id": "global"})
        if d2:
            rows.extend(d2)
    # sắp xếp mới nhất trước, giới hạn
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


# ---------- KNOWLEDGE ----------
def save_knowledge(title: str, body: str, embedding: Optional[list[float]] = None,
                  guild_id: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
    """Lưu 1 skill/kiến thức. embedding: list 1536 chiều (từ OpenAI embedding)."""
    row = {"title": title, "body": body, "guild_id": guild_id}
    if embedding is not None:
        row["embedding"] = embedding
    data = safe_insert("knowledge", row)
    if data is None:
        return None, "Lỗi lưu knowledge (DB null)"
    return data, None


def search_knowledge(query_embedding: list[float], guild_id: Optional[str] = None,
                     top_k: int = 5) -> list[dict]:
    """Semantic search knowledge bằng cosine distance (pgvector)."""
    def _q(client):
        # pgvector: dùng <-> (cosine) hoặc <=> (l2). Ở đây cosine distance.
        sql = (
            "select id, title, body, 1 - (embedding <=> :q) as score "
            "from knowledge where embedding is not null"
        )
        params: dict = {"q": query_embedding}
        if guild_id:
            sql += " and (guild_id = :gid or guild_id is null)"
            params["gid"] = guild_id
        sql += " order by embedding <=> :q limit :k"
        params["k"] = top_k
        return client.rpc if False else None  # placeholder, dùng execute bên dưới

    # Dùng execute() linh hoạt hơn cho SQL động + vector param
    def _builder(client):
        from supabase import Client
        # client đã là Client; chạy SQL thô qua execute
        sql = (
            "select id, title, body, 1 - (embedding <=> :q) as score "
            "from knowledge where embedding is not null"
        )
        params: dict = {"q": query_embedding}
        if guild_id:
            sql += " and (guild_id = :gid or guild_id is null)"
            params["gid"] = guild_id
        sql += " order by embedding <=> :q limit :k"
        params["k"] = top_k
        return client.sql(sql).params(**params)

    data, err = execute(_builder)
    if err:
        logger.warning("search_knowledge lỗi: %s", err)
        return []
    return data or []


def list_knowledge(limit: int = 20) -> list[dict]:
    d, _ = safe_select("knowledge", columns="id,title,created_at", limit=limit)
    return d or []
