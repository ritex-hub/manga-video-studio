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

st.set_page_config(page_title="Manga Studio AI: Serverless & Continuity", layout="wide", page_icon="🎬")

# ==============================================================================
# 1. SECRETOS Y CONFIGURACIÓN
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
DEFAULT_MODAL_URL = get_secret("modal", "endpoint_url", "")

DEFAULT_FREESOUND_KEY = get_secret("freesound", "api_key", "")
DEFAULT_TG_TOKEN = get_secret("telegram", "bot_token", "")
DEFAULT_TG_CHAT = get_secret("telegram", "chat_id", "")

DEFAULT_CLIENT_KEY = get_secret("tiktok", "client_key", "")
DEFAULT_CLIENT_SECRET = get_secret("tiktok", "client_secret", "")
DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "")
DEFAULT_REFRESH_TOKEN = get_secret("tiktok", "refresh_token", "")

NOTEBOOK_KAGGLE = get_secret("notebooks", "kaggle_url", "https://www.kaggle.com/code")
NOTEBOOK_COLAB1 = get_secret("notebooks", "colab1_url", "https://colab.research.google.com/")
NOTEBOOK_COLAB2 = get_secret("notebooks", "colab2_url", "https://colab.research.google.com/")

if "tiktok_access_token" not in st.session_state:
    st.session_state["tiktok_access_token"] = DEFAULT_ACCESS_TOKEN

CHARACTER_ANCHOR = "Jun-woo, 24yo korean male soldier, short undercut black hair, left cybernetic glowing cyan eye, high collar black tactical jacket"

# ==============================================================================
# 2. SUPABASE: CONTINUIDAD ESTRICTA Y CLUSTER AUTO-SYNC
# ==============================================================================

def sb_headers():
    return {
        "apikey": DEFAULT_SB_KEY,
        "Authorization": f"Bearer {DEFAULT_SB_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def get_dynamic_cluster_urls():
    nodes = {
        "modal": DEFAULT_MODAL_URL,
        "kaggle": get_secret("colab", "url_kaggle", ""),
        "colab1": get_secret("colab", "url_colab1", ""),
        "colab2": get_secret("colab", "url_colab2", "")
    }
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return nodes
    try:
        r = requests.get(f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/cluster_nodes?select=*", headers=sb_headers(), timeout=5)
        if r.status_code == 200:
            for row in r.json():
                name = row.get("node_name")
                url = row.get("url", "").strip()
                if name in nodes and url:
                    nodes[name] = url
    except Exception:
        pass
    return nodes

def get_series_continuity(series_name: str):
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return 1, "Inicio de la historia."
    url = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/series_progress?series_name=eq.{urllib.parse.quote(series_name)}"
    try:
        r = requests.get(url, headers=sb_headers(), timeout=6)
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            return row.get("last_chapter", 0) + 1, row.get("last_summary", "Inicio de la historia.")
    except Exception:
        pass
    return 1, "Inicio de la historia."

def update_series_progress(series_name: str, chapter_num: int, summary: str):
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return
    url = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/series_progress"
    headers = sb_headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    payload = {"series_name": series_name, "last_chapter": chapter_num, "last_summary": summary}
    try:
        requests.post(url, headers=headers, json=payload, timeout=8)
    except Exception:
        pass

def save_episode_to_supabase(series_name, chapter_num, title, desc, tags, prompt, narr, status="pending"):
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return 0
    endpoint = f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes"
    payload = {
        "series_name": series_name,
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
        if r.status_code in [200, 201] and r.json():
            return r.json()[0].get("id", 0)
    except Exception:
        pass
    return 0

def fetch_recent_episodes():
    if not DEFAULT_SB_URL or not DEFAULT_SB_KEY:
        return []
    try:
        r = requests.get(f"{DEFAULT_SB_URL.rstrip('/')}/rest/v1/manga_episodes?select=*&order=id.desc&limit=15", headers=sb_headers(), timeout=6)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def ping_url(url: str):
    if not url:
        return False
    try:
        r = requests.get(url.strip(), timeout=4)
        return r.status_code in [200, 405]
    except Exception:
        return False

def send_telegram_with_approval(bot_token: str, chat_id: str, message: str, video_path: str = None, episode_id: int = 0):
    if not bot_token or not chat_id:
        return False
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Publicar en TikTok", "callback_data": f"publish_{episode_id}"},
            {"text": "🔄 Regenerar", "callback_data": f"retry_{episode_id}"}
        ]]
    }
    try:
        if video_path and os.path.exists(video_path):
            url = f"https://api.telegram.org/bot{bot_token.strip()}/sendVideo"
            with open(video_path, "rb") as vf:
                res = requests.post(url, files={"video": vf}, data={"chat_id": chat_id.strip(), "caption": message, "reply_markup": json.dumps(keyboard)}, timeout=90)
            return res.status_code == 200
    except Exception:
        pass
    return False

