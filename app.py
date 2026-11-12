import os
import re
import json
import time
import base64
import asyncio
import urllib.parse
import requests
import streamlit as st
from studio_engine import (
    get_sb_headers,
    get_dynamic_cluster_urls,
    ping_url,
    get_series_continuity,
    update_series_progress,
    save_episode_to_supabase,
    animate_image_cascade,
    get_freesound_audio,
    build_scene_clip,
    synthesize_multivoice_speech,
    produce_series_episode,
    generate_character_portrait,
    upload_to_supabase_storage
)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip, concatenate_videoclips
except ImportError:
    VideoFileClip = None
    concatenate_videoclips = None

st.set_page_config(page_title="Manga Studio AI | Production Hub", layout="wide", page_icon="🎬")

st.markdown("""
<style>
    .main-header {
        background: linear-gradient(135deg, #1a1c29 0%, #2d1b4e 100%);
        padding: 1.6rem;
        border-radius: 14px;
        border: 1px solid rgba(255, 255, 255, 0.1);
        margin-bottom: 1.4rem;
    }
    .main-header h1 { color: #ffffff; margin: 0; font-size: 2rem; font-weight: 800; }
    .main-header p { color: #b8c0e0; margin: 0.3rem 0 0 0; font-size: 0.95rem; }
    .status-card {
        background-color: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 10px;
        padding: 0.75rem 1rem;
    }
    .script-box {
        background-color: #161822;
        border-left: 4px solid #7c4dff;
        padding: 1.1rem;
        border-radius: 8px;
        margin: 0.8rem 0;
    }
</style>
""", unsafe_allow_html=True)

SUPPORTED_LANGUAGES = ["Español", "Inglés", "Portugués", "Francés", "Japonés"]

def get_secret(section: str, key: str, default: str = "") -> str:
    try:
        val = st.secrets[section][key]
        return str(val).strip() if val is not None else default
    except Exception:
        return default

ENGINE_CONFIG = {
    "gemini_key": get_secret("gemini", "api_key", ""),
    "hf_token": get_secret("huggingface", "api_token", ""),
    "sb_url": get_secret("supabase", "url", ""),
    "sb_key": get_secret("supabase", "key", ""),
    "modal_url": get_secret("modal", "endpoint_url", ""),
    "fs_key": get_secret("freesound", "api_key", ""),
    "tg_token": get_secret("telegram", "bot_token", ""),
    "tg_chat": get_secret("telegram", "chat_id", "")
}

DEFAULT_ACCESS_TOKEN = get_secret("tiktok", "access_token", "")
NOTEBOOK_KAGGLE = get_secret("notebooks", "kaggle_url", "https://www.kaggle.com/code")
NOTEBOOK_COLAB1 = get_secret("notebooks", "colab1_url", "https://colab.research.google.com/")
NOTEBOOK_COLAB2 = get_secret("notebooks", "colab2_url", "https://colab.research.google.com/")

def save_script_with_portrait(title: str, premise: str, char_prompt: str, script_content: str, language: str):
    sb_url, sb_key = ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"]
    if not sb_url or not sb_key or not title.strip():
        return False

    ref_url = ""
    portrait_file = generate_character_portrait(char_prompt, ENGINE_CONFIG["hf_token"], ENGINE_CONFIG["gemini_key"])
    if portrait_file:
        safe_t = re.sub(r"[^a-zA-Z0-9_-]", "_", title.strip())
        ref_url = upload_to_supabase_storage(sb_url, sb_key, portrait_file, f"portraits/{safe_t}.png", "image/png")

    url = f"{sb_url.rstrip('/')}/rest/v1/saved_scripts"
    headers = get_sb_headers(sb_key)
    headers["Prefer"] = "resolution=merge-duplicates"
    payload = {
        "title": title.strip(),
        "premise": premise.strip(),
        "character_prompt": char_prompt.strip(),
        "reference_image_url": ref_url,
        "script_content": script_content,
        "language": language
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        return r.status_code in [200, 201]
    except Exception:
        return False

def fetch_saved_scripts():
    sb_url, sb_key = ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"]
    if not sb_url or not sb_key:
        return []
    try:
        r = requests.get(f"{sb_url.rstrip('/')}/rest/v1/saved_scripts?select=*&order=created_at.desc", headers=get_sb_headers(sb_key), timeout=6)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def fetch_recent_episodes():
    sb_url, sb_key = ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"]
    if not sb_url or not sb_key:
        return []
    try:
        r = requests.get(f"{sb_url.rstrip('/')}/rest/v1/manga_episodes?select=*&order=id.desc&limit=20", headers=get_sb_headers(sb_key), timeout=6)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def send_remote_telegram_command(cmd_text: str):
    tg_token, tg_chat = ENGINE_CONFIG["tg_token"], ENGINE_CONFIG["tg_chat"]
    if not tg_token or not tg_chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat, "text": cmd_text},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

