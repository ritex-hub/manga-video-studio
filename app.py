import os
import time
import requests
import streamlit as st

st.set_page_config(page_title="Manga & Story Studio", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. FUNCIONES AUXILIARES Y GESTIÓN DE SECRETOS
# ==============================================================================

def get_secret(section: str, key: str, default: str = "") -> str:
    """Recupera variables desde st.secrets de manera segura sin romper la ejecución."""
    try:
        return st.secrets[section][key]
    except Exception:
        return default

# Variables de TikTok (obtenidas desde st.secrets si existen)
DEFAULT_CLIENT_KEY = get_secret("tiktok", "client_key", "sbawgv6qorcmbtk9qx")
DEFAULT_CLIENT_SECRET = get_secret("tiktok", "client_secret", "0rdY9yBOfMMkmTLLwjfxcPZvWOkyX9LJ")
DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "act.cuwQeEcQPrfg91SURpGkVe2bhSRAZyyGKVbCPsai75zbKqWtjvQ6OqgiWdIh!5618.s1")
DEFAULT_REFRESH_TOKEN = get_secret("tiktok", "refresh_token", "rft.z05jQNXtC24zqLNoBEAHsuMTpLSOBDbTKj1APHeTiwvVlcyUMngx6W9iSdnR!5635.s1")

# Inicialización del estado de sesión para TikTok
if "tiktok_access_token" not in st.session_state:
    st.session_state["tiktok_access_token"] = DEFAULT_ACCESS_TOKEN

if "tiktok_refresh_token" not in st.session_state:
    st.session_state["tiktok_refresh_token"] = DEFAULT_REFRESH_TOKEN


# ==============================================================================
# 2. VALIDACIONES EN VIVO (GEMINI & TELEGRAM)
# ==============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def verify_gemini_api_key(api_key: str):
    """Valida la API Key de Google Gemini consultando el endpoint de modelos."""
    if not api_key or len(api_key.strip()) < 10:
        return False, "Clave vacía o incompleta"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key.strip()}"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return True, "API Key válida y conectada"
        data = res.json()
        error_msg = data.get("error", {}).get("message", "Clave no válida")
        return False, error_msg
    except Exception as e:
        return False, f"Error de conexión: {str(e)}"


@st.cache_data(ttl=300, show_spinner=False)
def verify_telegram_bot_token(bot_token: str):
    """Valida el Bot Token de Telegram invocando getMe."""
    if not bot_token or ":" not in bot_token:
        return False, "Formato de token inválido", None
    url = f"https://api.telegram.org/bot{bot_token.strip()}/getMe"
    try:
        res = requests.get(url, timeout=5)
        data = res.json()
        if res.status_code == 200 and data.get("ok"):
            bot_username = data["result"].get("username", "Bot")
            return True, f"Conectado (@{bot_username})", bot_username
        return False, data.get("description", "Token inválido"), None
    except Exception as e:
        return False, f"Error de conexión: {str(e)}", None


@st.cache_data(ttl=300, show_spinner=False)
def verify_telegram_chat_id(bot_token: str, chat_id: str):
    """Valida si el bot de Telegram tiene acceso al chat indicado."""
    if not chat_id or not chat_id.strip():
        return False, "Chat ID no proporcionado"
    clean_id = chat_id.strip()
    if not (clean_id.isdigit() or (clean_id.startswith("-") and clean_id[1:].isdigit())):
        return False, "El Chat ID debe ser numérico"
    if not bot_token:
        return False, "Se requiere un Bot Token válido primero"

    url = f"https://api.telegram.org/bot{bot_token.strip()}/getChat?chat_id={clean_id}"
    try:
        res = requests.get(url, timeout=5)
        data = res.json()
        if res.status_code == 200 and data.get("ok"):
            chat_name = data["result"].get("first_name") or data["result"].get("title") or "Chat privado"
            return True, f"Chat verificado: {chat_name}"
        return False, data.get("description", "Chat ID no encontrado o bot sin iniciar")
    except Exception as e:
        return False, f"Error de conexión con el chat: {str(e)}"


