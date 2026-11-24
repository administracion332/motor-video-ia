import base64
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import requests
from faster_whisper import WhisperModel


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


def tiene_audio(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(video_path),
    ]
    res = subprocess.check_output(cmd).decode("utf-8").strip()
    return bool(res)


def cargar_payload():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8") as f:
            event_data = json.load(f)
        if isinstance(event_data.get("client_payload"), dict) and event_data["client_payload"]:
            return event_data["client_payload"]
    return json.loads(os.environ.get("PAYLOAD") or (sys.argv[1] if len(sys.argv) > 1 else "{}"))


def descargar_archivo_drive(file_id, destino):
    session = requests.Session()
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = session.get(url, stream=True, timeout=60)
    if "text/html" in resp.headers.get("Content-Type", ""):
        url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        resp = session.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(destino, "wb") as f:
        for chunk in resp.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)


def interpretar_prompt_edicion(gemini_key, producto, gancho, guion, prompt_edicion, cant_clips):
    """Gemini traduce tu instrucción libre en parámetros técnicos de edición + prompt visual para IA."""
    if not gemini_key:
        return {
            "cortar_silencios": True,
            "subtitulos": True,
            "color_subtitulo_ass": "&H00FFFFFF",
            "generar_broll_ia": False,
            "prompt_video_ia": "",
            "posicion_broll": 1,
            "mostrar_cartel_gancho": bool(gancho),
            "mostrar_cartel_precio": True,
        }

    instruccion = f"""
Eres un editor de video senior. El usuario subió {cant_clips} clip(s) reales del producto "{producto}".
Gancho sugerido: "{gancho}"
Guion: "{guion}"
INSTRUCCIÓN DE EDICIÓN DEL USUARIO: "{prompt_edicion or 'Edición dinámica estándar para Reels con subtítulos claros y ritmo ágil.'}"

Traduce esta instrucción en un JSON estricto con estas claves:
- "cortar_silencios": boolean (true salvo que pida mantener pausas)
- "subtitulos": boolean (true salvo que pida sin subtítulos)
- "color_subtitulo_ass": string en formato BGR ASS ("&H0000FFFF" para amarillo, "&H00FFFFFF" para blanco, "&H0000FF00" para verde)
- "generar_broll_ia": boolean (true si el usuario pide insertar escenas IA, demostración en uso, B-roll o video generado)
- "prompt_video_ia": string en INGLÉS describiendo una toma cinematográfica vertical 9:16 de 5 segundos mostrando "{producto}" en funcionamiento real intenso, iluminación comercial realista, sin texto en pantalla
- "posicion_broll": integer (índice donde insertar el video IA: 0 al inicio, 1 después del primer clip, etc.)
- "mostrar_cartel_gancho": boolean
- "mostrar_cartel_precio": boolean
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
    res = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": instruccion}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
        },
        timeout=30,
    )
    if res.status_code == 200:
        txt = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt)
    return {
        "cortar_silencios": True,
        "subtitulos": True,
        "color_subtitulo_ass": "&H0000FFFF",
        "generar_broll_ia": False,
        "prompt_video_ia": "",
        "posicion_broll": 1,
        "mostrar_cartel_gancho": bool(gancho),
        "mostrar_cartel_precio": True,
    }


def generar_clip_ia_veo(gemini_key, prompt_ingles, destino_mp4):
    """Genera un clip de demostración con Google Veo usando la misma API Key de Gemini."""
    if not gemini_key or not prompt_ingles:
        return False
    try:
        print(f"Generando toma B-Roll con IA: {prompt_ingles}")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/veo-2.0-generate-001:predictLongRunning?key={gemini_key}"
        payload = {
            "instances": [{"prompt": prompt_ingles}],
            "parameters": {"aspectRatio": "9:16", "personGeneration": "allow_adult"},
        }
        op_res = requests.post(url, json=payload, timeout=30)
        if op_res.status_code != 200:
            print(f"Veo no disponible en esta cuenta ({op_res.status_code}): {op_res.text[:200]}")
            return False

        op_name = op_res.json().get("name")
        for _ in range(24):
            time.sleep(5)
            poll = requests.get(f"https://generativelanguage.googleapis.com/v1beta/{op_name}?key={gemini_key}", timeout=20).json()
            if poll.get("done"):
                samples = poll.get("response", {}).get("generateVideoResponse", {}).get("generatedSamples", [])
                if samples:
                    video_uri = samples[0]["video"]["uri"]
                    vid_bytes = requests.get(f"{video_uri}&key={gemini_key}", timeout=60).content
                    destino_mp4.write_bytes(vid_bytes)
                    print("Clip IA generado y descargado OK.")
                    return True
                break
    except Exception as e:
        print(f"Error generando clip IA: {e}")
    return False


def format_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = min(int(round((seconds - int(seconds)) * 100)), 99)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generar_subtitulos_ia(video_path, ass_path, color_ass="&H0000FFFF"):
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video_path), language="es", word_timestamps=True)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,DejaVu Sans,58,{color_ass},&H00FFFFFF,&H00111827,&HC8111827,-1,0,0,0,100,100,0,0,3,14,0,2,70,70,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for seg in segments:
        words = getattr(seg, "words", None) or []
        chunk = []
        for w in words:
            chunk.append(w)
            if len(chunk) == 4:
                lines.append(
                    f"Dialogue: 0,{format_ass_time(chunk[0].start)},{format_ass_time(chunk[-1].end)},TikTok,,0,0,0,,"
                    f"{' '.join(x.word.strip() for x in chunk).upper()}\n"
                )
                chunk = []
        if chunk:
            lines.append(
                f"Dialogue: 0,{format_ass_time(chunk[0].start)},{format_ass_time(chunk[-1].end)},TikTok,,0,0,0,,"
                f"{' '.join(x.word.strip() for x in chunk).upper()}\n"
            )
    ass_path.write_text("".join(lines), encoding="utf-8")


def main():
    payload = cargar_payload()
    apps_script_url = os.environ.get("APPS_SCRIPT_URL", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    row_id = str(payload.get("id") or "VID-TEST").strip()
    producto = str(payload.get("producto") or "").strip()
    precio = str(payload.get("precio") or "").strip()
    gancho = str(payload.get("gancho") or "").strip()
    guion = str(payload.get("guion") or "").strip()
    prompt_edicion = str(payload.get("prompt_edicion") or "").strip()
    clip_file_ids = payload.get("clip_file_ids") or []

    if not clip_file_ids:
        raise RuntimeError("Falta subir al menos Clip_1.")

    work = Path("/tmp/render_work")
    work.mkdir(parents=True, exist_ok=True)

    # 1. Traducir el prompt de edición con Gemini
    plan = interpretar_prompt_edicion(gemini_key, producto, gancho, guion, prompt_edicion, len(clip_file_ids))
    print(f"Plan de edición IA: {plan}")

    # 2. Descargar clips reales del usuario
    clips_secuencia = []
    for idx, fid in enumerate(clip_file_ids):
        dest = work / f"raw_clip_{idx}.mp4"
        descargar_archivo_drive(fid, dest)
        clips_secuencia.append(dest)

    # 3. Si el prompt pidió escena de demostración con IA, generarla e intercalarla
    if plan.get("generar_broll_ia"):
        broll_path = work / "broll_ia.mp4"
        if generar_clip_ia_veo(gemini_key, plan.get("prompt_video_ia", ""), broll_path):
            pos = min(max(int(plan.get("posicion_broll", 1)), 0), len(clips_secuencia))
            clips_secuencia.insert(pos, broll_path)

    # 4. Normalizar y pegar todos los clips (reales + IA)
    vf_norm = (
        "split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[b];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p"
    )
    norm_clips = []
    for idx, rc in enumerate(clips_secuencia):
        nc = work / f"norm_{idx}.mp4"
        if tiene_audio(rc):
            run_cmd([
                "ffmpeg", "-y", "-i", str(rc), "-filter_complex", vf_norm,
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(nc)
            ])
        else:
            run_cmd([
                "ffmpeg", "-y", "-i", str(rc),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex", vf_norm, "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(nc)
            ])
        norm_clips.append(nc)

    concat_txt = work / "clips.txt"
    concat_txt.write_text("\n".join([f"file '{p}'" for p in norm_clips]), encoding="utf-8")
    clip_unido = work / "clip_unido.mp4"
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(clip_unido)])

    # 5. Corte de silencios si el plan lo indica
    clip_editado = clip_unido
    if plan.get("cortar_silencios", True):
        try:
            cortado = work / "clip_cortado.mp4"
            run_cmd(["auto-editor", str(clip_unido), "--margin", "0.15sec", "-o", str(cortado)])
            clip_editado = cortado
        except Exception:
            pass

    duracion = get_video_duration(clip_editado)
    inicio_cierre = max(duracion - 4.0, 3.6)

    # 6. Subtítulos dinámicos
    ass_path = work / "subtitulos.ass"
    tiene_subs = False
    if plan.get("subtitulos", True) and tiene_audio(clip_editado):
        try:
            generar_subtitulos_ia(clip_editado, ass_path, plan.get("color_subtitulo_ass", "&H0000FFFF"))
            tiene_subs = True
        except Exception:
            pass

    # 7. Render final en 3 formatos (IG/TikTok, YouTube y WhatsApp)
    out_ig = work / "out_ig.mp4"
    vf_final = "fps=30,format=yuv420p"
    if tiene_subs:
        vf_final += f",ass={ass_path}"

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if plan.get("mostrar_cartel_gancho") and gancho:
        g_txt = work / "gancho.txt"
        g_txt.write_text(textwrap.fill(gancho[:65], width=26), encoding="utf-8")
        vf_final += (
            f",drawtext=fontfile={font_path}:textfile={g_txt}:fontcolor=black:fontsize=42:"
            f"box=1:boxcolor=#FACC15@0.95:boxborderw=18:x=(w-text_w)/2:y=130:enable='between(t,0,3.5)'"
        )

    if plan.get("mostrar_cartel_precio") and (producto or precio):
        p_txt = work / "pie.txt"
        p_txt.write_text(textwrap.fill(f"{producto[:40]}\n{precio}".strip(), width=30), encoding="utf-8")
        vf_final += (
            f",drawtext=fontfile={font_path}:textfile={p_txt}:fontcolor=white:fontsize=40:"
            f"box=1:boxcolor=#0F172A@0.90:boxborderw=20:x=(w-text_w)/2:y=h-text_h-160:enable='gte(t,{inicio_cierre:.2f})'"
        )

    run_cmd([
        "ffmpeg", "-y", "-i", str(clip_editado), "-vf", vf_final,
        "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", str(out_ig)
    ])

    out_yt = work / "out_yt.mp4"
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig),
        "-filter_complex", "[0:v]split=2[bg][fg];[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[b];[fg]scale=-2:1080[f];[b][f]overlay=(W-w)/2:(H-h)/2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "copy", str(out_yt)
    ])

    out_wa = work / "out_wa.mp4"
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig), "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "27", "-c:a", "aac", "-b:a", "96k", str(out_wa)
    ])

    resp_payload = {
        "accion": "guardar_videos",
        "id": row_id,
        "ig_tiktok_b64": base64.b64encode(out_ig.read_bytes()).decode("utf-8"),
        "youtube_b64": base64.b64encode(out_yt.read_bytes()).decode("utf-8"),
        "whatsapp_b64": base64.b64encode(out_wa.read_bytes()).decode("utf-8"),
    }
    requests.post(apps_script_url, json=resp_payload, timeout=120, allow_redirects=True).raise_for_status()


if __name__ == "__main__":
    main()
