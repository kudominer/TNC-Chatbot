"""
config_store.py — Centralized, cached config access layer.

Vấn đề cũ: mỗi cog tự query Supabase riêng → N query trùng lặp, không cache,
không invalidate thống nhất.

Giải pháp:
- Single in-memory cache per (table, guild_id).
- Load lazy qua safe_select (có retry).
- Invalidate khi dashboard PATCH (gọi bot webhook → on_config_reload).
- Guard: nếu client thiếu, trả default, không crash.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from .config import GUILD_ID
from .db import safe_select, safe_upsert, safe_insert

logger = logging.getLogger("bot.config_store")

_cache: dict[tuple[str, str], Any] = {}
_cache_lock = threading.Lock()
_enabled = True  # tắt cache nếu muốn luôn đọc tươi


def get_config(table: str, guild_id: str | None = None,
               default: Any | Callable[[], Any] = None,
               id_column: str = "guild_id") -> Any:
    """Lấy config từ cache hoặc Supabase. Trả default nếu thiếu."""
    gid = str(guild_id or GUILD_ID)
    key = (table, gid)

    if _enabled:
        with _cache_lock:
            if key in _cache:
                return _cache[key]

    data, err = safe_select(table, columns="*",
                             filters={id_column: gid}, single=True)
    if err:
        logger.warning("get_config %s/%s: %s", table, gid, err)
    if data is None:
        data = default() if callable(default) else default

    with _cache_lock:
        _cache[key] = data
    return data


def save_config(table: str, row: dict, *,
                id_column: str = "guild_id",
                on_conflict: str | None = None) -> bool:
    """Lưu config và cập nhật cache."""
    gid = str(row.get(id_column, ""))
    err = safe_upsert(table, row, on_conflict=on_conflict or id_column)
    if err:
        logger.error("save_config %s/%s: %s", table, gid, err)
        return False
    with _cache_lock:
        _cache[(table, gid)] = row
    return True


def invalidate(table: str | None = None, guild_id: str | None = None) -> None:
    """Xoá cache. Gọi khi dashboard đổi config."""
    gid = str(guild_id or GUILD_ID) if guild_id else None
    with _cache_lock:
        if table is None and gid is None:
            _cache.clear()
        else:
            keys = [k for k in _cache if (table is None or k[0] == table)
                    and (gid is None or k[1] == gid)]
            for k in keys:
                del _cache[k]
    logger.info("Config cache invalidated (table=%s, guild=%s)", table, gid)


def reload_all() -> None:
    """Alias cho invalidate toàn bộ — dùng khi bot nhận webhook."""
    invalidate()
    logger.info("Đã reload toàn bộ config từ Supabase.")
