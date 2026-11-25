import os
import re
import glob
import math
import wave
import struct
import json
import base64
import time
import urllib.request
import urllib.error
import subprocess
import whisper
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "").strip()
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "").strip()
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")
GCP_PROJECT_ID = "active-alcove-504517-h8"
GCP_LOCATION = "us-central1"

_cached_creds = None

def obtener_token_vertex():
    global _cached_creds
    if not GCP_SA_KEY:
        raise RuntimeError("Falta GCP_SA_KEY en los Secrets de GitHub.")
    if _cached_creds is None:
        info = json.loads(GCP_SA_KEY)
        _cached_creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _cached_creds.valid or _cached_creds.expired:
        _cached_creds.refresh(GoogleAuthRequest())
    return _cached_creds.token

def url_vertex(modelo, metodo="generateContent", location="us-central1"):
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{GCP_PROJECT_ID}"
        f"/locations/{location}/publishers/google/models/{modelo}:{metodo}"
    )

def limpiar_texto(txt):
    if not txt:
        return ""
    txt = str(txt)
    txt = re.sub(r'[*_~`#<>|]', '', txt)
    txt = re.sub(r'[^\w\sáéíóúÁÉÍÓÚñÑüÜ.,:;!?¡¿$%()/-]', '', txt)
    return re.sub(r'\s+', ' ', txt).strip()

def buscar_archivo_logo():
    prioritarios = [
        "casa dion.pdf.png", "logo.png", "logo.jpg", "logo.jpeg",
        "assets/logo.png", "assets/logo.jpg"
    ]
    for p in prioritarios:
        if os.path.exists(p):
            return p
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        encontrados = sorted(glob.glob(ext))
        if encontrados:
            return encontrados[0]
    return ""

