import os
import re
import json
import time
import base64
import asyncio
import shutil
import urllib.parse
import requests
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import streamlit as st

# Dependencias multimedia opcionales
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

try:
    from gradio_client import Client as GradioClient, handle_file
except ImportError:
    GradioClient = None
    handle_file = None

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

st.set_page_config(page_title="Manga & Motion Studio Pro", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. LECTURA EXCLUSIVA DE SECRETOS (STREAMLIT CLOUD SECRETS)
# ==============================================================================

def get_secret(section: str, key: str, default: str = "") -> str:
    """Extrae valores de forma segura desde st.secrets sin exponer credenciales."""
    try:
        val = st.secrets[section][key]
        return str(val).strip() if val is not None else default
    except Exception:
        return default

# Extracción automática desde st.secrets
DEFAULT_GEMINI_KEY = get_secret("gemini", "api_key", "")
DEFAULT_HF_TOKEN = get_secret("huggingface", "api_token", "")

DEFAULT_SB_URL = get_secret("supabase", "url", "")
DEFAULT_SB_KEY = get_secret("supabase", "key", "")

DEFAULT_COLAB_1 = get_secret("colab", "url_colab1", "")
DEFAULT_COLAB_2 = get_secret("colab", "url_colab2", "")
DEFAULT_KAGGLE = get_secret("colab", "url_kaggle", "")

DEFAULT_KLING_KEY = get_secret("kling", "api_key", "")
DEFAULT_LUMA_KEY = get_secret("luma", "api_key", "")
DEFAULT_FREESOUND_KEY = get_secret("freesound", "api_key", "")

DEFAULT_TG_TOKEN = get_secret("telegram", "bot_token", "")
DEFAULT_TG_CHAT = get_secret("telegram", "chat_id", "")

DEFAULT_CLIENT_KEY = get_secret("tiktok", "client_key", "")
DEFAULT_CLIENT_SECRET = get_secret("tiktok", "client_secret", "")
DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "")
DEFAULT_REFRESH_TOKEN = get_secret("tiktok", "refresh_token", "")

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

CHARACTER_ANCHOR = "Jun-woo, 24yo korean male soldier, short undercut black hair, left cybernetic glowing cyan eye, high collar black tactical jacket"

# ==============================================================================
# 2. VALIDACIONES Y RED
# ==============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def verify_gemini_api_key(api_key: str):
    if not api_key or len(api_key.strip()) < 10:
        return False, "Clave incompleta o vacía"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key.strip()}"
    try:
        res = requests.get(url, timeout=5)
        return (True, "Conectado a Gemini") if res.status_code == 200 else (False, "Clave inválida")
    except Exception as e:
        return False, f"Error: {e}"

def ping_gradio_url(url: str):
    if not url:
        return False
    try:
        r = requests.get(url.strip(), timeout=4)
        return r.status_code == 200
    except Exception:
        return False

def send_telegram_with_approval(bot_token: str, chat_id: str, message: str, video_path: str = None, episode_id: int = 0):
    if not bot_token or not chat_id:
        return False, "Faltan credenciales de Telegram"
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
# 3. SUPABASE
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
# 4. AUDIO (VOZ + SFX + BGM DE FREESOUND)
# ==============================================================================

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

def get_cyberpunk_bgm(output_path: str = "/tmp/bgm.mp3"):
    if not DEFAULT_FREESOUND_KEY:
        return None
    query = "cyberpunk synth loop dark action"
    url = f"https://freesound.org/apiv2/search/text/?query={urllib.parse.quote(query)}&token={DEFAULT_FREESOUND_KEY}&filter=duration:[15+TO+60]&fields=id,name,previews"
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            data = res.json()
            results = data.get("results", [])
            if results:
                preview_url = results[0]["previews"]["preview-hq-mp3"]
                r_audio = requests.get(preview_url, timeout=20)
                if r_audio.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(r_audio.content)
                    return output_path
    except Exception as e:
        print(f"Error descargando BGM de Freesound: {e}")
    return None

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

# ==============================================================================
# 5. GENERACIÓN VISUAL Y CLUSTER GPU (FAILOVER AUTOMÁTICO)
# ==============================================================================

def generate_image_base(prompt: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{CHARACTER_ANCHOR}, {prompt}, manhwa action style, vibrant neon colors, detailed comic book line art, vertical 9:16 composition"
    if hf_token:
        try:
            api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token.strip()}"}
            payload = {"inputs": full_prompt, "parameters": {"seed": seed, "negative_prompt": "blurry, low quality, distorted, bad anatomy"}}
            res = requests.post(api_url, headers=headers, json=payload, timeout=40)
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return True, "Hugging Face"
        except Exception:
            pass

    try:
        encoded_prompt = urllib.parse.quote(full_prompt)
        poll_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&seed={seed}&nologo=true&model=flux"
        r_poll = requests.get(poll_url, timeout=40)
        if r_poll.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r_poll.content)
            return True, "Pollinations AI"
        return False, "Fallo en generación visual"
    except Exception as ex:
        return False, str(ex)

