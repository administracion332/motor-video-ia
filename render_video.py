import os
import json
import base64
import asyncio
import subprocess
import textwrap
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont

payload = json.loads(os.environ["PAYLOAD_JSON"])
apps_script_url = os.environ["APPS_SCRIPT_URL"]

row_id = payload["id"]
producto = payload.get("producto", "")
precio = payload.get("precio", "")
gancho = payload.get("gancho", "")
guion = payload.get("guion", "")
imagenes_urls = payload.get("imagenes", [])
clips_b64 = payload.get("clips_b64", [])

os.makedirs("work", exist_ok=True)

# 1. Generar voz en off natural (Voz argentina masculina: es-AR-TomasNeural | Femenina: es-AR-ElenaNeural)
async def crear_voz():
    communicate = edge_tts.Communicate(guion, "es-AR-TomasNeural", rate="+6%")
    await communicate.save("work/voz.mp3")

asyncio.run(crear_voz())

# Obtener duración exacta del audio
probe = subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", "work/voz.mp3"
]).decode().strip()
duracion_total = max(float(probe), 6.0)

# 2. Crear placa gráfica transparente con Gancho arriba y Producto/Precio abajo
def crear_overlay(width, height, filename):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    f_hook = ImageFont.truetype(font_path, int(height * 0.026))
    f_prod = ImageFont.truetype(font_path, int(height * 0.021))
    f_price = ImageFont.truetype(font_path, int(height * 0.032))

    # Caja superior (Gancho)
    if gancho:
        lines = textwrap.wrap(gancho.upper(), width=26 if width < height else 45)[:3]
        box_h = len(lines) * int(height * 0.034) + 36
        y0 = int(height * 0.08)
        draw.rounded_rectangle([40, y0, width - 40, y0 + box_h], radius=20, fill=(255, 204, 0, 235))
        for idx, line in enumerate(lines):
            draw.text((65, y0 + 18 + idx * int(height * 0.034)), line, font=f_hook, fill=(15, 23, 42, 255))

    # Caja inferior (Producto + Precio)
    y_bot = int(height * 0.78) if width < height else int(height * 0.76)
    draw.rounded_rectangle([40, y_bot, width - 40, y_bot + int(height * 0.11)], radius=20, fill=(15, 23, 42, 225))
    draw.text((65, y_bot + 16), producto[:38], font=f_prod, fill=(255, 255, 255, 255))
    draw.text((65, y_bot + 16 + int(height * 0.042)), precio, font=f_price, fill=(74, 222, 128, 255))

    img.save(filename)

crear_overlay(1080, 1920, "work/overlay_v.png")
crear_overlay(1920, 1080, "work/overlay_h.png")

# 3. Preparar segmentos visuales (Clips propios si es Híbrido + Fotos de Tiendanube)
segmentos = []

for i, b64_str in enumerate(clips_b64):
    clip_path = f"work/clip_{i}.mp4"
    with open(clip_path, "wb") as f:
        f.write(base64.b64decode(b64_str))
    norm_path = f"work/seg_clip_{i}.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-i", clip_path, "-t", "6",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", norm_path
    ], check=True)
    segmentos.append(norm_path)

# Completar con fotos de Tiendanube si faltan segundos o si es modo Automático
fotos_locales = []
for i, url in enumerate(imagenes_urls[:4]):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            p = f"work/img_{i}.jpg"
            with open(p, "wb") as f:
                f.write(r.content)
            fotos_locales.append(p)
    except Exception:
        pass

if not segmentos and not fotos_locales:
    img_fallback = Image.new("RGB", (1080, 1920), (30, 58, 76))
    img_fallback.save("work/fallback.jpg")
    fotos_locales.append("work/fallback.jpg")

if fotos_locales:
    seg_dur = max(duracion_total / max(len(fotos_locales), 1), 3.0)
    frames = int(seg_dur * 30)
    for i, img_p in enumerate(fotos_locales):
        norm_path = f"work/seg_img_{i}.mp4"
        # Fondo difuminado + producto centrado con zoom suave
        filtro = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
            f"[0:v]scale=980:980:force_original_aspect_ratio=decrease,"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=980x980[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30"
        )
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-t", str(seg_dur), "-i", img_p,
            "-filter_complex", filtro, "-c:v", "libx264", "-pix_fmt", "yuv420p", norm_path
        ], check=True)
        segmentos.append(norm_path)

# Unir segmentos en bucle hasta cubrir la duración del audio
with open("work/concat.txt", "w") as f:
    for _ in range(4):
        for s in segmentos:
            f.write(f"file '{os.path.abspath(s)}'\n")

subprocess.run([
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "work/concat.txt",
    "-t", str(duracion_total), "-c:v", "libx264", "-pix_fmt", "yuv420p", "work/base_vertical.mp4"
], check=True)

# 4. Exportar las 3 versiones finales
# Versión 1: Instagram Reels & TikTok (1080x1920 HD)
subprocess.run([
    "ffmpeg", "-y", "-i", "work/base_vertical.mp4", "-i", "work/overlay_v.png", "-i", "work/voz.mp3",
    "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-shortest",
    "work/ig_tiktok.mp4"
], check=True)

# Versión 2: YouTube Horizontal (1920x1080 con fondo difuminado elegante)
subprocess.run([
    "ffmpeg", "-y", "-i", "work/base_vertical.mp4", "-i", "work/overlay_h.png", "-i", "work/voz.mp3",
    "-filter_complex",
    "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=30:8[bg];"
    "[0:v]scale=-1:1080[fg];[bg][fg]overlay=(W-w)/2:0[vid];[vid][1:v]overlay=0:0[v]",
    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-shortest",
    "work/youtube.mp4"
], check=True)

# Versión 3: WhatsApp (720x1280 ultraliviano para Estados y chats)
subprocess.run([
    "ffmpeg", "-y", "-i", "work/ig_tiktok.mp4",
    "-vf", "scale=720:1280", "-r", "24", "-c:v", "libx264", "-crf", "29", "-b:a", "96k",
    "work/whatsapp.mp4"
], check=True)

# 5. Enviar los 3 videos terminados de vuelta a tu Google Drive vía Apps Script
def to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

respuesta = {
    "accion": "guardar_videos",
    "id": row_id,
    "ig_tiktok_b64": to_b64("work/ig_tiktok.mp4"),
    "youtube_b64": to_b64("work/youtube.mp4"),
    "whatsapp_b64": to_b64("work/whatsapp.mp4")
}

requests.post(apps_script_url, json=respuesta, timeout=120)
