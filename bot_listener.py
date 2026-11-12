import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Manga Studio Bot Listener & Queue Worker LIVE!")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_KEY", "")
TG_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}" if BOT_TOKEN else ""

def get_sb_headers():
    return {
        "apikey": SB_KEY.strip(),
        "Authorization": f"Bearer {SB_KEY.strip()}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def update_supabase_status(episode_id: str, new_status: str):
    if not SB_URL or not SB_KEY:
        return False
    endpoint = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}"
    try:
        r = requests.patch(endpoint, headers=get_sb_headers(), json={"status": new_status}, timeout=10)
        return r.status_code in [200, 204]
    except Exception:
        return False

def answer_callback(callback_id: str, text: str = ""):
    if not TG_API_BASE:
        return
    try:
        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except Exception:
        pass

def edit_caption(chat_id: int, message_id: int, new_caption: str):
    if not TG_API_BASE:
        return
    try:
        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": new_caption}, timeout=10)
    except Exception:
        pass

def background_queue_worker():
    while True:
        try:
            time.sleep(300)
            if not SB_URL or not SB_KEY:
                continue
            endpoint = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?status=eq.queued_retry"
            r = requests.get(endpoint, headers=get_sb_headers(), timeout=15)
            if r.status_code == 200:
                for ep in r.json():
                    update_supabase_status(ep.get("id"), "retry_ready")
        except Exception:
            pass

threading.Thread(target=background_queue_worker, daemon=True).start()

def start_polling():
    if not TG_API_BASE:
        return
    offset = 0
    while True:
        try:
            res = requests.get(f"{TG_API_BASE}/getUpdates?timeout=30&offset={offset}", timeout=40)
            data = res.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    message_id = msg.get("message_id")

                    if cb_data.startswith("publish_"):
                        ep_id = cb_data.replace("publish_", "")
                        answer_callback(cb_id, text="Aprobado para TikTok")
                        ok = update_supabase_status(ep_id, "uploaded")
                        txt = f"✅ Episodio #{ep_id} aprobado y listo en TikTok." if ok else f"⚠️ Error BD #{ep_id}"
                        edit_caption(chat_id, message_id, txt)

                    elif cb_data.startswith("retry_"):
                        ep_id = cb_data.replace("retry_", "")
                        answer_callback(cb_id, text="Encolado para reintento")
                        update_supabase_status(ep_id, "queued_retry")
                        edit_caption(chat_id, message_id, f"⏳ Episodio #{ep_id} en cola de reintento.")
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    start_polling()