def generate_video_with_gradio_endpoint(endpoint_url: str, image_path: str, output_video_path: str):
    if not GradioClient or not handle_file:
        return False, "Requiere gradio_client"
    try:
        client = GradioClient(endpoint_url.strip())
        result = client.predict(input_img=handle_file(image_path), api_name="/generate_motion")
        if result and os.path.exists(result):
            shutil.copy(result, output_video_path)
            return True, output_video_path
        return False, "Endpoint no devolvió archivo de video."
    except Exception as e:
        return False, str(e)

def generate_video_cluster(image_path: str, output_video_path: str, preferred_node: str = "Automático (Balanceo / Cascada)"):
    endpoints = []
    if preferred_node == "Colab Cuenta 1":
        endpoints = [("Colab 1", DEFAULT_COLAB_1), ("Colab 2", DEFAULT_COLAB_2), ("Kaggle", DEFAULT_KAGGLE)]
    elif preferred_node == "Colab Cuenta 2":
        endpoints = [("Colab 2", DEFAULT_COLAB_2), ("Colab 1", DEFAULT_COLAB_1), ("Kaggle", DEFAULT_KAGGLE)]
    elif preferred_node == "Kaggle T4 x2":
        endpoints = [("Kaggle", DEFAULT_KAGGLE), ("Colab 1", DEFAULT_COLAB_1), ("Colab 2", DEFAULT_COLAB_2)]
    else:
        endpoints = [("Colab 1", DEFAULT_COLAB_1), ("Kaggle", DEFAULT_KAGGLE), ("Colab 2", DEFAULT_COLAB_2)]

    for name, url in endpoints:
        if url and ping_gradio_url(url):
            st.write(f"⚡ Procesando video en nodo: **{name}**...")
            ok, res = generate_video_with_gradio_endpoint(url, image_path, output_video_path)
            if ok:
                return True, name, res
            else:
                st.write(f"⚠️ El nodo {name} no pudo completar el frame. Pasando al siguiente...")

    return False, "Ninguno", "Ningún nodo GPU Gradio disponible en este momento."

def generate_video_with_kling(image_path: str, prompt_text: str, output_video_path: str, duration: str = "5"):
    if not DEFAULT_KLING_KEY:
        return False, "Falta API Key de Kling."
    endpoint = "https://api-singapore.klingai.com/v1/videos/image2video"
    headers = {"Authorization": f"Bearer {DEFAULT_KLING_KEY}", "Content-Type": "application/json"}
    with open(image_path, "rb") as f:
        b64_img = base64.b64encode(f.read()).decode("utf-8")
    payload = {
        "model_name": "kling-v1",
        "image": b64_img,
        "prompt": f"{prompt_text}, dynamic combat motion, cinematic camera pan, 4k",
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

def generate_video_with_luma(image_path: str, prompt_text: str, output_video_path: str):
    if not DEFAULT_LUMA_KEY:
        return False, "Falta API Key de Luma."
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64}"
    endpoint = "https://api.lumalabs.ai/dream-machine/v1/generations"
    headers = {"Authorization": f"Bearer {DEFAULT_LUMA_KEY}", "Content-Type": "application/json", "accept": "application/json"}
    payload = {
        "prompt": f"{prompt_text}, manhwa dynamic movement, cinematic lighting, vertical 9:16",
        "aspect_ratio": "9:16",
        "loop": False,
        "keyframes": {"frame0": {"type": "image", "url": data_uri}}
    }
    try:
        res = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        data = res.json()
        generation_id = data.get("id")
        if not generation_id:
            return False, f"Error Luma: {data}"
    except Exception as e:
        return False, f"Error iniciando Luma: {str(e)}"

    status_url = f"https://api.lumalabs.ai/dream-machine/v1/generations/{generation_id}"
    for _ in range(60):
        time.sleep(4)
        try:
            r_stat = requests.get(status_url, headers=headers, timeout=20)
            stat_data = r_stat.json()
            if stat_data.get("state") == "completed":
                video_url = stat_data.get("assets", {}).get("video")
                r_vid = requests.get(video_url, timeout=60)
                with open(output_video_path, "wb") as f:
                    f.write(r_vid.content)
                return True, output_video_path
            elif stat_data.get("state") == "failed":
                return False, f"Luma falló: {stat_data.get('failure_reason')}"
        except Exception:
            continue
    return False, "Tiempo de espera agotado en Luma."

