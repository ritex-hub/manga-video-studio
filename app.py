import os
import fitz  # PyMuPDF
import asyncio
import edge_tts
import subprocess
import requests
import urllib.parse
import streamlit as st
from google import genai
from PIL import Image

# Configuración del panel
st.set_page_config(page_title="Manga & Story Studio Pro", layout="wide")
st.title("🎬 Manga & Story Studio: Automatización Completa")

# Barra lateral de credenciales y control
st.sidebar.header("🔑 Configuración")
gemini_key = st.sidebar.text_input("Gemini API Key (Google AI Studio)", type="password")
telegram_token = st.sidebar.text_input("Telegram Bot Token (Opcional para VoBo)", type="password")
telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", type="text")

# Carpetas de trabajo
os.makedirs("temp_cloud", exist_ok=True)
os.makedirs("output_cloud", exist_ok=True)

# Descargar pista de música libre de fondo si no existe
MUSIC_PATH = "temp_cloud/bg_music.mp3"
if not os.path.exists(MUSIC_PATH):
    sample_music_url = "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3?filename=cinematic-atmosphere-score-2-22136.mp3"
    try:
        r = requests.get(sample_music_url, timeout=10)
        with open(MUSIC_PATH, "wb") as f:
            f.write(r.content)
    except Exception:
        pass

# --- MOTORES DE PRODUCCIÓN ---

async def tts_engine(text, output_file="temp_cloud/voice.mp3"):
    """Genera locución neuronal con Microsoft Edge TTS."""
    comm = edge_tts.Communicate(text, "es-MX-DaliaNeural", rate="+5%")
    await comm.save(output_file)

def fetch_ai_image(prompt, out_path="temp_cloud/ai_scene.png"):
    """Genera imagen temática con IA gratuita (sin clave ni costo)."""
    clean_prompt = urllib.parse.quote(f"{prompt}, high quality anime style, dramatic lighting, detailed manga art, 8k")
    url = f"https://image.pollinations.ai/prompt/{clean_prompt}?width=1080&height=1920&nologo=true"
    res = requests.get(url, timeout=30)
    with open(out_path, "wb") as f:
        f.write(res.content)
    return out_path

def create_subtitles_file(script_text, srt_path="temp_cloud/subtitles.srt"):
    """Genera archivo de subtítulos cronometrado para FFmpeg."""
    words = script_text.split()
    chunk_size = 5
    chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
    
    with open(srt_path, "w", encoding="utf-8") as f:
        t = 0
        for idx, chunk in enumerate(chunks, 1):
            start_m, start_s = divmod(t, 60)
            end_m, end_s = divmod(t + 2, 60)
            f.write(f"{idx}\n")
            f.write(f"00:{int(start_m):02d}:{int(start_s):02d},000 --> 00:{int(end_m):02d}:{int(end_s):02d},000\n")
            f.write(f"{chunk}\n\n")
            t += 2
    return srt_path

def render_pro_video(image_path, audio_path, output_video="output_cloud/final_video.mp4", srt_path=None):
    """Ensambla video vertical 9:16 con efecto zoom, música y voz."""
    # Filtro de video: movimiento 9:16 vertical
    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "zoompan=z='min(zoom+0.0018,1.25)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920"
    )
    
    # Si hay música de fondo, se mezcla con la voz al 15%
    if os.path.exists(MUSIC_PATH):
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", image_path,
            "-i", audio_path, "-i", MUSIC_PATH,
            "-filter_complex",
            f"[0:v]{video_filter}[v];"
            f"[2:a]volume=0.15[bg];"
            f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_video
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
            "-vf", video_filter,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_video
        ]
    
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def send_telegram_vobo(token, chat_id, video_path, text_caption):
    """Envía notificación directa con el video al celular."""
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path, "rb") as v:
        files = {"video": v}
        data = {"chat_id": chat_id, "caption": f"🎬 Nuevo Video Listo para VoBo:\n\n{text_caption[:200]}..."}
        r = requests.post(url, data=data, files=files)
        return r.status_code == 200

# --- PESTAÑAS DE TRABAJO ---
tab1, tab2, tab3 = st.tabs([
    "📖 1. Animación de Manga (PDF)", 
    "📝 2. Historias / Temas (Monetizable)", 
    "✨ 3. Creación Completa con Arte IA"
])

