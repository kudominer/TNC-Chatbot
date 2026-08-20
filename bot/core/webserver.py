import os
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
    return f"🛡️ {bot_name} Manager v40 [Siphoned + Massing + GuildCheck] Live! ID: {BOT_SESSION_ID}"


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


def keep_alive(bot=None):
    global bot_instance
    bot_instance = bot
    Thread(target=_run).start()
