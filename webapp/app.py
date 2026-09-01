"""
Disparador de Conteúdo – Web App (PWA)
Agenda e dispara imagem/vídeo + texto para grupos WhatsApp e Instagram.
"""
import base64
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

def now_brasilia():
    """Retorna datetime atual no fuso de Brasília (UTC-3)."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3)))
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent.parent
IS_CLOUD  = os.getenv("RENDER") == "true" or os.getenv("RAILWAY_ENVIRONMENT") is not None

if os.getenv("DATA_DIR"):
    # Render com disco persistente montado em /var/data
    APP_DIR = Path(os.getenv("DATA_DIR")) / "Disparador"
elif IS_CLOUD:
    # Fallback sem disco (efêmero)
    APP_DIR = Path("/tmp/Disparador")
else:
    APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Disparador"

DB_PATH      = APP_DIR / "posts.db"
CONFIG_PATH  = APP_DIR / "config.json"
UPLOADS_DIR  = APP_DIR / "uploads"
APP_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO = {".mp4", ".mov", ".m4v"}

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_ENSURE_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # env vars sobrescrevem
    for env, key in [("EVO_URL","evo_url"),("EVO_TOKEN","evo_token"),
                     ("EVO_INSTANCE","evo_instance"),("IG_USER_ID","ig_user_id"),
                     ("IG_TOKEN","ig_token"),("APP_URL","app_url")]:
        val = os.getenv(env, "")
        if val:
            cfg[key] = val
    return cfg

def save_config(data: dict):
    current = load_config()
    current.update(data)
    CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS posts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        caption     TEXT    DEFAULT '',
        filename    TEXT    DEFAULT '',
        media_type  TEXT    DEFAULT 'image',
        wa_groups   TEXT    DEFAULT '[]',
        ig_feed     INTEGER DEFAULT 0,
        ig_stories  INTEGER DEFAULT 0,
        ig_reels    INTEGER DEFAULT 0,
        wa_status   INTEGER DEFAULT 0,
        scheduled_at TEXT,
        status      TEXT    DEFAULT 'pending',
        created_at  TEXT,
        sent_at     TEXT,
        result      TEXT    DEFAULT '{}',
        batch_id    TEXT    DEFAULT '',
        batch_title TEXT    DEFAULT ''
    )""")
    # migração: adiciona colunas se não existirem
    for col, defval in [("batch_id","''"), ("batch_title","''"), ("wa_status","0")]:
        try:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT DEFAULT {defval}")
        except Exception:
            pass
    conn.commit(); conn.close()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

init_db()

# ── WhatsApp / Evolution API ───────────────────────────────────────────────────
def _evo_headers(cfg):
    return {"apikey": cfg.get("evo_token", ""), "Content-Type": "application/json"}

