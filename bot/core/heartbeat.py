"""
heartbeat.py — Bot đập tim định kỳ lên Supabase (bảng bot_status).

Web dashboard đọc bảng này để biết bot có đang online hay không,
thay vì hiện chữ "online" giả. Nếu bot chết > 90s không đập tim,
dashboard tự nhận là offline (xem web_dashboard/src/app/api/bot-status).
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from core.storage import save_json

logger = logging.getLogger("bot.heartbeat")

STATUS_KEY = os.getenv("HEARTBEAT_STATUS_KEY", "tnc_chatbot_status.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "Storage", STATUS_KEY)

HEARTBEAT_INTERVAL = 60  # giây


async def _heartbeat(bot) -> None:
    """Ghi trạng thái bot vào Supabase mỗi HEARTBEAT_INTERVAL giây."""
    import asyncio
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            latency = bot.latency
            latency_ms = None if latency == float("inf") else round(latency * 1000)
            payload = {
                "online": True,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "latency_ms": latency_ms,
                "shard_id": getattr(bot, "shard_id", None),
            }
            # Lưu vào json_storage (key = tnc_chatbot_status.json)
            # Dashboard sẽ đọc qua API /api/bot-status
            # Chạy ở thread riêng — request Supabase đồng bộ không được
            # chặn event loop, nếu không bot sẽ ngưng rep khi mạng chậm.
            await asyncio.to_thread(save_json, payload, STATUS_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Heartbeat lỗi: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def start(bot) -> None:
    bot.loop.create_task(_heartbeat(bot))
    logger.info("Heartbeat đã khởi chạy (mỗi %ds)", HEARTBEAT_INTERVAL)
