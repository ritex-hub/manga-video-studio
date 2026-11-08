import os
import time
import requests
import numpy as np
from PIL import Image
import streamlit as st

# Importaciones condicionales para procesamiento multimedia
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from moviepy.editor import ImageClip
except ImportError:
    ImageClip = None

st.set_page_config(page_title="Manga & Story Studio", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. GESTIÓN AUTOMÁTICA DE SECRETOS Y CONFIGURACIÓN
# ==============================================================================

def get_secret(section: str, key: str, default: str = "") -> str:
    """Recupera variables desde st.secrets de manera segura sin interrumpir la app."""
    try:
        val = st.secrets[section][key]
        return str(val) if val is not None else default
    except Exception:
        return default

# Carga de credenciales por defecto desde Secrets
DEFAULT_GEMINI_KEY = get_secret("gemini", "api_key", get_secret("general", "gemini_api_key"))
DEFAULT_TG_TOKEN = get_secret("telegram", "bot_token")
DEFAULT_TG_CHAT = get_secret("telegram", "chat_id")

DEFAULT_CLIENT_KEY = get_secret("tiktok", "client_key", "sbawgv6qorcmbtk9qx")
DEFAULT_CLIENT_SECRET = get_secret("tiktok", "client_secret", "0rdY9yBOfMMkmTLLwjfxcPZvWOkyX9LJ")
DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "act.cuwQeEcQPrfg91SURpGkVe2bhSRAZyyGKVbCPsai75zbKqWtjvQ6OqgiWdIh!5618.s1")
DEFAULT_REFRESH_TOKEN = get_secret("tiktok", "refresh_token", "rft.z05jQNXtC24zqLNoBEAHsuMTpLSOBDbTKj1APHeTiwvVlcyUMngx6W9iSdnR!5635.s1")

# Inicialización de estado en sesión para TikTok
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


def verify_telegram_chat_id(bot_token: str, chat_id: str):
    """Valida si el bot de Telegram tiene acceso al chat del usuario."""
    if not chat_id or not str(chat_id).strip():
        return False, "Chat ID no proporcionado"
    clean_id = str(chat_id).strip()
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
        return False, f"Error de conexión: {str(e)}"


def send_telegram_notification(bot_token: str, chat_id: str, message: str, video_path: str = None):
    """Envía un mensaje o video de confirmación a Telegram."""
    if not bot_token or not chat_id:
        return False, "Faltan credenciales de Telegram"
    try:
        if video_path and os.path.exists(video_path):
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendVideo"
            with open(video_path, "rb") as vf:
                files = {"video": vf}
                data = {"chat_id": chat_id.strip(), "caption": message}
                res = requests.post(url, files=files, data=data, timeout=60)
        else:
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            payload = {"chat_id": chat_id.strip(), "text": message}
            res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200, res.text
    except Exception as ex:
        return False, str(ex)


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
    """Sube un video MP4 a TikTok v2 directo a borradores/inbox."""
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

    res_init = _initiate_post(access_token)
    init_data = res_init.json()

    if init_data.get("error", {}).get("code") in ["access_token_invalid", "token_expired", "invalid_token"]:
        st.warning("El token de TikTok ha expirado. Renovando automáticamente...")
        access_token = renew_tiktok_token()
        if not access_token:
            raise Exception("No se pudo renovar el token de TikTok.")
        res_init = _initiate_post(access_token)
        init_data = res_init.json()

    if init_data.get("error", {}).get("code") != "ok":
        raise Exception(f"Fallo en TikTok API: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    upload_headers = {
        "Content-Type": "video/mp4",
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
    }

    with open(video_path, "rb") as vf:
        res_upload = requests.put(upload_url, headers=upload_headers, data=vf, timeout=120)

    if res_upload.status_code not in (200, 201, 206):
        raise Exception(f"Error subiendo bytes a TikTok: {res_upload.status_code} - {res_upload.text}")

    return publish_id


# ==============================================================================
# 4. BARRA LATERAL (CONFIGURACIÓN EN VIVO)
# ==============================================================================

st.sidebar.title("🔑 Configuración")