def upload_video_to_tiktok(video_path: str, title: str):
    access_token = st.session_state.get("tiktok_access_token", "")
    if not access_token or not os.path.exists(video_path):
        return False, "Falta Access Token de TikTok o archivo de video."
    file_size = os.path.getsize(video_path)
    init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "post_info": {"title": title[:150], "privacy_level": "SELF_ONLY"},
        "source_info": {"source": "FILE_UPLOAD", "video_size": file_size, "chunk_size": file_size, "total_chunk_count": 1}
    }
    try:
        r_init = requests.post(init_url, headers=headers, json=payload, timeout=20)
        data = r_init.json().get("data", {})
        upload_url = data.get("upload_url")
        if not upload_url:
            return False, f"Error init TikTok: {r_init.text}"
        with open(video_path, "rb") as f:
            vid_bytes = f.read()
        up_headers = {"Content-Range": f"bytes 0-{file_size-1}/{file_size}", "Content-Type": "video/mp4"}
        r_up = requests.put(upload_url, headers=up_headers, data=vid_bytes, timeout=60)
        return r_up.status_code in [200, 201], "Subido a bandeja de TikTok."
    except Exception as e:
        return False, str(e)

# ==============================================================================
# 3. RENDERIZADO GPU EN CASCADA (MODAL -> KAGGLE -> COLAB)
# ==============================================================================

def render_motion_modal(modal_url: str, image_path: str, output_video_path: str):
    if not modal_url:
        return False
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        res = requests.post(modal_url.strip(), json={"image_base64": img_b64}, timeout=120)
        if res.status_code == 200 and "video_base64" in res.json():
            with open(output_video_path, "wb") as vf:
                vf.write(base64.b64decode(res.json()["video_base64"]))
            return True
    except Exception:
        pass
    return False

def render_motion_gradio(endpoint_url: str, image_path: str, output_video_path: str):
    if not GradioClient or not handle_file or not endpoint_url:
        return False
    try:
        client = GradioClient(endpoint_url.strip())
        result = client.predict(input_img=handle_file(image_path), api_name="/generate_motion")
        if result and os.path.exists(result):
            shutil.copy(result, output_video_path)
            return True
    except Exception:
        pass
    return False

def animate_image_cascade(image_path: str, output_video_path: str, cluster_urls: dict):
    if cluster_urls.get("modal"):
        if render_motion_modal(cluster_urls["modal"], image_path, output_video_path):
            return True, "Modal Labs (Serverless)"
    for node_key, label in [("kaggle", "Kaggle T4"), ("colab1", "Colab 1"), ("colab2", "Colab 2")]:
        url = cluster_urls.get(node_key)
        if url and ping_url(url):
            if render_motion_gradio(url, image_path, output_video_path):
                return True, label
    return False, "Zoom Cinemático (CPU)"

# ==============================================================================
# 4. GENERACIÓN VISUAL, VOZ, MÚSICA Y MONTAJE
# ==============================================================================

def generate_image_base(prompt: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{CHARACTER_ANCHOR}, {prompt}, manhwa action style, detailed comic line art, vibrant cinematic lighting, vertical 9:16 composition"
    if hf_token:
        try:
            api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token.strip()}"}
            res = requests.post(api_url, headers=headers, json={"inputs": full_prompt, "parameters": {"seed": seed}}, timeout=40)
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return True
        except Exception:
            pass
    try:
        encoded = urllib.parse.quote(full_prompt)
        r_poll = requests.get(f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={seed}&nologo=true&model=flux", timeout=40)
        if r_poll.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r_poll.content)
            return True
    except Exception:
        pass
    return False