def wa_get_groups(cfg) -> tuple[list, str]:
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return [], "Evolution API não configurada"
    try:
        r = requests.get(
            f"{base}/group/fetchAllGroups/{instance}?getParticipants=false",
            headers=_evo_headers(cfg), timeout=15
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        data = r.json()
        groups = [{"id": g.get("id",""), "name": g.get("subject", g.get("id",""))}
                  for g in (data if isinstance(data, list) else [])]
        return sorted(groups, key=lambda g: g["name"].lower()), ""
    except Exception as exc:
        return [], str(exc)

def _compress_image_b64(filepath: Path, max_kb: int = 400) -> tuple[str, str]:
    """Comprime imagem para no máximo max_kb KB e retorna (base64, mimetype)."""
    from PIL import Image
    import io as _io
    img = Image.open(filepath)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    # Reduz dimensão se muito grande
    max_dim = 1280
    if max(img.width, img.height) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    # Comprime até caber em max_kb
    quality = 85
    buf = _io.BytesIO()
    while quality >= 40:
        buf.seek(0); buf.truncate()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_kb * 1024:
            break
        quality -= 10
    b64 = base64.b64encode(buf.getvalue()).decode()
    return b64, "image/jpeg"

def _media_url(filename: str, cfg) -> str:
    """URL pública do arquivo para a Evolution API buscar diretamente."""
    app_url = cfg.get("app_url", "").rstrip("/")
    if not app_url:
        app_url = "https://social-midia.onrender.com"
    if filename.startswith("__lib__"):
        lib_filename = filename[len("__lib__"):]
        return f"{app_url}/api/library/file/{lib_filename}"
    return f"{app_url}/api/media/{filename}"

def wa_send_image(group_id: str, caption: str, filepath: Path, cfg, db_filename: str = "") -> tuple[bool, str]:
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    try:
        url = _media_url(db_filename or filepath.name, cfg)
        r = requests.post(
            f"{base}/message/sendMedia/{instance}",
            headers=_evo_headers(cfg),
            json={"number": group_id, "mediatype": "image",
                  "mimetype": "image/jpeg", "caption": caption, "media": url},
            timeout=120
        )
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

def wa_send_video(group_id: str, caption: str, filepath: Path, cfg, db_filename: str = "") -> tuple[bool, str]:
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    try:
        url = _media_url(db_filename or filepath.name, cfg)
        r = requests.post(
            f"{base}/message/sendMedia/{instance}",
            headers=_evo_headers(cfg),
            json={"number": group_id, "mediatype": "video",
                  "mimetype": "video/mp4", "caption": caption, "media": url},
            timeout=180
        )
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

def wa_send_text(group_id: str, text: str, cfg) -> tuple[bool, str]:
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    try:
        r = requests.post(
            f"{base}/message/sendText/{instance}",
            headers=_evo_headers(cfg),
            json={"number": group_id, "text": text},
            timeout=30
        )
        print(f"[sendText] {group_id} status={r.status_code} body={r.text[:200]}")
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

def wa_send(group_id: str, caption: str, filepath: Path, media_type: str, cfg, db_filename: str = "") -> tuple[bool, str]:
    if media_type == "video":
        return wa_send_video(group_id, caption, filepath, cfg, db_filename)
    return wa_send_image(group_id, caption, filepath, cfg, db_filename)

def wa_send_status(caption: str, filepath: Path, media_type: str, cfg, db_filename: str = "") -> tuple[bool, str]:
    """Publica no WhatsApp Status (Stories). Vai para todos os contatos."""
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    app_url  = cfg.get("app_url", "").rstrip("/")

    if db_filename:
        media_url = f"{app_url}/api/library/file/{db_filename}"
    else:
        media_url = f"{app_url}/api/media/{filepath.name}"

    stype = "video" if media_type == "video" else "image"
    body = {
        "type":        stype,
        "content":     media_url,
        "caption":     caption,
        "allContacts": True,
    }
    try:
        r = requests.post(
            f"{base}/message/sendStatus/{instance}",
            headers=_evo_headers(cfg),
            json=body,
            timeout=120
        )
        print(f"[sendStatus] status={r.status_code} body={r.text[:200]}")
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

# ── Instagram Graph API ────────────────────────────────────────────────────────
IG_BASE = "https://graph.instagram.com/v21.0"

def _ig_params(cfg):
    return {"access_token": cfg.get("ig_token", "")}

def ig_media_url(filename: str, cfg) -> str:
    base = cfg.get("app_url", "").rstrip("/")
    return f"{base}/api/media/{filename}"

def ig_create_container(media_url: str, caption: str, dest: str, cfg) -> tuple[str, str]:
    """
    dest: 'feed' | 'stories' | 'reels'
    Retorna (container_id, erro)
    """
    ig_id  = cfg.get("ig_user_id", "")
    params = _ig_params(cfg)
    is_video = any(media_url.lower().endswith(ext) for ext in (".mp4",".mov",".m4v"))

    body: dict = {}
    if dest == "feed":
        if is_video:
            body = {"video_url": media_url, "media_type": "VIDEO", "caption": caption}
        else:
            body = {"image_url": media_url, "caption": caption}
    elif dest == "stories":
        if is_video:
            body = {"video_url": media_url, "media_type": "STORIES"}
        else:
            body = {"image_url": media_url, "media_type": "STORIES"}
    elif dest == "reels":
        body = {"video_url": media_url, "media_type": "REELS", "caption": caption}

    try:
        r = requests.post(f"{IG_BASE}/{ig_id}/media", params=params, json=body, timeout=30)
        data = r.json()
        if "id" in data:
            return data["id"], ""
        return "", data.get("error", {}).get("message", str(data))
    except Exception as exc:
        return "", str(exc)

def ig_wait_ready(container_id: str, cfg, max_wait=300) -> tuple[bool, str]:
    """Aguarda o container de vídeo ficar pronto (status FINISHED)."""
    params = _ig_params(cfg)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{IG_BASE}/{container_id}",
                params={**params, "fields": "status_code,status"},
                timeout=15
            )
            data = r.json()
            code = data.get("status_code", "")
            if code == "FINISHED":
                return True, ""
            if code == "ERROR":
                return False, data.get("status", "Erro no processamento")
        except Exception:
            pass
        time.sleep(10)
    return False, "Timeout aguardando processamento do vídeo"

