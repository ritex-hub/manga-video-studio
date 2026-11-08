import os
import time
import asyncio
import requests
import numpy as np
from PIL import Image
import streamlit as st

# Importaciones condicionales para procesamiento de video y voz
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    from moviepy.editor import ImageClip, AudioFileClip
except ImportError:
    ImageClip = None
    AudioFileClip = None

st.set_page_config(page_title="Manga & Story Studio Pro", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. GESTIÓN AUTOMÁTICA DE SECRETOS
# ==============================================================================

def get_secret(section: str, key: str, default: str = "") -> str:
    try:
        val = st.secrets[section][key]
        return str(val) if val is not None else default
    except Exception:
        return default

DEFAULT_GEMINI_KEY = get_secret("gemini", "api_key", get_secret("general", "gemini_api_key"))
DEFAULT_HF_TOKEN = get_secret("huggingface", "api_token")
DEFAULT_TG_TOKEN = get_secret("telegram", "bot_token")
DEFAULT_TG_CHAT = get_secret("telegram", "chat_id")

DEFAULT_CLIENT_KEY = get_secret("tiktok", "client_key", "sbawgv6qorcmbtk9qx")
DEFAULT_CLIENT_SECRET = get_secret("tiktok", "client_secret", "0rdY9yBOfMMkmTLLwjfxcPZvWOkyX9LJ")
DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "act.cuwQeEcQPrfg91SURpGkVe2bhSRAZyyGKVbCPsai75zbKqWtjvQ6OqgiWdIh!5618.s1")
DEFAULT_REFRESH_TOKEN = get_secret("tiktok", "refresh_token", "rft.z05jQNXtC24zqLNoBEAHsuMTpLSOBDbTKj1APHeTiwvVlcyUMngx6W9iSdnR!5635.s1")

if "tiktok_access_token" not in st.session_state:
    st.session_state["tiktok_access_token"] = DEFAULT_ACCESS_TOKEN

if "tiktok_refresh_token" not in st.session_state:
    st.session_state["tiktok_refresh_token"] = DEFAULT_REFRESH_TOKEN


# ==============================================================================
# 2. VALIDACIONES DE CONEXIONES EN VIVO
# ==============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def verify_gemini_api_key(api_key: str):
    if not api_key or len(api_key.strip()) < 10:
        return False, "Clave vacía o incompleta"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key.strip()}"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return True, "API Key válida y conectada"
        data = res.json()
        return False, data.get("error", {}).get("message", "Clave no válida")
    except Exception as e:
        return False, f"Error de conexión: {str(e)}"


