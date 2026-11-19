import base64
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import requests
from faster_whisper import WhisperModel
from PIL import Image


def run_cmd(cmd):
    subprocess.run(cmd, check=True)


def get_video_duration(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    res = subprocess.check_output(cmd).decode("utf-8").strip()
    return max(float(res), 3.0)


def cargar_payload():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
            if isinstance(event_data.get("client_payload"), dict) and event_data["client_payload"]:
                return event_data["client_payload"]
        except Exception as e:
            print(f"Error leyendo GITHUB_EVENT_PATH: {e}")

    raw_payload = (
        os.environ.get("PAYLOAD")
        or os.environ.get("CLIENT_PAYLOAD")
        or (sys.argv[1] if len(sys.argv) > 1 else "{}")
    )
    return json.loads(raw_payload)


def descargar_archivo_drive(file_id, destino):
    session = requests.Session()
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = session.get(url, stream=True, timeout=60)

    # Si Google Drive pide confirmación por tamaño de archivo
    if "text/html" in resp.headers.get("Content-Type", ""):
        html = resp.text
        match = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
        if match:
            url = f"https://drive.google.com/uc?export=download&confirm={match.group(1)}&id={file_id}"
            resp = session.get(url, stream=True, timeout=60)
        else:
            url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
            resp = session.get(url, stream=True, timeout=60)

    resp.raise_for_status()
    with open(destino, "wb") as f:
        for chunk in resp.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    print(f"Clip descargado desde Drive: {destino.stat().st_size / (1024*1024):.2f} MB")


def format_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generar_subtitulos_ia(video_path, ass_path):
    print("Transcribiendo audio con Whisper AI para generar subtítulos...")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video_path), language="es", word_timestamps=True)

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,DejaVu Sans,58,&H00FFFFFF,&H0000FFFF,&H00111827,&HC8111827,-1,0,0,0,100,100,0,0,3,14,0,2,70,70,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]

    for seg in segments:
        words = getattr(seg, "words", None) or []
        if words:
            # Agrupa de a 4 palabras para lectura rápida estilo Reels/TikTok
            chunk = []
            for w in words:
                chunk.append(w)
                if len(chunk) == 4:
                    start_t = format_ass_time(chunk[0].start)
                    end_t = format_ass_time(chunk[-1].end)
                    txt = " ".join([x.word.strip() for x in chunk]).upper()
                    lines.append(f"Dialogue: 0,{start_t},{end_t},TikTok,,0,0,0,,{txt}\n")
                    chunk = []
            if chunk:
                start_t = format_ass_time(chunk[0].start)
                end_t = format_ass_time(chunk[-1].end)
                txt = " ".join([x.word.strip() for x in chunk]).upper()
                lines.append(f"Dialogue: 0,{start_t},{end_t},TikTok,,0,0,0,,{txt}\n")
        else:
            start_t = format_ass_time(seg.start)
            end_t = format_ass_time(seg.end)
            txt = seg.text.strip().upper()
            if txt:
                lines.append(f"Dialogue: 0,{start_t},{end_t},TikTok,,0,0,0,,{txt}\n")

    ass_path.write_text("".join(lines), encoding="utf-8")
    print(f"Subtítulos generados ({len(lines) - 1} bloques).")