def ig_publish(container_id: str, cfg) -> tuple[bool, str]:
    ig_id  = cfg.get("ig_user_id", "")
    params = _ig_params(cfg)
    try:
        r = requests.post(
            f"{IG_BASE}/{ig_id}/media_publish",
            params=params,
            json={"creation_id": container_id},
            timeout=30
        )
        data = r.json()
        if "id" in data:
            return True, ""
        return False, data.get("error", {}).get("message", str(data))
    except Exception as exc:
        return False, str(exc)

def ig_post(media_url: str, caption: str, dest: str, cfg, is_video: bool) -> tuple[bool, str]:
    cid, err = ig_create_container(media_url, caption, dest, cfg)
    if err:
        return False, f"Container: {err}"
    if is_video:
        ok, err = ig_wait_ready(cid, cfg)
        if not ok:
            return False, f"Processamento: {err}"
    return ig_publish(cid, cfg)

# ── Post Processor ─────────────────────────────────────────────────────────────
def process_post(post_id: int):
    cfg = load_config()
    conn = db()
    row  = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close(); return

    conn.execute("UPDATE posts SET status='sending' WHERE id=?", (post_id,))
    conn.commit(); conn.close()

    caption    = row["caption"] or ""
    filename   = row["filename"] or ""
    media_type = row["media_type"] or "image"
    wa_groups  = json.loads(row["wa_groups"] or "[]")
    ig_feed    = bool(row["ig_feed"])
    ig_stories = bool(row["ig_stories"])
    ig_reels   = bool(row["ig_reels"])
    wa_status  = bool(row["wa_status"]) if "wa_status" in row.keys() else False
    # Suporte a arquivo da biblioteca
    if filename.startswith("__lib__"):
        lib_filename = filename[len("__lib__"):]
        filepath = LIBRARY_DIR / lib_filename
    else:
        filepath = UPLOADS_DIR / filename
    is_video   = media_type == "video"

    result = {"wa": {}, "ig": {}}
    errors = []

    def _save_partial():
        c = db()
        c.execute("UPDATE posts SET result=? WHERE id=?",
                  (json.dumps(result, ensure_ascii=False), post_id))
        c.commit(); c.close()

    # WhatsApp groups
    for i, g in enumerate(wa_groups):
        gid  = g.get("id", g) if isinstance(g, dict) else g
        name = g.get("name", gid) if isinstance(g, dict) else gid
        result["wa"][name] = "⏳ enviando..."
        _save_partial()
        print(f"[post {post_id}] Enviando para {name} ({i+1}/{len(wa_groups)})")
        ok, err = wa_send(gid, caption, filepath, media_type, cfg, db_filename=filename)
        result["wa"][name] = "ok" if ok else err
        _save_partial()
        if not ok:
            errors.append(f"WA {name}: {err}")
            print(f"[post {post_id}] ERRO {name}: {err}")
        else:
            print(f"[post {post_id}] OK {name}")
        if i < len(wa_groups) - 1:
            time.sleep(1)

    # Instagram
    media_url = ig_media_url(filename, cfg)
    if ig_feed:
        ok, err = ig_post(media_url, caption, "feed", cfg, is_video)
        result["ig"]["feed"] = "ok" if ok else err
        if not ok: errors.append(f"IG Feed: {err}")

    if ig_stories:
        ok, err = ig_post(media_url, "", "stories", cfg, is_video)
        result["ig"]["stories"] = "ok" if ok else err
        if not ok: errors.append(f"IG Stories: {err}")

    if ig_reels:
        ok, err = ig_post(media_url, caption, "reels", cfg, is_video)
        result["ig"]["reels"] = "ok" if ok else err
        if not ok: errors.append(f"IG Reels: {err}")

    # WhatsApp Status
    if wa_status:
        ok, err = wa_send_status(caption, filepath, media_type, cfg, db_filename=lib_filename if filename.startswith("__lib__") else "")
        result.setdefault("wa_status", {})["status"] = "ok" if ok else err
        if not ok: errors.append(f"WA Status: {err}")

    final_status = "partial" if errors and (result["wa"] or result["ig"]) else \
                   "failed"  if errors else "sent"

    conn = db()
    conn.execute("""UPDATE posts SET status=?, sent_at=?, result=? WHERE id=?""",
                 (final_status,
                  now_brasilia().strftime("%Y-%m-%dT%H:%M:%S"),
                  json.dumps(result, ensure_ascii=False),
                  post_id))
    conn.commit(); conn.close()

