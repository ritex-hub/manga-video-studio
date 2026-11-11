import os
import re
import json
import time
import base64
import asyncio
import shutil
import urllib.parse
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import streamlit as st

# Dependencias multimedia
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
        concatenate_videoclips
    )
except ImportError:
    VideoFileClip = None
    ImageClip = None
    concatenate_videoclips = None

try:
    from gradio_client import Client as GradioClient, handle_file
except ImportError:
    GradioClient = None
    handle_file = None

st.set_page_config(page_title="Manga Video Studio Pro", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. EXTRACCIÓN DINÁMICA DE SECRETOS (STREAMLIT SECRETS)
# ==============================================================================

def get_secret(section: str, key: str, default: str = "") -> str:
    try:
        val = st.secrets[section][key]
        return str(val).strip() if val is not None else default
    except Exception:
        return default

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

CHARACTER_ANCHOR = "Jun-woo, 24yo korean male soldier, short undercut black hair, left cybernetic glowing cyan eye, high collar black tactical jacket"

# ==============================================================================
# 2. CLIENTE REST SUPABASE (DIRECTO, SIN DEPENDENCIAS ROTAS)
# ==============================================================================

def sb_headers():
    return {
        "apikey": DEFAULT_SB_KEY,
        "Authorization": f"Bearer {DEFAULT_SB_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def test_supabase_connection():
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return False, "Faltan credenciales en Secrets"
    endpoint = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes?select=id&limit=1"
    try:
        r = requests.get(endpoint, headers=sb_headers(), timeout=5)
        if r.status_code in [200, 206]:
            return True, "Conectado"
        return False, f"HTTP {r.status_code}: {r.text}"
    except Exception as e:
        return False, str(e)

def get_current_episode_number():
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return 1
    endpoint = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes?select=chapter_num&order=chapter_num.desc&limit=1"
    try:
        r = requests.get(endpoint, headers=sb_headers(), timeout=6)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                last = data[0]["chapter_num"]
                return 1 if last >= 10 else last + 1
        return 1
    except Exception:
        return 1

def save_episode_to_supabase(chapter_num, title, desc, tags, prompt, narr, status="pending"):
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return 0
    endpoint = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes"
    payload = {
        "chapter_num": chapter_num,
        "title": title,
        "description": desc,
        "hashtags": tags,
        "prompt": prompt,
        "narration": narr,
        "status": status
    }
    try:
        r = requests.post(endpoint, headers=sb_headers(), json=payload, timeout=8)
        if r.status_code in [200, 201]:
            data = r.json()
            if data and len(data) > 0:
                return data[0].get("id", 0)
        return 0
    except Exception as e:
        st.error(f"Error guardando en Supabase: {e}")
        return 0

def fetch_all_episodes():
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return []
    endpoint = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes?select=*&order=id.desc&limit=10"
    try:
        r = requests.get(endpoint, headers=sb_headers(), timeout=8)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception:
        return []

# ==============================================================================
# 3. VALIDACIONES Y TELEGRAM
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
# 4. AUDIO Y BGM CONTEXTUAL INTELIGENTE (FREESOUND)
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

def get_smart_bgm(mood_tags: str, output_path: str = "/tmp/bgm.mp3"):
    if not DEFAULT_FREESOUND_KEY:
        return None
    clean_query = mood_tags if mood_tags else "cyberpunk dark synth loop"
    url = f"https://freesound.org/apiv2/search/text/?query={urllib.parse.quote(clean_query)}&token={DEFAULT_FREESOUND_KEY}&filter=duration:[15+TO+60]&fields=id,name,previews"
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
        print(f"Error descargando BGM contextual: {e}")
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
# 5. GENERACIÓN VISUAL Y CLUSTER SVD (FAILOVER)
# ==============================================================================

def generate_image_base(prompt: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{CHARACTER_ANCHOR}, {prompt}, manhwa action style, detailed comic line art, vibrant cinematic lighting, vertical 9:16 composition"
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
        return False, "Fallo visual"
    except Exception as ex:
        return False, str(ex)

def generate_video_with_svd(endpoint_url: str, image_path: str, output_video_path: str):
    if not GradioClient or not handle_file or not endpoint_url:
        return False, "Endpoint no disponible"
    try:
        client = GradioClient(endpoint_url.strip())
        result = client.predict(input_img=handle_file(image_path), api_name="/generate_motion")
        if result and os.path.exists(result):
            shutil.copy(result, output_video_path)
            return True, output_video_path
        return False, "Endpoint no entregó video"
    except Exception as e:
        return False, str(e)

def generate_video_cluster(image_path: str, output_video_path: str, preferred_node: str = "Automático (Balanceo / Cascada)"):
    endpoints = []
    if preferred_node == "Colab Cuenta 1":
        endpoints = [("Colab 1", DEFAULT_COLAB_1), ("Kaggle", DEFAULT_KAGGLE), ("Colab 2", DEFAULT_COLAB_2)]
    elif preferred_node == "Colab Cuenta 2":
        endpoints = [("Colab 2", DEFAULT_COLAB_2), ("Kaggle", DEFAULT_KAGGLE), ("Colab 1", DEFAULT_COLAB_1)]
    elif preferred_node == "Kaggle T4 x2":
        endpoints = [("Kaggle", DEFAULT_KAGGLE), ("Colab 1", DEFAULT_COLAB_1), ("Colab 2", DEFAULT_COLAB_2)]
    else:
        endpoints = [("Kaggle", DEFAULT_KAGGLE), ("Colab 1", DEFAULT_COLAB_1), ("Colab 2", DEFAULT_COLAB_2)]

    for name, url in endpoints:
        if url and ping_gradio_url(url):
            st.write(f"⚡ Procesando video en nodo: **{name}**...")
            ok, res = generate_video_with_svd(url, image_path, output_video_path)
            if ok:
                return True, name, res

    return False, "Ninguno", "Todos los nodos GPU Gradio están offline."

# ==============================================================================
# 6. MONTAJE DE ESCENAS Y CONCATENACIÓN MULTI-TOMA
# ==============================================================================

def overlay_subtitles_on_frame(pil_img, current_text):
    if not current_text:
        return pil_img
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 55)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), current_text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (w - text_w) // 2
    y = int(h * 0.78)

    stroke_w = 4
    for dx in range(-stroke_w, stroke_w + 1):
        for dy in range(-stroke_w, stroke_w + 1):
            draw.text((x + dx, y + dy), current_text, font=font, fill=(0, 0, 0))
    draw.text((x, y), current_text, font=font, fill=(255, 235, 59))
    return pil_img

def build_single_scene(visual_path: str, audio_path: str, subtitles: list, output_scene_path: str, is_motion: bool = False):
    voice_clip = AudioFileClip(audio_path)
    dur = voice_clip.duration + 0.2

    if is_motion and VideoFileClip:
        base = VideoFileClip(visual_path)
        base = base.loop(duration=dur) if base.duration < dur else base.subclip(0, dur)
        scene = base.fl(lambda gf, t: np.array(overlay_subtitles_on_frame(
            Image.fromarray(gf(t)),
            " ".join([s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]).upper()
        )))
    else:
        target_w, target_h = 1080, 1920
        b_img = Image.open(visual_path).convert("RGB")
        w, h = b_img.size
        sc = max(target_w / w, target_h / h)
        res = b_img.resize((int(w * sc), int(h * sc)), Image.Resampling.LANCZOS)
        l, top = (res.width - target_w) // 2, (res.height - target_h) // 2
        crp = res.crop((l, top, l + target_w, top + target_h))

        def frame_zoom(t):
            zf = 1.0 + 0.04 * (t / max(dur, 1))
            zw, zh = int(target_w * zf), int(target_h * zf)
            z_img = crp.resize((zw, zh), Image.Resampling.BILINEAR)
            f_img = z_img.crop(((zw - target_w) // 2, (zh - target_h) // 2, (zw + target_w) // 2, (zh + target_h) // 2))
            active_w = " ".join([s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]).upper()
            return np.array(overlay_subtitles_on_frame(f_img, active_w))

        clip_img = ImageClip(np.array(crp)).set_duration(dur)
        scene = clip_img.fl(lambda gf, t: frame_zoom(t))

    scene = scene.set_audio(voice_clip)
    scene.write_videofile(output_scene_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return output_scene_path

def assemble_multi_scene_video(scene_paths: list, mood_tags: str, final_output_path: str):
    clips = [VideoFileClip(p) for p in scene_paths]
    final_video = concatenate_videoclips(clips, method="compose")
    total_duration = final_video.duration

    tracks = [final_video.audio]

    sfx_p = get_action_sfx()
    if sfx_p and os.path.exists(sfx_p):
        try:
            sfx = AudioFileClip(sfx_p).volumex(0.3).subclip(0, min(1.2, total_duration)).set_start(0.1)
            tracks.append(sfx)
        except Exception:
            pass

    bgm_p = get_smart_bgm(mood_tags)
    if bgm_p and os.path.exists(bgm_p):
        try:
            bgm = AudioFileClip(bgm_p).volumex(0.14)
            bgm = bgm.loop(duration=total_duration) if bgm.duration < total_duration else bgm.subclip(0, total_duration)
            tracks.append(bgm)
        except Exception:
            pass

    final_audio = CompositeAudioClip(tracks)
    final_video = final_video.set_audio(final_audio)
    final_video.write_videofile(final_output_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)

    for c in clips:
        c.close()
    return final_output_path

# ==============================================================================
# 7. PUBLICACIÓN A TIKTOK INBOX (V2)
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
        raise FileNotFoundError("Video no encontrado.")
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
# 8. INTERFAZ STREAMLIT
# ==============================================================================

st.sidebar.title("⚡ Estado del Sistema")

# Estado de Gemini sin operador ternario que genere DeltaGenerator
if DEFAULT_GEMINI_KEY:
    ok_gem, msg_gem = verify_gemini_api_key(DEFAULT_GEMINI_KEY)
    if ok_gem:
        st.sidebar.success(f"✅ {msg_gem}")
    else:
        st.sidebar.error(f"❌ {msg_gem}")
else:
    st.sidebar.warning("⚠️ Falta Gemini API Key en Secrets")

st.sidebar.markdown("---")
st.sidebar.markdown("**Estado del Cluster GPU Gratuito:**")
ok_kg = ping_gradio_url(DEFAULT_KAGGLE)
ok_c1 = ping_gradio_url(DEFAULT_COLAB_1)
ok_c2 = ping_gradio_url(DEFAULT_COLAB_2)

st.sidebar.markdown(f"• **Kaggle T4 x2:** {'🟢 Online' if ok_kg else '🔴 Offline'}")
st.sidebar.markdown(f"• **Colab Cuenta 1:** {'🟢 Online' if ok_c1 else '🔴 Offline'}")
st.sidebar.markdown(f"• **Colab Cuenta 2:** {'🟢 Online' if ok_c2 else '🔴 Offline'}")

st.sidebar.markdown("---")
if DEFAULT_FREESOUND_KEY:
    st.sidebar.success("✅ Freesound BGM Vinculado")

# Verificación de conexión REST a Supabase
sb_ok, sb_msg = test_supabase_connection()
if sb_ok:
    st.sidebar.success("✅ Supabase DB Activa")
else:
    st.sidebar.warning(f"⚠️ Sin conexión a Supabase: {sb_msg}")

if DEFAULT_TG_TOKEN and DEFAULT_TG_CHAT:
    st.sidebar.success("✅ Telegram Bot Activo")

if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ TikTok Sandbox Vinculado")

st.title("🎬 Manga Studio: Multi-Escena con Triple GPU Cluster")

tabs = st.tabs(["🚀 Productor Multi-Toma", "📑 Base de Datos", "📤 Publicación TikTok"])

with tabs[0]:
    next_chapter = get_current_episode_number()
    st.subheader(f"Capítulo #{next_chapter}: Guion Estructurado en 3 Tomas")

    premise_input = st.text_area(
        "Premisa Principal:",
        value=f"Capítulo {next_chapter}: Jun-woo enfrenta a los asesinos cibernéticos en el callejón de Neo-Seúl.",
        height=70
    )

    col1, col2 = st.columns(2)
    with col1:
        voice_sel = st.selectbox("Voz de la serie:", ["es-MX-JorgeNeural", "es-ES-AlvaroNeural"])
    with col2:
        engine_mode = st.selectbox("Motor de Video:", ["Cluster GPU (Automático)", "Kaggle T4 x2", "Colab Cuenta 1", "Colab Cuenta 2", "Zoom Cinemático (CPU)"])

    if st.button("🔥 Generar Capítulo Completo", type="primary"):
        if not DEFAULT_GEMINI_KEY:
            st.error("Gemini API Key requerida en Secrets.")
        else:
            with st.status("Produciendo historia de 3 escenas...", expanded=True) as status:
                st.write("🧠 1. Desglosando guion y mood musical con Gemini...")
                gem_prompt = f"""
                Eres un director de Manhwas de acción. Genera un guion en 3 escenas secuenciales para TikTok basado en: '{premise_input}'.
                Responde en JSON ESTRICTO:
                {{
                  "title": "Título con emojis",
                  "description": "Texto suspenso",
                  "hashtags": "#manhwa #cyberpunk #action",
                  "mood_music": "cyberpunk dark synth fast",
                  "scenes": [
                    {{"prompt": "Jun-woo looking at scanner in dark neon alley", "narration": "Detecté su rastro en el sector cuatro."}},
                    {{"prompt": "Jun-woo drawing energy blade with glowing blue eye", "narration": "No vinieron a negociar."}},
                    {{"prompt": "Jun-woo surrounded by shadows looking at camera", "narration": "El verdadero juego acaba de empezar."}}
                  ]
                }}
                """
                url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={DEFAULT_GEMINI_KEY}"
                res_g = requests.post(url_g, headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": gem_prompt}]}]}, timeout=30)
                
                try:
                    txt = res_g.json()["candidates"][0]["content"]["parts"][0]["text"]
                    clean_json = re.search(r"\{.*\}", txt, re.DOTALL).group(0)
                    script_data = json.loads(clean_json)
                except Exception:
                    script_data = {
                        "title": f"Capítulo {next_chapter}: Emboscada Neón",
                        "description": "La trampa fue tendida.",
                        "hashtags": "#manhwa #cyberpunk #action",
                        "mood_music": "cyberpunk action synth",
                        "scenes": [
                            {"prompt": "Jun-woo scanning shadows", "narration": "Están cerca."},
                            {"prompt": "Jun-woo activating cyan cybernetic eye", "narration": "No pienso retroceder."},
                            {"prompt": "Jun-woo smiling ready for combat", "narration": "Que vengan todos."}
                        ]
                    }

                title = script_data.get("title")
                desc = script_data.get("description")
                tags = script_data.get("hashtags")
                mood = script_data.get("mood_music")
                scenes = script_data.get("scenes", [])

                ep_id = save_episode_to_supabase(next_chapter, title, desc, tags, scenes[0]["prompt"], scenes[0]["narration"], "processing")

                rendered_scene_files = []
                all_gpu_offline = True

                for idx, sc in enumerate(scenes):
                    st.write(f"🎬 Renderizando Escena {idx+1}/{len(scenes)}: {sc['narration']}")
                    img_f = f"/tmp/scene_{idx}.png"
                    aud_f = f"/tmp/scene_{idx}.mp3"
                    scene_v_out = f"/tmp/scene_{idx}.mp4"

                    generate_image_base(sc["prompt"], DEFAULT_HF_TOKEN, img_f, seed=42 + idx)
                    subs = asyncio.run(synthesize_speech_with_subtitles(sc["narration"], voice_sel, aud_f))

                    is_motion = False
                    anim_f = f"/tmp/anim_{idx}.mp4"

                    if engine_mode != "Zoom Cinemático (CPU)":
                        ok_v, node_used, _ = generate_video_cluster(img_f, anim_f, preferred_node=engine_mode)
                        if ok_v:
                            img_f = anim_f
                            is_motion = True
                            all_gpu_offline = False

                    build_single_scene(img_f, aud_f, subs, scene_v_out, is_motion=is_motion)
                    rendered_scene_files.append(scene_v_out)

                if engine_mode != "Zoom Cinemático (CPU)" and all_gpu_offline:
                    save_episode_to_supabase(next_chapter, title, desc, tags, scenes[0]["prompt"], scenes[0]["narration"], "queued_retry")
                    st.warning("⚠️ Nodos GPU fuera de línea. Tarea registrada en cola de reintentos.")

                st.write("🎞️ Ensamblando las 3 escenas con BGM dinámico de Freesound...")
                final_movie = f"/tmp/capitulo_{next_chapter}_final.mp4"
                assemble_multi_scene_video(rendered_scene_files, mood, final_movie)

                status.update(label="¡Capítulo Multi-Escena completado!", state="complete")
                st.session_state["latest_video_path"] = final_movie
                st.video(final_movie)

                if DEFAULT_TG_TOKEN and DEFAULT_TG_CHAT:
                    tg_msg = f"🎬 **{title}**\n\n{desc}\n\n{tags}"
                    send_telegram_with_approval(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT, tg_msg, final_movie, ep_id)
                    st.success("📲 Video enviado a Telegram con botones de publicación.")

with tabs[1]:
    st.subheader("Estado de Episodios en Supabase")
    records = fetch_all_episodes()
    if records:
        st.dataframe(records, use_container_width=True)
    else:
        st.info("Sin registros cargados aún.")

with tabs[2]:
    st.subheader("Publicación Inmediata a TikTok")
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
