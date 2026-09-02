"""
ZapShot – Web App (PWA) – Multi-tenant SaaS
Agenda e dispara imagem/vídeo + texto para WhatsApp e Instagram.
"""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, stream_with_context, url_for)


def now_brasilia():
    """Retorna datetime atual no fuso de Brasília (UTC-3)."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3)))


# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
IS_CLOUD = os.getenv("RENDER") == "true" or os.getenv("RAILWAY_ENVIRONMENT") is not None

if os.getenv("DATA_DIR"):
    APP_DIR = Path(os.getenv("DATA_DIR")) / "ZapShot"
elif IS_CLOUD:
    APP_DIR = Path("/tmp/ZapShot")
else:
    APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "ZapShot"

DB_PATH     = APP_DIR / "zapshot.db"
UPLOADS_DIR = APP_DIR / "uploads"
LIBRARY_DIR = APP_DIR / "library"
APP_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
LIBRARY_DIR.mkdir(exist_ok=True)

ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO = {".mp4", ".mov", ".m4v"}

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_ENSURE_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

# ── Database ───────────────────────────────────────────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Tabela de usuários
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT    UNIQUE NOT NULL,
        password_hash TEXT    NOT NULL,
        name          TEXT    DEFAULT '',
        plan          TEXT    DEFAULT 'trial',
        is_admin      INTEGER DEFAULT 0,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")
    # Config por usuário
    conn.execute("""CREATE TABLE IF NOT EXISTS user_configs (
        user_id     INTEGER PRIMARY KEY,
        config_json TEXT    DEFAULT '{}'
    )""")
    # Posts
    conn.execute("""CREATE TABLE IF NOT EXISTS posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER DEFAULT 0,
        caption      TEXT    DEFAULT '',
        filename     TEXT    DEFAULT '',
        media_type   TEXT    DEFAULT 'image',
        wa_groups    TEXT    DEFAULT '[]',
        ig_feed      INTEGER DEFAULT 0,
        ig_stories   INTEGER DEFAULT 0,
        ig_reels     INTEGER DEFAULT 0,
        wa_status    INTEGER DEFAULT 0,
        scheduled_at TEXT,
        status       TEXT    DEFAULT 'pending',
        created_at   TEXT,
        sent_at      TEXT,
        result       TEXT    DEFAULT '{}',
        batch_id     TEXT    DEFAULT '',
        batch_title  TEXT    DEFAULT ''
    )""")
    # Migrations
    for col, defval in [
        ("batch_id",    "''"),
        ("batch_title", "''"),
        ("wa_status",   "0"),
        ("user_id",     "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT DEFAULT {defval}")
        except Exception:
            pass
    conn.commit()
    conn.close()

init_db()

def _hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def seed_admin():
    """Cria usuário admin padrão via env vars (ADMIN_EMAIL + ADMIN_PASSWORD)."""
    email    = os.getenv("ADMIN_EMAIL", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    conn = db()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, plan, is_admin) VALUES (?,?,?,?,?)",
            (email.lower(), _hash_pw(password), "Admin", "active", 1)
        )
        conn.commit()
        print(f"[seed_admin] Admin criado: {email}")
    else:
        # Garante que o admin existente tem is_admin=1 e plan=active
        conn.execute("UPDATE users SET is_admin=1, plan='active' WHERE email=?", (email.lower(),))
        conn.commit()
    conn.close()

seed_admin()

# ── Auth helpers ───────────────────────────────────────────────────────────────
def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return dict(u) if u else None

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = get_current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "not_authenticated"}), 401
            return redirect(f"/login?next={request.path}")
        if u["plan"] == "inactive":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Conta inativa. Entre em contato com o suporte."}), 403
            session.clear()
            return redirect("/login?msg=inactive")
        return f(*args, **kwargs)
    return wrapper

def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = get_current_user()
        if not u or not u["is_admin"]:
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

# ── Config por usuário ─────────────────────────────────────────────────────────
def load_config(user_id=None) -> dict:
    uid = user_id or session.get("user_id")
    cfg = {}
    if uid:
        conn = db()
        row = conn.execute("SELECT config_json FROM user_configs WHERE user_id=?", (uid,)).fetchone()
        conn.close()
        if row:
            try:
                cfg = json.loads(row["config_json"])
            except Exception:
                pass
    # Env vars como fallback (para primeira instalação / admin)
    for env, key in [
        ("EVO_URL",        "evo_url"),
        ("EVO_TOKEN",      "evo_token"),
        ("EVO_INSTANCE",   "evo_instance"),
        ("IG_USER_ID",     "ig_user_id"),
        ("IG_TOKEN",       "ig_token"),
        ("APP_URL",        "app_url"),
        ("GOOGLE_API_KEY", "google_api_key"),
    ]:
        if not cfg.get(key):
            val = os.getenv(env, "")
            if val:
                cfg[key] = val
    return cfg

def save_config(data: dict, user_id=None):
    uid = user_id or session.get("user_id")
    if not uid:
        return
    conn = db()
    row = conn.execute("SELECT config_json FROM user_configs WHERE user_id=?", (uid,)).fetchone()
    if row:
        try:
            current = json.loads(row["config_json"])
        except Exception:
            current = {}
        current.update(data)
        conn.execute("UPDATE user_configs SET config_json=? WHERE user_id=?",
                     (json.dumps(current, ensure_ascii=False), uid))
    else:
        conn.execute("INSERT INTO user_configs (user_id, config_json) VALUES (?,?)",
                     (uid, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()

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
        groups = [{"id": g.get("id", ""), "name": g.get("subject", g.get("id", ""))}
                  for g in (data if isinstance(data, list) else [])]
        return sorted(groups, key=lambda g: g["name"].lower()), ""
    except Exception as exc:
        return [], str(exc)

def _media_url(filename: str, cfg) -> str:
    app_url = cfg.get("app_url", "").rstrip("/")
    if not app_url:
        app_url = "https://social-midia.onrender.com"
    if filename.startswith("__lib__"):
        lib_filename = filename[len("__lib__"):]
        return f"{app_url}/api/library/file/{lib_filename}"
    return f"{app_url}/api/media/{filename}"

def wa_send_image(group_id, caption, filepath, cfg, db_filename=""):
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

def wa_send_video(group_id, caption, filepath, cfg, db_filename=""):
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

def wa_send_text(group_id, text, cfg):
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    try:
        r = requests.post(
            f"{base}/message/sendText/{instance}",
            headers=_evo_headers(cfg),
            json={"number": group_id, "text": text},
            timeout=30
        )
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

def wa_send(group_id, caption, filepath, media_type, cfg, db_filename=""):
    if media_type == "video":
        return wa_send_video(group_id, caption, filepath, cfg, db_filename)
    return wa_send_image(group_id, caption, filepath, cfg, db_filename)

def wa_send_status(caption, filepath, media_type, cfg, db_filename=""):
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    app_url  = cfg.get("app_url", "").rstrip("/")
    if db_filename:
        media_url = f"{app_url}/api/library/file/{db_filename}"
    else:
        media_url = f"{app_url}/api/media/{filepath.name}"
    stype = "video" if media_type == "video" else "image"
    body  = {"type": stype, "content": media_url, "caption": caption, "allContacts": True}
    try:
        r = requests.post(
            f"{base}/message/sendStatus/{instance}",
            headers=_evo_headers(cfg),
            json=body,
            timeout=120
        )
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)

# ── Instagram Graph API ────────────────────────────────────────────────────────
IG_BASE = "https://graph.instagram.com/v21.0"

def _ig_params(cfg):
    return {"access_token": cfg.get("ig_token", "")}

def ig_media_url(filename, cfg):
    base = cfg.get("app_url", "").rstrip("/")
    return f"{base}/api/media/{filename}"

def ig_create_container(media_url, caption, dest, cfg):
    ig_id  = cfg.get("ig_user_id", "")
    params = _ig_params(cfg)
    is_video = any(media_url.lower().endswith(ext) for ext in (".mp4", ".mov", ".m4v"))
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

def ig_wait_ready(container_id, cfg, max_wait=300):
    params   = _ig_params(cfg)
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

def ig_publish(container_id, cfg):
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

def ig_post(media_url, caption, dest, cfg, is_video):
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
    conn = db()
    row  = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close()
        return

    user_id = row["user_id"] if "user_id" in row.keys() else 0
    cfg = load_config(user_id=user_id)

    conn.execute("UPDATE posts SET status='sending' WHERE id=?", (post_id,))
    conn.commit()
    conn.close()

    caption    = row["caption"] or ""
    filename   = row["filename"] or ""
    media_type = row["media_type"] or "image"
    wa_groups  = json.loads(row["wa_groups"] or "[]")
    ig_feed    = bool(row["ig_feed"])
    ig_stories = bool(row["ig_stories"])
    ig_reels   = bool(row["ig_reels"])
    wa_status  = bool(row["wa_status"]) if "wa_status" in row.keys() else False

    lib_filename = ""
    if filename.startswith("__lib__"):
        lib_filename = filename[len("__lib__"):]
        filepath = LIBRARY_DIR / lib_filename
    else:
        filepath = UPLOADS_DIR / filename
    is_video = media_type == "video"

    result = {"wa": {}, "ig": {}}
    errors = []

    def _save_partial():
        c = db()
        c.execute("UPDATE posts SET result=? WHERE id=?",
                  (json.dumps(result, ensure_ascii=False), post_id))
        c.commit()
        c.close()

    for i, g in enumerate(wa_groups):
        gid  = g.get("id", g) if isinstance(g, dict) else g
        name = g.get("name", gid) if isinstance(g, dict) else gid
        result["wa"][name] = "⏳ enviando..."
        _save_partial()
        ok, err = wa_send(gid, caption, filepath, media_type, cfg, db_filename=filename)
        result["wa"][name] = "ok" if ok else err
        _save_partial()
        if not ok:
            errors.append(f"WA {name}: {err}")
        if i < len(wa_groups) - 1:
            time.sleep(1)

    media_url = ig_media_url(filename, cfg)
    if ig_feed:
        ok, err = ig_post(media_url, caption, "feed", cfg, is_video)
        result["ig"]["feed"] = "ok" if ok else err
        if not ok:
            errors.append(f"IG Feed: {err}")

    if ig_stories:
        ok, err = ig_post(media_url, "", "stories", cfg, is_video)
        result["ig"]["stories"] = "ok" if ok else err
        if not ok:
            errors.append(f"IG Stories: {err}")

    if ig_reels:
        ok, err = ig_post(media_url, caption, "reels", cfg, is_video)
        result["ig"]["reels"] = "ok" if ok else err
        if not ok:
            errors.append(f"IG Reels: {err}")

    if wa_status:
        ok, err = wa_send_status(caption, filepath, media_type, cfg, db_filename=lib_filename)
        result.setdefault("wa_status", {})["status"] = "ok" if ok else err
        if not ok:
            errors.append(f"WA Status: {err}")

    final_status = ("partial" if errors and (result["wa"] or result["ig"]) else
                    "failed"  if errors else "sent")

    conn = db()
    conn.execute("UPDATE posts SET status=?, sent_at=?, result=? WHERE id=?",
                 (final_status,
                  now_brasilia().strftime("%Y-%m-%dT%H:%M:%S"),
                  json.dumps(result, ensure_ascii=False),
                  post_id))
    conn.commit()
    conn.close()

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

# ══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    msg = request.args.get("msg", "")
    if request.method == "POST":
        email    = (request.form.get("email", "") or "").strip().lower()
        password = request.form.get("password", "") or ""
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if not user or user["password_hash"] != _hash_pw(password):
            return render_template("login.html", error="Email ou senha inválidos.", tab="login")
        if user["plan"] == "inactive":
            return render_template("login.html", error="Sua conta está inativa. Entre em contato com o suporte.", tab="login")
        session["user_id"] = user["id"]
        session["user_name"] = user["name"] or user["email"]
        session["is_admin"] = bool(user["is_admin"])
        next_url = request.args.get("next", "/")
        return redirect(next_url)
    tab = request.args.get("tab", "login")
    error_msg = "Conta inativa. Entre em contato com o suporte." if msg == "inactive" else ""
    return render_template("login.html", error=error_msg, tab=tab)

@app.route("/register", methods=["POST"])
def register():
    email    = (request.form.get("email", "") or "").strip().lower()
    name     = (request.form.get("name",  "") or "").strip()
    password = request.form.get("password", "") or ""
    confirm  = request.form.get("confirm",  "") or ""

    if not email or not password:
        return render_template("login.html", error="Preencha todos os campos.", tab="register")
    if password != confirm:
        return render_template("login.html", error="As senhas não conferem.", tab="register")
    if len(password) < 6:
        return render_template("login.html", error="Senha deve ter ao menos 6 caracteres.", tab="register")

    conn = db()
    # Primeiro usuário vira admin automaticamente
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    is_admin = 1 if count == 0 else 0
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, plan, is_admin) VALUES (?,?,?,?,?)",
            (email, _hash_pw(password), name, "trial", is_admin)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return render_template("login.html", error="Este email já está cadastrado.", tab="register")
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()

    session["user_id"]   = user["id"]
    session["user_name"] = user["name"] or user["email"]
    session["is_admin"]  = bool(user["is_admin"])
    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin")
@require_admin
def admin_panel():
    conn  = db()
    users = conn.execute("""
        SELECT u.*, (SELECT COUNT(*) FROM posts WHERE user_id=u.id) as posts_count
        FROM users u ORDER BY u.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("admin.html", users=[dict(u) for u in users],
                           current_user=get_current_user())