# ----------------- PESTAÑA 1: MANGA -----------------
with tab1:
    st.header("Animación Dinámica de Viñetas")
    pdf_file = st.file_uploader("Sube el PDF del Manga", type=["pdf"], key="pdf_in")
    
    if pdf_file and gemini_key:
        if st.button("🚀 Procesar Manga"):
            with st.status("Generando video...", expanded=True) as status:
                st.write("1. Extrayendo viñeta...")
                doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=150)
                img_path = "temp_cloud/manga_panel.png"
                pix.save(img_path)

                st.write("2. Analizando con Gemini para guion de alta retención...")
                client = genai.Client(api_key=gemini_key)
                sample = client.files.upload(file=img_path)
                res = client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[sample, "Crea un guion intrigante de 60 palabras para TikTok sobre esta viñeta. Empieza con un gancho explosivo. Solo texto de locución."]
                )
                script = res.text
                st.session_state["last_script"] = script
                
                st.write("3. Generando locución y música...")
                asyncio.run(tts_engine(script))
                
                st.write("4. Montando video vertical 9:16 con FFmpeg...")
                render_pro_video(img_path, "temp_cloud/voice.mp3", "output_cloud/manga_final.mp4")
                st.session_state["ready_video"] = "output_cloud/manga_final.mp4"
                status.update(label="¡Video completado!", state="complete")

# ----------------- PESTAÑA 2: TEMAS (+60s) -----------------
with tab2:
    st.header("Adaptación de Temas / Historias Largas (+1 min)")
    story_raw = st.text_area("Pega el texto, noticia o historia que quieras adaptar:", height=150)
    
    if story_raw and gemini_key:
        if st.button("🚀 Generar Video Largo"):
            with st.status("Procesando formato monetizable...", expanded=True) as status:
                st.write("1. Resumiendo a formato narrativo de 60-70 segundos...")
                client = genai.Client(api_key=gemini_key)
                prompt = (
                    f"Escribe un guion para TikTok de 120 palabras basado en este texto. "
                    f"Estructura: Gancho inicial (hook), desarrollo intrigante y remate/cliffhanger. Texto continuo sin acotaciones:\n{story_raw}"
                )
                res = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
                script = res.text
                st.session_state["last_script"] = script

                st.write("2. Creando ilustración de fondo con IA...")
                ai_img = fetch_ai_image("dark fantasy epic landscape cinematic wallpaper")

                st.write("3. Sintetizando voz neuronal...")
                asyncio.run(tts_engine(script))

                st.write("4. Renderizando video completo con música...")
                render_pro_video(ai_img, "temp_cloud/voice.mp3", "output_cloud/story_final.mp4")
                st.session_state["ready_video"] = "output_cloud/story_final.mp4"
                status.update(label="¡Video de tema completado!", state="complete")

# ----------------- PESTAÑA 3: CREACIÓN DESDE CERO -----------------
with tab3:
    st.header("Creación Total: Trama + Arte IA + Video")
    premise = st.text_input("Premisa o idea principal:", placeholder="Ej: Un samurái inmortal que olvidó su nombre")
    
    if premise and gemini_key:
        if st.button("🚀 Crear Historia y Arte"):
            with st.status("Creando historia y arte con IA...", expanded=True) as status:
                st.write("1. Redactando historia original con Gemini...")
                client = genai.Client(api_key=gemini_key)
                prompt = (
                    f"Escribe un micro-relato de suspenso de 80 palabras basado en: '{premise}'. "
                    "Empieza con un gancho que atrape en los primeros 3 segundos. Solo la narración."
                )
                res = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
                script = res.text
                st.session_state["last_script"] = script

                st.write("2. Generando arte manga original...")
                ai_img = fetch_ai_image(premise)

                st.write("3. Sintetizando locución...")
                asyncio.run(tts_engine(script))

                st.write("4. Renderizando video con banda sonora...")
                render_pro_video(ai_img, "temp_cloud/voice.mp3", "output_cloud/created_final.mp4")
                st.session_state["ready_video"] = "output_cloud/created_final.mp4"
                status.update(label="¡Producción terminada!", state="complete")

# ----------------- ÁREA DE REVISIÓN Y VoBo -----------------
if "ready_video" in st.session_state and os.path.exists(st.session_state["ready_video"]):
    st.divider()
    st.subheader("👁️ Centro de Revisión y Visto Bueno (VoBo)")
    
    c1, c2 = st.columns([1, 1])
    with c1:
        st.video(st.session_state["ready_video"])
    with c2:
        st.write("### 📜 Guion Utilizado:")
        st.info(st.session_state.get("last_script", ""))
        
        with open(st.session_state["ready_video"], "rb") as vfile:
            st.download_button("📥 Descargar MP4 para publicar", data=vfile, file_name="tiktok_video.mp4")
        
        if telegram_token and telegram_chat_id:
            if st.button("📲 Enviar a mi Telegram para VoBo Móvil"):
                ok = send_telegram_vobo(telegram_token, telegram_chat_id, st.session_state["ready_video"], st.session_state.get("last_script", ""))
                if ok:
                    st.success("¡Video enviado a tu Telegram con éxito!")
                else:
                    st.error("Error al enviar a Telegram. Revisa el Token y Chat ID.")