def descargar_drive(file_id, destino):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        html_o_bin = resp.read()
        if b"confirm=" in html_o_bin[:4000] and b"<html" in html_o_bin[:4000].lower():
            m = re.search(rb'confirm=([0-9A-Za-z_-]+)', html_o_bin)
            token = m.group(1).decode() if m else "t"
            url2 = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
            with urllib.request.urlopen(urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"}), timeout=90) as r2:
                html_o_bin = r2.read()
        with open(destino, "wb") as f:
            f.write(html_o_bin)

def interpretar_prompt_edicion(prompt_edicion, producto):
    plan = {"generar_broll_ia": False, "prompt_veo": ""}
    if not prompt_edicion or not str(prompt_edicion).strip():
        return plan

    p_low = str(prompt_edicion).lower()
    palabras_clave = [
        "ia", "inteligencia artificial", "veo", "escena", "funcionando",
        "funcionamiento", "cocinando", "trabajando", "uso", "usando",
        "demostrando", "creado", "genera", "mostrando", "b-roll", "broll"
    ]
    if any(k in p_low for k in palabras_clave):
        plan["generar_broll_ia"] = True

    p_prod = producto.lower()
    if "robot" in p_prod or "chef" in p_prod or "procesadora" in p_prod:
        desc_visual = (
            "modern all-in-one thermal cooking food processor robot with a stainless steel bowl, "
            "digital touchscreen display on the base, transparent lid, and internal blades actively "
            "chopping and sautéing colorful fresh vegetables with gentle steam, NOT a stand mixer, NOT a planetary mixer"
        )
    else:
        desc_visual = f"modern {producto} actively operating and demonstrating its main function"

    plan["prompt_veo"] = (
        f"Vertical 9:16 photorealistic commercial close-up video of a {desc_visual} "
        f"on a clean modern kitchen countertop, smooth cinematic slow push-in camera movement, "
        f"bright natural daylight, sharp focus, completely silent video without any voice, speech, text, logos, or watermarks."
    )
    return plan

def extraer_frame_referencia(clips_norm, salida_jpg):
    clip_ref = clips_norm[1] if len(clips_norm) > 1 else clips_norm[0]
    subprocess.run([
        "ffmpeg", "-y", "-ss", "00:00:02", "-i", clip_ref,
        "-frames:v", "1", "-q:v", "2", salida_jpg
    ], check=True)
    if os.path.exists(salida_jpg) and os.path.getsize(salida_jpg) > 10000:
        with open(salida_jpg, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    return ""

def intentar_modelo_veo(modelo, prompt_veo, salida_mp4, img_ref_b64="", duracion_seg=6):
    token = obtener_token_vertex()
    url = url_vertex(modelo, "predictLongRunning", location="us-central1")
    instancia = {"prompt": prompt_veo}
    if img_ref_b64:
        instancia["image"] = {
            "bytesBase64Encoded": img_ref_b64,
            "mimeType": "image/jpeg"
        }

    payload = json.dumps({
        "instances": [instancia],
        "parameters": {
            "aspectRatio": "9:16",
            "sampleCount": 1,
            "generateAudio": False,
            "durationSeconds": duracion_seg
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        op = json.loads(res.read().decode("utf-8"))
    op_name = op.get("name")
    if not op_name:
        return False

    print(f"Operación Veo iniciada ({modelo}): {op_name}")
    poll_url = url_vertex(modelo, "fetchPredictOperation", location="us-central1")

    for _ in range(24):
        time.sleep(8)
        token = obtener_token_vertex()
        poll_body = json.dumps({"operationName": op_name}).encode("utf-8")
        poll_req = urllib.request.Request(
            poll_url,
            data=poll_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        with urllib.request.urlopen(poll_req, timeout=30) as p_res:
            status = json.loads(p_res.read().decode("utf-8"))

        if status.get("done"):
            if status.get("error"):
                print(f"Error interno en operación {modelo}:", status["error"])
                return False
            resp_obj = status.get("response", {})
            videos = resp_obj.get("videos", [])
            if videos:
                b64_vid = videos[0].get("bytesBase64Encoded", "")
                if b64_vid:
                    with open(salida_mp4, "wb") as f:
                        f.write(base64.b64decode(b64_vid))
                    print(f"Escena IA guardada desde {modelo} ({os.path.getsize(salida_mp4)} bytes).")
                    return os.path.exists(salida_mp4) and os.path.getsize(salida_mp4) > 50000
            return False
    return False

def generar_clip_veo(prompt_veo, salida_mp4, img_ref_b64="", duracion_seg=6):
    if not prompt_veo:
        return False
    for modelo in ["veo-3.1-fast-generate-001", "veo-3.1-generate-001", "veo-3.1-lite-generate-001"]:
        try:
            print(f"Probando generación IA en Vertex con {modelo}...")
            if intentar_modelo_veo(modelo, prompt_veo, salida_mp4, img_ref_b64, duracion_seg):
                return True
        except urllib.error.HTTPError as he:
            detalle = he.read().decode("utf-8", errors="ignore")
            print(f"HTTP Error en {modelo} ({he.code}): {detalle[:300]}")
            if img_ref_b64 and he.code == 400:
                try:
                    if intentar_modelo_veo(modelo, prompt_veo, salida_mp4, "", duracion_seg):
                        return True
                except Exception as e2:
                    print(f"Reintento texto en {modelo} fallo: {e2}")
        except Exception as e:
            print(f"Excepción en {modelo}: {e}")
    return False

def obtener_duracion(ruta_mp4):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", ruta_mp4
    ]
    out = subprocess.check_output(cmd).decode("utf-8").strip()
    return float(out)

def generar_musica_fondo_suave(duracion_voz, duracion_total, salida_wav):
    sr = 44100
    total_samples = int((duracion_total + 0.5) * sr)
    acordes = [
        [174.61, 220.00, 261.63, 329.63],
        [164.81, 196.00, 246.94, 293.66],
        [146.83, 174.61, 220.00, 261.63],
        [130.81, 164.81, 196.00, 246.94],
    ]
    dur_compas = 3.2
    with wave.open(salida_wav, "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for n in range(total_samples):
            t = n / sr
            idx_acorde = int(t / dur_compas) % len(acordes)
            t_local = t % dur_compas
            env_compas = math.sin(math.pi * min(1.0, t_local / dur_compas)) ** 0.5
            env_global = min(1.0, t / 1.2) * min(1.0, max(0.0, (duracion_total - t) / 1.5))

            # Sube suavemente el protagonismo de la musica durante la placa final del logo
            boost_outro = 1.0
            if t > duracion_voz:
                progreso_outro = min(1.0, (t - duracion_voz) / 0.6)
                boost_outro = 1.0 + 0.85 * progreso_outro

            freqs = acordes[idx_acorde]
            val = 0.0
            for f in freqs:
                val += math.sin(2.0 * math.pi * f * t) * 0.18
                val += math.sin(2.0 * math.pi * (f * 2.0) * t) * 0.035
            pulso = 0.85 + 0.15 * math.sin(2.0 * math.pi * (2.0 / dur_compas) * t_local)
            muestra = int(max(-32767, min(32767, val * env_compas * env_global * pulso * boost_outro * 11500)))
            frames.extend(struct.pack("<hh", muestra, muestra))
        wf.writeframes(frames)

def crear_outro_logo_3d(salida_mp4, font_bold, duracion=3.0):
    ruta_logo = buscar_archivo_logo()
    if ruta_logo:
        print(f"Preparando outro 3D profesional con logo: {ruta_logo}")
        # Componemos el logo centrado sobre fondo negro puro en 1080x1920 para pasarlo a Veo 3.1
        logo_card_jpg = "work/logo_card_9x16.jpg"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920",
            "-i", ruta_logo,
            "-filter_complex", "[1:v]scale=660:-1[lg];[0:v][lg]overlay=(W-w)/2:(H-h)/2",
            "-frames:v", "1", "-q:v", "2", logo_card_jpg
        ], check=True)

        with open(logo_card_jpg, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("utf-8")

        prompt_logo_3d = (
            "Vertical 9:16 sleek luxury commercial brand outro on a pure pitch-black background. "
            "The exact brand logo from the image smoothly emerges in glossy 3D metallic relief with subtle depth, "
            "a soft golden-white specular light sweep gliding across the emblem, gentle cinematic 3D camera push-in, "
            "high contrast, clean studio lighting, no extra text, completely silent."
        )

        veo_logo_raw = "work/veo_logo_raw.mp4"
        if generar_clip_veo(prompt_logo_3d, veo_logo_raw, logo_b64, duracion_seg=4):
            subprocess.run([
                "ffmpeg", "-y", "-i", veo_logo_raw,
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", str(duracion),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fade=t=in:st=0:d=0.45,setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "19",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                salida_mp4
            ], check=True)
            print("Outro 3D del logo generado con Veo 3.1 Fast OK.")
            return

        # Respaldo cinematografico en FFmpeg (zoom-in progresivo + fundido limpio)
        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(duracion), "-i", logo_card_jpg,
            "-f", "lavfi", "-t", str(duracion), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf",
            "zoompan=z='min(zoom+0.0018,1.14)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=90:s=1080x1920:fps=30,"
            "fade=t=in:st=0:d=0.5,format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "19",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            salida_mp4
        ], check=True)
    else:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={duracion}",
            "-f", "lavfi", "-t", str(duracion), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf",
            f"drawtext=fontfile={font_bold}:text='CASA DION':fontcolor=white:fontsize=86:x=(w-tw)/2:y=(h-th)/2-30,"
            f"drawtext=fontfile={font_bold}:text='HERRAMIENTAS Y HOGAR':fontcolor=#FFD700:fontsize=32:x=(w-tw)/2:y=(h-th)/2+55,"
            f"fade=t=in:st=0:d=0.5,format=yuv420p",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "19",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            salida_mp4
        ], check=True)

def seg_a_srt_time(segundos):
    ms = int(round((segundos - int(segundos)) * 1000))
    s = int(segundos)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

def transcribir_audio_local_whisper(video_mp4, ruta_srt, producto=""):
    audio_wav = "work/audio_whisper.wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", video_mp4, "-vn", "-ac", "1", "-ar", "16000", audio_wav
    ], check=True)

    print("Cargando modelo Whisper medium para transcripcion precisa...")
    model = whisper.load_model("medium")
    vocabulario_guia = (
        f"Hola, bienvenidos a Casa Dion. Hoy les presentamos {producto}. "
        f"Telefunken, modelo 9028, robot chef de cocina multifunción, pantalla digital, "
        f"vaso de acero inoxidable, vaporera, amasa, pica, cocina al vapor, cuotas sin interés, envíos a todo el país."
    )
    result = model.transcribe(
        audio_wav,
        language="es",
        word_timestamps=True,
        initial_prompt=vocabulario_guia,
        condition_on_previous_text=False
    )

    bloques = []
    idx_bloque = 1

    for seg in result.get("segments", []):
        palabras = seg.get("words", [])
        if not palabras:
            texto = seg.get("text", "").strip()
            if texto:
                inicio = seg_a_srt_time(seg["start"])
                fin = seg_a_srt_time(seg["end"])
                if idx_bloque % 2 == 0:
                    texto = f"{{\\c&H00FFFF&}}{texto}{{\\c&HFFFFFF&}}"
                bloques.append(f"{idx_bloque}\n{inicio} --> {fin}\n{texto}")
                idx_bloque += 1
            continue

        for i in range(0, len(palabras), 4):
            grupo = palabras[i:i+4]
            texto = " ".join(w["word"].strip() for w in grupo).strip()
            if not texto:
                continue
            inicio = seg_a_srt_time(grupo[0]["start"])
            fin = seg_a_srt_time(grupo[-1]["end"])
            if idx_bloque % 2 == 0:
                texto = f"{{\\c&H00FFFF&}}{texto}{{\\c&HFFFFFF&}}"
            bloques.append(f"{idx_bloque}\n{inicio} --> {fin}\n{texto}")
            idx_bloque += 1

    if bloques:
        srt_final = "\n\n".join(bloques) + "\n"
        with open(ruta_srt, "w", encoding="utf-8") as f:
            f.write(srt_final)
        print(f"Subtitulos Whisper medium generados OK ({len(bloques)} bloques).")
        return True

    print("No se detecto voz en el audio.")
    return False