@app.route("/admin/users/<int:uid>/plan", methods=["POST"])
@require_admin
def admin_set_plan(uid):
    plan = request.form.get("plan", "trial")
    if plan not in ("trial", "active", "inactive"):
        return "Plano inválido", 400
    conn = db()
    conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, uid))
    conn.commit()
    conn.close()
    return redirect("/admin")

@app.route("/admin/users/<int:uid>/admin", methods=["POST"])
@require_admin
def admin_toggle_admin(uid):
    # Não pode remover próprio admin
    if uid == session.get("user_id"):
        return redirect("/admin")
    conn = db()
    u = conn.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    if u:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (0 if u["is_admin"] else 1, uid))
        conn.commit()
    conn.close()
    return redirect("/admin")

@app.route("/admin/users/<int:uid>", methods=["DELETE", "POST"])
@require_admin
def admin_delete_user(uid):
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "error": "Não pode excluir a própria conta"}), 400
    conn = db()
    conn.execute("DELETE FROM posts WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM user_configs WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/admin/stats")
@require_admin
def api_admin_stats():
    conn = db()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_users = conn.execute("SELECT COUNT(*) FROM users WHERE plan IN ('trial','active')").fetchone()[0]
    total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    sent_posts = conn.execute("SELECT COUNT(*) FROM posts WHERE status='sent'").fetchone()[0]
    conn.close()
    return jsonify({"total_users": total_users, "active_users": active_users,
                    "total_posts": total_posts, "sent_posts": sent_posts})