# ==============================================================================
# 6. MONTAJE, SUBTÍTULOS DINÁMICOS Y MULTI-AUDIO
# ==============================================================================

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

    audio_tracks = [voice_clip.volumex(1.0)]

    sfx_path = get_action_sfx()
    if sfx_path and os.path.exists(sfx_path):
        try:
            sfx = AudioFileClip(sfx_path).volumex(0.35)
            sfx = sfx.subclip(0, min(1.5, sfx.duration)).set_start(0.1)
            audio_tracks.append(sfx)
        except Exception:
            pass

    bgm_file = "/tmp/bgm.mp3" if os.name != "nt" else "bgm.mp3"
    bgm_path = get_cyberpunk_bgm(bgm_file)
    if bgm_path and os.path.exists(bgm_path):
        try:
            bgm = AudioFileClip(bgm_path).volumex(0.15)
            if bgm.duration < total_duration:
                bgm = bgm.loop(duration=total_duration)
            else:
                bgm = bgm.subclip(0, total_duration)
            audio_tracks.append(bgm)
        except Exception:
            pass

    final_audio = CompositeAudioClip(audio_tracks)
    video_final = video_with_fx.set_audio(final_audio)
    video_final.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return output_video_path

# ==============================================================================
# 7. PUBLICACIÓN AUTOMÁTICA EN TIKTOK
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
# 8. INTERFAZ Y FLUJO DE STREAMLIT
# ==============================================================================

st.sidebar.title("⚡ Estado del Sistema")

gemini_key = st.sidebar.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
if gemini_key:
    ok_gem, msg_gem = verify_gemini_api_key(gemini_key)
    st.sidebar.success(f"✅ {msg_gem}") if ok_gem else st.sidebar.error(f"❌ {msg_gem}")

st.sidebar.markdown("---")
st.sidebar.markdown("**Estado del Cluster GPU Gratuito:**")
ok_c1 = ping_gradio_url(DEFAULT_COLAB_1)
ok_c2 = ping_gradio_url(DEFAULT_COLAB_2)
ok_kg = ping_gradio_url(DEFAULT_KAGGLE)

st.sidebar.markdown(f"• **Colab Cuenta 1:** {'🟢 Online' if ok_c1 else '🔴 Offline'}")
st.sidebar.markdown(f"• **Colab Cuenta 2:** {'🟢 Online' if ok_c2 else '🔴 Offline'}")
st.sidebar.markdown(f"• **Kaggle T4 x2:** {'🟢 Online' if ok_kg else '🔴 Offline'}")

st.sidebar.markdown("---")
if DEFAULT_KLING_KEY:
    st.sidebar.success("✅ Kling AI API Vinculado")
if DEFAULT_LUMA_KEY:
    st.sidebar.success("✅ Luma Dream Machine Vinculado")
if DEFAULT_FREESOUND_KEY:
    st.sidebar.success("✅ Freesound BGM Vinculado")

st.sidebar.markdown("---")
if supabase:
    st.sidebar.success("✅ Supabase DB Conectada")
else:
    st.sidebar.warning("⚠️ Sin conexión a Supabase")

telegram_token = st.sidebar.text_input("Telegram Bot Token", value=DEFAULT_TG_TOKEN, type="password")
telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", value=DEFAULT_TG_CHAT)
if telegram_token and telegram_chat_id:
    st.sidebar.success("✅ Telegram Bot Activo")

if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ TikTok Sandbox Vinculado")

st.title("🎬 Manga Studio: Generador Autónomo Multi-Cluster")

tabs = st.tabs([
    "🚀 1. Productor Autónomo (Video + Voz + BGM + TikTok)",
    "📑 2. Historial de Capítulos (Supabase)",
    "📤 3. Publicación Manual"
])