# --- 4.1. Gemini API Key ---
gemini_key = st.sidebar.text_input(
    "Gemini API Key",
    value=DEFAULT_GEMINI_KEY,
    type="password",
    help="Configurada en secrets o desde Google AI Studio"
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
telegram_token = st.sidebar.text_input(
    "Telegram Bot Token",
    value=DEFAULT_TG_TOKEN,
    type="password",
    help="Token configurado en secrets o proporcionado por @BotFather"
)

bot_is_valid = False
if telegram_token:
    is_tg_ok, tg_msg, bot_user = verify_telegram_bot_token(telegram_token)
    if is_tg_ok:
        st.sidebar.success(f"✅ {tg_msg}")
        bot_is_valid = True
    else:
        st.sidebar.error(f"❌ {tg_msg}")

telegram_chat_id = st.sidebar.text_input(
    "Telegram Chat ID",
    value=DEFAULT_TG_CHAT,
    help="ID numérico de Telegram (ej. 8870484184)"
)

chat_is_valid = False
if telegram_chat_id:
    if bot_is_valid:
        is_chat_ok, chat_msg = verify_telegram_chat_id(telegram_token, telegram_chat_id)
        if is_chat_ok:
            st.sidebar.success(f"✅ {chat_msg}")
            chat_is_valid = True
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
# 5. PANELES Y FLUJOS PRINCIPALES
# ==============================================================================

st.title("🎬 Manga & Story Studio: Automatización Completa")

tabs = st.tabs([
    "📖 1. Animación de Manga (PDF)", 
    "📝 2. Historias / Temas", 
    "🚀 3. TikTok Uploader"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: Animación de Manga / Manhwa PDF
# ------------------------------------------------------------------------------
with tabs[0]:
    st.subheader("Animación Dinámica de Viñetas (Formato Vertical 9:16)")
    pdf_file = st.file_uploader("Sube el PDF del Manga o Manhwa", type=["pdf"])
    
    if pdf_file:
        if fitz is None or ImageClip is None:
            st.error("Para animar viñetas asegúrate de tener instalados `pymupdf` y `moviepy` en requirements.txt.")
        else:
            st.success("Archivo PDF cargado correctamente.")
            col_a, col_b = st.columns([1, 2])
            with col_a:
                page_num = st.number_input("Número de página a procesar", min_value=1, value=1, step=1)
                duration = st.slider("Duración del video (segundos)", min_value=3, max_value=15, value=6)
                btn_animate = st.button("🎞️ Convertir Página a Video 9:16", type="primary")
                
            if btn_animate:
                with st.spinner("Extrayendo viñetas y generando barrido cinematográfico vertical..."):
                    try:
                        doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
                        if page_num > len(doc):
                            st.error(f"El PDF solo contiene {len(doc)} páginas.")
                        else:
                            page = doc[page_num - 1]
                            pix = page.get_pixmap(dpi=200)
                            img_path = f"/tmp/page_{page_num}.png" if os.name != "nt" else f"page_{page_num}.png"
                            pix.save(img_path)
                            
                            # Cargar imagen y ajustar a lienzo vertical 9:16 (1080x1920)
                            pil_img = Image.open(img_path).convert("RGB")
                            w, h = pil_img.size
                            target_w, target_h = 1080, 1920
                            
                            # Escalar proporcional al ancho vertical
                            scale = target_w / float(w)
                            new_h = int(h * scale)
                            pil_img_resized = pil_img.resize((target_w, new_h), Image.Resampling.LANCZOS)
                            
                            # Si es más alto que 1920, realizamos un barrido de arriba a abajo
                            video_output = f"/tmp/manga_anim_{page_num}.mp4" if os.name != "nt" else f"manga_anim_{page_num}.mp4"
                            
                            if new_h > target_h:
                                max_scroll = new_h - target_h
                                def make_frame(t):
                                    y_offset = int((t / duration) * max_scroll)
                                    cropped = pil_img_resized.crop((0, y_offset, target_w, y_offset + target_h))
                                    return np.array(cropped)
                                
                                clip = ImageClip(np.array(pil_img_resized.crop((0, 0, target_w, target_h)))).set_duration(duration)
                                video = clip.fl(lambda gf, t: make_frame(t))
                            else:
                                # Centrar sobre fondo negro
                                bg = Image.new("RGB", (target_w, target_h), (10, 10, 10))
                                y_pos = (target_h - new_h) // 2
                                bg.paste(pil_img_resized, (0, y_pos))
                                video = ImageClip(np.array(bg)).set_duration(duration)
                                
                            video.write_videofile(video_output, fps=24, codec="libx264", audio=False, verbose=False, logger=None)
                            
                            st.success("¡Video vertical 9:16 generado con éxito!")
                            st.video(video_output)
                            
                            # Guardar ruta en sesión para publicación directa
                            st.session_state["latest_video_path"] = video_output
                            
                            c1, c2 = st.columns(2)
                            with c1:
                                with open(video_output, "rb") as f:
                                    st.download_button("📥 Descargar Video MP4", data=f, file_name=f"manga_scene_{page_num}.mp4", mime="video/mp4")
                            with c2:
                                if chat_is_valid and st.button("📲 Enviar a mi Telegram"):
                                    ok, resp = send_telegram_notification(telegram_token, telegram_chat_id, f"🎬 Animación generada: Página {page_num}", video_output)
                                    if ok:
                                        st.success("Video enviado a tu Telegram.")
                                    else:
                                        st.error(f"Fallo al enviar a Telegram: {resp}")
                    except Exception as err:
                        st.error(f"Error procesando la animación del manga: {str(err)}")

# ------------------------------------------------------------------------------
# PESTAÑA 2: Generación de Historias y Guiones IA (Gemini)
# ------------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Generación de Historias y Guiones IA")
    story_theme = st.text_area(
        "Tema de la historia, idea central o resumen:",
        value="Género: Acción, Thriller cibernético, Suspenso urbano. Protagonista: Jun-woo, un exsoldado de operaciones encubiertas que trabaja como reparador clandestino en los bajos fondos.",
        height=100
    )
    
    col1, col2 = st.columns([1, 4])
    with col1:
        num_scenes = st.slider("Número de Capítulos / Escenas", min_value=3, max_value=10, value=10)
    
    if st.button("🚀 Generar Guion y Prompts", type="primary"):
        if not gemini_key:
            st.error("Por favor ingresa y valida tu Gemini API Key en la barra lateral.")
        else:
            with st.spinner("🤖 Gemini está redactando los 10 capítulos con prompts verticales..."):
                prompt_content = f"""
                Eres un guionista y director experto en crear historias virales de manga y webtoon de acción vertical (9:16) para TikTok.
                
                Basado en esta premisa:
                "{story_theme}"
                
                Genera una historia dividida en exactamente {num_scenes} capítulos o escenas continuas.
                Para CADA capítulo proporciona:
                1. 🎬 **Título del Capítulo y Sinopsis rápida** (2-3 líneas).
                2. 🎙️ **Voz en off / Narración** (Texto cautivador en español para narrar en 15-20 segundos).
                3. 🎨 **Prompt de Imagen/Video (en inglés)** optimizado para Midjourney/Stable Diffusion en formato vertical 9:16 con estilo de manhwa cyberpunk de alta acción.
                4. 📱 **Hook / Texto en pantalla** para retener a la audiencia en TikTok.
                """
                
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key.strip()}"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "contents": [{
                        "parts": [{"text": prompt_content}]
                    }],
                    "generationConfig": {
                        "temperature": 0.7,
                        "maxOutputTokens": 4000
                    }
                }
                
                try:
                    res = requests.post(url, headers=headers, json=payload, timeout=60)
                    data = res.json()
                    
                    if "candidates" in data and len(data["candidates"]) > 0:
                        resultado_texto = data["candidates"][0]["content"]["parts"][0]["text"]
                        st.success("✨ ¡Guion y prompts generados con éxito!")
                        st.markdown(resultado_texto)
                        
                        st.download_button(
                            label="📥 Descargar Guion Completo (TXT)",
                            data=resultado_texto,
                            file_name="guion_historia_manga.txt",
                            mime="text/plain"
                        )
                        
                        if chat_is_valid and st.button("📲 Enviar Guion a Telegram"):
                            ok, resp = send_telegram_notification(telegram_token, telegram_chat_id, f"📝 Guion Generado:\n\n{resultado_texto[:3000]}")
                            if ok:
                                st.info("Resumen del guion enviado a tu Telegram.")
                    else:
                        st.error(f"Error devuelto por Gemini: {data.get('error', {}).get('message', 'Respuesta inesperada')}")
                except Exception as ex:
                    st.error(f"Fallo al conectar con Gemini: {str(ex)}")

