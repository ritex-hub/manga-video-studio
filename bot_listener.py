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
        self.wfile.write(b"Telegram Listener Bot is LIVE!")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9107826349:AAF3wV6KsnGBco7kIc0Qoi15K_yPr_qrDQA")
SB_URL = os.getenv("SUPABASE_URL", "https://xfarkrxclnhibjvbrpxt.supabase.co")
SB_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_i3Sn59uhvXjPiA7XmzWCEg_VkR-deY1")

TG_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}"

def update_supabase_status(episode_id: str, new_status: str):
    if not SB_URL or not SB_KEY:
        return False
    endpoint = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}"
    headers = {
        "apikey": SB_KEY.strip(),
        "Authorization": f"Bearer {SB_KEY.strip()}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    try:
        r = requests.patch(endpoint, headers=headers, json={"status": new_status}, timeout=10)
        return r.status_code in [200, 204]
    except Exception as e:
        print(f"Error actualizando Supabase: {e}")
        return False

def answer_callback(callback_id: str, text: str = ""):
    try:
        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except Exception:
        pass

def edit_caption(chat_id: int, message_id: int, new_caption: str):
    try:
        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": new_caption}, timeout=10)
    except Exception:
        pass

def start_polling():
    if not BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN no configurado.")
        return

    print("🚀 Telegram Bot Listener activo y en espera de eventos...")
    offset = 0

    while True:
        try:
            url = f"{TG_API_BASE}/getUpdates?timeout=30&offset={offset}"
            res = requests.get(url, timeout=40)
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
                        answer_callback(cb_id, text="Subiendo a TikTok...")
                        ok = update_supabase_status(ep_id, "uploaded")
                        msg_txt = f"✅ ¡Episodio #{ep_id} marcado como publicado en TikTok!" if ok else f"⚠️ Error en BD para episodio #{ep_id}."
                        edit_caption(chat_id, message_id, msg_txt)

                    elif cb_data.startswith("retry_"):
                        ep_id = cb_data.replace("retry_", "")
                        answer_callback(cb_id, text="Regenerando...")
                        update_supabase_status(ep_id, "retry_requested")
                        edit_caption(chat_id, message_id, f"🔄 Solicitud de regeneración registrada para el episodio #{ep_id}.")

        except requests.exceptions.RequestException:
            time.sleep(3)
        except Exception as e:
            print(f"Error en bucle de polling: {e}")
            time.sleep(2)

if __name__ == "__main__":
    start_polling()