# ==============================================================================
# BARRA LATERAL COMPLETA
# ==============================================================================

cluster_urls = get_dynamic_cluster_urls(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], ENGINE_CONFIG["modal_url"])

with st.sidebar:
    st.markdown("## ⚡ Centro de Control")
    if cluster_urls.get("modal"):
        st.success("🚀 **Modal Labs:** Activo (24/7)")
    else:
        st.warning("⚠️ **Modal Labs:** Sin URL")

    st.markdown("### 🖥️ Cluster GPU de Respaldo")
    for key, label, link in [
        ("kaggle", "Kaggle T4", NOTEBOOK_KAGGLE),
        ("colab1", "Colab 1", NOTEBOOK_COLAB1),
        ("colab2", "Colab 2", NOTEBOOK_COLAB2)
    ]:
        c1, c2 = st.columns([2.2, 1])
        c1.markdown(f"**{label}:** {'🟢 Online' if ping_url(cluster_urls.get(key)) else '🔴 Offline'}")
        c2.link_button("Abrir", link, use_container_width=True)

    if st.button("🔄 Refrescar Estado Cluster", use_container_width=True):
        st.rerun()

    st.markdown("---")
    st.markdown("### 🔌 Plataformas Conectadas")
    st.markdown(f"""
    <div class="status-card">
        <div>🗄️ <b>Supabase BD & Storage:</b> {'🟢 Conectado' if ENGINE_CONFIG['sb_url'] else '🔴 Offline'}</div>
        <div>🧠 <b>Gemini AI:</b> {'🟢 Listo' if ENGINE_CONFIG['gemini_key'] else '🔴 Sin Key'}</div>
        <div>🎨 <b>Hugging Face:</b> {'🟢 Listo' if ENGINE_CONFIG['hf_token'] else '🟡 Pollinations'}</div>
        <div>🎵 <b>Freesound SFX/BGM:</b> {'🟢 Listo' if ENGINE_CONFIG['fs_key'] else '🔴 Sin Key'}</div>
        <div>✈️ <b>Telegram Bot:</b> {'🟢 Conectado' if ENGINE_CONFIG['tg_token'] else '🔴 Sin Token'}</div>
        <div>📱 <b>TikTok API:</b> {'🟢 Token Activo' if DEFAULT_ACCESS_TOKEN else '🔴 Sin Token'}</div>
    </div>
    """, unsafe_allow_html=True)

# ==============================================================================
# ENCABEZADO Y PESTAÑAS
# ==============================================================================

st.markdown("""
<div class="main-header">
    <h1>🎬 Manga Studio AI — Ultimate Multi-Language Edition</h1>
    <p>Memoria Visual (Retrato Fijo) • Multi-Idioma • SFX de Acción • Hook Viral • Doblaje Multi-Voz • TikTok Directo</p>
</div>
""", unsafe_allow_html=True)

saved_scripts_list = fetch_saved_scripts()
recent_eps_list = fetch_recent_episodes()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Motor Principal", "Modal T4 24/7")
m2.metric("Series Guardadas", len(saved_scripts_list))
m3.metric("Episodios Producidos", len(recent_eps_list))
m4.metric("Idiomas Soportados", "ES / EN / PT / FR / JA")

tabs = st.tabs([
    "🚀 Producción de Series",
    "✨ Biblioteca y Retrato de Personaje",
    "📚 Manga PDF a Clips (1 Min)",
    "📑 Historial de Producción"
])

