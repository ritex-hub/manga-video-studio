import os
import fitz  # PyMuPDF
import asyncio
import edge_tts
import subprocess
import streamlit as st
from google import genai
from PIL import Image

st.set_page_config(page_title="Manga & Story Studio Cloud", layout="wide")
st.title("🎬 Multi-Studio Cloud: Manga & Story Animator")

st.sidebar.header("Configuración")
gemini_key = st.sidebar.text_input("Gemini API Key (Google AI Studio)", type="password")

os.makedirs("temp_cloud", exist_ok=True)
os.makedirs("output_cloud", exist_ok=True)

async def tts_engine(text, output_file="temp_cloud/voice.mp3"):
    comm = edge_tts.Communicate(text, "es-MX-DaliaNeural")
    await comm.save(output_file)

def render_motion(image_path, audio_path, output_video="output_cloud/final.mp4"):
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
               "zoompan=z='min(zoom+0.002,1.25)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_video
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

tab1, tab2, tab3 = st.tabs([
    "📖 1. Animación de Manga (PDF)", 
    "📝 2. Animación de Historias / Temas", 
    "✨ 3. Creación de Historias + Animación"
])

# --- PESTAÑA 1 ---
with tab1:
    st.header("Animación de Manga (PDF)")
    pdf_file = st.file_uploader("Sube el PDF del Manga", type=["pdf"], key="pdf_manga")
    if pdf_file and gemini_key:
        if st.button("Procesar Manga a Video"):
            with st.spinner("Extrayendo viñeta y analizando con IA..."):
                doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=150)
                img_path = "temp_cloud/manga_p1.png"
                pix.save(img_path)

                client = genai.Client(api_key=gemini_key)
                sample = client.files.upload(file=img_path)
                res = client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[sample, "Narra esta escena para un video corto e intrigante. Máximo 40 palabras. Devuelve solo la narración."]
                )
                script = res.text
                st.write("**Guion generado:**", script)
                asyncio.run(tts_engine(script))
                render_motion(img_path, "temp_cloud/voice.mp3", "output_cloud/manga_done.mp4")
                st.video("output_cloud/manga_done.mp4")
                with open("output_cloud/manga_done.mp4", "rb") as file:
                    st.download_button("Descargar Video MP4", data=file, file_name="manga_video.mp4")

# --- PESTAÑA 2 ---
with tab2:
    st.header("Animación de Historias / Temas")
    story_input = st.text_area("Pega aquí tu relato, noticia o historia:")
    if story_input and gemini_key:
        if st.button("Generar Video del Relato"):
            with st.spinner("Estructurando guion de impacto..."):
                client = genai.Client(api_key=gemini_key)
                prompt = f"Resume este texto en un guion viral de suspenso de máximo 40 palabras para video vertical:\n{story_input}"
                res = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
                script_theme = res.text
                st.write("**Guion generado:**", script_theme)
                
                asyncio.run(tts_engine(script_theme))
                bg = Image.new('RGB', (1080, 1920), color=(20, 20, 35))
                bg.save("temp_cloud/theme_bg.png")
                
                render_motion("temp_cloud/theme_bg.png", "temp_cloud/voice.mp3", "output_cloud/story_done.mp4")
                st.video("output_cloud/story_done.mp4")
                with open("output_cloud/story_done.mp4", "rb") as file:
                    st.download_button("Descargar Video MP4", data=file, file_name="historia_video.mp4")

# --- PESTAÑA 3 ---
with tab3:
    st.header("Creación Original + Animación")
    idea_prompt = st.text_input("Ingresa la premisa (Ej: Un guerrero encuentra un arma sellada por milenios):")
    if idea_prompt and gemini_key:
        if st.button("Crear Todo desde Cero"):
            with st.spinner("Creando trama, narración y visual..."):
                client = genai.Client(api_key=gemini_key)
                full_prompt = f"Inventa un micro-cuento de suspenso basado en: {idea_prompt}. Máximo 40 palabras. Solo el guion narrativo."
                res = client.models.generate_content(model="gemini-1.5-flash", contents=full_prompt)
                created_story = res.text
                st.write("**Historia creada:**", created_story)
                
                asyncio.run(tts_engine(created_story))
                bg = Image.new('RGB', (1080, 1920), color=(10, 15, 25))
                bg.save("temp_cloud/created_bg.png")
                
                render_motion("temp_cloud/created_bg.png", "temp_cloud/voice.mp3", "output_cloud/created_done.mp4")
                st.video("output_cloud/created_done.mp4")
                with open("output_cloud/created_done.mp4", "rb") as file:
                    st.download_button("Descargar Video MP4", data=file, file_name="cuento_video.mp4")
