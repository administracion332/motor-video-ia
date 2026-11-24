import os
import re
import json
import base64
import time
import urllib.request
import urllib.error
import subprocess

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")

def limpiar_texto(txt):
    if not txt:
        return ""
    txt = str(txt)
    txt = re.sub(r'[*_~`#<>|]', '', txt)
    txt = re.sub(r'[^\w\sáéíóúÁÉÍÓÚñÑüÜ.,:;!?¡¿$%()/-]', '', txt)
    return re.sub(r'\s+', ' ', txt).strip()

def escapar_drawtext(txt):
    txt = limpiar_texto(txt)
    txt = txt.replace("\\", "\\\\").replace(":", "\\:").replace("'", "").replace('"', '')
    return txt

def dividir_lineas(txt, max_chars=24):
    palabras = limpiar_texto(txt).split()
    lineas, actual = [], ""
    for p in palabras:
        if len(actual) + len(p) + 1 <= max_chars:
            actual = (actual + " " + p).strip()
        else:
            if actual:
                lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)
    return "\n".join(lineas[:3])

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
    if not prompt_edicion:
        return plan

    p_low = prompt_edicion.lower()
    if any(k in p_low for k in ["ia", "inteligencia artificial", "veo", "escena", "cocina", "demostrando", "creado"]):
        plan["generar_broll_ia"] = True

    if not GEMINI_API_KEY:
        plan["prompt_veo"] = f"Vertical 9:16 realistic commercial video showing {producto} in use in a bright modern kitchen, cinematic lighting, no text"
        return plan

    try:
        sys_prompt = f"""Analiza esta instruccion de edicion: "{prompt_edicion}" para el producto "{producto}".
Devuelve SOLO un JSON valido:
{{
  "generar_broll_ia": true o false,
  "posicion_broll": 1,
  "prompt_veo": "Detailed 5-second vertical 9:16 realistic commercial shot in English showing {producto} in everyday use in a modern home, natural lighting, smooth camera movement, no text or watermarks"
}}"""
        body = json.dumps({
            "contents": [{"parts": [{"text": sys_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
            data=body,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            plan.update(parsed)
    except Exception as e:
        print("Aviso Gemini Flash:", e)
        plan["prompt_veo"] = f"Vertical 9:16 realistic commercial video showing {producto} working in a bright modern kitchen, cinematic lighting, no text"

    return plan

def generar_clip_veo(prompt_veo, salida_mp4):
    if not GEMINI_API_KEY or not prompt_veo:
        return False
    try:
        print("Solicitando escena IA a Google Veo:", prompt_veo)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/veo-2.0-generate-001:predictLongRunning?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "instances": [{"prompt": prompt_veo}],
            "parameters": {"aspectRatio": "9:16", "durationSeconds": 5}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as res:
            op = json.loads(res.read().decode("utf-8"))
        op_name = op.get("name")
        if not op_name:
            return False

        for _ in range(18):
            time.sleep(8)
            poll_url = f"https://generativelanguage.googleapis.com/v1beta/{op_name}?key={GEMINI_API_KEY}"
            with urllib.request.urlopen(poll_url, timeout=30) as p_res:
                status = json.loads(p_res.read().decode("utf-8"))
            if status.get("done"):
                samples = status.get("response", {}).get("generateVideoResponse", {}).get("generatedSamples", [])
                if samples:
                    vid_uri = samples[0]["video"]["uri"]
                    sep = "&" if "?" in vid_uri else "?"
                    dl_url = f"{vid_uri}{sep}key={GEMINI_API_KEY}"
                    urllib.request.urlretrieve(dl_url, salida_mp4)
                    print("Escena IA descargada OK.")
                    return os.path.exists(salida_mp4) and os.path.getsize(salida_mp4) > 50000
                break
    except urllib.error.HTTPError as he:
        print("HTTP Error Veo IA:", he.code, he.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print("Error Veo IA:", e)
    return False

def normalizar_clip(entrada, salida, w=1080, h=1920):
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps=30,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", entrada,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
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
    precio = limpiar_texto(payload.get("precio", ""))
    gancho = limpiar_texto(payload.get("gancho", ""))
    guion = limpiar_texto(payload.get("guion", ""))
    prompt_edicion = payload.get("prompt_edicion", "")
    clip_ids = payload.get("clip_file_ids", [])

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
                "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", "-b:a", "128k", veo_norm
            ], check=True)
            pos = min(max(1, int(plan.get("posicion_broll", 1))), len(clips_norm))
            clips_norm.insert(pos, veo_norm)

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

    dur_out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", unido_mp4
    ]).decode().strip()
    duracion = float(dur_out or 15.0)
    inicio_cierre = max(0.0, duracion - 4.0)

    font_bold = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_bold):
        font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Subtítulos dinámicos repartidos a lo largo del video a la altura del pecho (y=h*0.60)
    # 1) Opacidad del video al 83% (capa negra al 17%: drawbox=color=black@0.17:t=fill) para resaltar letras sin fondo
    # 2) Marca de agua sutil CASA DION arriba a la derecha
    # 3) Subtítulos sin caja (box=0), blanco/amarillo alternados con contorno negro nítido en y=h*0.60
    filtros = [
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.17:t=fill",
        f"drawtext=fontfile={font_bold}:text='CASA DION':fontcolor=white@0.28:fontsize=34:x=w-tw-45:y=55"
    ]

    # Armar bloques de subtítulos (gancho + frases del guion + cierre con producto y precio)
    frases = []
    if gancho:
        frases.append((gancho, "yellow"))
    if guion:
        partes = [p.strip() for p in re.split(r'[.!?]+', guion) if len(p.strip()) > 4]
        for i, p in enumerate(partes[:4]):
            color = "white" if i % 2 == 0 else "yellow"
            frases.append((p, color))

    tiempo_util = max(3.0, inicio_cierre - 0.3)
    if frases:
        dur_cada = tiempo_util / len(frases)
        for idx, (texto_f, color_f) in enumerate(frases):
            t_ini = 0.2 + idx * dur_cada
            t_fin = t_ini + dur_cada - 0.08
            txt_esc = escapar_drawtext(dividir_lineas(texto_f, 22))
            if txt_esc:
                filtros.append(
                    f"drawtext=fontfile={font_bold}:text='{txt_esc}':fontcolor={color_f}:fontsize=56:"
                    f"borderw=4:bordercolor=black:shadowcolor=black@0.75:shadowx=2:shadowy=3:"
                    f"line_spacing=10:x=(w-text_w)/2:y=h*0.60:enable='between(t,{t_ini:.2f},{t_fin:.2f})'"
                )

    cierre_txt = escapar_drawtext(dividir_lineas(f"{producto} - {precio}" if precio else producto, 24))
    if cierre_txt:
        filtros.append(
            f"drawtext=fontfile={font_bold}:text='{cierre_txt}':fontcolor=yellow:fontsize=52:"
            f"borderw=4:bordercolor=black:shadowcolor=black@0.75:shadowx=2:shadowy=3:"
            f"line_spacing=10:x=(w-text_w)/2:y=h*0.60:enable='gte(t,{inicio_cierre:.2f})'"
        )

    vf_final = ",".join(filtros)

    ig_mp4 = "work/final_ig.mp4"
    wa_mp4 = "work/final_wa.mp4"

    # Full HD 1080x1920 nítido (~8-12 MB para no saturar Apps Script)
    subprocess.run([
        "ffmpeg", "-y", "-i", unido_mp4, "-vf", vf_final,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-maxrate", "3200k", "-bufsize", "6400k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", ig_mp4
    ], check=True)

    # Versión liviana HD 720x1280 para WhatsApp
    subprocess.run([
        "ffmpeg", "-y", "-i", ig_mp4, "-vf", "scale=720:1280",
        "-c:v", "libx264", "-preset", "fast", "-crf", "25", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", wa_mp4
    ], check=True)

    print(f"Pesos finales -> FullHD: {os.path.getsize(ig_mp4)/1024/1024:.1f} MB | WA: {os.path.getsize(wa_mp4)/1024/1024:.1f} MB")

    def a_b64(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    # Enviamos ig_tiktok_b64 y whatsapp_b64 (Apps Script usará el mismo FullHD para YouTube sin duplicar peso)
    ig_b64 = a_b64(ig_mp4)
    payload_vuelta = json.dumps({
        "accion": "guardar_videos",
        "id": row_id,
        "ig_tiktok_b64": ig_b64,
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
