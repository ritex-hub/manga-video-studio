import os
import re
import json
import base64
import asyncio
import shutil
import urllib.parse
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    from gtts import gTTS
except ImportError:
    gTTS = None

try:
    from moviepy.editor import (
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        CompositeAudioClip,
        concatenate_videoclips,
        concatenate_audioclips
    )
    from moviepy.video.fx.all import fadein
except ImportError:
    VideoFileClip = None
    ImageClip = None
    concatenate_videoclips = None
    concatenate_audioclips = None

try:
    from gradio_client import Client as GradioClient, handle_file
except ImportError:
    GradioClient = None
    handle_file = None

VOICE_ROLES_BY_LANG = {
    "Español": {
        "NARRADOR": ("es-ES-AlvaroNeural", "+0Hz", "+0%"),
        "PROTAGONISTA": ("es-MX-JorgeNeural", "+2Hz", "+5%"),
        "CHICA": ("es-MX-DaliaNeural", "+4Hz", "+3%"),
        "VILLANO": ("es-AR-TomasNeural", "-18Hz", "-5%")
    },
    "Inglés": {
        "NARRADOR": ("en-US-ChristopherNeural", "+0Hz", "+0%"),
        "PROTAGONISTA": ("en-US-GuyNeural", "+2Hz", "+5%"),
        "CHICA": ("en-US-JennyNeural", "+4Hz", "+3%"),
        "VILLANO": ("en-GB-RyanNeural", "-18Hz", "-5%")
    },
    "Portugués": {
        "NARRADOR": ("pt-BR-AntonioNeural", "+0Hz", "+0%"),
        "PROTAGONISTA": ("pt-BR-FabioNeural", "+2Hz", "+5%"),
        "CHICA": ("pt-BR-FranciscaNeural", "+4Hz", "+3%"),
        "VILLANO": ("pt-PT-DuarteNeural", "-18Hz", "-5%")
    },
    "Francés": {
        "NARRADOR": ("fr-FR-HenriNeural", "+0Hz", "+0%"),
        "PROTAGONISTA": ("fr-CA-AntoineNeural", "+2Hz", "+5%"),
        "CHICA": ("fr-FR-DeniseNeural", "+4Hz", "+3%"),
        "VILLANO": ("fr-FR-AlainNeural", "-18Hz", "-5%")
    },
    "Japonés": {
        "NARRADOR": ("ja-JP-KeitaNeural", "+0Hz", "+0%"),
        "PROTAGONISTA": ("ja-JP-DaichiNeural", "+2Hz", "+5%"),
        "CHICA": ("ja-JP-NanamiNeural", "+4Hz", "+3%"),
        "VILLANO": ("ja-JP-NaokiNeural", "-18Hz", "-5%")
    }
}

LANG_TO_GTTS = {
    "Español": "es",
    "Inglés": "en",
    "Portugués": "pt",
    "Francés": "fr",
    "Japonés": "ja"
}

def get_sb_headers(sb_key: str):
    return {
        "apikey": sb_key.strip(),
        "Authorization": f"Bearer {sb_key.strip()}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def discover_gemini_models(key_clean: str, headers: dict):
    discovered = []
    for ver in ["v1beta", "v1"]:
        try:
            r = requests.get(f"https://generativelanguage.googleapis.com/{ver}/models", headers=headers, timeout=10)
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    methods = m.get("supportedGenerationMethods", [])
                    name = m.get("name", "")
                    if "generateContent" in methods and "gemini" in name:
                        discovered.append(f"https://generativelanguage.googleapis.com/{ver}/{name}:generateContent")
        except Exception:
            pass
    return discovered

def call_gemini_api(gemini_key: str, parts: list, timeout: int = 40):
    if not gemini_key or not gemini_key.strip():
        raise ValueError("Falta configurar la Gemini API Key en Secrets.")

    key_clean = gemini_key.strip()
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key_clean
    }

    endpoints = discover_gemini_models(key_clean, headers)
    fallback_endpoints = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent",
        "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    ]
    for ep in fallback_endpoints:
        if ep not in endpoints:
            endpoints.append(ep)

    last_err = ""
    for url in endpoints:
        try:
            res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=timeout)
            data = res.json()
            if "candidates" in data and data["candidates"]:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            last_err = data.get("error", {}).get("message", str(data))
        except Exception as e:
            last_err = str(e)

    raise RuntimeError(f"Error de Gemini API: {last_err}")

