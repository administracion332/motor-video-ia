import os
import re
import json
import base64
import time
import urllib.request
import urllib.error
import subprocess
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

def url_vertex(modelo, metodo="generateContent"):
    return (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT_ID}"
        f"/locations/{GCP_LOCATION}/publishers/google/models/{modelo}:{metodo}"
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
    plan = {"generar_broll_ia": False, "posicion_broll": 1, "prompt_veo": ""}
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

    plan["prompt_veo"] = (
        f"Vertical 9:16 photorealistic commercial close-up video showing a modern {producto} "
        f"actively cooking and chopping fresh vegetables in a bright modern kitchen, "
        f"smooth cinematic camera movement, natural lighting, no text or watermarks"
    )

    try:
        token = obtener_token_vertex()
        sys_prompt = (
            f'Analiza esta instruccion de edicion de video: "{prompt_edicion}" para el producto "{producto}". '
            f'Devuelve SOLO un JSON valido: '
            f'{{"generar_broll_ia": true, "posicion_broll": 1, '
            f'"prompt_veo": "Detailed 5-second vertical 9:16 photorealistic commercial video in English showing {producto} actively operating in a bright modern kitchen, smooth cinematic camera motion, natural lighting, no text"}}'
        )
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": sys_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }).encode("utf-8")
        req = urllib.request.Request(
            url_vertex("gemini-2.5-flash", "generateContent"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            plan.update(parsed)
    except Exception as e:
        print("Aviso Gemini Flash plan:", e)

    return plan

def intentar_modelo_veo(modelo, prompt_veo, salida_mp4):
    token = obtener_token_vertex()
    url = url_vertex(modelo, "predictLongRunning")
    payload = json.dumps({
        "instances": [{"prompt": prompt_veo}],
        "parameters": {
            "aspectRatio": "9:16",
            "sampleCount": 1
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
    poll_url = url_vertex(modelo, "fetchPredictOperation")

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
            print("Respuesta Veo sin bytesBase64Encoded:", json.dumps(resp_obj)[:400])
            return False
    return False

def generar_clip_veo(prompt_veo, salida_mp4):
    if not prompt_veo:
        return False

    for modelo in ["veo-2.0-generate-001", "veo-3.0-generate-001"]:
        try:
            print(f"Probando generación IA en Vertex con {modelo}: {prompt_veo}")
            if intentar_modelo_veo(modelo, prompt_veo, salida_mp4):
                return True
        except urllib.error.HTTPError as he:
            detalle = he.read().decode("utf-8", errors="ignore")
            print(f"HTTP Error en {modelo} ({he.code}): {detalle}")
        except Exception as e:
            print(f"Excepción en {modelo}: {e}")
    return False

def formatear_srt_alternado(srt_crudo):
    bloques = re.split(r'\n\s*\n', srt_crudo.strip())
    resultado = []
    for idx, b in enumerate(bloques):
        lineas = [l.strip() for l in b.splitlines() if l.strip()]
        if len(lineas) >= 3:
            num = lineas[0]
            tiempo = lineas[1]
            texto = " ".join(lineas[2:])
            if idx % 2 == 1:
                texto = f"{{\\c&H00FFFF&}}{texto}{{\\c&HFFFFFF&}}"
            resultado.append(f"{num}\n{tiempo}\n{texto}")
    return "\n\n".join(resultado) + "\n"

def transcribir_audio_a_srt(video_mp4, ruta_srt, producto=""):
    audio_mp3 = "work/audio_transcripcion.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-i", video_mp4, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", audio_mp3
    ], check=True)

    with open(audio_mp3, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt_srt = (
        f"Escuchá con precisión el audio de este video comercial en español argentino (sobre {producto}).\n"
        f"Transcribí EXACTAMENTE las palabras que dicen las personas en el video, sincronizadas segundo a segundo, en formato de subtítulos SRT estándar.\n\n"
        f"REGLAS OBLIGATORIAS:\n"
        f"1. Escribí ÚNICAMENTE lo que se escucha en el audio (no inventes texto ni agregues comentarios; si hay silencios, no pongas subtítulos en esos segundos).\n"
        f"2. Cada bloque debe ser corto (entre 3 y 6 palabras máximo, máximo 25 caracteres por línea) para que entre cómodo en pantalla vertical sin cortarse.\n"
        f"3. Respetá exactamente los tiempos de inicio y fin de cada frase hablada (formato HH:MM:SS,mmm --> HH:MM:SS,mmm).\n"
        f"4. Devolvé SOLO el contenido del archivo SRT válido, sin bloques markdown ``` ni texto extra."
    )

    body = json.dumps({
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt_srt},
                {"inlineData": {"mimeType": "audio/mp3", "data": audio_b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.1}
    }).encode("utf-8")

    try:
        token = obtener_token_vertex()
        req = urllib.request.Request(
            url_vertex("gemini-2.5-flash", "generateContent"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.loads(res.read().decode("utf-8"))
            srt_txt = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            srt_txt = re.sub(r'^```[a-zA-Z]*\n?', '', srt_txt)
            srt_txt = re.sub(r'\n?```$', '', srt_txt).strip()
            if "-->" in srt_txt:
                srt_final = formatear_srt_alternado(srt_txt)
                with open(ruta_srt, "w", encoding="utf-8") as f:
                    f.write(srt_final)
                print("Transcripción SRT generada OK:\n", srt_final[:500])
                return True
    except urllib.error.HTTPError as he:
        print("Error HTTP transcribiendo audio:", he.code, he.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print("Error transcribiendo audio:", e)
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

    plan = interpretar_prompt_edicion(prompt_edicion, producto)
    if plan.get("generar_broll_ia"):
        veo_raw = "work/veo_raw.mp4"
        veo_norm = "work/veo_norm.mp4"
        if generar_clip_veo(plan.get("prompt_veo", ""), veo_raw):
            subprocess.run([
                "ffmpeg", "-y", "-i", veo_raw, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest", "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-c:a", "aac", "-b:a", "160k", veo_norm
            ], check=True)
            pos = min(max(1, int(plan.get("posicion_broll", 1))), len(clips_norm))
            clips_norm.insert(pos, veo_norm)
            print(f"Escena IA insertada en posición {pos}.")

    if not clips_norm:
        raise RuntimeError("No se encontraron clips validos.")

    concat_txt = "work/lista.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for c in clips_norm:
            f.write(f"file '{os.path.abspath(c)}'\n")

    unido_mp4 = "work/unido.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c", "copy", unido_mp4
    ], check=True)

    font_bold = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_bold):
        font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    filtros = [
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.17:t=fill",
        f"drawtext=fontfile={font_bold}:text='CASA DION':fontcolor=white@0.28:fontsize=34:x=w-tw-45:y=55"
    ]

    ruta_srt = "work/subtitulos.srt"
    if transcribir_audio_a_srt(unido_mp4, ruta_srt, producto):
        estilo_sub = (
            "FontName=Liberation Sans,FontSize=14,Bold=1,"
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