def main():
    payload = cargar_payload()
    apps_script_url = os.environ.get("APPS_SCRIPT_URL") or os.environ.get("WEBHOOK_URL", "")

    row_id = str(payload.get("id") or "VID-TEST").strip()
    producto = str(payload.get("producto") or "Oferta Destacada").strip()
    precio = str(payload.get("precio") or "").strip()
    gancho = str(payload.get("gancho") or producto).strip()
    clip_file_ids = payload.get("clip_file_ids") or []

    if not clip_file_ids:
        raise RuntimeError("No se recibió ningún video propio en Clip_1. Sube tu clip antes de aprobar.")

    work = Path("/tmp/render_work")
    work.mkdir(parents=True, exist_ok=True)

    # 1. Descargar clip(s) grabado(s) por el usuario desde Google Drive
    raw_clips = []
    for idx, fid in enumerate(clip_file_ids):
        dest = work / f"raw_clip_{idx}.mp4"
        descargar_archivo_drive(fid, dest)
        raw_clips.append(dest)

    # Si subió 2 clips, los une primero
    if len(raw_clips) > 1:
        norm_clips = []
        for idx, rc in enumerate(raw_clips):
            nc = work / f"norm_{idx}.mp4"
            run_cmd([
                "ffmpeg", "-y", "-i", str(rc),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(nc)
            ])
            norm_clips.append(nc)
        concat_txt = work / "clips.txt"
        concat_txt.write_text("\n".join([f"file '{p}'" for p in norm_clips]), encoding="utf-8")
        clip_unido = work / "clip_unido.mp4"
        run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(clip_unido)])
    else:
        clip_unido = raw_clips[0]

    # 2. IA Paso 1: Recorte automático de silencios muertos (ritmo dinámico)
    clip_sin_silencios = work / "clip_cortado.mp4"
    try:
        print("Eliminando silencios automáticamente con auto-editor...")
        run_cmd(["auto-editor", str(clip_unido), "--margin", "0.15sec", "-o", str(clip_sin_silencios)])
    except Exception as e:
        print(f"Aviso auto-editor (se usa clip completo): {e}")
        clip_sin_silencios = clip_unido

    duracion = get_video_duration(clip_sin_silencios)
    inicio_cierre = max(duracion - 4.0, 3.6)

    # 3. IA Paso 2: Subtítulos automáticos estilo TikTok/Reels
    ass_path = work / "subtitulos.ass"
    tiene_subs = False
    try:
        generar_subtitulos_ia(clip_sin_silencios, ass_path)
        tiene_subs = True
    except Exception as e:
        print(f"Aviso subtítulos IA: {e}")

    # 4. Formato 1: Instagram Reels / TikTok (1080x1920) con audio mejorado + subtítulos + carteles
    gancho_txt = work / "gancho.txt"
    pie_txt = work / "pie.txt"
    gancho_txt.write_text(textwrap.fill(gancho[:65], width=26), encoding="utf-8")
    pie_txt.write_text(textwrap.fill(f"{producto[:40]}\n{precio}".strip(), width=30), encoding="utf-8")

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    out_ig = work / "out_ig.mp4"

    base_vf = (
        "split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[b];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p"
    )
    if tiene_subs:
        base_vf += f",ass={ass_path}"

    overlays = (
        f",drawtext=fontfile={font_path}:textfile={gancho_txt}:fontcolor=black:fontsize=42:"
        f"box=1:boxcolor=#FACC15@0.95:boxborderw=18:x=(w-text_w)/2:y=130:enable='between(t,0,3.5)',"
        f"drawtext=fontfile={font_path}:textfile={pie_txt}:fontcolor=white:fontsize=40:"
        f"box=1:boxcolor=#0F172A@0.90:boxborderw=20:x=(w-text_w)/2:y=h-text_h-160:enable='gte(t,{inicio_cierre:.2f})'"
    )

    audio_fx = "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11"

    try:
        run_cmd([
            "ffmpeg", "-y", "-i", str(clip_sin_silencios),
            "-filter_complex", base_vf + overlays,
            "-af", audio_fx,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", str(out_ig)
        ])
    except Exception as e:
        print(f"Reintentando sin drawtext por respaldo: {e}")
        run_cmd([
            "ffmpeg", "-y", "-i", str(clip_sin_silencios),
            "-filter_complex", base_vf,
            "-af", audio_fx,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", str(out_ig)
        ])

    # 5. Formato 2: YouTube Horizontal (1920x1080)
    out_yt = work / "out_yt.mp4"
    yt_filter = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[b];"
        "[fg]scale=-2:1080[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2"
    )
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig),
        "-filter_complex", yt_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy", str(out_yt)
    ])

    # 6. Formato 3: WhatsApp Estados / Chats ligero (720x1280)
    out_wa = work / "out_wa.mp4"
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig),
        "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", str(out_wa)
    ])

    # 7. Devolver los 3 MP4 editados a Apps Script -> Google Drive
    if not apps_script_url:
        raise RuntimeError("Falta configurar el secreto APPS_SCRIPT_URL en GitHub.")

    resp_payload = {
        "accion": "guardar_videos",
        "id": row_id,
        "ig_tiktok_b64": base64.b64encode(out_ig.read_bytes()).decode("utf-8"),
        "youtube_b64": base64.b64encode(out_yt.read_bytes()).decode("utf-8"),
        "whatsapp_b64": base64.b64encode(out_wa.read_bytes()).decode("utf-8"),
    }

    res = requests.post(apps_script_url, json=resp_payload, timeout=120, allow_redirects=True)
    print(f"Respuesta Apps Script ({res.status_code}): {res.text}")
    res.raise_for_status()


if __name__ == "__main__":
    main()