def get_smart_bgm(mood_tags: str, output_path: str = "/tmp/bgm.mp3"):
    if not DEFAULT_FREESOUND_KEY:
        return None
    query = urllib.parse.quote(mood_tags if mood_tags else "anime action epic soundtrack")
    url = f"https://freesound.org/apiv2/search/text/?query={query}&token={DEFAULT_FREESOUND_KEY}&filter=duration:[25+TO+120]&fields=id,name,previews"
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200 and res.json().get("results"):
            preview_url = res.json()["results"][0]["previews"]["preview-hq-mp3"]
            r_audio = requests.get(preview_url, timeout=20)
            if r_audio.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(r_audio.content)
                return output_path
    except Exception:
        pass
    return None

async def synthesize_speech(text: str, voice: str, audio_path: str):
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

def overlay_subtitles_on_frame(pil_img, current_text):
    if not current_text:
        return pil_img
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 52)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), current_text, font=font)
    x = (w - (bbox[2] - bbox[0])) // 2
    y = int(h * 0.80)
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            draw.text((x + dx, y + dy), current_text, font=font, fill=(0, 0, 0))
    draw.text((x, y), current_text, font=font, fill=(255, 235, 59))
    return pil_img

def build_scene_clip(visual_path: str, audio_path: str, subtitles: list, out_mp4: str, is_video: bool):
    voice_clip = AudioFileClip(audio_path)
    dur = voice_clip.duration + 0.15
    if is_video and VideoFileClip:
        base = VideoFileClip(visual_path)
        base = base.loop(duration=dur) if base.duration < dur else base.subclip(0, dur)
        scene = base.fl(lambda gf, t: np.array(overlay_subtitles_on_frame(
            Image.fromarray(gf(t)),
            " ".join([s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]).upper()
        )))
    else:
        target_w, target_h = 1080, 1920
        b_img = Image.open(visual_path).convert("RGB")
        sc = max(target_w / b_img.width, target_h / b_img.height)
        res = b_img.resize((int(b_img.width * sc), int(b_img.height * sc)), Image.Resampling.LANCZOS)
        l, top = (res.width - target_w) // 2, (res.height - target_h) // 2
        crp = res.crop((l, top, l + target_w, top + target_h))

        def frame_zoom(t):
            zf = 1.0 + 0.05 * (t / max(dur, 1))
            zw, zh = int(target_w * zf), int(target_h * zf)
            z_img = crp.resize((zw, zh), Image.Resampling.BILINEAR)
            f_img = z_img.crop(((zw - target_w) // 2, (zh - target_h) // 2, (zw + target_w) // 2, (zh + target_h) // 2))
            active_w = " ".join([s["word"] for s in subtitles if s["start"] <= t <= s["end"] + 0.15]).upper()
            return np.array(overlay_subtitles_on_frame(f_img, active_w))

        scene = ImageClip(np.array(crp)).set_duration(dur).fl(lambda gf, t: frame_zoom(t))

    scene = scene.set_audio(voice_clip)
    scene.write_videofile(out_mp4, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return out_mp4

def analyze_manga_page_with_gemini(image_path: str, previous_page_context: str):
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = f"""
    Eres un narrador de resúmenes de Manga/Manhwa estilo Anime Recap.
    Contexto de la página anterior (no repitas esto, continúa la acción): '{previous_page_context}'.
    Lee los globos de texto de esta imagen y crea una narración fluida y atrapante en español (máximo 25 palabras).
    Responde SOLO en JSON:
    {{"narration": "Texto narrado", "music_mood": "tags en inglés"}}
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={DEFAULT_GEMINI_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}]}]}
    try:
        res = requests.post(url, json=payload, timeout=25)
        txt = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(re.search(r"\{.*\}", txt, re.DOTALL).group(0))
    except Exception:
        return {"narration": "El combate entra en su fase más crítica mientras los poderes chocan.", "music_mood": "anime dramatic battle"}

# ==============================================================================
# 5. INTERFAZ STREAMLIT
# ==============================================================================

cluster_urls = get_dynamic_cluster_urls()

st.sidebar.title("⚡ Cluster GPU & Control")
if cluster_urls.get("modal"):
    st.sidebar.success("🚀 Modal Labs: Activo (24/7)")
else:
    st.sidebar.warning("⚠️ Modal Labs: Sin URL en Secrets")

st.sidebar.markdown("---")
st.sidebar.markdown("**Respaldo GPU (Auto-Sync URLs):**")

c1, c2 = st.sidebar.columns([2, 1])
c1.markdown(f"**Kaggle:** {'🟢 Online' if ping_url(cluster_urls.get('kaggle')) else '🔴 Offline'}")
c2.link_button("Abrir", NOTEBOOK_KAGGLE)

c3, c4 = st.sidebar.columns([2, 1])
c3.markdown(f"**Colab 1:** {'🟢 Online' if ping_url(cluster_urls.get('colab1')) else '🔴 Offline'}")
c4.link_button("Abrir", NOTEBOOK_COLAB1)

c5, c6 = st.sidebar.columns([2, 1])
c5.markdown(f"**Colab 2:** {'🟢 Online' if ping_url(cluster_urls.get('colab2')) else '🔴 Offline'}")
c6.link_button("Abrir", NOTEBOOK_COLAB2)

if st.sidebar.button("🔄 Refrescar Estado Cluster"):
    st.rerun()

st.title("🎬 Manga Studio AI: Continuidad & PDF-to-Anime")

tabs = st.tabs([
    "🚀 Serie Original (Continuidad)",
    "📚 Manga PDF a Clips (1 Min)",
    "✨ Generador de Guiones IA",
    "📑 Historial en Supabase"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: SERIE ORIGINAL CON CONTINUIDAD ESTRICTA (CERO REPETICIONES)
# ------------------------------------------------------------------------------
with tabs[0]:
    series_name = st.text_input("Nombre de la Serie:", value="Jun-woo: Neo-Seúl")
    next_chap, prev_summary = get_series_continuity(series_name)

    st.subheader(f"Capítulo #{next_chap} (Continuación Automática)")
    st.info(f"📖 **Contexto del capítulo anterior (para evitar repetir):** {prev_summary}")

    premise_input = st.text_area(
        "Dirección para este capítulo:",
        value=f"Continúa la historia tras '{prev_summary}'. Jun-woo enfrenta un nuevo peligro en Neo-Seúl.",
        height=70
    )
    col_v, col_tk = st.columns(2)
    voice_sel = col_v.selectbox("Voz:", ["es-MX-JorgeNeural", "es-ES-AlvaroNeural"])
    auto_tiktok = col_tk.checkbox("Subir automáticamente a bandeja de TikTok", value=False)

    if st.button(f"🔥 Generar Capítulo #{next_chap} y Avanzar Historia", type="primary"):
        if not DEFAULT_GEMINI_KEY:
            st.error("Falta Gemini API Key.")
        else:
            with st.status(f"Produciendo Capítulo #{next_chap}...", expanded=True) as status:
                gem_prompt = f"""
                Eres el director de '{series_name}'. Escribe el CAPÍTULO #{next_chap} en 3 escenas consecutivas.
                LO QUE YA PASÓ ANTES (NO REPETIR): '{prev_summary}'.
                NUEVA DIRECCIÓN: '{premise_input}'.
                Responde en JSON ESTRICTO:
                {{
                  "title": "Cap. {next_chap}: Título",
                  "description": "Resumen de lo ocurrido en este capítulo para continuar en el siguiente",
                  "hashtags": "#manhwa #cyberpunk #action",
                  "mood_music": "cyberpunk dark synth fast",
                  "scenes": [
                    {{"prompt": "Scene 1 visual prompt in English", "narration": "Narración 1"}},
                    {{"prompt": "Scene 2 visual prompt in English", "narration": "Narración 2"}},
                    {{"prompt": "Scene 3 visual prompt in English", "narration": "Narración 3"}}
                  ]
                }}
                """
                url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={DEFAULT_GEMINI_KEY}"
                res_g = requests.post(url_g, json={"contents": [{"parts": [{"text": gem_prompt}]}]}, timeout=30)
                txt = res_g.json()["candidates"][0]["content"]["parts"][0]["text"]
                script_data = json.loads(re.search(r"\{.*\}", txt, re.DOTALL).group(0))

                title = script_data["title"]
                desc = script_data["description"]
                tags = script_data["hashtags"]
                mood = script_data["mood_music"]
                scenes = script_data["scenes"]

                ep_id = save_episode_to_supabase(series_name, next_chap, title, desc, tags, scenes[0]["prompt"], scenes[0]["narration"], "processing")

                rendered_files = []
                for idx, sc in enumerate(scenes):
                    st.write(f"🎬 Escena {idx+1}/3: {sc['narration']}")
                    img_f = f"/tmp/s1_{idx}.png"
                    aud_f = f"/tmp/s1_{idx}.mp3"
                    mp4_f = f"/tmp/s1_{idx}.mp4"
                    anim_f = f"/tmp/a1_{idx}.mp4"

                    generate_image_base(sc["prompt"], DEFAULT_HF_TOKEN, img_f, seed=next_chap * 10 + idx)
                    subs = asyncio.run(synthesize_speech(sc["narration"], voice_sel, aud_f))
                    ok_anim, engine = animate_image_cascade(img_f, anim_f, cluster_urls)
                    st.write(f"   ↳ Motor utilizado: **{engine}**")
                    build_scene_clip(anim_f if ok_anim else img_f, aud_f, subs, mp4_f, is_video=ok_anim)
                    rendered_files.append(mp4_f)

                clips = [VideoFileClip(p) for p in rendered_files]
                final_vid = concatenate_videoclips(clips, method="compose")
                bgm_p = get_smart_bgm(mood)
                if bgm_p and os.path.exists(bgm_p):
                    bgm = AudioFileClip(bgm_p).volumex(0.14)
                    bgm = bgm.loop(duration=final_vid.duration) if bgm.duration < final_vid.duration else bgm.subclip(0, final_vid.duration)
                    final_vid = final_vid.set_audio(CompositeAudioClip([final_vid.audio, bgm]))

                out_final = f"/tmp/cap_{next_chap}_final.mp4"
                final_vid.write_videofile(out_final, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
                for c in clips:
                    c.close()

                update_series_progress(series_name, next_chap, desc)
                send_telegram_with_approval(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT, f"🎬 **{title}**\n{desc}\n{tags}", out_final, ep_id)
                if auto_tiktok:
                    ok_tk, msg_tk = upload_video_to_tiktok(out_final, f"{title} {tags}")
                    st.write(f"📱 TikTok: {msg_tk}")

                status.update(label=f"¡Capítulo #{next_chap} listo! El siguiente será el #{next_chap + 1}.", state="complete")
                st.video(out_final)

# ------------------------------------------------------------------------------
# PESTAÑA 2: SUBIR PDF DE MANGA -> CLIPS DE 1 MINUTO CON GUION Y MÚSICA
# ------------------------------------------------------------------------------
with tabs[1]:
    st.subheader("📚 Convertir PDF de Manga en Clips de 1 Minuto")
    uploaded_pdf = st.file_uploader("Sube tu archivo Manga (.pdf)", type=["pdf"])
    manga_series = st.text_input("Nombre del Manga (para llevar control de partes):", value="Manga PDF Viral")
    next_part_start, prev_manga_ctx = get_series_continuity(manga_series)
    st.caption(f"Continuará a partir de la **Parte #{next_part_start}**.")

    if uploaded_pdf and st.button("🎬 Animar PDF y Cortar en Clips de 1 Minuto", type="primary"):
        if fitz and DEFAULT_GEMINI_KEY:
            with st.status("Procesando páginas del manga...", expanded=True) as status:
                doc = fitz.open(stream=uploaded_pdf.read(), filetype="pdf")
                page_clips = []
                running_ctx = prev_manga_ctx
                mood_bgm = "anime epic battle"

                for i in range(len(doc)):
                    st.write(f"📖 Página {i+1}/{len(doc)}...")
                    pix = doc.load_page(i).get_pixmap(dpi=150)
                    img_p = f"/tmp/pdf_p_{i}.jpg"
                    pix.save(img_p)

                    v_data = analyze_manga_page_with_gemini(img_p, running_ctx)
                    narr = v_data.get("narration", "")
                    running_ctx = narr
                    if i == 0:
                        mood_bgm = v_data.get("music_mood", mood_bgm)

                    aud_p = f"/tmp/pdf_a_{i}.mp3"
                    subs = asyncio.run(synthesize_speech(narr, "es-MX-JorgeNeural", aud_p))
                    anim_p = f"/tmp/pdf_anim_{i}.mp4"
                    ok_a, _ = animate_image_cascade(img_p, anim_p, cluster_urls)
                    clip_p = f"/tmp/pdf_c_{i}.mp4"
                    build_scene_clip(anim_p if ok_a else img_p, aud_p, subs, clip_p, is_video=ok_a)
                    c_obj = VideoFileClip(clip_p)
                    page_clips.append((clip_p, c_obj.duration))
                    c_obj.close()

                parts = []
                batch, dur_acc = [], 0.0
                for cp, d in page_clips:
                    batch.append(cp)
                    dur_acc += d
                    if dur_acc >= 55.0:
                        parts.append(batch)
                        batch, dur_acc = [], 0.0
                if batch:
                    parts.append(batch)

                bgm_f = get_smart_bgm(mood_bgm)
                current_p_num = next_part_start

                for b_files in parts:
                    s_clips = [VideoFileClip(f) for f in b_files]
                    concat = concatenate_videoclips(s_clips, method="compose")
                    if bgm_f and os.path.exists(bgm_f):
                        bgm = AudioFileClip(bgm_f).volumex(0.14)
                        bgm = bgm.loop(duration=concat.duration) if bgm.duration < concat.duration else bgm.subclip(0, concat.duration)
                        concat = concat.set_audio(CompositeAudioClip([concat.audio, bgm]))

                    final_p = f"/tmp/manga_parte_{current_p_num}.mp4"
                    concat.write_videofile(final_p, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
                    for c in s_clips:
                        c.close()

                    cap_txt = f"🔥 **{manga_series} - Parte {current_p_num}**\n¡Sigue para la siguiente parte! #manga #anime"
                    ep_id = save_episode_to_supabase(manga_series, current_p_num, f"{manga_series} Pt.{current_p_num}", running_ctx, "#manga #anime", "PDF", running_ctx, "ready")
                    update_series_progress(manga_series, current_p_num, running_ctx)
                    send_telegram_with_approval(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT, cap_txt, final_p, ep_id)
                    st.video(final_p)
                    current_p_num += 1

                status.update(label="¡Clips de 1 minuto generados y guardados en continuidad!", state="complete")

# ------------------------------------------------------------------------------
# PESTAÑA 3: GENERADOR DE GUIONES IA
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("✨ Generador de Ideas y Guiones por Temporada")
    idea = st.text_area("Escribe la premisa base de tu nueva historia:")
    if st.button("🧠 Crear Guion de 3 Capítulos"):
        if DEFAULT_GEMINI_KEY and idea:
            url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={DEFAULT_GEMINI_KEY}"
            res = requests.post(url_g, json={"contents": [{"parts": [{"text": f"Crea una escaleta de 3 capítulos conectados con cliffhangers basada en: {idea}"}]}]}, timeout=25)
            st.markdown(res.json()["candidates"][0]["content"]["parts"][0]["text"])

# ------------------------------------------------------------------------------
# PESTAÑA 4: HISTORIAL Y CONTINUIDAD EN SUPABASE
# ------------------------------------------------------------------------------
with tabs[3]:
    st.subheader("📑 Registro de Capítulos Producidos")
    recs = fetch_recent_episodes()
    if recs:
        st.dataframe(recs, use_container_width=True)
