import os
import re
import json
import asyncio
import urllib.parse
import requests
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import streamlit as st

# Dependencias multimedia
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    from moviepy.editor import (
        ImageClip,
        AudioFileClip,
        CompositeAudioClip,
        concatenate_videoclips,
    )
except ImportError:
    ImageClip = None

st.set_page_config(page_title="Manga & Story Studio Autonomous", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. GESTIÓN AUTOMÁTICA DE CREDENCIALES
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
# 2. VALIDACIONES DE CONEXIONES
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
        return False, f"Error: {str(e)}"


@st.cache_data(ttl=300, show_spinner=False)
def verify_hf_token(token: str):
    if not token or not token.startswith("hf_"):
        return False, "Formato inválido (debe iniciar con hf_)"
    url = "https://huggingface.co/api/whoami-v2"
    headers = {"Authorization": f"Bearer {token.strip()}"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            user = res.json().get("name", "Usuario HF")
            return True, f"Conectado a HF ({user})"
        return False, "Token inválido o sin permisos"
    except Exception as e:
        return False, f"Error: {str(e)}"


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
        return False, f"Error: {str(e)}", None


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
        return False, "Faltan credenciales"
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
# 3. MOTOR MULTIMEDIA GRATUITO (TTS + SUBTÍTULOS + MÚSICA + FALLBACK VISUAL)
# ==============================================================================

CHARACTER_ANCHOR = "Jun-woo, 24yo korean male soldier, short undercut black hair, left cybernetic glowing cyan eye, high collar black tactical jacket"

def ensure_background_music():
    """Descarga o prepara una pista de acción gratuita para mezclar de fondo."""
    bgm_path = "/tmp/bgm_action.mp3" if os.name != "nt" else "bgm_action.mp3"
    if not os.path.exists(bgm_path):
        sample_url = "https://cdn.pixabay.com/download/audio/2022/05/16/audio_db6591201e.mp3?filename=cyberpunk-action-111863.mp3"
        try:
            r = requests.get(sample_url, timeout=15)
            if r.status_code == 200:
                with open(bgm_path, "wb") as f:
                    f.write(r.content)
            else:
                bgm_path = None
        except Exception:
            bgm_path = None
    return bgm_path


async def synthesize_speech_with_subtitles(text: str, voice: str, audio_path: str):
    """Genera audio MP3 y extrae los timestamps de cada palabra para los subtítulos."""
    communicate = edge_tts.Communicate(text, voice)
    subtitles = []
    
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                subtitles.append({
                    "word": chunk["text"],
                    "start": chunk["offset"] / 10_000_000,
                    "end": (chunk["offset"] + chunk["duration"]) / 10_000_000
                })
    return subtitles


def generate_image_with_fallback(prompt: str, hf_token: str, output_path: str, seed: int = 42):
    """Genera la imagen en Hugging Face; si está ocupado, conmuta a Pollinations sin frenar la app."""
    full_prompt = f"{CHARACTER_ANCHOR}, {prompt}, manhwa action style, vibrant neon colors, detailed comic book line art, vertical 9:16 composition"
    
    # 1. Intentar con Hugging Face (SDXL)
    if hf_token:
        try:
            api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token.strip()}"}
            payload = {
                "inputs": full_prompt,
                "parameters": {"seed": seed, "negative_prompt": "blurry, low quality, distorted, extra limbs, bad anatomy"}
            }
            res = requests.post(api_url, headers=headers, json=payload, timeout=40)
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return True, "Hugging Face (SDXL)"
        except Exception:
            pass

    # 2. Respaldo transparente e inmediato con Pollinations.ai (100% gratuito)
    try:
        encoded_prompt = urllib.parse.quote(full_prompt)
        poll_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&seed={seed}&nologo=true&model=flux"
        r_poll = requests.get(poll_url, timeout=40)
        if r_poll.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r_poll.content)
            return True, "Pollinations AI (Flux Gratis)"
        return False, "Fallo en ambos proveedores visuales"
    except Exception as ex:
        return False, str(ex)


def overlay_subtitles_on_frame(pil_img, current_text):
    """Dibuja subtítulos estilo TikTok (amarillo con trazo negro grueso centrado)."""
    if not current_text:
        return pil_img
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
    
    # Fallback a fuente básica
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 60)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), current_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    x = (w - text_w) // 2
    y = int(h * 0.75)  # En el tercio inferior para retención TikTok

    # Borde negro
    stroke_w = 4
    for dx in range(-stroke_w, stroke_w + 1):
        for dy in range(-stroke_w, stroke_w + 1):
            draw.text((x + dx, y + dy), current_text, font=font, fill=(0, 0, 0))

    # Texto en amarillo brillante
    draw.text((x, y), current_text, font=font, fill=(255, 235, 59))
    return pil_img


