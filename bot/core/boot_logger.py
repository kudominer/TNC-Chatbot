"""
boot_logger.py — Gửi log boot lên Supabase system_logs NGAY từ lúc process start.

Vấn đề: SystemLogger chỉ start trong setup_hook (SAU khi Discord login thành
công). Nếu bot.run() treo/fail ở bước login (rate-limit 429, token lỗi,
mạng), KHÔNG một dấu vết nào lọt vào system_logs → không thể chẩn đoán từ xa.

Giải pháp: logger thuần urllib, fire-and-forget qua thread daemon, timeout 5s
— không phụ thuộc discord.py, không bao giờ block hay giết process.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


def boot_log(message: str, level: str = "INFO") -> None:
    """In ra stdout VÀ đẩy lên system_logs (fire-and-forget)."""
    line = f"[BOOT] {message}"
    print(line, flush=True)
    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    def _send():
        try:
            row = {
                "level": level,
                "module": "boot",
                "message": line,
            }
            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/system_logs",
                data=json.dumps(row).encode("utf-8"),
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # log không được phép giết bot

    threading.Thread(target=_send, daemon=True).start()