# ── Scheduler Thread ───────────────────────────────────────────────────────────
def _scheduler_loop():
    while True:
        try:
            now  = now_brasilia().strftime("%Y-%m-%dT%H:%M")
            conn = db()
            rows = conn.execute(
                "SELECT id FROM posts WHERE status='pending' AND scheduled_at<=?",
                (now + ":59",)
            ).fetchall()
            conn.close()
            for row in rows:
                threading.Thread(target=process_post, args=(row["id"],), daemon=True).start()
        except Exception as exc:
            print(f"[scheduler] {exc}")
        time.sleep(30)

threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler").start()

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ── Media serve ────────────────────────────────────────────────────────────────
@app.route("/api/media/<filename>")
def api_media(filename):
    safe = Path(filename).name  # evita path traversal
    path = UPLOADS_DIR / safe
    if not path.exists():
        return "Not found", 404
    ext = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/mp4", ".m4v": "video/mp4"}
    mimetype = mime_map.get(ext, "application/octet-stream")
    return send_file(str(path), mimetype=mimetype, conditional=False)

# ── Upload ─────────────────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Nenhum arquivo enviado"})

    ext = Path(f.filename).suffix.lower()
    if ext in ALLOWED_IMAGE:
        media_type = "image"
    elif ext in ALLOWED_VIDEO:
        media_type = "video"
    else:
        return jsonify({"ok": False, "error": f"Formato não suportado: {ext}"})

    filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    f.save(str(UPLOADS_DIR / filename))
    return jsonify({"ok": True, "filename": filename, "media_type": media_type})

# ── Library ────────────────────────────────────────────────────────────────────
LIBRARY_DIR = APP_DIR / "library"
LIBRARY_DIR.mkdir(exist_ok=True)

@app.route("/api/library", methods=["GET"])
def api_library_list():
    files = []
    for p in sorted(LIBRARY_DIR.iterdir(), key=lambda f: -f.stat().st_mtime):
        ext = p.suffix.lower()
        if ext in ALLOWED_IMAGE:
            mtype = "image"
        elif ext in ALLOWED_VIDEO:
            mtype = "video"
        else:
            continue
        size_kb = round(p.stat().st_size / 1024)
        files.append({"filename": p.name, "media_type": mtype, "size_kb": size_kb})
    return jsonify({"ok": True, "files": files})

