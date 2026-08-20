import os
import time
import threading
import urllib.request
from threading import Thread

from flask import Flask

from .config import BOT_SESSION_ID

# ==============================================================================
# WEB SERVER FLASK (TREO BOT ONLINE TRÊN RENDER)
# ==============================================================================
app = Flask("")
bot_instance = None


@app.route("/")
def home():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "index.html")
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
            return content.replace("{{ session_id }}", str(BOT_SESSION_ID))
    except Exception as e:
        print(f"❌ Lỗi load web template: {e}")

    bot_name = os.getenv("BOT_NAME", "TNT")
    return f"🛡️ {bot_name} Chatbot v1.0 [AI Chat] Live! ID: {BOT_SESSION_ID}"


@app.route("/health")
def health():
    return "ok", 200


def _run():
    app.run(host="0.0.0.0", port=5000)


@app.route("/api/webhook/reload", methods=["POST"])
def webhook_reload():
    global bot_instance
    if bot_instance:
        try:
            bot_instance.loop.call_soon_threadsafe(bot_instance.dispatch, "config_reload")
            return {"status": "success", "message": "Triggered config_reload event"}, 200
        except Exception as e:
            return {"status": "error", "message": str(e)}, 500
    return {"status": "error", "message": "Bot instance not found"}, 500


# ==============================================================================
# SELF-PING — giữ Render free tier không sleep (mỗi 4 phút)
# ==============================================================================
def _self_ping():
    """Gửi GET request đến chính nó mỗi 4 phút để Render không sleep."""
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        return  # Chạy local thì không cần ping
    url = f"https://{hostname}/health"
    print(f"🔄 Self-ping enabled: {url} mỗi 4 phút")
    while True:
        time.sleep(240)  # 4 phút
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"🏓 Self-ping OK ({resp.status})")
        except Exception as e:
            print(f"⚠️ Self-ping failed: {e}")


def keep_alive(bot=None):
    global bot_instance
    bot_instance = bot
    # QUAN TRỌNG: Flask thread KHÔNG được daemon=True
    # Trên Render, nếu bot.run() crash, daemon thread sẽ bị kill theo
    # → Flask die → process exit → bot offline vĩnh viễn
    Thread(target=_run).start()
    # Self-ping thread IS daemon (chỉ cần giữ awake, không cần giữ process)
    threading.Thread(target=_self_ping, daemon=True).start()
