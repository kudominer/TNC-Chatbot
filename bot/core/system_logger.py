import sys
import asyncio
import traceback
import aiohttp
from datetime import datetime
from core.database import execute
import os

LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL", "")

class SupabaseLogInterceptor:
    def __init__(self, original_stream, level="INFO"):
        self.original_stream = original_stream
        self.level = level
        self.buffer = ""

    def write(self, message):
        self.original_stream.write(message)
        
        # Ngăn chặn vòng lặp vô tận nếu chính quá trình ghi log bị lỗi hoặc sinh ra log
        if SystemLogger._is_consuming:
            return

        self.buffer += message
        if "\n" in self.buffer:
            lines = self.buffer.split("\n")
            self.buffer = lines.pop() # Phần cuối chưa có \n giữ lại
            for line in lines:
                if line.strip():
                    SystemLogger.add_log(self.level, "system", line)

    def flush(self):
        self.original_stream.flush()


class SystemLogger:
    log_queue = []
    _task = None
    _is_consuming = False

    @classmethod
    def add_log(cls, level, module, message):
        # Tự động nâng cấp level lên ERROR nếu phát hiện dấu hiệu lỗi
        if "Traceback (most recent call last)" in message or "Exception:" in message or "Error:" in message:
            level = "ERROR"
            
        cls.log_queue.append({
            "level": level,
            "module": module,
            "message": message
        })

    @classmethod
    def start(cls, bot):
        sys.stdout = SupabaseLogInterceptor(sys.stdout, "INFO")
        sys.stderr = SupabaseLogInterceptor(sys.stderr, "ERROR")
        cls._task = bot.loop.create_task(cls.consume_logs())

    @classmethod
    async def consume_logs(cls):
        while True:
            await asyncio.sleep(2)
            if not cls.log_queue:
                continue

            # Pop all items
            batch = cls.log_queue[:]
            cls.log_queue.clear()

            cls._is_consuming = True
            
            try:
                # Gửi theo mảng để tối ưu API call
                _, err = execute(lambda c: c.table("system_logs").insert(batch))
                if err:
                    # Không print ở đây để tránh infinite loop qua interceptor
                    pass
            except Exception:
                pass # Silently ignore để không tạo ra infinite loop

            # Lọc các lỗi ERROR để bắn Webhook
            errors = [item for item in batch if item["level"] == "ERROR"]
            if errors and LOG_WEBHOOK_URL:
                try:
                    async with aiohttp.ClientSession() as session:
                        for error in errors:
                            msg = error["message"]
                            if len(msg) > 1900:
                                msg = msg[:1900] + "..."
                            
                            payload = {
                                "content": f"🚨 **[HỆ THỐNG BÁO LỖI]**\n```\n{msg}\n```"
                            }
                            await session.post(LOG_WEBHOOK_URL, json=payload)
                except Exception:
                    pass
            
            cls._is_consuming = False
