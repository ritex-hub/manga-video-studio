import os
import requests
import streamlit as st

def get_valid_access_token():
    """Retorna un access_token válido; si está próximo a expirar usa el refresh_token."""
    # En desarrollo local puedes usar os.environ o st.secrets
    refresh_token = st.secrets["tiktok"]["refresh_token"]
    client_key = st.secrets["tiktok"]["client_key"]
    client_secret = st.secrets["tiktok"]["client_secret"]
    
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    # Si quieres usar el token actual directamente:
    return st.secrets["tiktok"]["access_token"]


def upload_manga_video_to_tiktok(video_path: str, caption: str):
    """Sube un archivo de video local a TikTok Content Posting API v2."""
    access_token = get_valid_access_token()
    file_size = os.path.getsize(video_path)

    # 1. Inicializar la subida
    init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    
    body = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,
            "total_chunk_count": 1
        }
    }

    res_init = requests.post(init_url, headers=headers, json=body)
    init_data = res_init.json()
    
    if init_data.get("error", {}).get("code") != "ok":
        raise Exception(f"Fallo al inicializar subida en TikTok: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    # 2. Subir el archivo de video binario a la URL devuelta por TikTok
    upload_headers = {
        "Content-Type": "video/mp4",
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
    }
    
    with open(video_path, "rb") as video_file:
        res_upload = requests.put(upload_url, headers=upload_headers, data=video_file)

    if res_upload.status_code not in (200, 201, 206):
        raise Exception(f"Error subiendo bytes a TikTok: {res_upload.text}")

    return publish_id
