import os
import re
import json
import time
import base64
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
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        CompositeAudioClip,
    )
except ImportError:
    VideoFileClip = None
    ImageClip = None

# Supabase
try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

st.set_page_config(page_title="Manga & Motion Studio Pro", layout="wide", page_icon="🎬")

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
DEFAULT_KLING_KEY = get_secret("kling", "api_key", "api-key-kling-otaEHG1Oc3dbVlCl4IWWvPdRlmW88EQ_qq_n1Lvuw1U")

DEFAULT_SB_URL = get_secret("supabase", "url", "https://xfarkrxclnhibjvbrpxt.supabase.co")
DEFAULT_SB_KEY = get_secret("supabase", "key", "sb_publishable_i3Sn59uhvXjPiA7XmzWCEg_VkR-deY1")

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

# Cliente Supabase
supabase: Client = None
if create_client and DEFAULT_SB_URL and DEFAULT_SB_KEY:
    try:
        supabase = create_client(DEFAULT_SB_URL, DEFAULT_SB_KEY)
    except Exception:
        supabase = None


# ==============================================================================
# 2. VALIDACIONES Y TELEGRAM
# ==============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def verify_gemini_api_key(api_key: str):
    if not api_key or len(api_key.strip()) < 10:
        return False, "Clave incompleta"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key.strip()}"
    try:
        res = requests.get(url, timeout=5)
        return (True, "Conectado a Gemini") if res.status_code == 200 else (False, "Clave inválida")
    except Exception as e:
        return False, f"Error: {e}"


def send_telegram_with_approval(bot_token: str, chat_id: str, message: str, video_path: str = None, episode_id: int = 0):
    if not bot_token or not chat_id:
        return False, "Faltan credenciales"
    
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Publicar en TikTok", "callback_data": f"publish_{episode_id}"},
                {"text": "🔄 Regenerar Escena", "callback_data": f"retry_{episode_id}"}
            ]
        ]
    }
    reply_markup = json.dumps(keyboard)
    
    try:
        if video_path and os.path.exists(video_path):
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendVideo"
            with open(video_path, "rb") as vf:
                files = {"video": vf}
                data = {"chat_id": chat_id.strip(), "caption": message, "reply_markup": reply_markup}
                res = requests.post(url, files=files, data=data, timeout=90)
        else:
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            payload = {"chat_id": chat_id.strip(), "text": message, "reply_markup": reply_markup}
            res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200, res.text
    except Exception as ex:
        return False, str(ex)


# ==============================================================================
# 3. SUPABASE (GESTIÓN DEL ESTADO)
# ==============================================================================

def get_current_episode_number():
    if not supabase:
        return 1
    try:
        res = supabase.table("manga_episodes").select("chapter_num").order("chapter_num", desc=True).limit(1).execute()
        if res.data and len(res.data) > 0:
            last = res.data[0]["chapter_num"]
            return 1 if last >= 10 else last + 1
        return 1
    except Exception:
        return 1


def save_episode_to_supabase(chapter_num, title, desc, tags, prompt, narr):
    if not supabase:
        return 0
    try:
        payload = {
            "chapter_num": chapter_num,
            "title": title,
            "description": desc,
            "hashtags": tags,
            "prompt": prompt,
            "narration": narr,
            "status": "pending"
        }
        res = supabase.table("manga_episodes").insert(payload).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["id"]
        return 0
    except Exception as e:
        st.error(f"Error guardando en Supabase: {e}")
        return 0


# ==============================================================================
# 4. MOTOR MULTIMEDIA (KLING AI + IMAGE + TTS + SFX + SUBS)
# ==============================================================================

CHARACTER_ANCHOR = "Jun-woo, 24yo korean male soldier, short undercut black hair, left cybernetic glowing cyan eye, high collar black tactical jacket"