@app.route("/api/library/debug/<filename>")
def api_library_debug(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    exists = path.exists()
    size = path.stat().st_size if exists else -1
    try:
        with open(str(path), "rb") as fh:
            head = fh.read(16)
        head_hex = head.hex()
    except Exception as e:
        head_hex = str(e)
    return jsonify({"exists": exists, "size": size, "path": str(path), "head_hex": head_hex,
                    "library_dir": str(LIBRARY_DIR), "library_dir_exists": LIBRARY_DIR.exists()})

@app.route("/api/library/upload", methods=["POST"])
def api_library_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Nenhum arquivo"})
    ext = Path(f.filename).suffix.lower()
    if ext in ALLOWED_IMAGE:
        mtype = "image"
    elif ext in ALLOWED_VIDEO:
        mtype = "video"
    else:
        return jsonify({"ok": False, "error": "Formato não suportado"})
    filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg" if mtype == "image" else f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    dest = LIBRARY_DIR / filename
    if mtype == "image":
        try:
            from PIL import Image
            import io
            img = Image.open(f)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # Redimensiona se maior que 1200px em qualquer dimensão
            img.thumbnail((1200, 1200), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            content = buf.getvalue()
        except Exception as e:
            print(f"[library_upload] compressão falhou: {e}, salvando original")
            f.seek(0)
            content = f.read()
    else:
        content = f.read()
    print(f"[library_upload] filename={filename} content_len={len(content)} dest={dest}")
    with open(str(dest), "wb") as fh:
        fh.write(content)
    saved_size = dest.stat().st_size
    print(f"[library_upload] saved_size={saved_size}")
    return jsonify({"ok": True, "filename": filename, "media_type": mtype, "size": saved_size})

@app.route("/api/library/<filename>", methods=["DELETE"])
def api_library_delete(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})

@app.route("/api/library/file/<filename>")
def api_library_file(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    if not path.exists():
        return "Not found", 404
    ext = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/mp4", ".m4v": "video/mp4"}
    mimetype = mime_map.get(ext, "application/octet-stream")
    with open(str(path), "rb") as f:
        data = f.read()
    return Response(data, mimetype=mimetype, headers={"Content-Length": str(len(data))})

# ── Test ───────────────────────────────────────────────────────────────────────
@app.route("/api/wa/test-text")
def api_wa_test_text():
    """Envia texto de teste para o primeiro grupo disponível."""
    cfg = load_config()
    groups, err = wa_get_groups(cfg)
    if err or not groups:
        return jsonify({"ok": False, "error": err or "Sem grupos"})
    group = groups[0]
    ok, err2 = wa_send_text(group["id"], "🔧 Teste de conexão - pode ignorar", cfg)
    return jsonify({"ok": ok, "group": group["name"], "error": err2})

# ── Groups ─────────────────────────────────────────────────────────────────────
@app.route("/api/wa/groups")
def api_wa_groups():
    cfg    = load_config()
    groups, err = wa_get_groups(cfg)
    if err and not groups:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "groups": groups})