# ------------------------------------------------------------------------------
# PESTAÑA 1: PRODUCCIÓN DE SERIES
# ------------------------------------------------------------------------------
with tabs[0]:
    col_l, col_r = st.columns([1.15, 1])

    with col_l:
        st.subheader("1️⃣ Selección de Serie Guardada")
        options = ["✍️ Escribir título personalizado..."] + [s["title"] for s in saved_scripts_list]
        sel_opt = st.selectbox("Elige un guion guardado:", options)

        sel_script = None
        if sel_opt != "✍️ Escribir título personalizado...":
            series_name = sel_opt
            sel_script = next((s for s in saved_scripts_list if s["title"] == sel_opt), None)
        else:
            series_name = st.text_input("Título de la Serie:", value="Jun-woo: Neo-Seúl")

        next_chap, prev_summary = get_series_continuity(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], series_name)
        char_desc = sel_script.get("character_prompt") if sel_script and sel_script.get("character_prompt") else "Joven guerrero anime, ojo cibernético brillante, chaqueta táctica negra"
        ref_portrait = sel_script.get("reference_image_url", "") if sel_script else ""
        saved_lang = sel_script.get("language", "Español") if sel_script else "Español"
        lang_idx = SUPPORTED_LANGUAGES.index(saved_lang) if saved_lang in SUPPORTED_LANGUAGES else 0

        c_info, c_img = st.columns([2, 1])
        with c_info:
            st.markdown(f"""
            <div class="script-box">
                <b>📌 Serie:</b> {series_name}  |  <b>🎬 Próximo:</b> Cap. #{next_chap}<br>
                <small style="color:#a5b4fc;"><b>🧬 Ficha Visual:</b> {char_desc}</small><br>
                <small style="color:#b8c0e0;"><b>📖 Último evento:</b> {prev_summary}</small>
            </div>
            """, unsafe_allow_html=True)
        with c_img:
            if ref_portrait:
                st.image(ref_portrait, caption="Retrato Fijo", use_container_width=True)

    with col_r:
        st.subheader("2️⃣ Idioma y Renderizado")
        prod_lang = st.selectbox("🌐 Idioma de Doblaje y Subtítulos:", SUPPORTED_LANGUAGES, index=lang_idx)
        premise_input = st.text_area(
            "Dirección del capítulo:",
            value=f"Continúa la trama de '{series_name}' después de: '{prev_summary}'.",
            height=85
        )

        b1, b2 = st.columns(2)
        if b1.button(f"🔥 Producir Aquí (Cap. #{next_chap})", type="primary", use_container_width=True):
            with st.status(f"Produciendo Capítulo #{next_chap} en {prod_lang}...", expanded=True) as status:
                out_vid, title, _ = produce_series_episode(
                    series_name=series_name,
                    custom_direction=premise_input,
                    config=ENGINE_CONFIG,
                    language_override=prod_lang,
                    progress_callback=st.write
                )
                status.update(label=f"✅ ¡{title} completado en {prod_lang}!", state="complete")
                st.video(out_vid)

        if b2.button("☁️ Producir en Segundo Plano (Render)", use_container_width=True):
            if send_remote_telegram_command(f"/producir {series_name}"):
                st.success("🚀 Orden enviada a Render. Puedes cerrar esta pestaña; tu bot te enviará el video listo.")
            else:
                st.info(f"💡 Envía `/producir {series_name}` en tu chat de Telegram.")

        if st.button("🤖 Ejecutar Ronda Completa de TODAS las Series Guardadas", use_container_width=True):
            send_remote_telegram_command("/producir_todas")
            st.success("🚀 Ronda multi-serie activada en segundo plano.")