# ══════════════════════════════════════════════════════════════════════════════
# APP ROUTES (require login)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
@require_login
def index():
    return render_template("index.html", current_user=get_current_user())

# ── Media serve (público – Evolution API chama de fora) ───────────────────────
@app.route("/api/media/<filename>")
def api_media(filename):
    safe = Path(filename).name
    path = UPLOADS_DIR / safe
    if not path.exists():
        return "Not found", 404
    ext = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/mp4", ".m4v": "video/mp4"}
    return send_file(str(path), mimetype=mime_map.get(ext, "application/octet-stream"), conditional=False)

@app.route("/api/library/file/<filename>")
def api_library_file(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    if not path.exists():
        return "Not found", 404
    ext = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/mp4", ".m4v": "video/mp4"}
    with open(str(path), "rb") as f:
        data = f.read()
    return Response(data, mimetype=mime_map.get(ext, "application/octet-stream"),
                    headers={"Content-Length": str(len(data))})

# ── Upload ─────────────────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
@require_login
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
@app.route("/api/library", methods=["GET"])
@require_login
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

@app.route("/api/library/upload", methods=["POST"])
@require_login
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
    filename = (f"{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg" if mtype == "image"
                else f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}")
    dest = LIBRARY_DIR / filename
    if mtype == "image":
        try:
            from PIL import Image
            img = Image.open(f)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((1200, 1200), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            content = buf.getvalue()
        except Exception as e:
            print(f"[library_upload] compressão falhou: {e}")
            f.seek(0)
            content = f.read()
    else:
        content = f.read()
    with open(str(dest), "wb") as fh:
        fh.write(content)
    return jsonify({"ok": True, "filename": filename, "media_type": mtype,
                    "size": dest.stat().st_size})

@app.route("/api/library/<filename>", methods=["DELETE"])
@require_login
def api_library_delete(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})

@app.route("/api/library/debug/<filename>")
@require_login
def api_library_debug(filename):
    safe = Path(filename).name
    path = LIBRARY_DIR / safe
    exists = path.exists()
    size   = path.stat().st_size if exists else -1
    try:
        with open(str(path), "rb") as fh:
            head = fh.read(16)
        head_hex = head.hex()
    except Exception as e:
        head_hex = str(e)
    return jsonify({"exists": exists, "size": size, "path": str(path), "head_hex": head_hex})

# ── WhatsApp groups ─────────────────────────────────────────────────────────────
@app.route("/api/wa/groups")
@require_login
def api_wa_groups():
    cfg    = load_config()
    groups, err = wa_get_groups(cfg)
    if err and not groups:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "groups": groups})