@st.cache_data(ttl=300, show_spinner=False)
def verify_hf_token(token: str):
    if not token or not token.startswith("hf_"):
        return False, "Token con formato inválido (debe iniciar con hf_)"
    url = "https://huggingface.co/api/whoami-v2"
    headers = {"Authorization": f"Bearer {token.strip()}"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            user = res.json().get("name", "Usuario HF")
            return True, f"Conectado a Hugging Face ({user})"
        return False, "Token inválido o sin permisos"
    except Exception as e:
        return False, f"Error de conexión: {str(e)}"


@st.cache_data(ttl=300, show_spinner=False)
def verify_telegram_bot_token(bot_token: str):
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
    if not chat_id or not str(chat_id).strip():
        return False, "Chat ID no proporcionado"
    clean_id = str(chat_id).strip()
    if not (clean_id.isdigit() or (clean_id.startswith("-") and clean_id[1:].isdigit())):
        return False, "El Chat ID debe ser numérico"
    if not bot_token:
        return False, "Se requiere Bot Token previo"
    url = f"https://api.telegram.org/bot{bot_token.strip()}/getChat?chat_id={clean_id}"
    try:
        res = requests.get(url, timeout=5)
        data = res.json()
        if res.status_code == 200 and data.get("ok"):
            chat_name = data["result"].get("first_name") or data["result"].get("title") or "Chat privado"
            return True, f"Chat verificado: {chat_name}"
        return False, data.get("description", "Chat no encontrado o bot sin iniciar")
    except Exception as e:
        return False, f"Error: {str(e)}"


def send_telegram_notification(bot_token: str, chat_id: str, message: str, video_path: str = None):
    if not bot_token or not chat_id:
        return False, "Faltan credenciales de Telegram"
    try:
        if video_path and os.path.exists(video_path):
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendVideo"
            with open(video_path, "rb") as vf:
                files = {"video": vf}
                data = {"chat_id": chat_id.strip(), "caption": message}
                res = requests.post(url, files=files, data=data, timeout=90)
        else:
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            payload = {"chat_id": chat_id.strip(), "text": message}
            res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200, res.text
    except Exception as ex:
        return False, str(ex)


# ==============================================================================
# 3. GENERACIÓN DE IMAGEN Y VOZ GRATIS (HF + EDGE-TTS)
# ==============================================================================

def generate_image_hf(prompt: str, token: str, output_path: str):
    """Genera una imagen con IA gratuita usando el endpoint de Hugging Face."""
    # Usamos Stability AI SDXL base o FLUX-schnell
    api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
    headers = {"Authorization": f"Bearer {token.strip()}"}
    payload = {
        "inputs": f"{prompt}, manhwa style, comic art, high action, cinematic lighting, 8k, vertical 9:16 composition",
        "parameters": {"negative_prompt": "blurry, low quality, distorted, extra limbs, bad anatomy"}
    }
    
    response = requests.post(api_url, headers=headers, json=payload, timeout=90)
    if response.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(response.content)
        return True, output_path
    else:
        return False, f"Error HF ({response.status_code}): {response.text}"


async def generate_speech_edge(text: str, voice: str, output_path: str):
    """Genera audio mp3 ultrarrealista sin coste con edge-tts."""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def create_vertical_video_clip(image_path: str, audio_path: str, output_video_path: str):
    """Combina la imagen 9:16 con el audio y genera el clip MP4 final."""
    target_w, target_h = 1080, 1920
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    
    scale = max(target_w / w, target_h / h)
    nw, nh = int(w * scale), int(h * scale)
    img_resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    
    # Centrar recorte vertical
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    img_cropped = img_resized.crop((left, top, left + target_w, top + target_h))
    
    audio_clip = AudioFileClip(audio_path)
    duration = audio_clip.duration + 0.5
    
    # Crear clip con movimiento sutil (zoom in)
    clip = ImageClip(np.array(img_cropped)).set_duration(duration)
    video = clip.set_audio(audio_clip)
    video.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    audio_clip.close()
    return output_video_path


# ==============================================================================
# 4. TIKTOK CONTENT POSTING API V2
# ==============================================================================

def renew_tiktok_token():
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
        return None
    except Exception:
        return None


def upload_video_to_tiktok(video_path: str):
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Archivo inexistente: {video_path}")

    file_size = os.path.getsize(video_path)
    access_token = st.session_state["tiktok_access_token"]

    def _init(token):
        url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        b = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            }
        }
        return requests.post(url, headers=h, json=b, timeout=30)

    res_init = _init(access_token)
    init_data = res_init.json()

    if init_data.get("error", {}).get("code") in ["access_token_invalid", "token_expired", "invalid_token"]:
        access_token = renew_tiktok_token()
        if not access_token:
            raise Exception("No se pudo renovar token de TikTok.")
        res_init = _init(access_token)
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
        raise Exception(f"Error subiendo bytes a TikTok: {res_upload.status_code}")

    return publish_id


# ==============================================================================
# 5. BARRA LATERAL (CONFIGURACIÓN)
# ==============================================================================

st.sidebar.title("🔑 Configuración")

# 1. Gemini
gemini_key = st.sidebar.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
if gemini_key:
    ok_gem, msg_gem = verify_gemini_api_key(gemini_key)
    if ok_gem:
        st.sidebar.success(f"✅ {msg_gem}")
    else:
        st.sidebar.error(f"❌ {msg_gem}")
else:
    st.sidebar.info("ℹ️ Ingresa tu clave de Gemini")

# 2. Hugging Face
hf_token = st.sidebar.text_input("Hugging Face Token", value=DEFAULT_HF_TOKEN, type="password", help="Para generar imágenes gratis")
hf_is_valid = False
if hf_token:
    ok_hf, msg_hf = verify_hf_token(hf_token)
    if ok_hf:
        st.sidebar.success(f"✅ {msg_hf}")
        hf_is_valid = True
    else:
        st.sidebar.error(f"❌ {msg_hf}")
else:
    st.sidebar.info("ℹ️ Agrega tu Token de Hugging Face")

st.sidebar.markdown("---")

# 3. Telegram
telegram_token = st.sidebar.text_input("Telegram Bot Token", value=DEFAULT_TG_TOKEN, type="password")
bot_is_valid = False
if telegram_token:
    ok_tg, msg_tg, _ = verify_telegram_bot_token(telegram_token)
    if ok_tg:
        st.sidebar.success(f"✅ {msg_tg}")
        bot_is_valid = True
    else:
        st.sidebar.error(f"❌ {msg_tg}")

telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", value=DEFAULT_TG_CHAT)
chat_is_valid = False
if telegram_chat_id:
    if bot_is_valid:
        ok_chat, msg_chat = verify_telegram_chat_id(telegram_token, telegram_chat_id)
        if ok_chat:
            st.sidebar.success(f"✅ {msg_chat}")
            chat_is_valid = True
        else:
            st.sidebar.error(f"❌ {msg_chat}")
    else:
        st.sidebar.warning("⚠️ Primero ingresa un Bot Token válido")

st.sidebar.markdown("---")

# 4. TikTok
st.sidebar.subheader("🎵 Estado de TikTok")
if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ Conectado a TikTok")
    if st.sidebar.button("🔄 Refrescar Token"):
        new_tok = renew_tiktok_token()
        if new_tok:
            st.sidebar.info("Token renovado con éxito")
else:
    st.sidebar.warning("⚠️ Sin token activo")


# ==============================================================================
# 6. PESTAÑAS PRINCIPALES DE TRABAJO
# ==============================================================================

st.title("🎬 Manga & Story Studio: Generación y Publicación Autónoma")

tabs = st.tabs([
    "🤖 1. Productor IA Autónomo (Guion + Voz + Imagen)", 
    "📖 2. Animador de Manga (PDF)", 
    "🚀 3. TikTok Uploader Directo"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: Flujo Autónomo Completo
# ------------------------------------------------------------------------------
with tabs[0]:
    st.subheader("Generador Autónomo de Video para TikTok")
    story_theme = st.text_area(
        "Idea Central o Sinopsis:",
        value="Jun-woo, un exsoldado en el Seúl ciberpunk, descubre una lista de objetivos encubiertos y escapa por los techos bajo la lluvia.",
        height=80
    )
    
    c_voice, c_btn = st.columns([2, 2])
    with c_voice:
        selected_voice = st.selectbox(
            "Voz de Narración (Gratis con Edge-TTS):",
            ["es-MX-JorgeNeural (Hombre - Suspenso/Acción)", "es-ES-AlvaroNeural (Hombre - Narrador)", "es-MX-DaliaNeural (Mujer - Rápido)", "es-ES-ElviraNeural (Mujer - Dramático)"]
        )
        voice_id = selected_voice.split(" ")[0]

    if st.button("⚡ Crear Capítulo Completo (Guion + Imagen IA + Voz + Video 9:16)", type="primary"):
        if not gemini_key:
            st.error("Se requiere la clave de Gemini en la barra lateral.")
        elif not hf_is_valid:
            st.error("Se requiere un Token válido de Hugging Face para generar la imagen gratis.")
        else:
            with st.status("Procesando creación multimedia...", expanded=True) as status:
                # 1. Gemini: Generar narración y prompt visual
                st.write("🧠 1. Gemini diseñando guion y prompt fotográfico...")
                prompt_ai = f"""
                Eres director de micro-historias de manga/webtoon para TikTok.
                Basado en esta idea: '{story_theme}'
                Devuelve EXACTAMENTE dos líneas:
                NARRACION: (Texto en español impactante para narrar en 10-12 segundos).
                PROMPT: (Prompt en inglés muy detallado para generar una imagen estilo webtoon manhwa vertical de acción).
                """
                url_gem = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key.strip()}"
                res_gem = requests.post(url_gem, headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": prompt_ai}]}]}, timeout=30)
                gem_text = res_gem.json()["candidates"][0]["content"]["parts"][0]["text"]
                
                narracion = "En las sombras de la ciudad, el protocolo cero ha comenzado."
                img_prompt = "Cyberpunk soldier jumping between neon skyscrapers in rain, manhwa artstyle"
                for line in gem_text.split("\n"):
                    if line.startswith("NARRACION:"):
                        narracion = line.replace("NARRACION:", "").strip()
                    elif line.startswith("PROMPT:"):
                        img_prompt = line.replace("PROMPT:", "").strip()

                st.info(f"🎙️ **Narración:** {narracion}")
                st.info(f"🎨 **Prompt Visual:** {img_prompt}")

                # 2. Generar Voz Gratis con Edge-TTS
                st.write("🗣️ 2. Sintetizando voz en off realista con Edge-TTS...")
                temp_audio = "/tmp/story_voice.mp3" if os.name != "nt" else "story_voice.mp3"
                asyncio.run(generate_speech_edge(narracion, voice_id, temp_audio))

                # 3. Generar Imagen Gratis con Hugging Face
                st.write("🖼️ 3. Generando arte manhwa con Hugging Face (SDXL)...")
                temp_img = "/tmp/story_img.png" if os.name != "nt" else "story_img.png"
                ok_img, msg_img = generate_image_hf(img_prompt, hf_token, temp_img)
                
                if not ok_img:
                    st.error(f"Fallo al generar imagen: {msg_img}")
                    status.update(label="Error en generación visual", state="error")
                else:
                    # 4. Ensamblar Video 9:16 con MoviePy
                    st.write("🎬 4. Renderizando video vertical 9:16 con audio sincronizado...")
                    final_video = "/tmp/tiktok_ready.mp4" if os.name != "nt" else "tiktok_ready.mp4"
                    create_vertical_video_clip(temp_img, temp_audio, final_video)
                    
                    st.session_state["latest_video_path"] = final_video
                    status.update(label="¡Video generado exitosamente!", state="complete")
                    
                    st.success("🎉 Video producido de principio a fin:")
                    st.video(final_video)
                    
                    c_up1, c_up2 = st.columns(2)
                    with c_up1:
                        if st.button("📤 Publicar Directo en Borradores de TikTok"):
                            pid = upload_video_to_tiktok(final_video)
                            st.success(f"¡Subido a TikTok! Publish ID: `{pid}`")
                            if chat_is_valid:
                                send_telegram_notification(telegram_token, telegram_chat_id, f"✅ Video subido a TikTok.\nPublish ID: {pid}", final_video)
                    with c_up2:
                        if chat_is_valid and st.button("📲 Enviar Vista Previa a Telegram"):
                            send_telegram_notification(telegram_token, telegram_chat_id, f"🎬 Previsualización de escena:\n\n{narracion}", final_video)
                            st.success("Enviado a tu Telegram.")

# ------------------------------------------------------------------------------
# PESTAÑA 2: Animación de Manga PDF
# ------------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Animación Dinámica de Viñetas desde PDF")
    pdf_file = st.file_uploader("Sube el PDF del Manga o Manhwa", type=["pdf"])
    if pdf_file and fitz:
        page_num = st.number_input("Página a animar", min_value=1, value=1)
        dur = st.slider("Duración (segundos)", 3, 12, 6)
        if st.button("Animar Página"):
            doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
            if page_num <= len(doc):
                pix = doc[page_num - 1].get_pixmap(dpi=200)
                temp_p = f"/tmp/p_{page_num}.png" if os.name != "nt" else f"p_{page_num}.png"
                pix.save(temp_p)
                
                # Barrido vertical continuo
                pil_i = Image.open(temp_p).convert("RGB")
                target_w, target_h = 1080, 1920
                scale = target_w / float(pil_i.size[0])
                nh = int(pil_i.size[1] * scale)
                pil_res = pil_i.resize((target_w, nh), Image.Resampling.LANCZOS)
                
                out_p = f"/tmp/manga_scroll_{page_num}.mp4" if os.name != "nt" else f"manga_scroll_{page_num}.mp4"
                if nh > target_h:
                    max_scroll = nh - target_h
                    clip = ImageClip(np.array(pil_res.crop((0, 0, target_w, target_h)))).set_duration(dur)
                    vid = clip.fl(lambda gf, t: np.array(pil_res.crop((0, int((t/dur)*max_scroll), target_w, int((t/dur)*max_scroll)+target_h))))
                else:
                    bg = Image.new("RGB", (target_w, target_h), (0, 0, 0))
                    bg.paste(pil_res, (0, (target_h - nh)//2))
                    vid = ImageClip(np.array(bg)).set_duration(dur)
                    
                vid.write_videofile(out_p, fps=24, codec="libx264", audio=False, verbose=False, logger=None)
                st.session_state["latest_video_path"] = out_p
                st.video(out_p)

# ------------------------------------------------------------------------------
# PESTAÑA 3: Subida Manual a TikTok
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Subida Directa a TikTok (Borradores)")
    manual_vid = st.file_uploader("Selecciona archivo MP4", type=["mp4"])
    
    path_to_send = None
    if manual_vid:
        temp_man = f"/tmp/{manual_vid.name}" if os.name != "nt" else manual_vid.name
        with open(temp_man, "wb") as f:
            f.write(manual_vid.read())
        path_to_send = temp_man
        st.video(path_to_send)
    elif "latest_video_path" in st.session_state and os.path.exists(st.session_state["latest_video_path"]):
        st.info("Usando video creado en las pestañas anteriores:")
        path_to_send = st.session_state["latest_video_path"]
        st.video(path_to_send)

    if path_to_send and st.button("Subir ahora a TikTok"):
        try:
            pub_id = upload_video_to_tiktok(path_to_send)
            st.success(f"¡Publicado en borrador con éxito! ID: `{pub_id}`")
            if chat_is_valid:
                send_telegram_notification(telegram_token, telegram_chat_id, f"Video enviado a TikTok: {pub_id}")
        except Exception as e:
            st.error(f"Error al subir: {str(e)}")