# ------------------------------------------------------------------------------
# PESTAÑA 3: Publicación Directa en TikTok
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Publicación Directa en TikTok (Sandbox / Producción)")
    
    # Opción de usar el video generado en la pestaña 1 o subir uno propio
    use_latest = False
    if "latest_video_path" in st.session_state and os.path.exists(st.session_state["latest_video_path"]):
        use_latest = st.checkbox("Usar el último video animado desde la Pestaña 1", value=True)
        if use_latest:
            st.video(st.session_state["latest_video_path"])
            target_upload_path = st.session_state["latest_video_path"]
            
    if not use_latest:
        uploaded_video = st.file_uploader("O selecciona un video MP4 de tu equipo", type=["mp4"])
        if uploaded_video:
            temp_dir = "/tmp" if os.name != "nt" else "."
            target_upload_path = os.path.join(temp_dir, uploaded_video.name)
            with open(target_upload_path, "wb") as f:
                f.write(uploaded_video.read())
            st.video(target_upload_path)
        else:
            target_upload_path = None
            
    if target_upload_path:
        if st.button("📤 Subir como Borrador a TikTok", type="primary"):
            progress_bar = st.progress(0, text="Iniciando subida...")
            try:
                progress_bar.progress(25, text="Contactando con la API de TikTok v2...")
                publish_id = upload_video_to_tiktok(target_upload_path)
                progress_bar.progress(100, text="¡Completado!")
                
                st.success("🎉 ¡Video transferido con éxito a tu cuenta de TikTok!")
                st.info(f"**Publish ID:** `{publish_id}`\n\nAbre la aplicación de TikTok en tu teléfono: ya tienes el borrador listo en tu bandeja de entrada.")
                
                if chat_is_valid:
                    send_telegram_notification(telegram_token, telegram_chat_id, f"✅ Video subido a TikTok exitosamente.\nPublish ID: {publish_id}")
            except Exception as err:
                st.error(f"Error durante la subida a TikTok: {str(err)}")
