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
    from moviepy.editor import (
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        CompositeAudioClip,
        concatenate_videoclips,
        concatenate_audioclips
    )
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

# Mapeo de voces distintas por rol para doblaje multi-personaje
VOICE_ROLES = {
    "NARRADOR": ("es-ES-AlvaroNeural", "+0Hz", "+0%"),
    "PROTAGONISTA": ("es-MX-JorgeNeural", "+2Hz", "+5%"),
    "CHICA": ("es-MX-DaliaNeural", "+4Hz", "+3%"),
    "VILLANO": ("es-AR-TomasNeural", "-18Hz", "-5%")
}

def get_sb_headers(sb_key: str):
    return {
        "apikey": sb_key.strip(),
        "Authorization": f"Bearer {sb_key.strip()}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

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

def save_episode_to_supabase(sb_url, sb_key, series_name, chapter_num, title, desc, tags, prompt, narr, status="pending"):
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
        "status": status
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

# ==============================================================================
# DOBLAJE MULTI-VOZ Y SUBTÍTULOS DINÁMICOS ESTILO TIKTOK / CAPCUT
# ==============================================================================

async def synthesize_multivoice_speech(dialogue_lines: list, final_audio_path: str):
    temp_clips = []
    all_subtitles = []
    current_offset = 0.0

    for idx, line in enumerate(dialogue_lines):
        role = line.get("role", "NARRADOR").upper()
        text = line.get("text", "").strip()
        if not text:
            continue

        voice, pitch, rate = VOICE_ROLES.get(role, VOICE_ROLES["NARRADOR"])
        part_path = f"/tmp/mv_part_{idx}.mp3"

        communicate = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
        part_words = []
        with open(part_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    w_start = (chunk["offset"] / 10_000_000) + current_offset
                    w_end = ((chunk["offset"] + chunk["duration"]) / 10_000_000) + current_offset
                    part_words.append({"word": chunk["text"], "start": w_start, "end": w_end})

        if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
            clip = AudioFileClip(part_path)
            temp_clips.append(clip)
            all_subtitles.extend(part_words)
            current_offset += clip.duration

    if not temp_clips:
        return []

    combined = concatenate_audioclips(temp_clips)
    combined.write_audiofile(final_audio_path, verbose=False, logger=None)
    for c in temp_clips:
        c.close()
    return all_subtitles

def overlay_tiktok_subtitles(pil_img, subtitles: list, current_time: float):
    """Dibuja bloques de 3 palabras centradas resaltando la palabra activa en verde neón."""
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

    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
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

# ==============================================================================
# GENERACIÓN VISUAL Y RENDERIZADO GPU EN CASCADA
# ==============================================================================

def generate_image_base(prompt: str, char_anchor: str, hf_token: str, output_path: str, seed: int = 42):
    full_prompt = f"{char_anchor}, {prompt}, manhwa action style, detailed comic line art, vibrant cinematic lighting, vertical 9:16 composition"
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

def get_smart_bgm(freesound_key: str, mood_tags: str, output_path: str = "/tmp/bgm.mp3"):
    if not freesound_key:
        return None
    query = urllib.parse.quote(mood_tags if mood_tags else "anime action epic soundtrack")
    url = f"https://freesound.org/apiv2/search/text/?query={query}&token={freesound_key}&filter=duration:[25+TO+120]&fields=id,name,previews"
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

def build_scene_clip(visual_path: str, audio_path: str, subtitles: list, out_mp4: str, is_video: bool):
    voice_clip = AudioFileClip(audio_path)
    dur = voice_clip.duration + 0.15
    if is_video and VideoFileClip:
        base = VideoFileClip(visual_path)
        base = base.loop(duration=dur) if base.duration < dur else base.subclip(0, dur)
        scene = base.fl(lambda gf, t: np.array(overlay_tiktok_subtitles(Image.fromarray(gf(t)), subtitles, t)))
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
            return np.array(overlay_tiktok_subtitles(f_img, subtitles, t))

        scene = ImageClip(np.array(crp)).set_duration(dur).fl(lambda gf, t: frame_zoom(t))

    scene = scene.set_audio(voice_clip)
    scene.write_videofile(out_mp4, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    voice_clip.close()
    return out_mp4

# ==============================================================================
# PIPELINE COMPLETO EJECUTABLE DESDE STREAMLIT O EN SEGUNDO PLANO (RENDER/TG)
# ==============================================================================

def produce_series_episode(
    series_name: str,
    custom_direction: str,
    config: dict,
    progress_callback=None
):
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
    char_anchor = script_obj.get("character_prompt") if script_obj and script_obj.get("character_prompt") else "young anime warrior, detailed manhwa style"
    master_ref = f"\nGUION MAESTRO:\n{script_obj['script_content'][:2500]}" if script_obj else ""

    log(f"🧠 Escribiendo guion multi-voz para '{series_name}' — Capítulo #{next_chap}...")

    gem_prompt = f"""
    Eres el director de la serie '{series_name}'. Escribe el CAPÍTULO #{next_chap} en 3 escenas consecutivas.
    LO QUE YA PASÓ ANTES (PROHIBIDO REPETIR): '{prev_summary}'.
    DIRECCIÓN: '{custom_direction}'.
    {master_ref}
    Asigna roles de doblaje en cada escena usando SOLO estos roles: NARRADOR, PROTAGONISTA, CHICA, VILLANO.
    Responde ÚNICAMENTE en JSON válido:
    {{
      "title": "{series_name} - Cap. {next_chap}",
      "description": "Resumen breve de lo ocurrido en este capítulo para continuar en el siguiente",
      "hashtags": "#manhwa #anime #webtoon #fyp",
      "mood_music": "cyberpunk dark synth epic action",
      "scenes": [
        {{
          "prompt": "Detailed visual prompt in English for scene 1",
          "dialogues": [
            {{"role": "NARRADOR", "text": "Texto narrado"}},
            {{"role": "PROTAGONISTA", "text": "Diálogo del héroe"}}
          ]
        }},
        {{
          "prompt": "Detailed visual prompt in English for scene 2",
          "dialogues": [
            {{"role": "VILLANO", "text": "Amenaza del enemigo"}},
            {{"role": "CHICA", "text": "Reacción de aliada"}}
          ]
        }},
        {{
          "prompt": "Detailed visual prompt in English for scene 3",
          "dialogues": [
            {{"role": "PROTAGONISTA", "text": "Ataque final"}},
            {{"role": "NARRADOR", "text": "Gancho final del capítulo"}}
          ]
        }}
      ]
    }}
    """
    url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
    res_g = requests.post(url_g, json={"contents": [{"parts": [{"text": gem_prompt}]}]}, timeout=35)
    txt = res_g.json()["candidates"][0]["content"]["parts"][0]["text"]
    script_data = json.loads(re.search(r"\{.*\}", txt, re.DOTALL).group(0))

    title = script_data["title"]
    desc = script_data["description"]
    tags = script_data["hashtags"]
    mood = script_data["mood_music"]
    scenes = script_data["scenes"]

    first_line = scenes[0]["dialogues"][0]["text"] if scenes[0].get("dialogues") else ""
    ep_id = save_episode_to_supabase(sb_url, sb_key, series_name, next_chap, title, desc, tags, scenes[0]["prompt"], first_line, "processing")

    cluster_urls = get_dynamic_cluster_urls(sb_url, sb_key, modal_url)
    rendered_files = []

    for idx, sc in enumerate(scenes):
        log(f"🎬 Produciendo Escena {idx+1}/3 (Multi-Voz + GPU)...")
        img_f = f"/tmp/bg_s_{idx}.png"
        aud_f = f"/tmp/bg_a_{idx}.mp3"
        mp4_f = f"/tmp/bg_m_{idx}.mp4"
        anim_f = f"/tmp/bg_anim_{idx}.mp4"

        generate_image_base(sc["prompt"], char_anchor, hf_token, img_f, seed=next_chap * 100 + idx)
        subs = asyncio.run(synthesize_multivoice_speech(sc.get("dialogues", []), aud_f))
        ok_anim, engine = animate_image_cascade(img_f, anim_f, cluster_urls)
        log(f"   ↳ Renderizado con: {engine}")
        build_scene_clip(anim_f if ok_anim else img_f, aud_f, subs, mp4_f, is_video=ok_anim)
        rendered_files.append(mp4_f)

    clips = [VideoFileClip(p) for p in rendered_files]
    final_vid = concatenate_videoclips(clips, method="compose")
    bgm_p = get_smart_bgm(fs_key, mood)
    if bgm_p and os.path.exists(bgm_p):
        bgm = AudioFileClip(bgm_p).volumex(0.14)
        bgm = bgm.loop(duration=final_vid.duration) if bgm.duration < final_vid.duration else bgm.subclip(0, final_vid.duration)
        final_vid = final_vid.set_audio(CompositeAudioClip([final_vid.audio, bgm]))

    out_final = f"/tmp/cap_{next_chap}_final.mp4"
    final_vid.write_videofile(out_final, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    for c in clips:
        c.close()

    update_series_progress(sb_url, sb_key, series_name, next_chap, desc)

    if tg_token and tg_chat and os.path.exists(out_final):
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Publicar en TikTok", "callback_data": f"publish_{ep_id}"},
                {"text": "🔄 Regenerar", "callback_data": f"retry_{ep_id}"}
            ]]
        }
        url_tg = f"https://api.telegram.org/bot{tg_token.strip()}/sendVideo"
        with open(out_final, "rb") as vf:
            requests.post(
                url_tg,
                files={"video": vf},
                data={"chat_id": tg_chat.strip(), "caption": f"🎬 **{title}**\n{desc}\n{tags}", "reply_markup": json.dumps(keyboard)},
                timeout=90
            )

    return out_final, title, next_chap
