import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes
from supabase import create_client

# Variables de entorno leídas desde la configuración del servidor
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SB_URL = os.getenv("SUPABASE_URL", "https://xfarkrxclnhibjvbrpxt.supabase.co")
SB_KEY = os.getenv("SUPABASE_KEY")
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN")

supabase = None
if SB_URL and SB_KEY:
    try:
        supabase = create_client(SB_URL, SB_KEY)
    except Exception as e:
        print(f"Error conectando Supabase: {e}")

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("publish_"):
        episode_id = data.replace("publish_", "")
        await query.edit_message_caption(caption=f"⏳ Procesando publicación del episodio #{episode_id} a TikTok...")
        
        # 1. Actualizar estado en Supabase
        if supabase:
            try:
                supabase.table("manga_episodes").update({"status": "uploaded"}).eq("id", episode_id).execute()
            except Exception as ex:
                print(f"Error al actualizar Supabase: {ex}")

        await query.edit_message_caption(caption=f"✅ ¡Episodio #{episode_id} marcado como publicado en TikTok!")

    elif data.startswith("retry_"):
        episode_id = data.replace("retry_", "")
        if supabase:
            try:
                supabase.table("manga_episodes").update({"status": "retry_requested"}).eq("id", episode_id).execute()
            except Exception:
                pass
        await query.edit_message_caption(caption=f"🔄 Solicitud de regeneración registrada para el episodio #{episode_id}.")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN no configurado.")
    else:
        print("🚀 Telegram Webhook Listener activo y escuchando botones...")
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CallbackQueryHandler(handle_button_click))
        app.run_polling()