def get_action_sfx():
    sfx_path = "/tmp/action_sfx.mp3" if os.name != "nt" else "action_sfx.mp3"
    if not os.path.exists(sfx_path):
        sample_url = "https://cdn.pixabay.com/download/audio/2021/08/04/audio_12b0c7443c.mp3?filename=hit-impact-metal-85341.mp3"
        try:
            r = requests.get(sample_url, timeout=10)
            if r.status_code == 200:
                with open(sfx_path, "wb") as f:
                    f.write(r.content)
            else:
                sfx_path = None
        except Exception:
            sfx_path = None
    return sfx_path


async def synthesize_speech_with_subtitles(text: str, voice: str, audio_path: str):
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


def generate_image_base(prompt: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{CHARACTER_ANCHOR}, {prompt}, manhwa action style, vibrant neon colors, detailed comic book line art, vertical 9:16 composition"
    
    if hf_token:
        try:
            api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token.strip()}"}
            payload = {
                "inputs": full_prompt,
                "parameters": {"seed": seed, "negative_prompt": "blurry, low quality, distorted, bad anatomy"}
            }
            res = requests.post(api_url, headers=headers, json=payload, timeout=40)
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return True, "Hugging Face"
        except Exception:
            pass

    # Fallback inmediato con Pollinations AI
    try:
        encoded_prompt = urllib.parse.quote(full_prompt)
        poll_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&seed={seed}&nologo=true&model=flux"
        r_poll = requests.get(poll_url, timeout=40)
        if r_poll.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r_poll.content)
            return True, "Pollinations AI (Flux)"
        return False, "Fallo en generación visual"
    except Exception as ex:
        return False, str(ex)


def generate_video_with_kling(image_path: str, prompt_text: str, output_video_path: str, duration: str = "5"):
    """Llama a la API oficial de Kling AI para generar video vertical 9:16 con movimiento real."""
    if not DEFAULT_KLING_KEY:
        return False, "Falta la API Key de Kling."

    endpoint = "https://api-singapore.klingai.com/v1/videos/image2video"
    headers = {
        "Authorization": f"Bearer {DEFAULT_KLING_KEY.strip()}",
        "Content-Type": "application/json"
    }

    with open(image_path, "rb") as f:
        b64_img = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model_name": "kling-v1",
        "image": b64_img,
        "prompt": f"{prompt_text}, dynamic combat motion, cinematic camera movement, 4k",
        "cfg_scale": 0.5,
        "mode": "std",
        "duration": duration,
        "aspect_ratio": "9:16"
    }

    try:
        res = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        data = res.json()
        if data.get("code") != 0:
            return False, f"Kling error: {data.get('message')}"
        task_id = data["data"]["task_id"]
    except Exception as e:
        return False, f"Error iniciando Kling: {e}"

    status_url = f"https://api-singapore.klingai.com/v1/videos/image2video/{task_id}"
    for _ in range(60):
        time.sleep(3)
        try:
            r_stat = requests.get(status_url, headers=headers, timeout=20)
            stat_data = r_stat.json()
            task_status = stat_data.get("data", {}).get("task_status")

            if task_status == "succeed":
                video_url = stat_data["data"]["task_result"]["videos"][0]["url"]
                r_vid = requests.get(video_url, timeout=60)
                with open(output_video_path, "wb") as f:
                    f.write(r_vid.content)
                return True, output_video_path
            elif task_status == "failed":
                return False, f"Kling falló: {stat_data.get('data', {}).get('task_status_msg')}"
        except Exception:
            continue

    return False, "Tiempo de espera agotado en Kling."


def overlay_subtitles_on_frame(pil_img, current_text):
    if not current_text:
        return pil_img
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 60)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), current_text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (w - text_w) // 2
    y = int(h * 0.75)

    stroke_w = 4
    for dx in range(-stroke_w, stroke_w + 1):
        for dy in range(-stroke_w, stroke_w + 1):
            draw.text((x + dx, y + dy), current_text, font=font, fill=(0, 0, 0))
    draw.text((x, y), current_text, font=font, fill=(255, 235, 59))
    return pil_img


