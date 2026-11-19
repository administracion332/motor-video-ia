import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import requests
from PIL import Image


def run_cmd(cmd):
    subprocess.run(cmd, check=True)


def get_audio_duration(audio_path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    res = subprocess.check_output(cmd).decode("utf-8").strip()
    return max(float(res), 5.0)


def generate_tts(text, out_path):
    for voice in ["es-AR-TomasNeural", "es-MX-JorgeNeural"]:
        try:
            run_cmd(["edge-tts", "--voice", voice, "--text", text, "--write-media", str(out_path)])
            if out_path.exists() and out_path.stat().st_size > 1000:
                return
        except Exception as e:
            print(f"Fallo voz {voice}: {e}")
    raise RuntimeError("No se pudo generar el audio con edge-tts.")


def main():
    raw_payload = (
        os.environ.get("PAYLOAD")
        or os.environ.get("CLIENT_PAYLOAD")
        or (sys.argv[1] if len(sys.argv) > 1 else "{}")
    )
    payload = json.loads(raw_payload)
    apps_script_url = os.environ.get("APPS_SCRIPT_URL") or os.environ.get("WEBHOOK_URL", "")

    row_id = payload.get("id", "VID-TEST")
    producto = str(payload.get("producto", "Oferta Destacada")).strip()
    precio = str(payload.get("precio", "")).strip()
    gancho = str(payload.get("gancho", "")).strip()
    guion = str(payload.get("guion", "")).strip() or f"Conocé {producto}. {gancho}"
    imagenes_urls = payload.get("imagenes") or []
    clips_b64 = payload.get("clips_b64") or []

    work = Path("/tmp/render_work")
    work.mkdir(parents=True, exist_ok=True)

    # 1. Generar locución y medir duración exacta
    voz_path = work / "voz.mp3"
    generate_tts(guion, voz_path)
    duracion_total = round(get_audio_duration(voz_path) + 0.6, 2)
    print(f"Duración objetivo del video: {duracion_total}s")

    # 2. Preparar recursos visuales (clips propios o fotos de Tiendanube)
    visuales = []

    for idx, b64 in enumerate(clips_b64):
        try:
            clip_path = work / f"clip_{idx}.mp4"
            clip_path.write_bytes(base64.b64decode(b64))
            visuales.append(("video", clip_path))
        except Exception as e:
            print(f"Error decodificando clip {idx}: {e}")

    if not visuales:
        headers = {"User-Agent": "Mozilla/5.0"}
        for idx, url in enumerate(imagenes_urls[:4]):
            try:
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 1000:
                    raw_img = work / f"raw_{idx}"
                    raw_img.write_bytes(r.content)
                    clean_jpg = work / f"img_{idx}.jpg"
                    with Image.open(raw_img) as im:
                        im.convert("RGB").save(clean_jpg, "JPEG", quality=92)
                    visuales.append(("image", clean_jpg))
            except Exception as e:
                print(f"Error descargando imagen {url}: {e}")

    if not visuales:
        fallback_jpg = work / "fallback.jpg"
        Image.new("RGB", (1080, 1920), (20, 26, 38)).save(fallback_jpg, "JPEG")
        visuales.append(("image", fallback_jpg))

    # 3. Crear segmentos normalizados de duración exacta (evita bucles infinitos)
    seg_dur = round(duracion_total / len(visuales), 3)
    seg_files = []
    vf_base = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=#111827,"
        "fps=30,format=yuv420p"
    )

    for idx, (tipo, path) in enumerate(visuales):
        seg_out = work / f"seg_{idx}.mp4"
        if tipo == "video":
            cmd = [
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(path),
                "-t", str(seg_dur), "-vf", vf_base,
                "-c:v", "libx264", "-preset", "ultrafast", "-an", str(seg_out)
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(path),
                "-t", str(seg_dur), "-vf", vf_base,
                "-c:v", "libx264", "-preset", "ultrafast", "-an", str(seg_out)
            ]
        run_cmd(cmd)
        seg_files.append(seg_out)

    concat_list = work / "concat.txt"
    concat_list.write_text("\n".join([f"file '{p}'" for p in seg_files]), encoding="utf-8")

    base_916 = work / "base_916.mp4"
    run_cmd([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-t", str(duracion_total), "-c", "copy", str(base_916)
    ])

    # 4. Exportar Formato 1: Instagram Reels / TikTok (1080x1920) con textos + voz
    gancho_txt = work / "gancho.txt"
    pie_txt = work / "pie.txt"
    gancho_txt.write_text(textwrap.fill(gancho[:70], width=28), encoding="utf-8")
    pie_label = f"{producto[:40]}\n{precio}".strip()
    pie_txt.write_text(textwrap.fill(pie_label, width=32), encoding="utf-8")

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    out_ig = work / "out_ig.mp4"

    try:
        draw_filters = (
            f"drawtext=fontfile={font_path}:textfile={gancho_txt}:fontcolor=black:fontsize=44:"
            f"box=1:boxcolor=#FACC15@0.95:boxborderw=20:x=(w-text_w)/2:y=150,"
            f"drawtext=fontfile={font_path}:textfile={pie_txt}:fontcolor=white:fontsize=40:"
            f"box=1:boxcolor=#0F172A@0.88:boxborderw=20:x=(w-text_w)/2:y=h-text_h-180"
        )
        run_cmd([
            "ffmpeg", "-y", "-i", str(base_916), "-i", str(voz_path),
            "-t", str(duracion_total), "-vf", draw_filters,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_ig)
        ])
    except Exception as e:
        print(f"Renderizando sin drawtext por respaldo: {e}")
        run_cmd([
            "ffmpeg", "-y", "-i", str(base_916), "-i", str(voz_path),
            "-t", str(duracion_total),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_ig)
        ])

    # 5. Exportar Formato 2: YouTube Horizontal (1920x1080) con fondo difuminado
    out_yt = work / "out_yt.mp4"
    yt_filter = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[b];"
        "[fg]scale=-2:1080[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2"
    )
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig),
        "-t", str(duracion_total), "-filter_complex", yt_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy", str(out_yt)
    ])

    # 6. Exportar Formato 3: WhatsApp Estados ligero (720x1280)
    out_wa = work / "out_wa.mp4"
    run_cmd([
        "ffmpeg", "-y", "-i", str(out_ig),
        "-t", str(duracion_total), "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", str(out_wa)
    ])

    # 7. Enviar los 3 MP4 de vuelta a Apps Script -> Google Drive
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