def translate_prompt_to_english(text: str, gemini_key: str) -> str:
    if not text or not gemini_key:
        return text
    try:
        prompt = f"Translate this anime character visual description into concise English image-generation keywords (only return the English keywords, nothing else): '{text}'"
        translated = call_gemini_api(gemini_key, [{"text": prompt}], timeout=15).strip()
        return translated if translated else text
    except Exception:
        return text

def upload_to_supabase_storage(sb_url: str, sb_key: str, local_path: str, remote_filename: str, content_type: str = "video/mp4"):
    if not sb_url or not sb_key or not os.path.exists(local_path):
        return ""
    base = sb_url.rstrip("/")
    upload_url = f"{base}/storage/v1/object/manga-media/{remote_filename}"
    headers = {
        "apikey": sb_key.strip(),
        "Authorization": f"Bearer {sb_key.strip()}",
        "Content-Type": content_type,
        "x-upsert": "true"
    }
    try:
        with open(local_path, "rb") as f:
            r = requests.post(upload_url, headers=headers, data=f.read(), timeout=60)
        if r.status_code in [200, 201]:
            return f"{base}/storage/v1/object/public/manga-media/{remote_filename}"
    except Exception:
        pass
    return ""

def get_dynamic_cluster_urls(sb_url: str, sb_key: str, default_modal: str = ""):
    nodes = {"modal": default_modal, "kaggle": "", "colab1": "", "colab2": ""}
    if not sb_url or not sb_key:
        return nodes
    try:
        r = requests.get(f"{sb_url.rstrip('/')}/rest/v1/cluster_nodes?select=*", headers=get_sb_headers(sb_key), timeout=5)
        if r.status_code == 200:
            for row in r.json():
                name = row.get("node_name")
                url = row.get("url", "").strip()
                if name in nodes and url:
                    nodes[name] = url
    except Exception:
        pass
    return nodes

def ping_url(url: str):
    if not url:
        return False
    try:
        r = requests.get(url.strip(), timeout=4)
        return r.status_code in [200, 405]
    except Exception:
        return False

def get_series_continuity(sb_url: str, sb_key: str, series_name: str):
    if not sb_url or not sb_key:
        return 1, "Inicio de la historia."
    url = f"{sb_url.rstrip('/')}/rest/v1/series_progress?series_name=eq.{urllib.parse.quote(series_name)}"
    try:
        r = requests.get(url, headers=get_sb_headers(sb_key), timeout=6)
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            return row.get("last_chapter", 0) + 1, row.get("last_summary", "Inicio de la historia.")
    except Exception:
        pass
    return 1, "Inicio de la historia."

def update_series_progress(sb_url: str, sb_key: str, series_name: str, chapter_num: int, summary: str):
    if not sb_url or not sb_key:
        return
    url = f"{sb_url.rstrip('/')}/rest/v1/series_progress"
    headers = get_sb_headers(sb_key)
    headers["Prefer"] = "resolution=merge-duplicates"
    try:
        requests.post(url, headers=headers, json={"series_name": series_name, "last_chapter": chapter_num, "last_summary": summary}, timeout=8)
    except Exception:
        pass