def build_final_video_with_audio_and_subs(image_path: str, audio_path: str, subtitles: list, output_video_path: str):
    """Ensambla el clip 9:16 con efecto zoom, subtítulos dinámicos y mezcla de música."""
    target_w, target_h = 1080, 1920
    base_img = Image.open(image_path).convert("RGB")
    w, h = base_img.size
    
    scale = max(target_w / w, target_h / h)
    nw, nh = int(w * scale), int(h * scale)
    resized_img = base_img.resize((nw, nh), Image.Resampling.LANCZOS)
    
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    cropped_img = resized_img.crop((left, top, left + target_w, top + target_h))

    # Cargar audio de voz
    voice_clip = AudioFileClip(audio_path)
    total_duration = voice_clip.duration + 0.4

    # Generación de frames con zoom suave y subtítulos
    def frame_generator(t):
        # Zoom sutil: 1.0 a 1.05
        zoom_factor = 1.0 + 0.05 * (t / max(total_duration, 1))
        zw, zh = int(target_w * zoom_factor), int(target_h * zoom_factor)
        z_img = cropped_img.resize((zw, zh), Image.Resampling.BILINEAR)
        
        cx, cy = (zw - target_w) // 2, (zh - target_h) // 2
        f_img = z_img.crop((cx, cy, cx + target_w, cy + target_h))
        
        # Encontrar palabra activa
        current_words = []
        for s in subtitles:
            if s["start"] <= t <= s["end"] + 0.15:
                current_words.append(s["word"])
        
        active_str = " ".join(current_words).upper()
        f_img = overlay_subtitles_on_frame(f_img, active_str)
        return np.array(f_img)

    video_clip = ImageClip(np.array(cropped_img)).set_duration(total_duration)
    video_with_fx = video_clip.fl(lambda gf, t: frame_generator(t))

    # Mezcla de audio con fondo musical
    bgm_path = ensure_background_music()
    if bgm_path and os.path.exists(bgm_path):
        try:
            bgm = AudioFileClip(bgm_path).volumex(0.12)
            if bgm.duration < total_duration:
                bgm = bgm.loop(duration=total_duration)
            else:
                bgm = bgm.subclip(0, total_duration)
            final_audio = CompositeAudioClip([voice_clip.volumex(1.0), bgm])
        except Exception:
            final_audio = voice_clip
    else:
        final_audio = voice_clip

    video_final = video_with_fx.set_audio(final_audio)
    video_final.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return output_video_path


# ==============================================================================
# 4. SEGMENTACIÓN DE VIÑETAS CON OPENCV (MANGA / MANHWA PDF)
# ==============================================================================

def detect_and_crop_panels(image_path: str):
    """Detecta las viñetas rectangulares con OpenCV y las ordena verticalmente."""
    cv_img = cv2.imread(image_path)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    
    # Detección de bordes mediante umbral adaptable
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 5)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    panels = []
    img_h, img_w = gray.shape
    min_area = (img_w * img_h) * 0.05  # Viñetas que ocupen al menos 5% de la página
    
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if (w * h) > min_area and w > 200 and h > 150:
            panels.append((x, y, w, h))
            
    # Ordenar de arriba a abajo (lectura webtoon/manhwa)
    panels.sort(key=lambda b: b[1])
    
    cropped_paths = []
    pil_src = Image.open(image_path)
    
    for idx, (x, y, w, h) in enumerate(panels):
        # Margen de seguridad de 5 píxeles
        x1, y1 = max(0, x - 5), max(0, y - 5)
        x2, y2 = min(img_w, x + w + 5), min(img_h, y + h + 5)
        crop_p = pil_src.crop((x1, y1, x2, y2))
        
        c_path = f"/tmp/panel_{idx}.png" if os.name != "nt" else f"panel_{idx}.png"
        crop_p.save(c_path)
        cropped_paths.append(c_path)
        
    return cropped_paths


# ==============================================================================
# 5. TIKTOK CONTENT POSTING V2
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
        raise FileNotFoundError(f"Video no encontrado: {video_path}")

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
        raise Exception(f"Fallo TikTok API: {init_data}")

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
# 6. INTERFAZ Y BARRA LATERAL
# ==============================================================================

st.sidebar.title("⚡ Panel de Conexiones")