# ==============================================================================
# 3. LÓGICA DE TIKTOK CONTENT POSTING API V2
# ==============================================================================

def renew_tiktok_token():
    """Renueva el access_token utilizando el refresh_token permanente."""
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": DEFAULT_CLIENT_KEY,
        "client_secret": DEFAULT_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": st.session_state["tiktok_refresh_token"],
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        res = requests.post(url, headers=headers, data=payload, timeout=20)
        data = res.json()
        if "access_token" in data:
            st.session_state["tiktok_access_token"] = data["access_token"]
            if "refresh_token" in data:
                st.session_state["tiktok_refresh_token"] = data["refresh_token"]
            return data["access_token"]
        else:
            st.error(f"Fallo al renovar token de TikTok: {data}")
            return None
    except Exception as e:
        st.error(f"Error de red al renovar token: {e}")
        return None


def upload_video_to_tiktok(video_path: str):
    """
    Sube un video en formato MP4 a TikTok Content Posting API v2.
    El video llega directamente a los borradores/bandeja de la app vinculada.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"No existe el archivo de video: {video_path}")

    file_size = os.path.getsize(video_path)
    access_token = st.session_state["tiktok_access_token"]

    def _initiate_post(token):
        init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        body = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            }
        }
        return requests.post(init_url, headers=headers, json=body, timeout=30)

    # 1. Iniciar subida
    res_init = _initiate_post(access_token)
    init_data = res_init.json()

    # Si el token caducó, se refresca automáticamente y reintenta
    if init_data.get("error", {}).get("code") in ["access_token_invalid", "token_expired", "invalid_token"]:
        st.warning("El Access Token de TikTok ha expirado. Renovando sesión...")
        access_token = renew_tiktok_token()
        if not access_token:
            raise Exception("No se pudo renovar el token de TikTok.")
        res_init = _initiate_post(access_token)
        init_data = res_init.json()

    if init_data.get("error", {}).get("code") != "ok":
        raise Exception(f"Fallo en TikTok API: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    # 2. Subir el binario del video
    upload_headers = {
        "Content-Type": "video/mp4",
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
    }

    with open(video_path, "rb") as video_file:
        res_upload = requests.put(upload_url, headers=upload_headers, data=video_file, timeout=120)

    if res_upload.status_code not in (200, 201, 206):
        raise Exception(f"Error subiendo bytes a TikTok: {res_upload.status_code} - {res_upload.text}")

    return publish_id


# ==============================================================================
# 4. BARRA LATERAL (CONFIGURACIÓN CON INDICADORES EN TIEMPO REAL)
# ==============================================================================

st.sidebar.title("🔑 Configuración")

# --- 4.1. Gemini API Key ---
default_gemini = get_secret("general", "gemini_api_key")
gemini_key = st.sidebar.text_input(
    "Gemini API Key",
    value=default_gemini,
    type="password",
    help="Clave obtenida desde Google AI Studio"
)

if gemini_key:
    is_gemini_ok, gemini_msg = verify_gemini_api_key(gemini_key)
    if is_gemini_ok:
        st.sidebar.success(f"✅ {gemini_msg}")
    else:
        st.sidebar.error(f"❌ {gemini_msg}")
else:
    st.sidebar.info("ℹ️ Ingresa tu clave de Gemini")

st.sidebar.markdown("---")

# --- 4.2. Telegram Bot Token & Chat ID ---
default_tg_token = get_secret("telegram", "bot_token")
telegram_token = st.sidebar.text_input(
    "Telegram Bot Token",
    value=default_tg_token,
    type="password",
    help="Token de acceso proporcionado por @BotFather"
)

bot_is_valid = False
if telegram_token:
    is_tg_ok, tg_msg, bot_user = verify_telegram_bot_token(telegram_token)
    if is_tg_ok:
        st.sidebar.success(f"✅ {tg_msg}")
        bot_is_valid = True
    else:
        st.sidebar.error(f"❌ {tg_msg}")

default_tg_chat = get_secret("telegram", "chat_id")
telegram_chat_id = st.sidebar.text_input(
    "Telegram Chat ID",
    value=default_tg_chat,
    help="Tu ID numérico de Telegram (ej. 123456789 o -100123456789)"
)

if telegram_chat_id:
    if bot_is_valid:
        is_chat_ok, chat_msg = verify_telegram_chat_id(telegram_token, telegram_chat_id)
        if is_chat_ok:
            st.sidebar.success(f"✅ {chat_msg}")
        else:
            st.sidebar.error(f"❌ {chat_msg}")
    else:
        st.sidebar.warning("⚠️ Ingresa un Bot Token válido para comprobar el Chat ID")

st.sidebar.markdown("---")

# --- 4.3. Estado de TikTok ---
st.sidebar.subheader("🎵 Estado de TikTok")

if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ Conectado a TikTok")
    if st.sidebar.button("🔄 Refrescar Token"):
        new_token = renew_tiktok_token()
        if new_token:
            st.sidebar.info("Token renovado con éxito")
else:
    st.sidebar.warning("⚠️ Sin token activo")


# ==============================================================================
# 5. PANELES Y PESTAÑAS PRINCIPALES
# ==============================================================================

st.title("🎬 Manga & Story Studio: Automatización Completa")

tabs = st.tabs([
    "📖 1. Animación de Manga (PDF)", 
    "📝 2. Historias / Temas", 
    "🚀 3. TikTok Uploader"
])

# Pestaña 1: Animación de Manga
with tabs[0]:
    st.subheader("Animación Dinámica de Viñetas")
    pdf_file = st.file_uploader("Sube el PDF del Manga", type=["pdf"])
    if pdf_file:
        st.info("Archivo recibido con éxito. Listo para la segmentación y extracción de viñetas.")

# Pestaña 2: Historias / Temas
with tabs[1]:
    st.subheader("Generación de Historias y Guiones IA")
    story_theme = st.text_input("Tema de la historia, idea central o resumen:")
    if st.button("Generar Guion"):
        if not gemini_key:
            st.error("Por favor ingresa y valida tu Gemini API Key en la barra lateral.")
        else:
            st.write("Generando estructura de guion y prompts verticales...")

# Pestaña 3: TikTok Uploader
with tabs[2]:
    st.subheader("Publicación Directa en TikTok (Sandbox / Producción)")
    uploaded_video = st.file_uploader("Selecciona el video MP4 para subir", type=["mp4"])
    
    if uploaded_video:
        temp_dir = "/tmp" if os.name != "nt" else "."
        temp_video_path = os.path.join(temp_dir, uploaded_video.name)
        
        with open(temp_video_path, "wb") as f:
            f.write(uploaded_video.read())
            
        st.video(temp_video_path)
        
        if st.button("📤 Subir como Borrador a TikTok", type="primary"):
            progress_bar = st.progress(0, text="Iniciando proceso...")
            try:
                progress_bar.progress(25, text="Contactando con la API de TikTok...")
                publish_id = upload_video_to_tiktok(temp_video_path)
                progress_bar.progress(100, text="¡Completado con éxito!")
                
                st.success("🎉 ¡Video transferido exitosamente a tu cuenta de TikTok!")
                st.info(f"**Publish ID:** `{publish_id}`\n\nAbre la aplicación de TikTok en tu celular: verás una notificación en el buzón con el borrador listo para editar y publicar.")
            except Exception as err:
                st.error(f"Error durante la subida: {str(err)}")
            finally:
                if os.path.exists(temp_video_path):
                    os.remove(temp_video_path)