def build_final_video_with_audio_and_sfx(visual_path: str, audio_path: str, subtitles: list, output_video_path: str, is_motion_video: bool = False):
    voice_clip = AudioFileClip(audio_path)
    total_duration = voice_clip.duration + 0.4

    if is_motion_video and VideoFileClip:
        # Usar video con movimiento de Kling AI
        base_clip = VideoFileClip(visual_path)
        if base_clip.duration < total_duration:
            base_clip = base_clip.loop(duration=total_duration)
        else:
            base_clip = base_clip.subclip(0, total_duration)

        def frame_generator(gf, t):
            frame = gf(t)
            pil_f = Image.fromarray(frame)
            current_words = [s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]
            active_str = " ".join(current_words).upper()
            return np.array(overlay_subtitles_on_frame(pil_f, active_str))

        video_with_fx = base_clip.fl(frame_generator)
    else:
        # Fallback a zoom dinámico sobre imagen
        target_w, target_h = 1080, 1920
        base_img = Image.open(visual_path).convert("RGB")
        w, h = base_img.size
        scale = max(target_w / w, target_h / h)
        nw, nh = int(w * scale), int(h * scale)
        resized_img = base_img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - target_w) // 2
        top = (nh - target_h) // 2
        cropped_img = resized_img.crop((left, top, left + target_w, top + target_h))

        def frame_gen_static(t):
            zoom_factor = 1.0 + 0.05 * (t / max(total_duration, 1))
            zw, zh = int(target_w * zoom_factor), int(target_h * zoom_factor)
            z_img = cropped_img.resize((zw, zh), Image.Resampling.BILINEAR)
            cx, cy = (zw - target_w) // 2, (zh - target_h) // 2
            f_img = z_img.crop((cx, cy, cx + target_w, cy + target_h))
            current_words = [s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]
            active_str = " ".join(current_words).upper()
            return np.array(overlay_subtitles_on_frame(f_img, active_str))

        clip_img = ImageClip(np.array(cropped_img)).set_duration(total_duration)
        video_with_fx = clip_img.fl(lambda gf, t: frame_gen_static(t))

    # Pistas de audio: Voz + SFX inicial
    audio_tracks = [voice_clip.volumex(1.0)]
    sfx_path = get_action_sfx()
    if sfx_path and os.path.exists(sfx_path):
        try:
            sfx = AudioFileClip(sfx_path).volumex(0.35)
            sfx = sfx.subclip(0, min(1.5, sfx.duration)).set_start(0.1)
            audio_tracks.append(sfx)
        except Exception:
            pass

    final_audio = CompositeAudioClip(audio_tracks)
    video_final = video_with_fx.set_audio(final_audio)
    video_final.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return output_video_path


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
            return data["access_token"]
    except Exception:
        pass
    return None


