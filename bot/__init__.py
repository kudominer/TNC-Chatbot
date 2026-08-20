import os
import sys

# Thêm thư mục bot/ vào sys.path để tất cả modules (main, cogs, core)
# đều import được core.* khi chạy python -m bot.main từ repo root
_bot_dir = os.path.dirname(os.path.abspath(__file__))
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)