# ------------------------------------------------------------------------------
# PESTAÑA 2: BIBLIOTECA Y RETRATO OFICIAL DE PERSONAJE
# ------------------------------------------------------------------------------
with tabs[1]:
    col_gen, col_lib = st.columns([1.1, 1])

    with col_gen:
        st.subheader("✍️ Crear Guion y Retrato Oficial del Protagonista")
        c_t, c_lang = st.columns([2, 1])
        new_title = c_t.text_input("Título Identificador de la Serie:", placeholder="Ej: El Heredero de las Sombras")
        script_lang = c_lang.selectbox("🌐 Idioma del Guion y Voces:", SUPPORTED_LANGUAGES, index=0)

        char_visual = st.text_input(
            "🧬 Ficha Visual del Protagonista (en el idioma que prefieras):",
            value="Chico anime de 20 años, cabello plateado desordenado, ojos violeta brillantes, gabardina oscura con capucha y katana en la espalda"
        )
        num_caps = st.slider("Capítulos a planificar:", 3, 10, 5)
        idea = st.text_area("Premisa y personajes de la historia:", height=100)

        if st.button("🧠 Generar Guion Estructurado", type="primary", use_container_width=True):
            if ENGINE_CONFIG["gemini_key"] and idea:
                with st.spinner(f"Creando escaleta en {script_lang}..."):
                    url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={ENGINE_CONFIG['gemini_key']}"
                    prompt_s = f"Crea un guion de {num_caps} capítulos conectados con cliffhangers para '{new_title}' escrito totalmente en {script_lang}. Premisa: {idea}. Detalla Capítulo 1 al {num_caps}."
                    res = requests.post(url_g, json={"contents": [{"parts": [{"text": prompt_s}]}]}, timeout=30)
                    st.session_state["draft_script"] = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    st.session_state["draft_title"] = new_title
                    st.session_state["draft_char"] = char_visual
                    st.session_state["draft_premise"] = idea
                    st.session_state["draft_lang"] = script_lang

        if "draft_script" in st.session_state:
            c_et, c_el = st.columns([2, 1])
            ed_title = c_et.text_input("Confirma Título:", value=st.session_state.get("draft_title", ""))
            ed_lang = c_el.selectbox("Idioma Guardado:", SUPPORTED_LANGUAGES, index=SUPPORTED_LANGUAGES.index(st.session_state.get("draft_lang", "Español")))
            ed_char = st.text_input("Confirma Ficha Visual:", value=st.session_state.get("draft_char", ""))
            ed_script = st.text_area("Guion:", value=st.session_state["draft_script"], height=200)
            if st.button("💾 Guardar Guion y Generar Retrato Oficial del Héroe", use_container_width=True):
                with st.spinner("Generando retrato fijo del protagonista y guardando en Supabase..."):
                    if save_script_with_portrait(ed_title, st.session_state.get("draft_premise", ""), ed_char, ed_script, ed_lang):
                        st.success(f"✅ '{ed_title}' ({ed_lang}) guardado con su retrato oficial.")
                        time.sleep(1)
                        st.rerun()

    with col_lib:
        st.subheader("📚 Series Guardadas en Biblioteca")
        for item in saved_scripts_list:
            c_next, l_sum = get_series_continuity(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], item["title"])
            item_lang = item.get("language", "Español")
            with st.expander(f"🎬 {item['title']} [{item_lang}] — (Va en el Cap. #{c_next})"):
                if item.get("reference_image_url"):
                    st.image(item["reference_image_url"], width=160)
                st.caption(f"**Ficha Visual:** {item.get('character_prompt', '')}")
                st.caption(f"**Último avance:** {l_sum}")
                st.markdown(item["script_content"])