# Gemini
gemini_key = st.sidebar.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
if gemini_key:
    ok_gem, msg_gem = verify_gemini_api_key(gemini_key)
    if ok_gem:
        st.sidebar.success(f"✅ {msg_gem}")
    else:
        st.sidebar.error(f"❌ {msg_gem}")

# Hugging Face
hf_token = st.sidebar.text_input("Hugging Face Token", value=DEFAULT_HF_TOKEN, type="password")
hf_is_valid = False
if hf_token:
    ok_hf, msg_hf = verify_hf_token(hf_token)
    if ok_hf:
        st.sidebar.success(f"✅ {msg_hf}")
        hf_is_valid = True
    else:
        st.sidebar.error(f"❌ {msg_hf}")

st.sidebar.markdown("---")

# Telegram
telegram_token = st.sidebar.text_input("Telegram Bot Token", value=DEFAULT_TG_TOKEN, type="password")
bot_is_valid = False
if telegram_token:
    ok_tg, msg_tg, _ = verify_telegram_bot_token(telegram_token)
    if ok_tg:
        st.sidebar.success(f"✅ {msg_tg}")
        bot_is_valid = True

telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", value=DEFAULT_TG_CHAT)
chat_is_valid = False
if telegram_chat_id and bot_is_valid:
    ok_chat, msg_chat = verify_telegram_chat_id(telegram_token, telegram_chat_id)
    if ok_chat:
        st.sidebar.success(f"✅ {msg_chat}")
        chat_is_valid = True

st.sidebar.markdown("---")

# TikTok Status
st.sidebar.subheader("🎵 TikTok Status")
if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ Token Activo (Sandbox)")
    if st.sidebar.button("🔄 Renovar Token"):
        if renew_tiktok_token():
            st.sidebar.info("Renovado")


# ==============================================================================
# 7. WORKFLOWS PRINCIPALES
# ==============================================================================

st.title("🎬 Manga Studio: Pipeline Autónomo")

