import os
import re
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
    # Extrae un frame del medio del segundo o tercer clip (donde suelen mostrar la caja/producto)
    clip_ref = clips_norm[1] if len(clips_norm) > 1 else clips_norm[0]
    subprocess.run([
        "ffmpeg", "-y", "-ss", "00:00:02", "-i", clip_ref,
        "-frames:v", "1", "-q:v", "2", salida_jpg
    ], check=True)
    if os.path.exists(salida_jpg) and os.path.getsize(salida_jpg) > 10000:
        with open(salida_jpg, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    return ""

def intentar_modelo_veo(modelo, prompt_veo, salida_mp4, img_ref_b64=""):
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
            "durationSeconds": 6
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

def generar_clip_veo(prompt_veo, salida_mp4, img_ref_b64=""):
    if not prompt_veo:
        return False
    for modelo in ["veo-3.1-fast-generate-001", "veo-3.1-generate-001", "veo-3.1-lite-generate-001"]:
        try:
            print(f"Probando generación IA en Vertex con {modelo} (con imagen ref: {bool(img_ref_b64)})...")
            if intentar_modelo_veo(modelo, prompt_veo, salida_mp4, img_ref_b64):
                return True
        except urllib.error.HTTPError as he:
            detalle = he.read().decode("utf-8", errors="ignore")
            print(f"HTTP Error en {modelo} ({he.code}): {detalle[:300]}")
            # Si rechaza image-to-video por seguridad/filtro, reintentamos solo con texto
            if img_ref_b64 and he.code == 400:
                try:
                    if intentar_modelo_veo(modelo, prompt_veo, salida_mp4, ""):
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

    # Primero unimos los clips reales de las chicas (con sus voces originales intactas)
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

    # Si corresponde escena IA, la ponemos AL FINAL superponiendo la imagen de Veo sobre los ultimos segundos de la voz de la chica
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
            # Reemplazamos los ultimos segundos de imagen por el clip de Veo, manteniendo SIEMPRE la voz de la chica
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
                print(f"Escena IA aplicada como B-roll final desde {inicio_broll:.1f}s manteniendo voz original.")

    font_bold = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_bold):
        font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

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
    wa_mp4 = "work/final_wa.mp4"

    subprocess.run([
        "ffmpeg", "-y", "-i", unido_mp4, "-vf", vf_final,
        "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-maxrate", "6500k", "-bufsize", "13000k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", ig_mp4
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", ig_mp4, "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", wa_mp4
    ], check=True)

    print(f"Pesos finales -> FullHD: {os.path.getsize(ig_mp4)/1024/1024:.1f} MB | WA: {os.path.getsize(wa_mp4)/1024/1024:.1f} MB")

    def a_b64(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    payload_vuelta = json.dumps({
        "accion": "guardar_videos",
        "id": row_id,
        "ig_tiktok_b64": a_b64(ig_mp4),
        "youtube_b64": "",
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