# ------------------------------------------------------------------------------
# PESTAÑA 3: MANGA PDF A CLIPS DE 1 MINUTO
# ------------------------------------------------------------------------------
with tabs[2]:
    st.subheader("📚 Convertir PDF de Manga en Clips de 1 Minuto")
    col_p1, col_p2 = st.columns([2, 1])
    uploaded_pdf = col_p1.file_uploader("Sube tu archivo Manga (.pdf)", type=["pdf"])
    pdf_lang = col_p2.selectbox("🌐 Idioma de Narración del PDF:", SUPPORTED_LANGUAGES, index=0)

    manga_series = st.text_input("Título del Manga:", value="Manga PDF Viral")
    next_p, prev_m_ctx = get_series_continuity(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], manga_series)
    st.caption(f"Continuará desde la **Parte #{next_p}** en **{pdf_lang}**.")

    if uploaded_pdf and st.button("🎬 Animar PDF (SFX + Multi-Voz + Subtítulos TikTok)", type="primary"):
        if fitz and ENGINE_CONFIG["gemini_key"]:
            with st.status(f"Procesando páginas del manga en {pdf_lang}...", expanded=True) as status:
                doc = fitz.open(stream=uploaded_pdf.read(), filetype="pdf")
                page_clips = []
                running_ctx = prev_m_ctx
                mood_bgm = "anime epic battle"

                for i in range(len(doc)):
                    st.write(f"📖 Página {i+1}/{len(doc)}...")
                    pix = doc.load_page(i).get_pixmap(dpi=150)
                    img_p = f"/tmp/pdf_p_{i}.jpg"
                    pix.save(img_p)

                    with open(img_p, "rb") as f:
                        img_b64 = base64.b64encode(f.read()).decode("utf-8")
                    p_gem = f"Contexto previo (no repetir): '{running_ctx}'. Lee esta página de manga y devuelve JSON con diálogos en {pdf_lang} por rol (NARRADOR, PROTAGONISTA, CHICA, VILLANO) y sfx_query en inglés: {{\"sfx_query\": \"sword slash\", \"dialogues\": [{{\"role\": \"NARRADOR\", \"text\": \"...\"}}], \"music_mood\": \"anime battle\"}}"
                    url_g = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={ENGINE_CONFIG['gemini_key']}"
                    res = requests.post(url_g, json={"contents": [{"parts": [{"text": p_gem}, {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}]}]}, timeout=25)
                    v_data = json.loads(re.search(r"\{.*\}", res.json()["candidates"][0]["content"]["parts"][0]["text"], re.DOTALL).group(0))

                    dlgs = v_data.get("dialogues", [{"role": "NARRADOR", "text": "..."}])
                    running_ctx = " ".join([d["text"] for d in dlgs])
                    if i == 0:
                        mood_bgm = v_data.get("music_mood", mood_bgm)

                    aud_p = f"/tmp/pdf_a_{i}.mp3"
                    sfx_p = get_freesound_audio(ENGINE_CONFIG["fs_key"], v_data.get("sfx_query", "whoosh"), 1, 4, f"/tmp/pdf_sfx_{i}.mp3")
                    subs = asyncio.run(synthesize_multivoice_speech(dlgs, aud_p, language=pdf_lang))
                    anim_p = f"/tmp/pdf_anim_{i}.mp4"
                    ok_a, _ = animate_image_cascade(img_p, anim_p, cluster_urls)
                    clip_p = f"/tmp/pdf_c_{i}.mp4"
                    banner = f"{manga_series} - PT. {next_p}" if i == 0 else ""
                    build_scene_clip(anim_f if ok_a else img_p, aud_p, sfx_p, subs, clip_p, is_video=ok_a, hook_banner=banner)
                    c_obj = VideoFileClip(clip_p)
                    page_clips.append((clip_p, c_obj.duration))
                    c_obj.close()

                parts, batch, dur_acc = [], [], 0.0
                for cp, d in page_clips:
                    batch.append(cp)
                    dur_acc += d
                    if dur_acc >= 55.0:
                        parts.append(batch)
                        batch, dur_acc = [], 0.0
                if batch:
                    parts.append(batch)

                bgm_f = get_freesound_audio(ENGINE_CONFIG["fs_key"], mood_bgm, 25, 120, "/tmp/pdf_bgm.mp3")
                curr_p = next_p
                for b_files in parts:
                    s_clips = [VideoFileClip(f) for f in b_files]
                    concat = concatenate_videoclips(s_clips, method="compose")
                    if bgm_f and os.path.exists(bgm_f):
                        bgm = AudioFileClip(bgm_f).volumex(0.13)
                        bgm = bgm.loop(duration=concat.duration) if bgm.duration < concat.duration else bgm.subclip(0, concat.duration)
                        concat = concat.set_audio(CompositeAudioClip([concat.audio, bgm]))

                    final_p = f"/tmp/manga_parte_{curr_p}.mp4"
                    concat.write_videofile(final_p, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
                    for c in s_clips:
                        c.close()

                    save_episode_to_supabase(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], manga_series, curr_p, f"{manga_series} Pt.{curr_p}", running_ctx, "#manga #anime", "PDF", running_ctx, "ready")
                    update_series_progress(ENGINE_CONFIG["sb_url"], ENGINE_CONFIG["sb_key"], manga_series, curr_p, running_ctx)
                    st.video(final_p)
                    curr_p += 1

                status.update(label="✅ ¡Clips de 1 minuto generados!", state="complete")

# ------------------------------------------------------------------------------
# PESTAÑA 4: HISTORIAL
# ------------------------------------------------------------------------------
with tabs[3]:
    st.subheader("📑 Registro de Capítulos Producidos")
    if recent_eps_list:
        st.dataframe(recent_eps_list, use_container_width=True)