def normalizar_clip(entrada, salida, w=1080, h=1920):
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps=30,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", entrada,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        salida
    ]
    subprocess.run(cmd, check=True)

def main():
    if not APPS_SCRIPT_URL:
        raise RuntimeError("Falta APPS_SCRIPT_URL en Secrets de GitHub.")

    with open(EVENT_PATH, "r", encoding="utf-8") as f:
        event = json.load(f)
    payload = event.get("client_payload", {})
    row_id = payload.get("id", "VIDEO")
    producto = limpiar_texto(payload.get("producto", "Casa Dion"))
    prompt_edicion = payload.get("prompt_edicion", "")
    clip_ids = payload.get("clip_file_ids", [])

    print(f"Iniciando render ID={row_id} | Producto={producto} | Clips={len(clip_ids)}")

    os.makedirs("work", exist_ok=True)
    clips_norm = []

    for idx, fid in enumerate(clip_ids):
        raw_p = f"work/raw_{idx}.mp4"
        norm_p = f"work/norm_{idx}.mp4"
        try:
            descargar_drive(fid, raw_p)
            normalizar_clip(raw_p, norm_p)
            clips_norm.append(norm_p)
        except Exception as e:
            print(f"Error en clip {idx}: {e}")

    if not clips_norm:
        raise RuntimeError("No se encontraron clips validos.")

    concat_txt = "work/lista.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for c in clips_norm:
            f.write(f"file '{os.path.abspath(c)}'\n")

    unido_real_mp4 = "work/unido_real.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c", "copy", unido_real_mp4
    ], check=True)

    unido_mp4 = unido_real_mp4

    plan = interpretar_prompt_edicion(prompt_edicion, producto)
    if plan.get("generar_broll_ia"):
        veo_raw = "work/veo_raw.mp4"
        veo_norm = "work/veo_norm.mp4"
        img_ref_b64 = extraer_frame_referencia(clips_norm, "work/ref_prod.jpg")
        if generar_clip_veo(plan.get("prompt_veo", ""), veo_raw, img_ref_b64):
            subprocess.run([
                "ffmpeg", "-y", "-i", veo_raw,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "19", veo_norm
            ], check=True)

            dur_real = obtener_duracion(unido_real_mp4)
            dur_veo = obtener_duracion(veo_norm)
            if dur_real > dur_veo + 3.0:
                inicio_broll = dur_real - dur_veo
                unido_broll_mp4 = "work/unido_broll.mp4"
                subprocess.run([
                    "ffmpeg", "-y", "-i", unido_real_mp4, "-i", veo_norm,
                    "-filter_complex",
                    f"[1:v]setpts=PTS-STARTPTS+{inicio_broll}/TB[v_ia];"
                    f"[0:v][v_ia]overlay=0:0:enable='between(t,{inicio_broll},{dur_real})'[vout]",
                    "-map", "[vout]", "-map", "0:a",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "19",
                    "-c:a", "copy", unido_broll_mp4
                ], check=True)
                unido_mp4 = unido_broll_mp4
                print(f"Escena IA aplicada como B-roll final desde {inicio_broll:.1f}s.")

    dur_contenido = obtener_duracion(unido_mp4)

    font_bold = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_bold):
        font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Crear outro negro 3D con logo al final (3.0s) y pegarlo al video
    outro_mp4 = "work/outro_logo.mp4"
    crear_outro_logo_3d(outro_mp4, font_bold, duracion=3.0)
    concat_outro = "work/lista_outro.txt"
    with open(concat_outro, "w", encoding="utf-8") as f:
        f.write(f"file '{os.path.abspath(unido_mp4)}'\n")
        f.write(f"file '{os.path.abspath(outro_mp4)}'\n")

    unido_con_outro = "work/unido_con_outro.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_outro,
        "-c", "copy", unido_con_outro
    ], check=True)
    unido_mp4 = unido_con_outro

    # Mezclar musica leve de fondo debajo de la voz y subiendo suavemente durante los 3s finales del logo
    dur_total = obtener_duracion(unido_mp4)
    bgm_wav = "work/bgm.wav"
    generar_musica_fondo_suave(dur_contenido, dur_total, bgm_wav)
    unido_musica_mp4 = "work/unido_musica.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-i", unido_mp4, "-i", bgm_wav,
        "-filter_complex",
        "[0:a]volume=1.05[voz];[1:a]volume=0.14[mus];[voz][mus]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        unido_musica_mp4
    ], check=True)
    unido_mp4 = unido_musica_mp4

    filtros = [
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.17:t=fill",
        f"drawtext=fontfile={font_bold}:text='CASA DION':fontcolor=white@0.28:fontsize=34:x=w-tw-45:y=55"
    ]

    ruta_srt = "work/subtitulos.srt"
    if transcribir_audio_local_whisper(unido_mp4, ruta_srt, producto):
        estilo_sub = (
            "FontName=Liberation Sans,FontSize=15,Bold=1,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,"
            "MarginL=35,MarginR=35,MarginV=125"
        )
        filtros.append(f"subtitles={ruta_srt}:force_style='{estilo_sub}'")

    vf_final = ",".join(filtros)

    ig_mp4 = "work/final_ig.mp4"
    yt_mp4 = "work/final_yt.mp4"
    wa_mp4 = "work/final_wa.mp4"

    # 1) Instagram / TikTok (1080x1920 vertical)
    subprocess.run([
        "ffmpeg", "-y", "-i", unido_mp4, "-vf", vf_final,
        "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-maxrate", "6500k", "-bufsize", "13000k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", ig_mp4
    ], check=True)

    # 2) YouTube (1920x1080 horizontal 16:9 con fondo difuminado elegante + video central nitido)
    subprocess.run([
        "ffmpeg", "-y", "-i", ig_mp4,
        "-filter_complex",
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=25:5,eq=brightness=-0.15[bg];"
        "[0:v]scale=-2:1080[fg];"
        "[bg][fg]overlay=(W-w)/2:0[vyt]",
        "-map", "[vyt]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-maxrate", "6000k", "-bufsize", "12000k",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", yt_mp4
    ], check=True)

    # 3) WhatsApp (720x1280 liviano)
    subprocess.run([
        "ffmpeg", "-y", "-i", ig_mp4, "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", wa_mp4
    ], check=True)

    print(
        f"Pesos finales -> IG/TikTok: {os.path.getsize(ig_mp4)/1024/1024:.1f} MB | "
        f"YouTube: {os.path.getsize(yt_mp4)/1024/1024:.1f} MB | "
        f"WA: {os.path.getsize(wa_mp4)/1024/1024:.1f} MB"
    )

    def a_b64(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    payload_vuelta = json.dumps({
        "accion": "guardar_videos",
        "id": row_id,
        "ig_tiktok_b64": a_b64(ig_mp4),
        "youtube_b64": a_b64(yt_mp4),
        "whatsapp_b64": a_b64(wa_mp4)
    }).encode("utf-8")

    req_back = urllib.request.Request(
        APPS_SCRIPT_URL,
        data=payload_vuelta,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req_back, timeout=120) as resp_back:
            print("Respuesta Apps Script:", resp_back.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        print("Error HTTP Apps Script:", he.code, he.read().decode("utf-8", errors="ignore"))
        raise

if __name__ == "__main__":
    main()