@app.route("/api/wa/test-text")
@require_login
def api_wa_test_text():
    cfg = load_config()
    groups, err = wa_get_groups(cfg)
    if err or not groups:
        return jsonify({"ok": False, "error": err or "Sem grupos"})
    group = groups[0]
    ok, err2 = wa_send_text(group["id"], "🔧 Teste de conexão - pode ignorar", cfg)
    return jsonify({"ok": ok, "group": group["name"], "error": err2})

# ── Posts ───────────────────────────────────────────────────────────────────────
@app.route("/api/posts", methods=["GET"])
@require_login
def api_posts():
    uid  = session["user_id"]
    conn = db()
    rows = conn.execute(
        "SELECT * FROM posts WHERE user_id=? ORDER BY scheduled_at DESC LIMIT 200",
        (uid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/posts", methods=["POST"])
@require_login
def api_create_post():
    data       = request.get_json() or {}
    uid        = session["user_id"]
    filename   = data.get("filename", "")
    media_type = data.get("media_type", "image")
    caption    = data.get("caption", "")
    wa_groups  = data.get("wa_groups", [])
    ig_feed    = int(bool(data.get("ig_feed")))
    ig_stories = int(bool(data.get("ig_stories")))
    ig_reels   = int(bool(data.get("ig_reels")))
    wa_status  = int(bool(data.get("wa_status")))
    scheduled_at = data.get("scheduled_at", "")

    library = data.get("library", "")
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

    try:
        base_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        return jsonify({"ok": False, "error": "Horário inválido"})

    interval_minutes = int(24 * 60 / times_per_day)
    total    = repeat_days * times_per_day
    batch_id = str(uuid.uuid4())
    conn     = db()
    created_at     = datetime.now().isoformat(timespec="seconds")
    wa_groups_json = json.dumps(wa_groups)
    first_id = None

    for i in range(total):
        sched = (base_dt + timedelta(minutes=i * interval_minutes)).isoformat(timespec="minutes")
        cur = conn.execute("""INSERT INTO posts
            (user_id,caption,filename,media_type,wa_groups,ig_feed,ig_stories,ig_reels,wa_status,
             scheduled_at,status,created_at,batch_id,batch_title)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (uid, caption, filename, media_type, wa_groups_json,
             ig_feed, ig_stories, ig_reels, wa_status, sched, created_at, batch_id, batch_title))
        if i == 0:
            first_id = cur.lastrowid

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": first_id, "count": total, "batch_id": batch_id})

@app.route("/api/posts/<int:post_id>/send", methods=["POST"])
@require_login
def api_send_now(post_id):
    uid  = session["user_id"]
    conn = db()
    row  = conn.execute("SELECT status, user_id FROM posts WHERE id=?", (post_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "Post não encontrado"})
    if row["user_id"] != uid and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "Sem permissão"}), 403
    if row["status"] == "sending":
        return jsonify({"ok": False, "error": "Já está sendo enviado"})
    threading.Thread(target=process_post, args=(post_id,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
@require_login
def api_delete_post(post_id):
    uid  = session["user_id"]
    conn = db()
    row  = conn.execute("SELECT filename, status, user_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if row and (row["user_id"] == uid or session.get("is_admin")):
        if row["status"] == "pending":
            try:
                (UPLOADS_DIR / row["filename"]).unlink(missing_ok=True)
            except Exception:
                pass
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/posts/batch/<batch_id>", methods=["DELETE"])
@require_login
def api_delete_batch(batch_id):
    uid  = session["user_id"]
    conn = db()
    conn.execute("DELETE FROM posts WHERE batch_id=? AND user_id=?", (batch_id, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/posts/stream")
@require_login
def api_posts_stream():
    uid = session["user_id"]
    def generate():
        last = {}
        for _ in range(600):
            conn = db()
            rows = conn.execute(
                "SELECT id, status, sent_at, result FROM posts "
                "WHERE user_id=? AND status IN ('sending','pending')",
                (uid,)
            ).fetchall()
            conn.close()
            for r in rows:
                key = f"{r['id']}-{r['status']}-{r['sent_at']}"
                if last.get(r["id"]) != key:
                    last[r["id"]] = key
                    yield f"data: {json.dumps(dict(r))}\n\n"
            time.sleep(2)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Config ──────────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET", "POST"])
@require_login
def api_config():
    if request.method == "POST":
        save_config(request.get_json() or {})
        return jsonify({"ok": True})
    return jsonify(load_config())

@app.route("/api/config/evo-test", methods=["POST"])
@require_login
def api_evo_test():
    data     = request.get_json() or {}
    base     = data.get("evo_url", "").rstrip("/")
    token    = data.get("evo_token", "")
    instance = data.get("evo_instance", "")
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

@app.route("/api/grupos/nome", methods=["POST"])
@require_login
def api_grupo_nome():
    """Busca o nome real do grupo WhatsApp pela página de convite."""
    link = (request.get_json() or {}).get("link", "")
    if not link or "chat.whatsapp.com/invite/" not in link:
        return jsonify({"ok": False, "name": "Grupo WhatsApp"})
    try:
        r = requests.get(link, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
        html = r.text
        # og:title tem o nome do grupo
        m = re.search(r'<meta property="og:title"\s+content="([^"]+)"', html)
        if not m:
            m = re.search(r'<title>([^<]+)</title>', html)
        name = m.group(1).strip() if m else "Grupo WhatsApp"
        # Remove sufixos genéricos do WhatsApp
        name = re.sub(r'\s*[\|–-]\s*WhatsApp.*$', '', name).strip()
        return jsonify({"ok": True, "name": name or "Grupo WhatsApp"})
    except Exception as exc:
        return jsonify({"ok": False, "name": "Grupo WhatsApp", "error": str(exc)})

@app.route("/api/config/ig-test", methods=["POST"])
@require_login
def api_ig_test():
    data  = request.get_json() or {}
    ig_id = data.get("ig_user_id", "")
    token = data.get("ig_token", "")
    if not ig_id or not token:
        return jsonify({"ok": False, "error": "Preencha User ID e Token"})
    try:
        r = requests.get(f"{IG_BASE}/{ig_id}",
                         params={"fields": "username,name", "access_token": token}, timeout=10)
        data = r.json()
        if "username" in data:
            return jsonify({"ok": True, "username": data["username"]})
        return jsonify({"ok": False, "error": data.get("error", {}).get("message", "Erro")})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

# ── Busca de Grupos WhatsApp ────────────────────────────────────────────────────
@app.route("/api/grupos/buscar", methods=["POST"])
@require_login
def api_buscar_grupos():
    data  = request.get_json() or {}
    tema  = data.get("tema", "").strip()
    local = data.get("local", "").strip()
    cfg   = load_config()

    api_key = cfg.get("google_api_key", "") or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "no_key"})

    WA_PATTERN = re.compile(r'https://chat\.whatsapp\.com/invite/[A-Za-z0-9_-]+')
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

    def _scaleserp(q, num=10):
        try:
            r = requests.get("https://api.scaleserp.com/search",
                params={"api_key": api_key, "q": q, "num": num, "gl": "br", "hl": "pt"},
                timeout=20)
            return r.json().get("organic_results", [])
        except Exception:
            return []

    def _scrape_page(url):
        """Raspa uma página e extrai todos os links WA com nome do contexto."""
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": UA})
            html = r.text
            found = []
            for m in WA_PATTERN.finditer(html):
                link = m.group(0).rstrip("\"'\\>")
                code = link.split("/")[-1]
                # Busca o nome próximo ao link no HTML
                start = max(0, m.start() - 300)
                ctx   = re.sub(r'<[^>]+>', ' ', html[start:m.start()])  # strip tags
                # Limpa espaços e pega últimas palavras (provável nome do grupo)
                words = [w for w in ctx.split() if len(w) > 2]
                name  = " ".join(words[-8:]) if words else ""
                found.append((link, code, name))
            return found
        except Exception:
            return []

    # Monta queries
    base = f"grupos whatsapp {local}"
    if tema: base += f" {tema}"

    # Queries com foco nos maiores agregadores brasileiros
    queries = [
        f"site:gruposwhats.app {local} {tema}".strip(),
        f"site:whatsappgrupos.com.br {local} {tema}".strip(),
        f"site:gzap.com.br {local} {tema}".strip(),
        base,
        f"{base} site:linktr.ee OR site:notion.so OR site:linklist.bio",
    ]

    try:
        seen_codes = set()
        page_urls  = set()
        results    = []

        for q in queries:
            for item in _scaleserp(q, num=10):
                url = item.get("link", "")
                if url and url not in page_urls:
                    page_urls.add(url)

        print(f"[buscar_grupos] {len(page_urls)} paginas para raspar")

        # Raspa cada página encontrada em paralelo (max 15)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_scrape_page, url): url
                       for url in list(page_urls)[:15]}
            for fut in concurrent.futures.as_completed(futures):
                for link, code, name in fut.result():
                    if code not in seen_codes:
                        seen_codes.add(code)
                        results.append({"link": link, "name": name, "title": name, "snippet": ""})

        # Se ainda poucos, raspa o site principal do agregador diretamente
        if len(results) < 10 and local:
            slug = local.lower().replace(" ", "-")
            for direct_url in [
                f"https://gruposwhats.app/state/{slug}",
                f"https://gruposwhats.app/search?q={local}+{tema}".strip("+"),
            ]:
                for link, code, name in _scrape_page(direct_url):
                    if code not in seen_codes:
                        seen_codes.add(code)
                        results.append({"link": link, "name": name, "title": name, "snippet": ""})

        print(f"[buscar_grupos] total grupos={len(results)}")
        return jsonify({"ok": True, "results": results})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

# ── User profile ────────────────────────────────────────────────────────────────
@app.route("/api/me")
@require_login
def api_me():
    u = get_current_user()
    if not u:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "id": u["id"], "email": u["email"],
                    "name": u["name"], "plan": u["plan"], "is_admin": u["is_admin"]})

@app.route("/api/me/password", methods=["POST"])
@require_login
def api_change_password():
    data     = request.get_json() or {}
    old_pw   = data.get("old_password", "")
    new_pw   = data.get("new_password", "")
    uid      = session["user_id"]
    conn     = db()
    u = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["password_hash"] != _hash_pw(old_pw):
        conn.close()
        return jsonify({"ok": False, "error": "Senha atual incorreta"})
    if len(new_pw) < 6:
        conn.close()
        return jsonify({"ok": False, "error": "Nova senha deve ter ao menos 6 caracteres"})
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(new_pw), uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Main ────────────────────────────────────────────────────────────────────────
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

    print("\n" + "═" * 52)
    print("  ZapShot — Envie. Alcance. Conecte.")
    print("═" * 52)
    print(f"  💻  PC:      http://localhost:{port}")
    print(f"  📱  Celular: http://{local_ip}:{port}")
    print("═" * 52 + "\n")
    app.run(host=host, port=port, debug=False, threaded=True)