# ── Posts ──────────────────────────────────────────────────────────────────────
@app.route("/api/posts", methods=["GET"])
def api_posts():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM posts ORDER BY scheduled_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/posts", methods=["POST"])
def api_create_post():
    data = request.get_json() or {}
    filename   = data.get("filename", "")
    media_type = data.get("media_type", "image")
    caption    = data.get("caption", "")
    wa_groups  = data.get("wa_groups", [])
    ig_feed    = int(bool(data.get("ig_feed")))
    ig_stories = int(bool(data.get("ig_stories")))
    ig_reels   = int(bool(data.get("ig_reels")))
    wa_status  = int(bool(data.get("wa_status")))
    scheduled_at = data.get("scheduled_at", "")

    # Suporte a arquivo da biblioteca
    library = data.get("library", "")
    print(f"[create_post] filename={filename!r} library={library!r}")
    if library:
        filename = f"__lib__{library}"

    if not filename:
        return jsonify({"ok": False, "error": "Nenhum arquivo selecionado"})
    if not wa_groups and not ig_feed and not ig_stories and not ig_reels and not wa_status:
        return jsonify({"ok": False, "error": "Selecione ao menos um destino"})
    if not scheduled_at:
        return jsonify({"ok": False, "error": "Defina o horário de envio"})

    repeat_days   = max(1, min(int(data.get("repeat_days",   1) or 1), 30))
    times_per_day = max(1, min(int(data.get("times_per_day", 1) or 1), 24))
    batch_title   = data.get("batch_title", "").strip()

    conn = db()
    created_at = datetime.now().isoformat(timespec="seconds")
    wa_groups_json = json.dumps(wa_groups)
    batch_id = str(uuid.uuid4())

    from datetime import timedelta
    try:
        base_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        conn.close()
        return jsonify({"ok": False, "error": "Horário inválido"})

    interval_minutes = int(24 * 60 / times_per_day)
    total = repeat_days * times_per_day

    first_id = None
    for i in range(total):
        sched = (base_dt + timedelta(minutes=i * interval_minutes)).isoformat(timespec="minutes")
        cur = conn.execute("""INSERT INTO posts
            (caption,filename,media_type,wa_groups,ig_feed,ig_stories,ig_reels,wa_status,
             scheduled_at,status,created_at,batch_id,batch_title)
            VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (caption, filename, media_type, wa_groups_json,
             ig_feed, ig_stories, ig_reels, wa_status, sched, created_at, batch_id, batch_title))
        if i == 0:
            first_id = cur.lastrowid

    conn.commit(); conn.close()
    print(f"[create_post] total={total} batch_id={batch_id} batch_title={batch_title!r}")
    return jsonify({"ok": True, "id": first_id, "count": total, "batch_id": batch_id})

@app.route("/api/posts/<int:post_id>/send", methods=["POST"])
def api_send_now(post_id):
    """Disparo manual imediato."""
    conn = db()
    row = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "Post não encontrado"})
    if row["status"] == "sending":
        return jsonify({"ok": False, "error": "Já está sendo enviado"})
    threading.Thread(target=process_post, args=(post_id,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
def api_delete_post(post_id):
    conn = db()
    row = conn.execute("SELECT filename, status FROM posts WHERE id=?", (post_id,)).fetchone()
    if row and row["status"] == "pending":
        try:
            (UPLOADS_DIR / row["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/posts/batch/<batch_id>", methods=["DELETE"])
def api_delete_batch(batch_id):
    conn = db()
    conn.execute("DELETE FROM posts WHERE batch_id=?", (batch_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/posts/stream")
def api_posts_stream():
    """SSE – notifica mudanças de status em tempo real."""
    def generate():
        last = {}
        for _ in range(600):  # max 10 min
            conn = db()
            rows = conn.execute(
                "SELECT id, status, sent_at, result FROM posts WHERE status IN ('sending','pending')"
            ).fetchall()
            conn.close()
            for r in rows:
                key = f"{r['id']}-{r['status']}-{r['sent_at']}"
                if last.get(r["id"]) != key:
                    last[r["id"]] = key
                    yield f"data: {json.dumps(dict(r))}\n\n"
            time.sleep(2)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── Config ─────────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET","POST"])
def api_config():
    if request.method == "POST":
        save_config(request.get_json() or {})
        return jsonify({"ok": True})
    cfg = load_config()
    # Não expõe token completo, só indica se existe
    safe = {k: v for k, v in cfg.items()}
    return jsonify(safe)

@app.route("/api/config/evo-test", methods=["POST"])
def api_evo_test():
    data     = request.get_json() or {}
    base     = data.get("evo_url","").rstrip("/")
    token    = data.get("evo_token","")
    instance = data.get("evo_instance","")
    if not all([base, token, instance]):
        return jsonify({"ok": False, "error": "Preencha URL, Token e Instância"})
    try:
        r = requests.get(f"{base}/instance/connectionState/{instance}",
                         headers={"apikey": token}, timeout=10)
        if r.status_code == 200:
            state = r.json().get("instance", {}).get("state", "unknown")
            return jsonify({"ok": True, "state": state})
        return jsonify({"ok": False, "error": f"HTTP {r.status_code}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/config/ig-test", methods=["POST"])
def api_ig_test():
    data  = request.get_json() or {}
    ig_id = data.get("ig_user_id","")
    token = data.get("ig_token","")
    if not ig_id or not token:
        return jsonify({"ok": False, "error": "Preencha User ID e Token"})
    try:
        r = requests.get(f"{IG_BASE}/{ig_id}",
                         params={"fields":"username,name","access_token":token}, timeout=10)
        data = r.json()
        if "username" in data:
            return jsonify({"ok": True, "username": data["username"]})
        return jsonify({"ok": False, "error": data.get("error",{}).get("message","Erro")})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import socket
    host = "0.0.0.0"
    port = int(os.getenv("PORT", 5000))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print("\n" + "═"*52)
    print("  Disparador de Conteúdo – Web App")
    print("═"*52)
    print(f"  💻  PC:      http://localhost:{port}")
    print(f"  📱  Celular: http://{local_ip}:{port}")
    print("═"*52 + "\n")
    app.run(host=host, port=port, debug=False, threaded=True)