def save_episode_to_supabase(sb_url, sb_key, series_name, chapter_num, title, desc, tags, prompt, narr, status="pending", video_url=""):
    if not sb_url or not sb_key:
        return 0
    endpoint = f"{sb_url.rstrip('/')}/rest/v1/manga_episodes"
    payload = {
        "series_name": series_name,
        "chapter_num": chapter_num,
        "title": title,
        "description": desc,
        "hashtags": tags,
        "prompt": prompt,
        "narration": narr,
        "status": status,
        "video_url": video_url
    }
    try:
        r = requests.post(endpoint, headers=get_sb_headers(sb_key), json=payload, timeout=8)
        if r.status_code in [200, 201] and r.json():
            return r.json()[0].get("id", 0)
    except Exception:
        pass
    return 0

def get_script_details(sb_url: str, sb_key: str, series_name: str):
    if not sb_url or not sb_key:
        return None
    url = f"{sb_url.rstrip('/')}/rest/v1/saved_scripts?title=eq.{urllib.parse.quote(series_name)}"
    try:
        r = requests.get(url, headers=get_sb_headers(sb_key), timeout=6)
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception:
        pass
    return None

def generate_character_portrait(char_prompt: str, hf_token: str, gemini_key: str = "", out_path: str = "/tmp/char_portrait.png"):
    en_prompt = translate_prompt_to_english(char_prompt, gemini_key)
    full_p = f"portrait close up of {en_prompt}, manhwa character sheet, front view, clean line art, studio lighting"
    if hf_token:
        try:
            api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token.strip()}"}
            res = requests.post(api_url, headers=headers, json={"inputs": full_p}, timeout=45)
            if res.status_code == 200:
                with open(out_path, "wb") as f:
                    f.write(res.content)
                return out_path
        except Exception:
            pass
    try:
        encoded = urllib.parse.quote(full_p)
        r = requests.get(f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=1024&nologo=true&model=flux", timeout=40)
        if r.status_code == 200:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception:
        pass
    return None

def generate_image_with_memory(prompt: str, char_anchor: str, ref_image_url: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{char_anchor}, {prompt}, manhwa action style, detailed comic line art, vibrant cinematic lighting, vertical 9:16 composition"

    if ref_image_url and hf_token:
        try:
            r_ref = requests.get(ref_image_url, timeout=15)
            if r_ref.status_code == 200:
                i2i_url = "https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix"
                headers = {"Authorization": f"Bearer {hf_token.strip()}"}
                img_b64 = base64.b64encode(r_ref.content).decode("utf-8")
                payload = {"inputs": full_prompt, "parameters": {"image": img_b64, "image_guidance_scale": 1.4}}
                res = requests.post(i2i_url, headers=headers, json=payload, timeout=45)
                if res.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(res.content)
                    return True
        except Exception:
            pass

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

def get_freesound_audio(freesound_key: str, query_tags: str, min_dur: int, max_dur: int, output_path: str):
    if not freesound_key or not query_tags:
        return None
    query = urllib.parse.quote(query_tags)
    url = f"https://freesound.org/apiv2/search/text/?query={query}&token={freesound_key}&filter=duration:[{min_dur}+TO+{max_dur}]&fields=id,name,previews"
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

def clean_tts_text(raw_text: str) -> str:
    txt = re.sub(r"\[.*?\]", "", raw_text)
    txt = re.sub(r"[*_#~`]", "", txt).strip()
    return txt

async def synthesize_multivoice_speech(dialogue_lines: list, final_audio_path: str, language: str = "Español"):
    temp_clips = []
    all_subtitles = []
    current_offset = 0.0

    lang_voices = VOICE_ROLES_BY_LANG.get(language, VOICE_ROLES_BY_LANG["Español"])
    gtts_lang = LANG_TO_GTTS.get(language, "es")

    for idx, line in enumerate(dialogue_lines):
        role = line.get("role", "NARRADOR").upper()
        text = clean_tts_text(line.get("text", ""))
        if not text:
            continue

        voice, pitch, rate = lang_voices.get(role, lang_voices["NARRADOR"])
        part_path = f"/tmp/mv_part_{idx}.mp3"
        part_words = []
        edge_ok = False

        if edge_tts:
            try:
                communicate = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
                with open(part_path, "wb") as f:
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                        elif chunk["type"] == "WordBoundary":
                            w_start = (chunk["offset"] / 10_000_000) + current_offset
                            w_end = ((chunk["offset"] + chunk["duration"]) / 10_000_000) + current_offset
                            part_words.append({"word": chunk["text"], "start": w_start, "end": w_end})
                if os.path.exists(part_path) and os.path.getsize(part_path) > 100:
                    edge_ok = True
            except Exception:
                edge_ok = False

        if not edge_ok and gTTS:
            try:
                tts = gTTS(text=text, lang=gtts_lang, slow=False)
                tts.save(part_path)
            except Exception:
                continue

        if os.path.exists(part_path) and os.path.getsize(part_path) > 100:
            clip = AudioFileClip(part_path)
            if not part_words:
                words = text.split()
                if words:
                    w_dur = clip.duration / len(words)
                    for w_i, w_str in enumerate(words):
                        part_words.append({
                            "word": w_str,
                            "start": current_offset + (w_i * w_dur),
                            "end": current_offset + ((w_i + 1) * w_dur)
                        })
            temp_clips.append(clip)
            all_subtitles.extend(part_words)
            current_offset += clip.duration
            await asyncio.sleep(0.25)

    if not temp_clips:
        return []

    combined = concatenate_audioclips(temp_clips)
    combined.write_audiofile(final_audio_path, verbose=False, logger=None)
    for c in temp_clips:
        c.close()
    return all_subtitles

def overlay_tiktok_and_hook(pil_img, subtitles: list, current_time: float, hook_banner: str = ""):
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size

    if hook_banner and current_time <= 2.5:
        try:
            h_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        except Exception:
            h_font = ImageFont.load_default()
        hb = draw.textbbox((0, 0), hook_banner.upper(), font=h_font)
        hw, hh = hb[2] - hb[0], hb[3] - hb[1]
        hx = (w - hw) // 2
        hy = int(h * 0.14)
        draw.rectangle([hx - 24, hy - 14, hx + hw + 24, hy + hh + 18], fill=(180, 20, 60))
        draw.text((hx, hy), hook_banner.upper(), font=h_font, fill=(255, 255, 255))

    if not subtitles:
        return pil_img

    active_idx = -1
    for i, s in enumerate(subtitles):
        if s["start"] <= current_time <= s["end"] + 0.12:
            active_idx = i
            break

    if active_idx == -1:
        return pil_img

    chunk_start = (active_idx // 3) * 3
    chunk = subtitles[chunk_start:chunk_start + 3]

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 62)
    except Exception:
        font = ImageFont.load_default()

    words_upper = [c["word"].upper() for c in chunk]
    full_str = " ".join(words_upper)
    bbox = draw.textbbox((0, 0), full_str, font=font)
    total_w = bbox[2] - bbox[0]
    x_cursor = (w - total_w) // 2
    y_pos = int(h * 0.78)

    for idx_in_chunk, item in enumerate(chunk):
        w_text = item["word"].upper() + " "
        is_current = (chunk_start + idx_in_chunk) == active_idx
        fill_color = (57, 255, 20) if is_current else (255, 255, 255)

        for dx in range(-4, 5):
            for dy in range(-4, 5):
                draw.text((x_cursor + dx, y_pos + dy), w_text, font=font, fill=(0, 0, 0))
        draw.text((x_cursor, y_pos), w_text, font=font, fill=fill_color)

        w_bbox = draw.textbbox((0, 0), w_text, font=font)
        x_cursor += (w_bbox[2] - w_bbox[0])

    return pil_img

def animate_image_cascade(image_path: str, output_video_path: str, cluster_urls: dict):
    if cluster_urls.get("modal"):
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
            res = requests.post(cluster_urls["modal"].strip(), json={"image_base64": img_b64}, timeout=120)
            if res.status_code == 200 and "video_base64" in res.json():
                with open(output_video_path, "wb") as vf:
                    vf.write(base64.b64decode(res.json()["video_base64"]))
                return True, "Modal Labs (24/7)"
        except Exception:
            pass

    if GradioClient and handle_file:
        for node_key, label in [("kaggle", "Kaggle T4"), ("colab1", "Colab 1"), ("colab2", "Colab 2")]:
            url = cluster_urls.get(node_key)
            if url and ping_url(url):
                try:
                    client = GradioClient(url.strip())
                    result = client.predict(input_img=handle_file(image_path), api_name="/generate_motion")
                    if result and os.path.exists(result):
                        shutil.copy(result, output_video_path)
                        return True, label
                except Exception:
                    pass
    return False, "Zoom Cinemático (CPU)"

def build_scene_clip(visual_path: str, audio_path: str, sfx_path: str, subtitles: list, out_mp4: str, is_video: bool, hook_banner: str = ""):
    voice_clip = AudioFileClip(audio_path)
    dur = voice_clip.duration + 0.15

    if is_video and VideoFileClip:
        base = VideoFileClip(visual_path)
        base = base.loop(duration=dur) if base.duration < dur else base.subclip(0, dur)
        scene = base.fl(lambda gf, t: np.array(overlay_tiktok_and_hook(Image.fromarray(gf(t)), subtitles, t, hook_banner)))
    else:
        target_w, target_h = 1080, 1920
        b_img = Image.open(visual_path).convert("RGB")
        sc = max(target_w / b_img.width, target_h / b_img.height)
        res = b_img.resize((int(b_img.width * sc), int(b_img.height * sc)), Image.Resampling.LANCZOS)
        l, top = (res.width - target_w) // 2, (res.height - target_h) // 2
        crp = res.crop((l, top, l + target_w, top + target_h))

        def frame_zoom(t):
            zf = 1.0 + 0.06 * (t / max(dur, 1))
            zw, zh = int(target_w * zf), int(target_h * zf)
            z_img = crp.resize((zw, zh), Image.Resampling.BILINEAR)
            f_img = z_img.crop(((zw - target_w) // 2, (zh - target_h) // 2, (zw + target_w) // 2, (zh + target_h) // 2))
            return np.array(overlay_tiktok_and_hook(f_img, subtitles, t, hook_banner))

        scene = ImageClip(np.array(crp)).set_duration(dur).fl(lambda gf, t: frame_zoom(t))

    scene = fadein(scene, 0.25)

    audio_tracks = [voice_clip]
    if sfx_path and os.path.exists(sfx_path):
        try:
            sfx_clip = AudioFileClip(sfx_path).volumex(0.45)
            if sfx_clip.duration > dur:
                sfx_clip = sfx_clip.subclip(0, dur)
            audio_tracks.append(sfx_clip)
        except Exception:
            pass

    scene = scene.set_audio(CompositeAudioClip(audio_tracks))
    scene.write_videofile(out_mp4, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return out_mp4

def publish_to_tiktok_api(video_path: str, title: str, access_token: str):
    if not access_token or not os.path.exists(video_path):
        return False, "Falta Access Token de TikTok o archivo de video."
    file_size = os.path.getsize(video_path)
    init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    headers = {"Authorization": f"Bearer {access_token.strip()}", "Content-Type": "application/json"}
    payload = {
        "post_info": {"title": title[:150], "privacy_level": "SELF_ONLY"},
        "source_info": {"source": "FILE_UPLOAD", "video_size": file_size, "chunk_size": file_size, "total_chunk_count": 1}
    }
    try:
        r_init = requests.post(init_url, headers=headers, json=payload, timeout=20)
        upload_url = r_init.json().get("data", {}).get("upload_url")
        if not upload_url:
            return False, f"Error init TikTok: {r_init.text}"
        with open(video_path, "rb") as f:
            vid_bytes = f.read()
        up_headers = {"Content-Range": f"bytes 0-{file_size-1}/{file_size}", "Content-Type": "video/mp4"}
        r_up = requests.put(upload_url, headers=up_headers, data=vid_bytes, timeout=60)
        return r_up.status_code in [200, 201], "Subido a bandeja de TikTok."
    except Exception as e:
        return False, str(e)

def produce_series_episode(series_name: str, custom_direction: str, config: dict, language_override: str = "", progress_callback=None):
    def log(msg):
        if progress_callback:
            progress_callback(msg)

    sb_url = config.get("sb_url", "")
    sb_key = config.get("sb_key", "")
    gemini_key = config.get("gemini_key", "")
    hf_token = config.get("hf_token", "")
    fs_key = config.get("fs_key", "")
    tg_token = config.get("tg_token", "")
    tg_chat = config.get("tg_chat", "")
    modal_url = config.get("modal_url", "")

    next_chap, prev_summary = get_series_continuity(sb_url, sb_key, series_name)
    script_obj = get_script_details(sb_url, sb_key, series_name)

    target_lang = language_override or (script_obj.get("language") if script_obj and script_obj.get("language") else "Español")
    raw_char = script_obj.get("character_prompt") if script_obj and script_obj.get("character_prompt") else "young anime warrior, detailed manhwa style"
    char_anchor = translate_prompt_to_english(raw_char, gemini_key)
    ref_img_url = script_obj.get("reference_image_url", "") if script_obj else ""
    master_ref = f"\nGUION MAESTRO:\n{script_obj['script_content'][:2500]}" if script_obj else ""

    log(f"🧠 Escribiendo guion en **{target_lang}** con SFX y Hook para '{series_name}' — Cap. #{next_chap}...")

    gem_prompt = f"""
    Eres el director de la serie '{series_name}'. Escribe el CAPÍTULO #{next_chap} en 3 escenas consecutivas.
    IDIOMA OBLIGATORIO PARA TODO EL TEXTO, DIÁLOGOS Y HOOK: {target_lang}.
    LO QUE YA PASÓ ANTES (PROHIBIDO REPETIR): '{prev_summary}'.
    DIRECCIÓN: '{custom_direction}'.
    {master_ref}
    Asigna roles usando SOLO: NARRADOR, PROTAGONISTA, CHICA, VILLANO.
    Los campos 'prompt' y 'sfx_query' DEBEN ir en inglés; 'hook_banner', 'description' y 'dialogues[].text' DEBEN ir en {target_lang}.
    Responde ÚNICAMENTE en JSON válido:
    {{
      "title": "{series_name} - Cap. {next_chap}",
      "hook_banner": "Hook corto de máximo 5 palabras en {target_lang}",
      "description": "Resumen breve en {target_lang} de lo ocurrido en este capítulo",
      "hashtags": "#manhwa #anime #webtoon #fyp",
      "mood_music": "cyberpunk dark synth epic action",
      "scenes": [
        {{
          "prompt": "Detailed visual prompt in English for scene 1",
          "sfx_query": "sword unsheath",
          "dialogues": [
            {{"role": "NARRADOR", "text": "Texto narrado en {target_lang}"}},
            {{"role": "PROTAGONISTA", "text": "Diálogo del héroe en {target_lang}"}}
          ]
        }},
        {{
          "prompt": "Detailed visual prompt in English for scene 2",
          "sfx_query": "thunder explosion",
          "dialogues": [
            {{"role": "VILLANO", "text": "Amenaza del enemigo en {target_lang}"}},
            {{"role": "CHICA", "text": "Reacción de aliada en {target_lang}"}}
          ]
        }},
        {{
          "prompt": "Detailed visual prompt in English for scene 3",
          "sfx_query": "energy blast",
          "dialogues": [
            {{"role": "PROTAGONISTA", "text": "Ataque final en {target_lang}"}},
            {{"role": "NARRADOR", "text": "Gancho final del capítulo en {target_lang}"}}
          ]
        }}
      ]
    }}
    """
    txt = call_gemini_api(gemini_key, [{"text": gem_prompt}], timeout=40)
    script_data = json.loads(re.search(r"\{.*\}", txt, re.DOTALL).group(0))

    title = script_data["title"]
    hook_text = script_data.get("hook_banner", f"CAP. {next_chap}")
    desc = script_data["description"]
    tags = script_data["hashtags"]
    mood = script_data["mood_music"]
    scenes = script_data["scenes"]

    cluster_urls = get_dynamic_cluster_urls(sb_url, sb_key, modal_url)
    rendered_files = []

    for idx, sc in enumerate(scenes):
        log(f"🎬 Escena {idx+1}/3 ({target_lang} • Memoria Visual + SFX)...")
        img_f = f"/tmp/bg_s_{idx}.png"
        aud_f = f"/tmp/bg_a_{idx}.mp3"
        sfx_f = f"/tmp/bg_sfx_{idx}.mp3"
        mp4_f = f"/tmp/bg_m_{idx}.mp4"
        anim_f = f"/tmp/bg_anim_{idx}.mp4"

        generate_image_with_memory(sc["prompt"], char_anchor, ref_img_url, hf_token, img_f, seed=next_chap * 100 + idx)
        subs = asyncio.run(synthesize_multivoice_speech(sc.get("dialogues", []), aud_f, language=target_lang))
        sfx_path = get_freesound_audio(fs_key, sc.get("sfx_query", "whoosh"), 1, 4, sfx_f)

        ok_anim, engine = animate_image_cascade(img_f, anim_f, cluster_urls)
        log(f"   ↳ GPU: {engine} | SFX: {sc.get('sfx_query', 'none')}")

        banner = hook_text if idx == 0 else ""
        build_scene_clip(anim_f if ok_anim else img_f, aud_f, sfx_path, subs, mp4_f, is_video=ok_anim, hook_banner=banner)
        rendered_files.append(mp4_f)

    clips = [VideoFileClip(p) for p in rendered_files]
    final_vid = concatenate_videoclips(clips, method="compose")
    bgm_p = get_freesound_audio(fs_key, mood, 25, 120, "/tmp/bgm_main.mp3")
    if bgm_p and os.path.exists(bgm_p):
        bgm = AudioFileClip(bgm_p).volumex(0.13)
        bgm = bgm.loop(duration=final_vid.duration) if bgm.duration < final_vid.duration else bgm.subclip(0, final_vid.duration)
        final_vid = final_vid.set_audio(CompositeAudioClip([final_vid.audio, bgm]))

    out_final = f"/tmp/cap_{next_chap}_final.mp4"
    final_vid.write_videofile(out_final, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    for c in clips:
        c.close()

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", series_name)
    public_video_url = upload_to_supabase_storage(sb_url, sb_key, out_final, f"videos/{safe_name}_cap_{next_chap}.mp4", "video/mp4")

    first_line = scenes[0]["dialogues"][0]["text"] if scenes[0].get("dialogues") else ""
    ep_id = save_episode_to_supabase(sb_url, sb_key, series_name, next_chap, title, desc, tags, scenes[0]["prompt"], first_line, "ready", public_video_url)
    update_series_progress(sb_url, sb_key, series_name, next_chap, desc)

    if tg_token and tg_chat and os.path.exists(out_final):
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Publicar en TikTok", "callback_data": f"publish_tk_{ep_id}"},
                {"text": "🔄 Regenerar Capítulo", "callback_data": f"retry_{ep_id}"}
            ]]
        }
        url_tg = f"https://api.telegram.org/bot{tg_token.strip()}/sendVideo"
        with open(out_final, "rb") as vf:
            requests.post(
                url_tg,
                files={"video": vf},
                data={"chat_id": tg_chat.strip(), "caption": f"🎬 **{title}** [{target_lang}]\n{desc}\n{tags}", "reply_markup": json.dumps(keyboard)},
                timeout=90
            )

    return out_final, title, next_chap