tabs = st.tabs([
    "🚀 1. Productor Total Autónomo (IA + Voz + Subs + BGM)", 
    "📑 2. Segmentador Inteligente de Viñetas (OpenCV)", 
    "📤 3. TikTok Direct Publisher"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: Pipeline Autónomo Completo
# ------------------------------------------------------------------------------
with tabs[0]:
    st.subheader("Generación Automática de Video Completo")
    story_theme = st.text_area(
        "Idea Central:",
        value="Jun-woo corre sobre los techos mojados de Seúl mientras drones de combate abren fuego a sus espaldas.",
        height=80
    )
    
    c_voice, c_seed = st.columns([2, 1])
    with c_voice:
        selected_voice = st.selectbox(
            "Voz de Narración:",
            ["es-MX-JorgeNeural (Acción/Suspenso)", "es-ES-AlvaroNeural (Narrador Épico)", "es-MX-DaliaNeural (Enérgica)"]
        )
        voice_id = selected_voice.split(" ")[0]
    with c_seed:
        char_seed = st.number_input("Seed de Consistencia", value=42)

    if st.button("🔥 Generar Video Completo (Guion + Visual + Voz + Subs + BGM)", type="primary"):
        if not gemini_key:
            st.error("Requiere API Key de Gemini.")
        else:
            with st.status("Ejecutando pipeline de producción...", expanded=True) as status:
                # 1. Gemini: Prompt y Narración
                st.write("🧠 1. Redactando guion y prompt coherente...")
                p_text = f"""
                Eres director de videos cortos para TikTok de acción. Premisa: '{story_theme}'
                Devuelve exactamente:
                NARRACION: (Una frase en español corta y de alto impacto para narrar en 7-9 segundos).
                PROMPT: (Prompt visual en inglés describiendo la acción exacta con estilo manhwa cyberpunk).
                """
                url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key.strip()}"
                res_g = requests.post(url_g, headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": p_text}]}]}, timeout=30)
                txt_resp = res_g.json()["candidates"][0]["content"]["parts"][0]["text"]
                
                narracion = "El protocolo cero ha comenzado. Ya no hay vuelta atrás."
                img_prompt = "running across wet rooftops, dodging laser fire from gunships"
                for l in txt_resp.split("\n"):
                    if l.startswith("NARRACION:"):
                        narracion = l.replace("NARRACION:", "").strip()
                    elif l.startswith("PROMPT:"):
                        img_prompt = l.replace("PROMPT:", "").strip()

                st.info(f"🎙️ **Voz:** {narracion}")

                # 2. Voz + Subtítulos con Edge-TTS
                st.write("🗣️ 2. Sintetizando voz y extrayendo timestamps para subtítulos...")
                temp_aud = "/tmp/voice.mp3" if os.name != "nt" else "voice.mp3"
                subtitles = asyncio.run(synthesize_speech_with_subtitles(narracion, voice_id, temp_aud))

                # 3. Imagen con Fallback
                st.write("🎨 3. Generando arte con consistencia de personaje...")
                temp_img = "/tmp/scene.png" if os.name != "nt" else "scene.png"
                ok_img, prov = generate_image_with_fallback(img_prompt, hf_token, temp_img, seed=char_seed)
                st.caption(f"Proveedor visual: {prov}")

                # 4. Render Final con Subs y BGM
                st.write("🎬 4. Ensamblando video vertical 9:16 con subtítulos dinámicos y música...")
                final_out = "/tmp/tiktok_master.mp4" if os.name != "nt" else "tiktok_master.mp4"
                build_final_video_with_audio_and_subs(temp_img, temp_aud, subtitles, final_out)

                st.session_state["latest_video_path"] = final_out
                status.update(label="¡Video terminado y listo!", state="complete")
                
                st.success("🎉 Video ensamblado con subtítulos dinámicos y mezcla de audio:")
                st.video(final_out)
                
                col_u1, col_u2 = st.columns(2)
                with col_u1:
                    if st.button("📤 Enviar a Borradores de TikTok"):
                        pid = upload_video_to_tiktok(final_out)
                        st.success(f"¡Publicado en borrador! ID: `{pid}`")
                        if chat_is_valid:
                            send_telegram_notification(telegram_token, telegram_chat_id, f"✅ Video publicado en TikTok: {pid}", final_out)
                with col_u2:
                    if chat_is_valid and st.button("📲 Recibir Video por Telegram"):
                        send_telegram_notification(telegram_token, telegram_chat_id, f"🎬 Copia de respaldo:\n\n{narracion}", final_out)
                        st.success("Enviado a tu Telegram.")

# ------------------------------------------------------------------------------
# PESTAÑA 2: Segmentador OpenCV para PDF
# ------------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Segmentación y Recorte Automático de Viñetas")
    pdf_input = st.file_uploader("Sube el PDF de Manga o Manhwa", type=["pdf"])
    
    if pdf_input and fitz:
        p_num = st.number_input("Página a segmentar", min_value=1, value=1)
        if st.button("🔍 Detectar y Cortar Viñetas con OpenCV"):
            with st.spinner("Procesando bordes y recortando recuadros individuales..."):
                doc = fitz.open(stream=pdf_input.read(), filetype="pdf")
                if p_num <= len(doc):
                    page = doc[p_num - 1]
                    pix = page.get_pixmap(dpi=200)
                    t_page = f"/tmp/doc_p{p_num}.png" if os.name != "nt" else f"doc_p{p_num}.png"
                    pix.save(t_page)
                    
                    panel_files = detect_and_crop_panels(t_page)
                    if panel_files:
                        st.success(f"¡Se detectaron y recortaron {len(panel_files)} viñetas independientes!")
                        cols = st.columns(min(len(panel_files), 4))
                        for idx, p_path in enumerate(panel_files):
                            cols[idx % 4].image(p_path, caption=f"Viñeta {idx+1}")
                    else:
                        st.warning("No se detectaron viñetas cerradas con bordes gruesos. La página puede ser continua.")

# ------------------------------------------------------------------------------
# PESTAÑA 3: Publicador Directo
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Subida Manual de Videos MP4 a TikTok")
    uploaded_manual = st.file_uploader("Archivo MP4:", type=["mp4"])
    
    target_send = None
    if uploaded_manual:
        t_man = f"/tmp/{uploaded_manual.name}" if os.name != "nt" else uploaded_manual.name
        with open(t_man, "wb") as f:
            f.write(uploaded_manual.read())
        target_send = t_man
        st.video(target_send)
    elif "latest_video_path" in st.session_state and os.path.exists(st.session_state["latest_video_path"]):
        st.info("Usando video creado recientemente:")
        target_send = st.session_state["latest_video_path"]
        st.video(target_send)

    if target_send and st.button("Publicar en TikTok Inbox"):
        try:
            pub_id = upload_video_to_tiktok(target_send)
            st.success(f"Publicado con éxito. Publish ID: `{pub_id}`")
        except Exception as e:
            st.error(f"Error al subir: {str(e)}")