def upload_video_to_tiktok(video_path: str):
    if not os.path.exists(video_path):
        raise FileNotFoundError("Video inexistente.")
    file_size = os.path.getsize(video_path)
    access_token = st.session_state["tiktok_access_token"]

    def _init(token):
        url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        b = {"source_info": {"source": "FILE_UPLOAD", "video_size": file_size, "chunk_size": file_size, "total_chunk_count": 1}}
        return requests.post(url, headers=h, json=b, timeout=30)

    res_init = _init(access_token)
    init_data = res_init.json()

    if init_data.get("error", {}).get("code") in ["access_token_invalid", "token_expired", "invalid_token"]:
        access_token = renew_tiktok_token()
        res_init = _init(access_token)
        init_data = res_init.json()

    if init_data.get("error", {}).get("code") != "ok":
        raise Exception(f"Fallo TikTok API: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    headers = {"Content-Type": "video/mp4", "Content-Range": f"bytes 0-{file_size - 1}/{file_size}"}
    with open(video_path, "rb") as vf:
        requests.put(upload_url, headers=headers, data=vf, timeout=120)

    return publish_id


# ==============================================================================
# 6. SIDEBAR Y ESTADO
# ==============================================================================

st.sidebar.title("⚡ Panel de Conexiones")

# Gemini
gemini_key = st.sidebar.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
if gemini_key:
    ok_gem, msg_gem = verify_gemini_api_key(gemini_key)
    st.sidebar.success(f"✅ {msg_gem}") if ok_gem else st.sidebar.error(f"❌ {msg_gem}")

# Kling AI
st.sidebar.markdown("---")
kling_key_input = st.sidebar.text_input("Kling AI API Key", value=DEFAULT_KLING_KEY, type="password")
if kling_key_input:
    st.sidebar.success("✅ Kling AI Vinculado (Motion Video)")

# Supabase
st.sidebar.markdown("---")
if supabase:
    st.sidebar.success("✅ Supabase DB Conectada")
else:
    st.sidebar.warning("⚠️ Sin conexión a Supabase")

# Telegram & TikTok
st.sidebar.markdown("---")
telegram_token = st.sidebar.text_input("Telegram Bot Token", value=DEFAULT_TG_TOKEN, type="password")
telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", value=DEFAULT_TG_CHAT)
if telegram_token and telegram_chat_id:
    st.sidebar.success("✅ Telegram Bot Activo")

if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ TikTok Sandbox Vinculado")


# ==============================================================================
# 7. WORKFLOW PRINCIPAL
# ==============================================================================

st.title("🎬 Manga Studio: Generador con Kling AI & TikTok")

tabs = st.tabs([
    "🚀 1. Productor Autónomo (Kling Motion + Voz + Subs + TikTok)",
    "📑 2. Historial de Capítulos (Supabase)",
    "📤 3. Publicación Manual"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: Productor Autónomo
# ------------------------------------------------------------------------------
with tabs[0]:
    next_chapter = get_current_episode_number()
    st.subheader(f"Produciendo Capítulo #{next_chapter} de 10: 'Sombra de Neón'")

    premise_input = st.text_area(
        "Premisa o Evento Central:",
        value=f"Capítulo {next_chapter}: Jun-woo enfrenta a los agentes del Dragón Negro en una persecución vertiginosa bajo la lluvia.",
        height=70
    )

    col_v, col_opt = st.columns([2, 2])
    with col_v:
        selected_voice = st.selectbox("Voz de Narración:", ["es-MX-JorgeNeural (Acción)", "es-ES-AlvaroNeural (Épico)"])
        voice_id = selected_voice.split(" ")[0]
    with col_opt:
        use_kling = st.checkbox("Generar movimiento real con Kling AI", value=True)

    if st.button("🔥 Generar Episodio Completo y Publicar en Telegram", type="primary"):
        if not gemini_key:
            st.error("Requiere Gemini API Key.")
        else:
            with st.status("Ejecutando pipeline autónomo...", expanded=True) as status:
                # 1. Gemini: Guion y Metadatos
                st.write("🧠 1. Diseñando guion, título viral y hashtags...")
                prompt_gem = f"""
                Eres un director de Manhwas de acción para TikTok.
                Premisa del capítulo {next_chapter}: '{premise_input}'
                Devuelve EXACTAMENTE:
                TITULO: (Título corto con emoji para TikTok)
                DESCRIPCION: (2 líneas con suspenso/cliffhanger)
                HASHTAGS: (5 hashtags separados por espacio)
                NARRACION: (Frase corta de acción en español para narrar en 6-8 segundos)
                PROMPT: (Prompt visual en inglés de la escena)
                """
                url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key.strip()}"
                res_g = requests.post(url_g, headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": prompt_gem}]}]}, timeout=30)
                gem_text = res_g.json()["candidates"][0]["content"]["parts"][0]["text"]

                title = f"Capítulo {next_chapter}: El Escape ⚠️"
                desc = "Las sombras de Seúl cobraron vida. No hay salida."
                tags = "#manhwa #cyberpunk #action #webtoon #anime"
                narracion = "El protocolo cero ha comenzado. Ya no hay marcha atrás."
                img_prompt = "running across wet neon rooftops, dodging laser gunfire"

                for line in gem_text.split("\n"):
                    if line.startswith("TITULO:"):
                        title = line.replace("TITULO:", "").strip()
                    elif line.startswith("DESCRIPCION:"):
                        desc = line.replace("DESCRIPCION:", "").strip()
                    elif line.startswith("HASHTAGS:"):
                        tags = line.replace("HASHTAGS:", "").strip()
                    elif line.startswith("NARRACION:"):
                        narracion = line.replace("NARRACION:", "").strip()
                    elif line.startswith("PROMPT:"):
                        img_prompt = line.replace("PROMPT:", "").strip()

                # 2. Guardar en Supabase
                ep_id = save_episode_to_supabase(next_chapter, title, desc, tags, img_prompt, narracion)

                # 3. Audio + Subtítulos
                st.write("🗣️ 2. Sintetizando voz y marcas de tiempo con Edge-TTS...")
                temp_aud = "/tmp/aud.mp3" if os.name != "nt" else "aud.mp3"
                subtitles = asyncio.run(synthesize_speech_with_subtitles(narracion, voice_id, temp_aud))

                # 4. Imagen Base
                st.write("🎨 3. Generando arte base manhwa...")
                temp_img = "/tmp/img.png" if os.name != "nt" else "img.png"
                ok_img, prov = generate_image_base(img_prompt, DEFAULT_HF_TOKEN, temp_img, seed=42 + next_chapter)

                # 5. Kling AI Motion Video o Zoom
                visual_source = temp_img
                is_motion = False
                if use_kling and DEFAULT_KLING_KEY:
                    st.write("🎬 4. Generando física y animación real con Kling AI (5 segundos)...")
                    temp_kling = "/tmp/kling_motion.mp4" if os.name != "nt" else "kling_motion.mp4"
                    ok_kling, msg_kling = generate_video_with_kling(temp_img, img_prompt, temp_kling, duration="5")
                    if ok_kling:
                        visual_source = temp_kling
                        is_motion = True
                    else:
                        st.warning(f"Kling falló ({msg_kling}). Usando efecto zoom dinámico.")

                # 6. Render final (Montaje + Subs + SFX)
                st.write("🎞️ 5. Ensamblando video vertical 9:16 con subtítulos dinámicos y SFX...")
                final_out = "/tmp/ep_final.mp4" if os.name != "nt" else "ep_final.mp4"
                build_final_video_with_audio_and_sfx(visual_source, temp_aud, subtitles, final_out, is_motion_video=is_motion)

                st.session_state["latest_video_path"] = final_out
                status.update(label="¡Video final renderizado!", state="complete")

                st.success("🎉 Video completado con animación y subtítulos:")
                st.video(final_out)

                st.markdown(f"**Título:** {title}")
                st.markdown(f"**Descripción:** {desc}")
                st.markdown(f"**Hashtags:** `{tags}`")

                # 7. Telegram
                if telegram_token and telegram_chat_id:
                    tg_msg = f"🎬 **{title}**\n\n{desc}\n\n{tags}\n\n🎙️ *{narracion}*"
                    send_telegram_with_approval(telegram_token, telegram_chat_id, tg_msg, final_out, ep_id)
                    st.info("📲 Video enviado a Telegram con botones de aprobación rápida.")

# ------------------------------------------------------------------------------
# PESTAÑA 2: Historial en Supabase
# ------------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Estado de los Capítulos en Supabase")
    if supabase:
        try:
            records = supabase.table("manga_episodes").select("*").order("chapter_num", desc=False).execute()
            if records.data:
                st.dataframe(records.data, use_container_width=True)
            else:
                st.info("Sin registros previos en Supabase.")
        except Exception as err:
            st.error(f"Error consultando Supabase: {err}")
    else:
        st.warning("Configura Supabase en Secrets.")

# ------------------------------------------------------------------------------
# PESTAÑA 3: Publicación Manual
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Publicación Inmediata a TikTok Inbox")
    if "latest_video_path" in st.session_state and os.path.exists(st.session_state["latest_video_path"]):
        st.video(st.session_state["latest_video_path"])
        if st.button("📤 Subir a TikTok Inbox"):
            try:
                pid = upload_video_to_tiktok(st.session_state["latest_video_path"])
                st.success(f"¡Subido exitosamente! Publish ID: `{pid}`")
            except Exception as e:
                st.error(f"Error al subir: {e}")
    else:
        st.info("Genera un video en la Pestaña 1 primero.")
