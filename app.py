import os
import time
import requests
import streamlit as st

st.set_page_config(page_title="Manga & Story Studio", layout="wide", page_icon="🎬")

# ==========================================
# 1. GESTIÓN DE CREDENCIALES Y AUTENTICACIÓN
# ==========================================

def get_secret(section, key, default=""):
    """Recupera variables desde st.secrets de manera segura."""
    try:
        return st.secrets[section][key]
    except Exception:
        return default

# Cargar credenciales por defecto desde st.secrets o sesión
if "tiktok_access_token" not in st.session_state:
    st.session_state["tiktok_access_token"] = get_secret("tiktok", "access_token")

if "tiktok_refresh_token" not in st.session_state:
    st.session_state["tiktok_refresh_token"] = get_secret("tiktok", "refresh_token")

CLIENT_KEY = get_secret("tiktok", "client_key", "sbawgv6qorcmbtk9qx")
CLIENT_SECRET = get_secret("tiktok", "client_secret", "0rdY9yBOfMMkmTLLwjfxcPZvWOkyX9LJ")


def renew_tiktok_token():
    """Renueva el access_token usando el refresh_token."""
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": st.session_state["tiktok_refresh_token"],
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        res = requests.post(url, headers=headers, data=payload, timeout=20)
        data = res.json()
        if "access_token" in data:
            st.session_state["tiktok_access_token"] = data["access_token"]
            st.session_state["tiktok_refresh_token"] = data.get("refresh_token", st.session_state["tiktok_refresh_token"])
            return data["access_token"]
        else:
            st.error(f"Error renovando token: {data}")
            return None
    except Exception as e:
        st.error(f"Fallo de conexión al renovar token: {e}")
        return None


# ==========================================
# 2. MOTOR DE SUBIDA DE VIDEO A TIKTOK v2
# ==========================================

def upload_video_to_tiktok(video_path: str):
    """
    Sube un video local a TikTok Content Posting API v2 (Bandeja de entrada / Borrador).
    Maneja inicialización, transferencia binaria y reintento de token caducado.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"No se encontró el archivo de video: {video_path}")

    file_size = os.path.getsize(video_path)
    access_token = st.session_state["tiktok_access_token"]

    def _initiate_upload(token):
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

    # 1. Petición inicial para obtener upload_url
    res_init = _initiate_upload(access_token)
    init_data = res_init.json()

    # Si el token expiró, se renueva automáticamente y se reintenta
    if init_data.get("error", {}).get("code") in ["access_token_invalid", "token_expired", "invalid_token"]:
        st.warning("El Access Token ha expirado. Renovando automáticamente...")
        access_token = renew_tiktok_token()
        if not access_token:
            raise Exception("No fue posible renovar el Access Token de TikTok.")
        res_init = _initiate_upload(access_token)
        init_data = res_init.json()

    if init_data.get("error", {}).get("code") != "ok":
        raise Exception(f"Fallo de TikTok API: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    # 2. Subida del archivo binario a la upload_url
    upload_headers = {
        "Content-Type": "video/mp4",
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
    }

    with open(video_path, "rb") as f:
        res_upload = requests.put(upload_url, headers=upload_headers, data=f, timeout=120)

    if res_upload.status_code not in (200, 201, 206):
        raise Exception(f"Fallo en la carga del video binario: Código {res_upload.status_code} - {res_upload.text}")

    return publish_id


# ==========================================
# 3. INTERFAZ DE USUARIO (STREAMLIT)
# ==========================================

# Barra Lateral
st.sidebar.title("🔑 Configuración")
gemini_key = st.sidebar.text_input("Gemini API Key", type="password", value=get_secret("general", "gemini_api_key"))
telegram_token = st.sidebar.text_input("Telegram Bot Token", type="password")
telegram_chat_id = st.sidebar.text_input("Telegram Chat ID")

st.sidebar.markdown("---")
st.sidebar.subheader("🎵 Estado de TikTok")

if st.session_state["tiktok_access_token"]:
    st.sidebar.success("✅ Conectado a TikTok")
    if st.sidebar.button("🔄 Refrescar Token"):
        nuevo = renew_tiktok_token()
        if nuevo:
            st.sidebar.info("Token renovado con éxito.")
else:
    st.sidebar.warning("⚠️ Sin token activo")

# Panel Central
st.title("🎬 Manga & Story Studio: Automatización Completa")

tabs = st.tabs(["📖 1. Animación de Manga (PDF)", "📝 2. Historias / Temas", "🚀 3. TikTok Uploader"])

with tabs[0]:
    st.subheader("Animación Dinámica de Viñetas")
    pdf_file = st.file_uploader("Sube el PDF del Manga", type=["pdf"])
    if pdf_file:
        st.info("Archivo cargado con éxito. Listo para procesar y segmentar viñetas.")

with tabs[1]:
    st.subheader("Generación de Historias y Guiones IA")
    story_theme = st.text_input("Tema de la historia o resumen del capítulo:")
    if st.button("Generar Guion"):
        st.write("Generando contenido estructurado vertical con Gemini...")

with tabs[2]:
    st.subheader("Publicación Directa en TikTok (Sandbox / Producción)")
    uploaded_video = st.file_uploader("Selecciona un video MP4 para subir", type=["mp4"])
    
    if uploaded_video:
        # Guardar archivo temporalmente para su transferencia
        temp_video_path = os.path.join("/tmp" if os.name != "nt" else ".", uploaded_video.name)
        with open(temp_video_path, "wb") as f:
            f.write(uploaded_video.read())
            
        st.video(temp_video_path)
        
        if st.button("📤 Subir como Borrador a TikTok", type="primary"):
            progress_bar = st.progress(0, text="Iniciando subida a TikTok...")
            try:
                progress_bar.progress(30, text="Reservando sesión en TikTok API...")
                publish_id = upload_video_to_tiktok(temp_video_path)
                progress_bar.progress(100, text="¡Completado con éxito!")
                
                st.success("🎉 ¡Video enviado exitosamente a tu cuenta de TikTok!")
                st.info(f"**Publish ID:** `{publish_id}`\n\nEl video se encuentra disponible en tu bandeja de entrada / borradores de la app de TikTok para revisión y publicación final.")
            except Exception as err:
                st.error(f"Error durante la subida: {str(err)}")
            finally:
                if os.path.exists(temp_video_path):
                    os.remove(temp_video_path)
