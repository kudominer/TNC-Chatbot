"""
storage.py — JSON blob storage trên Supabase (bảng json_storage).

Cơ chế: dùng file_name làm khóa chính. Đọc/ghi có retry + validation cơ bản.
Thay thế cơ chế sync GitHub cũ (đã bỏ — xem git history).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from .db import safe_select, safe_upsert

logger = logging.getLogger("bot.storage")

TABLE = "json_storage"


def load_json(path: str, default: Any | Callable[[], Any]) -> Any:
    """Đọc JSON từ Supabase (key = basename(path)). Nếu thiếu → default."""
    filename = os.path.basename(path)
    try:
        data, err = safe_select(TABLE, columns="data",
                                filters={"file_name": filename}, single=True)
        if err:
            logger.warning("load_json %s: %s", filename, err)
        elif data is not None:
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return data
    except Exception as exc:  # noqa: BLE001
        logger.exception("load_json %s lỗi: %s", filename, exc)

    # Fallback default
    return default() if callable(default) else default


def save_json(data: Any, path: str) -> bool:
    """Lưu JSON lên Supabase. Trả True nếu thành công."""
    filename = os.path.basename(path)
    if not isinstance(data, (dict, list)):
        logger.error("save_json %s: data phải là dict/list, nhận %s",
                     filename, type(data).__name__)
        return False
    # serialize test để bắt lỗi trước khi gửi
    try:
        json.dumps(data)
    except (TypeError, ValueError) as exc:
        logger.error("save_json %s: data không serialize được: %s", filename, exc)
        return False

    err = safe_upsert(TABLE, {"file_name": filename, "data": data},
                      on_conflict="file_name")
    if err:
        logger.error("save_json %s thất bại: %s", filename, err)
        return False
    logger.info("save_json %s OK", filename)
    return True


async def save_json_async(data: Any, path: str, sync_github: bool = True) -> None:
    """Alias async của save_json để tương thích cog cũ (vd massing).

    Không chạy threadpool — safe_upsert đã wrap retry/timeout nội bộ,
    gọi trực tiếp trong event loop là an toàn (đồng bộ với toàn bộ dự án).
    """
    save_json(data, path)


def restore_from_github() -> None:
    """No-op backward-compatible.

    Trước đây (do bản cũ) hàm này kéo JSON data từ GitHub về trước khi load cog.
    Cơ chế sync GitHub đã bỏ (dữ liệu giờ nằm trên Supabase json_storage).
    Giữ hàm rỗng để main.py không bị ImportError. Không thực hiện network call.
    """
    logger.info("restore_from_github: no-op (data đã nằm trên Supabase)")
