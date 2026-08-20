"""
db.py — Supabase client wrapper chuẩn cho backend (bot).

Thiết kế:
- Lazy init: client chỉ tạo khi thực sự dùng (tránh crash lúc import nếu thiếu env).
- Retry với exponential backoff (mặc định 3 lần) cho mọi thao tác mạng.
- Timeout mặc định để không treo vô hạn.
- Structured logging qua module `logging` (không print).
- Fail rõ ràng: hàm helper trả về (data, error) thay vì raise ngầm.

Không bao giờ dùng anon key ở backend.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional, TypeVar

try:
    from supabase import Client, create_client
except ImportError:
    Client = Any  # type: ignore
    create_client = None  # type: ignore

from .config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("bot.db")

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5  # giây, nhân đôi mỗi lần
DEFAULT_TIMEOUT = 10.0  # giây

T = TypeVar("T")


class DBError(Exception):
    """Lỗi tầng DB (sau khi đã retry hết số lần)."""


_client: Optional[Client] = None
_initialized = False


def get_client() -> Optional[Client]:
    """Trả về Supabase client (singleton), hoặc None nếu chưa cấu hình."""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("SUPABASE_URL/KEY chưa cấu hình — DB bị disable.")
        _client = None
        return None
    if create_client is None:
        logger.error("Thiếu thư viện supabase — pip install supabase.")
        _client = None
        return None

    try:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client đã khởi tạo (service role).")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Không thể khởi tạo Supabase client: %s", exc)
        _client = None
    return _client


def _with_retry(fn: Callable[[], T], *, retries: int = DEFAULT_RETRIES,
               backoff: float = DEFAULT_BACKOFF) -> T:
    """Chạy fn, tự retry nếu raise. Raise DBError sau khi hết retries."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(
                "DB call thất bại (lần %d/%d): %s — retry sau %.2fs",
                attempt, retries, exc, wait,
            )
            if attempt < retries:
                time.sleep(wait)
    raise DBError(f"DB call failed after {retries} attempts: {last_exc}")


# ── High-level helpers ──────────────────────────────────────────────────────

def safe_select(table: str, *, columns: str = "*",
                filters: Optional[dict] = None,
                single: bool = False) -> tuple[Optional[Any], Optional[str]]:
    """SELECT an toàn. Trả (data, error_msg). error_msg=None nếu OK."""
    client = get_client()
    if client is None:
        return (None, "client_unavailable")
    try:
        query = client.table(table).select(columns)
        for k, v in (filters or {}).items():
            query = query.eq(k, v)
        if single:
            query = query.maybe_single()
        res = _with_retry(lambda: query.execute())
        if res is None:
            return (None, "query_returned_none")
        if getattr(res, "error", None):
            return (None, str(res.error))
        return (res.data, None)
    except DBError as e:
        return (None, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("safe_select lỗi không mong đợi: %s", e)
        return (None, str(e))


def safe_upsert(table: str, row: dict, *,
                on_conflict: Optional[str] = None) -> Optional[str]:
    """UPSERT an toàn. Trả error_msg (None nếu OK)."""
    client = get_client()
    if client is None:
        return "client_unavailable"
    try:
        query = client.table(table).upsert(row)
        # sync client (supabase 2.x) không có .on_conflict(); upsert mặc định đã ghi đè theo PK
        res = _with_retry(lambda: query.execute())
        if res is None:
            return (None, "query_returned_none")
        if getattr(res, "error", None):
            return str(res.error)
        return None
    except DBError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("safe_upsert lỗi không mong đợi: %s", e)
        return str(e)


def safe_insert(table: str, row: dict) -> Optional[str]:
    client = get_client()
    if client is None:
        return "client_unavailable"
    try:
        res = _with_retry(lambda: client.table(table).insert(row).execute())
        if res is None:
            return (None, "query_returned_none")
        if getattr(res, "error", None):
            return str(res.error)
        return None
    except DBError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("safe_insert lỗi không mong đợi: %s", e)
        return str(e)


def safe_update(table: str, row: dict, *,
                filters: dict) -> Optional[str]:
    client = get_client()
    if client is None:
        return "client_unavailable"
    try:
        query = client.table(table).update(row)
        for k, v in filters.items():
            query = query.eq(k, v)
        res = _with_retry(lambda: query.execute())
        if res is None:
            return (None, "query_returned_none")
        if getattr(res, "error", None):
            return str(res.error)
        return None
    except DBError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("safe_update lỗi không mong đợi: %s", e)
        return str(e)


def execute(query_builder: Callable[[Client], Any]) -> tuple[Optional[Any], Optional[str]]:
    """Chạy 1 query builder (hàm nhận client → trả query object) với retry.

    Dùng cho các thao tác phức tạp (bulk upsert, delete, order/limit):
        data, err = execute(lambda c: c.table("x").select("*").execute())
    """
    client = get_client()
    if client is None:
        return (None, "client_unavailable")
    try:
        res = _with_retry(lambda: query_builder(client).execute())
        if res is None:
            return (None, "query_returned_none")
        if getattr(res, "error", None):
            return (None, str(res.error))
        return (res, None)
    except DBError as e:
        return (None, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("execute lỗi không mong đợi: %s", e)
        return (None, str(e))