with tabs[0]:
    next_chapter = get_current_episode_number()
    st.subheader(f"Produciendo Capítulo #{next_chapter} de 10: 'Sombra de Neón'")

    premise_input = st.text_area(
        "Premisa o Evento Central:",
        value=f"Capítulo {next_chapter}: Jun-woo desata la sobrecarga de su implante ocular para repeler el ataque del Dragón Negro.",
        height=70
    )

    col_v, col_engine = st.columns([2, 2])
    with col_v:
        selected_voice = st.selectbox("Voz de Narración:", ["es-MX-JorgeNeural (Acción)", "es-ES-AlvaroNeural (Épico)"])
        voice_id = selected_voice.split(" ")[0]
    with col_engine:
        video_engine = st.selectbox(
            "Estrategia de Generación de Video:",
            [
                "Cluster GPU (Automático / Cascada)",
                "Colab Cuenta 1",
                "Colab Cuenta 2",
                "Kaggle T4 x2",
                "Kling AI API",
                "Luma Dream Machine API",
                "Zoom Dinámico (CPU Local)"
            ]
        )

    if st.button("🔥 Generar Episodio Completo y Notificar a Telegram", type="primary"):
        if not gemini_key:
            st.error("Requiere Gemini API Key.")
        else:
            with st.status("Ejecutando pipeline de producción...", expanded=True) as status:
                st.write("🧠 1. Creando guion, título y prompt visual con Gemini...")
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

                title = f"Capítulo {next_chapter}: Protocolo Neón ⚠️"
                desc = "Las frecuencias oscuras no tienen retorno."
                tags = "#manhwa #cyberpunk #action #webtoon #anime"
                narracion = "El protocolo cero ha comenzado. Ya no hay marcha atrás."
                img_prompt = "charging glowing cyan cybernetic eye, dynamic martial arts defense under neon rain"

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

                ep_id = save_episode_to_supabase(next_chapter, title, desc, tags, img_prompt, narracion)

                st.write("🗣️ 2. Sintetizando voz y marcas temporales con Edge-TTS...")
                temp_aud = "/tmp/aud.mp3" if os.name != "nt" else "aud.mp3"
                subtitles = asyncio.run(synthesize_speech_with_subtitles(narracion, voice_id, temp_aud))

                st.write("🎨 3. Generando imagen base en estilo manhwa 9:16...")
                temp_img = "/tmp/img.png" if os.name != "nt" else "img.png"
                ok_img, prov = generate_image_base(img_prompt, DEFAULT_HF_TOKEN, temp_img, seed=42 + next_chapter)

                st.write("⚡ 4. Animando escena mediante la estrategia seleccionada...")
                visual_source = temp_img
                is_motion = False
                temp_motion_out = "/tmp/cluster_motion.mp4" if os.name != "nt" else "cluster_motion.mp4"

                if "Cluster" in video_engine or "Colab" in video_engine or "Kaggle" in video_engine:
                    ok_gpu, node_used, msg_gpu = generate_video_cluster(temp_img, temp_motion_out, preferred_node=video_engine)
                    if ok_gpu:
                        visual_source = temp_motion_out
                        is_motion = True
                        st.write(f"✅ Video generado en nodo: **{node_used}**")
                    else:
                        st.warning(f"{msg_gpu}. Conmutando a respaldo...")
                        if DEFAULT_KLING_KEY:
                            ok_kling, msg_kling = generate_video_with_kling(temp_img, img_prompt, temp_motion_out)
                            if ok_kling:
                                visual_source = temp_motion_out
                                is_motion = True
                            else:
                                st.warning("Kling falló. Usando zoom cinemático por software.")
                        else:
                            st.warning("Sin respaldo disponible. Usando zoom cinemático por software.")

                elif video_engine == "Kling AI API" and DEFAULT_KLING_KEY:
                    ok_kling, msg_kling = generate_video_with_kling(temp_img, img_prompt, temp_motion_out)
                    if ok_kling:
                        visual_source = temp_motion_out
                        is_motion = True
                    else:
                        st.warning(f"Kling falló ({msg_kling}). Usando zoom por software.")

                elif video_engine == "Luma Dream Machine API" and DEFAULT_LUMA_KEY:
                    ok_luma, msg_luma = generate_video_with_luma(temp_img, img_prompt, temp_motion_out)
                    if ok_luma:
                        visual_source = temp_motion_out
                        is_motion = True
                    else:
                        st.warning(f"Luma falló ({msg_luma}). Usando zoom por software.")

                st.write("🎞️ 5. Ensamblando video vertical, subtítulos dinámicos, BGM de Freesound y SFX...")
                final_out = "/tmp/ep_final.mp4" if os.name != "nt" else "ep_final.mp4"
                build_final_video_with_audio_and_sfx(visual_source, temp_aud, subtitles, final_out, is_motion_video=is_motion)

                st.session_state["latest_video_path"] = final_out
                status.update(label="¡Video final renderizado con éxito!", state="complete")

                st.success("🎉 Episodio completado:")
                st.video(final_out)

                st.markdown(f"**Título:** {title}")
                st.markdown(f"**Descripción:** {desc}")
                st.markdown(f"**Hashtags:** `{tags}`")

                if telegram_token and telegram_chat_id:
                    tg_msg = f"🎬 **{title}**\n\n{desc}\n\n{tags}\n\n🎙️ *{narracion}*"
                    send_telegram_with_approval(telegram_token, telegram_chat_id, tg_msg, final_out, ep_id)
                    st.info("📲 Video enviado a tu chat de Telegram con botones de aprobación rápida.")

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
