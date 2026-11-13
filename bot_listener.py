import os
import time
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from studio_engine import (
    produce_series_episode,
    get_sb_headers,
    publish_to_tiktok_api
)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"MangaStudioPro_bot Worker (Queue + TikTok + Auto-Pilot) LIVE!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

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
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")

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
    print(f"[TG] {text}", flush=True)
    if not TG_API_BASE:
        return
    try:
        requests.post(f"{TG_API_BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        print(f"[TG ERROR] {e}", flush=True)

def get_episode_from_db(episode_id: str):
    if not SB_URL or not SB_KEY:
        return None
    try:
        r = requests.get(f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}", headers=get_sb_headers(SB_KEY), timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception:
        pass
    return None

def update_supabase_status(episode_id, new_status: str):
    if not SB_URL or not SB_KEY:
        return False
    endpoint = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}"
    try:
        r = requests.patch(endpoint, headers=get_sb_headers(SB_KEY), json={"status": new_status}, timeout=10)
        return r.status_code in [200, 204]
    except Exception:
        return False

def delete_queue_placeholder(episode_id):
    if not SB_URL or not SB_KEY:
        return
    try:
        requests.delete(f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?id=eq.{episode_id}", headers=get_sb_headers(SB_KEY), timeout=10)
    except Exception:
        pass

def recover_stuck_jobs():
    """Al arrancar Render, devuelve a 'queued' cualquier tarea que haya quedado atascada en 'processing'."""
    if not (SB_URL and SB_KEY):
        return
    try:
        url = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?status=eq.processing"
        r = requests.patch(url, headers=get_sb_headers(SB_KEY), json={"status": "queued"}, timeout=10)
        if r.status_code in [200, 204] and r.text and r.text != "[]":
            print("[WORKER] Tareas atascadas recuperadas automáticamente a 'queued'.", flush=True)
    except Exception as e:
        print(f"[WORKER RECOVER ERROR] {e}", flush=True)

def supabase_queue_worker():
    """Revisa cada 10 segundos si Streamlit encoló una serie con status='queued'."""
    recover_stuck_jobs()
    print("[WORKER] Cola de Supabase iniciada y escuchando...", flush=True)
    while True:
        time.sleep(10)
        if not (SB_URL and SB_KEY and GEMINI_KEY):
            continue
        try:
            url = f"{SB_URL.rstrip('/')}/rest/v1/manga_episodes?status=eq.queued&order=id.asc&limit=1"
            r = requests.get(url, headers=get_sb_headers(SB_KEY), timeout=10)
            if r.status_code == 200 and r.json():
                job = r.json()[0]
                job_id = job["id"]
                s_name = job["series_name"]
                desc_meta = job.get("description", "")
                lang_ov = desc_meta.replace("LANG:", "").strip() if desc_meta.startswith("LANG:") else ""

                update_supabase_status(job_id, "processing")
                send_tg_text(CHAT_ID, f"⚙️ Render tomó la orden en segundo plano: '{s_name}'...")
                try:
                    _, title, chap = produce_series_episode(
                        series_name=s_name,
                        custom_direction=f"Continúa la trama principal de {s_name} con acción intensa y un cliffhanger.",
                        config=ENGINE_CONFIG,
                        language_override=lang_ov,
                        progress_callback=lambda msg: send_tg_text(CHAT_ID, msg)
                    )
                    delete_queue_placeholder(job_id)
                except Exception as e:
                    update_supabase_status(job_id, "error")
                    send_tg_text(CHAT_ID, f"❌ Error produciendo '{s_name}': {e}")
        except Exception as e:
            print(f"[WORKER ERROR] {e}", flush=True)

threading.Thread(target=supabase_queue_worker, daemon=True).start()

def execute_tiktok_publish(episode_id: str, chat_id: int, message_id: int):
    ep = get_episode_from_db(episode_id)
    if not ep or not ep.get("video_url"):
        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": f"⚠️ No se encontró el archivo de video del Episodio #{episode_id}."}, timeout=10)
        return

    local_mp4 = f"/tmp/pub_{episode_id}.mp4"
    try:
        r_vid = requests.get(ep["video_url"], timeout=60)
        with open(local_mp4, "wb") as f:
            f.write(r_vid.content)
    except Exception as e:
        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": f"❌ Error descargando video: {e}"}, timeout=10)
        return

    title_full = f"{ep['title']} {ep.get('hashtags', '')}"
    ok_tk, msg_tk = publish_to_tiktok_api(local_mp4, title_full, TIKTOK_ACCESS_TOKEN)

    if ok_tk:
        update_supabase_status(episode_id, "published")
        caption_res = f"✅ Episodio #{episode_id} enviado a TikTok\n{msg_tk}"
    else:
        caption_res = f"⚠️ Aviso TikTok (Ep. #{episode_id}): {msg_tk}"

    requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": caption_res}, timeout=10)

def get_all_saved_series():
    if not SB_URL or not SB_KEY:
        return []
    try:
        r = requests.get(f"{SB_URL.rstrip('/')}/rest/v1/saved_scripts?select=title&order=created_at.asc", headers=get_sb_headers(SB_KEY), timeout=10)
        if r.status_code == 200:
            return [row["title"] for row in r.json() if row.get("title")]
    except Exception:
        pass
    return []

def trigger_single_production(series_name: str, chat_id):
    send_tg_text(chat_id, f"⏳ Produciendo '{series_name}' (720p HD + Memoria Visual + SFX)...")
    try:
        _, title, chap = produce_series_episode(
            series_name=series_name,
            custom_direction=f"Continúa la trama principal de {series_name} con acción intensa y un cliffhanger.",
            config=ENGINE_CONFIG,
            progress_callback=lambda msg: send_tg_text(chat_id, msg)
        )
        send_tg_text(chat_id, f"✅ ¡{title} listo! El próximo será el Capítulo #{chap + 1}.")
    except Exception as e:
        send_tg_text(chat_id, f"❌ Error produciendo '{series_name}': {e}")

def trigger_all_series_production(chat_id):
    series_list = get_all_saved_series()
    if not series_list:
        send_tg_text(chat_id, "⚠️ No hay series guardadas en Supabase.")
        return
    send_tg_text(chat_id, f"🚀 Piloto Automático Multi-Serie ({len(series_list)} series): {', '.join(series_list)}")
    for idx, s_name in enumerate(series_list):
        trigger_single_production(s_name, chat_id)
        if idx < len(series_list) - 1:
            time.sleep(15)
    send_tg_text(chat_id, "🏁 Ronda diaria completada.")

def daily_autopilot_scheduler():
    last_run_date = ""
    while True:
        time.sleep(45)
        now = datetime.datetime.utcnow()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == 18 and now.minute == 0 and last_run_date != today_str:
            last_run_date = today_str
            if GEMINI_KEY and SB_URL:
                threading.Thread(target=trigger_all_series_production, args=(CHAT_ID,), daemon=True).start()

threading.Thread(target=daily_autopilot_scheduler, daemon=True).start()

def start_polling():
    if not TG_API_BASE:
        print("[ERROR] Falta TELEGRAM_BOT_TOKEN", flush=True)
        return
    try:
        requests.get(f"{TG_API_BASE}/deleteWebhook", timeout=10)
    except Exception:
        pass

    print("[TELEGRAM] Polling iniciado para @MangaStudioPro_bot...", flush=True)
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

                    if msg_text == "/start":
                        send_tg_text(c_id, "🎬 ¡Bienvenido a Manga Studio Pro!\n\nComandos disponibles:\n• /producir_todas — Produce el siguiente capítulo de todas tus series.\n• /producir Nombre de la Serie — Produce una serie específica.")
                    elif msg_text == "/producir_todas":
                        threading.Thread(target=trigger_all_series_production, args=(c_id,), daemon=True).start()
                    elif msg_text.startswith("/producir"):
                        s_name = msg_text.replace("/producir", "").strip()
                        if s_name:
                            threading.Thread(target=trigger_single_production, args=(s_name, c_id), daemon=True).start()
                        else:
                            series_avail = get_all_saved_series()
                            lista = "\n".join([f"• /producir {s}" for s in series_avail]) if series_avail else "Ninguna serie guardada aún."
                            send_tg_text(c_id, f"📚 Series disponibles:\n{lista}\n\nO usa /producir_todas")

                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    message_id = msg.get("message_id")

                    if cb_data.startswith("publish_tk_"):
                        ep_id = cb_data.replace("publish_tk_", "")
                        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Subiendo a TikTok..."}, timeout=10)
                        threading.Thread(target=execute_tiktok_publish, args=(ep_id, chat_id, message_id), daemon=True).start()

                    elif cb_data.startswith("retry_"):
                        ep_id = cb_data.replace("retry_", "")
                        requests.post(f"{TG_API_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Encolado"}, timeout=10)
                        update_supabase_status(ep_id, "queued")
                        requests.post(f"{TG_API_BASE}/editMessageCaption", json={"chat_id": chat_id, "message_id": message_id, "caption": f"⏳ Episodio #{ep_id} encolado para regeneración."}, timeout=10)
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    start_polling()
