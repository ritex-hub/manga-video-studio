import os
import time
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from studio_engine import produce_series_episode, get_sb_headers

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Manga Studio Background Worker & Auto-Pilot LIVE!")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8870484184")
SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
HF_TOKEN = os.getenv("HF_API_TOKEN", "")
FS_KEY = os.getenv("FREESOUND_API_KEY", "")
MODAL_URL = os.getenv("MODAL_ENDPOINT_URL", "")
AUTO_SERIES = os.getenv("AUTO_PILOT_SERIES", "Jun-woo: Neo-Seúl")

TG_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}" if BOT_TOKEN else ""

ENGINE_CONFIG = {
    "sb_url": SB_URL,
    "sb_key": SB_KEY,
    "gemini_key": GEMINI_KEY,
    "hf_token": HF_TOKEN,
    "fs_key": FS_KEY,
    "tg_token": BOT_TOKEN,
    "tg_chat": CHAT_ID,
    "modal_url": MODAL_URL
}

def send_tg_text(chat_id, text):
    if not TG_API_BASE:
        return
    try:
        requests.post(f"{TG_API_BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass

def trigger_background_production(series_name: str, chat_id):
    send_tg_text(chat_id, f"⏳ Iniciando producción en segundo plano para '{series_name}' (Multi-Voz + Modal GPU)...")
    try:
        _, title, chap = produce_series_episode(
            series_name=series_name,
            custom_direction=f"Continúa la trama principal de {series_name} con acción intensa y un cliffhanger.",
            config=ENGINE_CONFIG
        )
        send_tg_text(chat_id, f"✅ ¡{title} terminado y enviado! El próximo será el Capítulo #{chap + 1}.")
    except Exception as e:
        send_tg_text(chat_id, f"❌ Error en producción en segundo plano: {e}")

def daily_autopilot_scheduler():
    """Produce 1 capítulo automático todos los días a las 18:00 UTC."""
    last_run_date = ""
    while True:
        time.sleep(45)
        now = datetime.datetime.utcnow()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == 18 and now.minute == 0 and last_run_date != today_str:
            last_run_date = today_str
            if GEMINI_KEY and SB_URL:
                threading.Thread(
                    target=trigger_background_production,
                    args=(AUTO_SERIES, CHAT_ID),
                    daemon=True
                ).start()

threading.Thread(target=daily_autopilot_scheduler, daemon=True).start()

def update_supabase_status(episode_id: str, new_status: str):
    if not SB_URL or not SB_KEY:
        return False
    endpoint = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}"
    try:
        r = requests.patch(endpoint, headers=get_sb_headers(SB_KEY), json={"status": new_status}, timeout=10)
        return r.status_code in [200, 204]
    except Exception:
        return False

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

                if "message" in update and "text" in update["message"]:
                    msg_text = update["message"]["text"].strip()
                    c_id = update["message"]["chat"]["id"]
                    if msg_text.startswith("/producir"):
                        s_name = msg_text.replace("/producir", "").strip() or AUTO_SERIES
                        threading.Thread(
                            target=trigger_background_production,
                            args=(s_name, c_id),
                            daemon=True
                        ).start()

                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    message_id = msg.get("message_id")

                    if cb_data.startswith("publish_"):
                        ep_id = cb_data.replace("publish_", "")
                        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Aprobado"}, timeout=10)
                        ok = update_supabase_status(ep_id, "uploaded")
                        txt = f"✅ Episodio #{ep_id} aprobado para TikTok." if ok else f"⚠️ Error BD #{ep_id}"
                        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": txt}, timeout=10)

                    elif cb_data.startswith("retry_"):
                        ep_id = cb_data.replace("retry_", "")
                        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Regenerando..."}, timeout=10)
                        threading.Thread(target=trigger_background_production, args=(AUTO_SERIES, chat_id), daemon=True).start()
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    start_polling()
