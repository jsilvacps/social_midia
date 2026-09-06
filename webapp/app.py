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

import concurrent.futures

import requests
from flask import (Flask, Response, jsonify, make_response, redirect, render_template,
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
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras

class _PgWrapper:
    """Wrapper que imita a interface sqlite3 para o restante do código."""
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=()):
        sql = self._adapt(sql)
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return _CursorWrapper(cur)

    def executemany(self, sql, seq):
        sql = self._adapt(sql)
        seq = list(seq)
        cur = self._conn.cursor()
        # execute_values envia todos os rows num único round-trip ao PostgreSQL
        # (muito mais rápido que executemany que faz 1 round-trip por linha)
        if seq:
            # Constrói o template com a quantidade certa de %s
            n_cols = len(seq[0])
            template = "(" + ",".join(["%s"] * n_cols) + ")"
            # Substitui o VALUES (...todos os %s...) pelo template do execute_values
            # execute_values espera: INSERT INTO t (cols) VALUES %s
            parts = sql.split("VALUES", 1)
            sql_ev = parts[0] + "VALUES %s"
            psycopg2.extras.execute_values(cur, sql_ev, seq, template=template, page_size=200)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def _adapt(self, sql):
        # Converte ? → %s e sintaxe SQLite → PostgreSQL
        sql = sql.replace("?", "%s")
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        sql = sql.replace("datetime('now')", "NOW()")
        sql = sql.replace("INSERT OR REPLACE", "INSERT")
        sql = sql.replace("INSERT OR IGNORE", "INSERT")
        return sql

class _CursorWrapper:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _RowWrapper(row, self._cur.description)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        desc = self._cur.description
        return [_RowWrapper(r, desc) for r in rows]

    def __iter__(self):
        desc = self._cur.description
        for row in self._cur:
            yield _RowWrapper(row, desc)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        self._cur.execute("SELECT LASTVAL()")
        return self._cur.fetchone()[0]

class _RowWrapper:
    """Imita sqlite3.Row — acesso por nome ou índice."""
    def __init__(self, row, description):
        self._row  = row
        self._cols = [d[0] for d in description] if description else []
        self._map  = {d[0]: i for i, d in enumerate(description)} if description else {}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        return self._row[self._map[key]]

    def get(self, key, default=None):
        idx = self._map.get(key)
        if idx is None:
            return default
        return self._row[idx]

    def keys(self):
        return self._cols

    def __contains__(self, key):
        return key in self._map

def db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        return _PgWrapper(conn)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _col_exists(conn, table, col):
    if USE_PG:
        r = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table, col)
        ).fetchone()
        return r is not None
    try:
        conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        return True
    except Exception:
        return False

def init_db():
    conn = db()
    if USE_PG:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id               SERIAL PRIMARY KEY,
            email            TEXT   UNIQUE NOT NULL,
            password_hash    TEXT   NOT NULL,
            name             TEXT   DEFAULT '',
            plan             TEXT   DEFAULT 'trial',
            is_admin         INTEGER DEFAULT 0,
            created_at       TEXT   DEFAULT TO_CHAR(NOW(),'YYYY-MM-DD"T"HH24:MI:SS'),
            trial_expires_at TEXT   DEFAULT NULL,
            features         TEXT   DEFAULT '[]',
            must_change_password INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_configs (
            user_id     INTEGER PRIMARY KEY,
            config_json TEXT    DEFAULT '{}'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS posts (
            id           SERIAL PRIMARY KEY,
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
            batch_title  TEXT    DEFAULT '',
            client_phone TEXT    DEFAULT '',
            suspend_from    TEXT    DEFAULT '',
            suspend_to      TEXT    DEFAULT '',
            send_all_groups INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_imported_groups (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            group_jid   TEXT    NOT NULL,
            name        TEXT    DEFAULT '',
            invite_link TEXT    DEFAULT '',
            imported_at TEXT    DEFAULT TO_CHAR(NOW(),'YYYY-MM-DD"T"HH24:MI:SS'),
            UNIQUE(user_id, group_jid)
        )""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT    UNIQUE NOT NULL,
            password_hash   TEXT    NOT NULL,
            name            TEXT    DEFAULT '',
            plan            TEXT    DEFAULT 'trial',
            is_admin        INTEGER DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now')),
            trial_expires_at TEXT   DEFAULT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_configs (
            user_id     INTEGER PRIMARY KEY,
            config_json TEXT    DEFAULT '{}'
        )""")
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
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_imported_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            group_jid   TEXT    NOT NULL,
            name        TEXT    DEFAULT '',
            invite_link TEXT    DEFAULT '',
            imported_at TEXT    DEFAULT (datetime('now')),
            UNIQUE(user_id, group_jid)
        )""")
        # Migrations SQLite
        for col, defval in [
            ("batch_id","''"),("batch_title","''"),("wa_status","0"),
            ("user_id","0"),("client_phone","''"),("suspend_from","''"),("suspend_to","''"),
            ("send_all_groups","0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT DEFAULT {defval}")
            except Exception:
                pass
        for col, defval in [
            ("trial_expires_at","NULL"),("features","'[]'"),("must_change_password","0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {defval}")
            except Exception:
                pass
        # Migrations PostgreSQL (colunas adicionadas depois do deploy inicial)
        for col, defval in [
            ("send_all_groups", "0"),
        ]:
            if not _col_exists(conn, "posts", col):
                try:
                    conn.execute(f"ALTER TABLE posts ADD COLUMN {col} INTEGER DEFAULT {defval}")
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
        # Verifica expiração do trial
        if u["plan"] == "trial" and u.get("trial_expires_at"):
            from datetime import datetime as _dt
            try:
                expires = _dt.fromisoformat(u["trial_expires_at"])
                if _dt.utcnow() > expires:
                    # Expirou — bloqueia automaticamente
                    conn2 = db()
                    conn2.execute("UPDATE users SET plan='inactive' WHERE id=?", (u["id"],))
                    conn2.commit()
                    conn2.close()
                    session.clear()
                    if request.path.startswith("/api/"):
                        return jsonify({"ok": False, "error": "Trial expirado. Entre em contato para continuar."}), 403
                    return redirect("/login?msg=trial_expired")
            except Exception:
                pass
        # Verifica se precisa trocar a senha (criado pelo admin)
        if u.get("must_change_password") and request.path not in ("/change-password",):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Troca de senha obrigatória"}), 403
            return redirect("/change-password")
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

def user_has_feature(u, feature: str) -> bool:
    """Retorna True se o usuário tem a feature ativa ou é admin."""
    if u.get("is_admin"):
        return True
    try:
        features = json.loads(u.get("features") or "[]")
        return feature in features
    except Exception:
        return False

def require_feature(feature: str):
    """Decorator que bloqueia acesso se o usuário não tiver a feature."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = get_current_user()
            if not u:
                return jsonify({"ok": False, "error": "not_authenticated"}), 401
            if not user_has_feature(u, feature):
                return jsonify({"ok": False, "error": "feature_locked",
                                "msg": "Recurso não disponível no seu plano."}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator

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
        raw  = data if isinstance(data, list) else []
        groups = []
        for g in raw:
            jid  = g.get("id", "")
            name = g.get("subject") or g.get("name") or jid
            # Alguns builds do Evolution já retornam o link no fetchAllGroups
            link = (g.get("inviteUrl") or g.get("invite_url") or "")
            if not link:
                code = g.get("inviteCode") or g.get("invite") or ""
                link = f"https://chat.whatsapp.com/{code}" if code else ""
            groups.append({"id": jid, "name": name, "invite_link": link})
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
    app_url  = (cfg.get("app_url") or "https://social-midia.onrender.com").rstrip("/")
    if db_filename:
        media_url = f"{app_url}/api/library/file/{db_filename}"
    elif filepath:
        media_url = f"{app_url}/api/media/{filepath.name}"
    else:
        return False, "Sem arquivo de mídia"
    stype = "video" if media_type == "video" else "image"
    # Tenta endpoint sendStatus (Evolution API v2)
    body = {"type": stype, "content": media_url, "caption": caption, "allContacts": True}
    try:
        r = requests.post(
            f"{base}/message/sendStatus/{instance}",
            headers=_evo_headers(cfg),
            json=body,
            timeout=120
        )
        print(f"[wa_status] sendStatus status={r.status_code} body={r.text[:300]}")
        if r.status_code in (200, 201):
            return True, ""
        # Fallback: tenta via sendMedia para status@broadcast
        body2 = {
            "number": "status@broadcast",
            "mediatype": stype,
            "mimetype": "video/mp4" if stype == "video" else "image/jpeg",
            "caption": caption,
            "media": media_url,
        }
        r2 = requests.post(
            f"{base}/message/sendMedia/{instance}",
            headers=_evo_headers(cfg),
            json=body2,
            timeout=120
        )
        print(f"[wa_status] sendMedia@broadcast status={r2.status_code} body={r2.text[:300]}")
        if r2.status_code in (200, 201):
            return True, ""
        return False, f"sendStatus: HTTP {r.status_code} | sendMedia: HTTP {r2.status_code}: {r2.text[:200]}"
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

# ── Relatório WhatsApp ────────────────────────────────────────────────────────
def _enviar_relatorio(client_phone: str, batch_title: str, result: dict, cfg: dict):
    """Envia relatório de disparo via WA para o celular do cliente."""
    if not client_phone:
        return
    base     = cfg.get("evo_url", "").rstrip("/")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return

    # Monta número com DDI Brasil se não tiver
    phone = client_phone
    if not phone.startswith("55"):
        phone = "55" + phone
    phone = phone + "@s.whatsapp.net"

    # Resumo dos grupos WA
    wa_res = result.get("wa", {})
    ok_list  = [g for g, s in wa_res.items() if s == "ok"]
    err_list = [g for g, s in wa_res.items() if s != "ok"]

    linhas = [f"📊 *Relatório de Disparo*"]
    if batch_title:
        linhas.append(f"📁 Lote: *{batch_title}*")
    linhas.append(f"🕐 Horário: {now_brasilia().strftime('%d/%m/%Y %H:%M')}")
    linhas.append("")
    if ok_list:
        linhas.append(f"✅ *Enviados com sucesso ({len(ok_list)}):*")
        for g in ok_list:
            linhas.append(f"  • {g}")
    if err_list:
        linhas.append("")
        linhas.append(f"❌ *Com erro ({len(err_list)}):*")
        for g in err_list:
            linhas.append(f"  • {g}: {wa_res[g]}")
    # Instagram
    ig_res = result.get("ig", {})
    if ig_res:
        linhas.append("")
        linhas.append("📸 *Instagram:*")
        for dest, s in ig_res.items():
            ico = "✅" if s == "ok" else "❌"
            linhas.append(f"  {ico} {dest}: {s}")

    msg = "\n".join(linhas)
    try:
        requests.post(
            f"{base}/message/sendText/{instance}",
            headers={"apikey": cfg.get("evo_token", ""), "Content-Type": "application/json"},
            json={"number": phone, "text": msg},
            timeout=15
        )
        print(f"[relatorio] enviado para {client_phone}")
    except Exception as exc:
        print(f"[relatorio] erro ao enviar: {exc}")

# ── Post Processor ─────────────────────────────────────────────────────────────
def process_post(post_id: int):
    conn = db()
    row  = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close()
        return

    user_id = row["user_id"] if "user_id" in row.keys() else 0
    cfg = load_config(user_id=user_id)

    # Só avança se ainda estiver 'queued' — evita duplicata se o scheduler
    # relançar a thread por engano enquanto outra já está rodando
    affected = conn.execute(
        "UPDATE posts SET status='sending' WHERE id=? AND status='queued'", (post_id,)
    ).rowcount
    conn.commit()
    conn.close()
    if not affected:
        print(f"[process_post] post {post_id} já não está queued, abortando")
        return
    try:
        _process_post_inner(post_id, row, cfg)
    except Exception as exc:
        print(f"[process_post] ERRO FATAL post {post_id}: {exc}")
        import traceback; traceback.print_exc()
        c = db()
        c.execute("UPDATE posts SET status='failed', result=? WHERE id=?",
                  (json.dumps({"error": str(exc)}), post_id))
        c.commit(); c.close()

def _process_post_inner(post_id, row, cfg):

    caption    = row["caption"] or ""
    filename   = row["filename"] or ""
    media_type = row["media_type"] or "image"
    wa_groups       = json.loads(row["wa_groups"] or "[]")
    send_all_groups = bool(row["send_all_groups"]) if "send_all_groups" in row.keys() else False

    # Se foi agendado com "todos os grupos", busca grupos frescos e adiciona novos
    if send_all_groups and wa_groups:
        try:
            fresh_groups, _ = wa_get_groups(cfg)
            saved_ids = {g.get("id", g) if isinstance(g, dict) else g for g in wa_groups}
            novos = [g for g in fresh_groups if g["id"] not in saved_ids]
            if novos:
                print(f"[process_post] send_all_groups: {len(novos)} grupos novos detectados, adicionando")
                wa_groups = wa_groups + [{"id": g["id"], "name": g["name"]} for g in novos]
        except Exception as e:
            print(f"[process_post] send_all_groups erro ao buscar novos grupos: {e}")

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
        ok, err = wa_send(gid, caption, filepath, media_type, cfg, db_filename=filename)
        result["wa"][name] = "ok" if ok else err
        if not ok:
            errors.append(f"WA {name}: {err}")
        # Salva progresso a cada 10 grupos (evita 150 round-trips ao banco)
        if i % 10 == 9 or i == len(wa_groups) - 1:
            _save_partial()
        if i < len(wa_groups) - 1:
            time.sleep(0.3)  # 300ms entre grupos (era 1s) — suficiente para evitar rate limit

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

    # Envia relatório para o celular do cliente (se informado)
    client_phone = row["client_phone"] if "client_phone" in row.keys() else ""
    batch_title  = row["batch_title"]  if "batch_title"  in row.keys() else ""
    if client_phone:
        import threading as _thr
        _thr.Thread(target=_enviar_relatorio,
                    args=(client_phone, batch_title, result, cfg),
                    daemon=True).start()
    conn.close()

def _in_suspend_window(suspend_from: str, suspend_to: str, now_hm: str) -> bool:
    """Verifica se now_hm (HH:MM) está dentro da janela de suspensão.
    Suporta virada de meia-noite: ex. suspend_from=22:00, suspend_to=06:00."""
    if not suspend_from or not suspend_to:
        return False
    try:
        sf = tuple(int(x) for x in suspend_from.split(":"))
        st = tuple(int(x) for x in suspend_to.split(":"))
        nw = tuple(int(x) for x in now_hm.split(":"))
        if sf < st:   # mesma faixa do dia, ex: 08:00 – 12:00
            return sf <= nw < st
        else:          # cruza meia-noite, ex: 22:00 – 06:00
            return nw >= sf or nw < st
    except Exception:
        return False

# ── Scheduler Thread ───────────────────────────────────────────────────────────
def _scheduler_loop():
    while True:
        try:
            now_dt   = now_brasilia()
            now_str  = now_dt.strftime("%Y-%m-%dT%H:%M")
            now_hm   = now_dt.strftime("%H:%M")
            conn = db()
            # Posts pendentes prontos para enviar
            rows = conn.execute(
                "SELECT id, suspend_from, suspend_to FROM posts WHERE status='pending' AND scheduled_at<=?",
                (now_str + ":59",)
            ).fetchall()
            # Posts presos como 'queued' há mais de 5 min (thread nunca chegou a rodar)
            # NÃO recupera 'sending' — esses podem estar rodando normalmente com muitos grupos
            stale = conn.execute(
                "SELECT id, suspend_from, suspend_to FROM posts WHERE status='queued' AND scheduled_at<=?",
                ((now_dt - __import__('datetime').timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M") + ":59",)
            ).fetchall()
            conn.close()
            for row in list(rows) + list(stale):
                sf = row["suspend_from"] or ""
                st = row["suspend_to"]   or ""
                if _in_suspend_window(sf, st, now_hm):
                    print(f"[scheduler] post {row['id']} suspenso ({now_hm} em {sf}–{st})")
                    c = db()
                    c.execute("UPDATE posts SET status='suspended' WHERE id=?", (row["id"],))
                    c.commit(); c.close()
                else:
                    # Marca como 'queued' atomicamente antes de lançar a thread
                    c = db()
                    affected = c.execute(
                        "UPDATE posts SET status='queued' WHERE id=? AND status IN ('pending','queued')",
                        (row["id"],)
                    ).rowcount
                    c.commit(); c.close()
                    if affected:
                        print(f"[scheduler] lançando post {row['id']}")
                        threading.Thread(target=process_post, args=(row["id"],), daemon=True).start()
        except Exception as exc:
            print(f"[scheduler] {exc}")
            import traceback; traceback.print_exc()
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
        remember = request.form.get("remember", "0") == "1"
        session.permanent = remember
        session["user_id"] = user["id"]
        session["user_name"] = user["name"] or user["email"]
        session["is_admin"] = bool(user["is_admin"])
        # Importa grupos WA em background no login (para admin ver no painel)
        _uid = user["id"]
        def _bg_import():
            try:
                _cfg = load_config(user_id=_uid)
                groups, _ = wa_get_groups(_cfg)
                if groups:
                    _salvar_grupos_silencioso(_cfg, _uid, groups)
            except Exception:
                pass
        threading.Thread(target=_bg_import, daemon=True).start()
        next_url = request.args.get("next", "/")
        return redirect(next_url)
    tab = request.args.get("tab", "login")
    if msg == "inactive":
        error_msg = "Conta inativa. Entre em contato com o suporte."
    elif msg == "trial_expired":
        error_msg = "⏰ Seu período de teste de 3 dias expirou. Entre em contato para continuar usando o ZapShot."
    else:
        error_msg = ""
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
    from datetime import datetime as _dt, timedelta as _td
    trial_exp = (_dt.utcnow() + _td(days=3)).strftime("%Y-%m-%dT%H:%M:%S") if not is_admin else None
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, plan, is_admin, trial_expires_at) VALUES (?,?,?,?,?,?)",
            (email, _hash_pw(password), name, "trial" if not is_admin else "active", is_admin, trial_exp)
        )
        conn.commit()
    except Exception as _ie:
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
    resp = make_response(render_template("admin.html", users=[dict(u) for u in users],
                           current_user=get_current_user()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

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

@app.route("/admin/users/<int:uid>/extend-trial", methods=["POST"])
@require_admin
def admin_extend_trial(uid):
    from datetime import datetime as _dt, timedelta as _td
    data = request.get_json() or {}
    days = int(data.get("days", 3))
    conn = db()
    u = conn.execute("SELECT trial_expires_at, plan FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"ok": False, "error": "Usuário não encontrado"})
    # Parte da data atual de vencimento ou de hoje
    try:
        base = _dt.fromisoformat(u["trial_expires_at"]) if u["trial_expires_at"] else _dt.utcnow()
        if base < _dt.utcnow():
            base = _dt.utcnow()
    except Exception:
        base = _dt.utcnow()
    new_exp = (base + _td(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("UPDATE users SET trial_expires_at=?, plan='trial' WHERE id=?", (new_exp, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "trial_expires_at": new_exp[:10]})

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

# ── WA Instance Management (admin cria instância para o cliente) ───────────────
def _admin_evo_cfg():
    """Retorna config do admin (EVO URL + chave global) para gerenciar instâncias."""
    admin = db().execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    if not admin:
        return None
    return load_config(user_id=admin["id"])

@app.route("/api/admin/users/<int:uid>/wa-instance", methods=["POST"])
@require_admin
def admin_create_wa_instance(uid):
    """Cria uma instância WA para o cliente no servidor Evolution API do admin."""
    cfg = _admin_evo_cfg()
    if not cfg or not cfg.get("evo_url"):
        return jsonify({"ok": False, "error": "Configure o Evolution API no seu perfil admin primeiro."})
    base   = cfg["evo_url"].rstrip("/")
    apikey = cfg.get("evo_token", "")
    instance_name = f"zapshot_u{uid}"
    headers = {"apikey": apikey, "Content-Type": "application/json"}
    try:
        # Cria instância
        r = requests.post(f"{base}/instance/create", headers=headers, json={
            "instanceName": instance_name,
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
        }, timeout=15)
        data = r.json()
        print(f"[wa-instance] create uid={uid}: {r.status_code} {data}")
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": data.get("message", str(data))})
        # Salva na config do cliente automaticamente
        user_cfg = load_config(user_id=uid)
        user_cfg["evo_url"]      = base
        user_cfg["evo_token"]    = apikey
        user_cfg["evo_instance"] = instance_name
        user_cfg["app_url"]      = cfg.get("app_url", "https://social-midia.onrender.com")
        save_config(uid, user_cfg)
        return jsonify({"ok": True, "instance": instance_name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/admin/users/<int:uid>/wa-qr")
@require_admin
def admin_wa_qr(uid):
    """Retorna QR Code base64 para o cliente escanear."""
    cfg = load_config(user_id=uid)
    base     = (cfg.get("evo_url") or "").rstrip("/")
    apikey   = cfg.get("evo_token", "")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return jsonify({"ok": False, "error": "Instância não configurada"})
    try:
        r = requests.get(f"{base}/instance/connect/{instance}",
                         headers={"apikey": apikey}, timeout=15)
        data = r.json()
        print(f"[wa-qr] uid={uid}: {r.status_code} keys={list(data.keys())}")
        # QR pode vir em vários formatos conforme versão do Evolution
        qr = (data.get("qrcode") or {}).get("base64") or data.get("base64") or data.get("qr") or ""
        if not qr and "code" in data:
            qr = data["code"]
        return jsonify({"ok": True, "qr": qr, "raw": data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/admin/users/<int:uid>/wa-pairing", methods=["POST"])
@require_admin
def admin_wa_pairing(uid):
    """Gera código de pareamento (8 dígitos) para vincular sem QR Code."""
    data     = request.get_json(force=True) or {}
    phone    = (data.get("phone", "") or "").strip().replace(" ","").replace("-","").replace("(","").replace(")","").replace("+","")
    if not phone:
        return jsonify({"ok": False, "error": "Informe o número do celular"}), 400
    cfg      = load_config(user_id=uid)
    base     = (cfg.get("evo_url") or "").rstrip("/")
    apikey   = cfg.get("evo_token", "")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return jsonify({"ok": False, "error": "Instância não configurada — crie primeiro"})
    try:
        r = requests.post(
            f"{base}/instance/pairingCode/{instance}",
            headers={"apikey": apikey, "Content-Type": "application/json"},
            json={"number": phone},
            timeout=15
        )
        d = r.json()
        print(f"[wa-pairing] uid={uid} phone={phone}: {r.status_code} {d}")
        code = d.get("code") or d.get("pairingCode") or d.get("pairing_code") or ""
        if code:
            return jsonify({"ok": True, "code": code})
        return jsonify({"ok": False, "error": d.get("message") or "Código não retornado", "raw": d})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/admin/users/<int:uid>/wa-status")
@require_admin
def admin_wa_status(uid):
    """Retorna estado da conexão WA do cliente."""
    cfg = load_config(user_id=uid)
    base     = (cfg.get("evo_url") or "").rstrip("/")
    apikey   = cfg.get("evo_token", "")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return jsonify({"ok": True, "state": "not_configured"})
    try:
        r = requests.get(f"{base}/instance/connectionState/{instance}",
                         headers={"apikey": apikey}, timeout=10)
        data = r.json()
        state = (data.get("instance") or {}).get("state") or data.get("state") or "unknown"
        # Tenta buscar número conectado
        phone = ""
        try:
            ri = requests.get(f"{base}/instance/fetchInstances",
                              headers={"apikey": apikey}, timeout=8)
            instances = ri.json() if ri.status_code == 200 else []
            if isinstance(instances, list):
                for inst in instances:
                    if inst.get("name") == instance or inst.get("instanceName") == instance:
                        owner = inst.get("ownerJid") or inst.get("owner") or ""
                        phone = owner.split("@")[0] if owner else ""
                        break
        except Exception:
            pass
        return jsonify({"ok": True, "state": state, "instance": instance, "phone": phone})
    except Exception as exc:
        return jsonify({"ok": True, "state": "error", "error": str(exc)})

@app.route("/api/admin/users/<int:uid>/wa-disconnect", methods=["POST"])
@require_admin
def admin_wa_disconnect(uid):
    """Desconecta e deleta a instância WA do cliente."""
    cfg = load_config(user_id=uid)
    base     = (cfg.get("evo_url") or "").rstrip("/")
    apikey   = cfg.get("evo_token", "")
    instance = cfg.get("evo_instance", "")
    if not base or not instance:
        return jsonify({"ok": False, "error": "Sem instância"})
    try:
        # 1. Logout do WhatsApp
        requests.delete(f"{base}/instance/logout/{instance}",
                        headers={"apikey": apikey}, timeout=10)
        # 2. Deleta a instância
        requests.delete(f"{base}/instance/delete/{instance}",
                        headers={"apikey": apikey}, timeout=10)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

def save_config(uid, cfg_dict):
    conn = db()
    data = json.dumps(cfg_dict, ensure_ascii=False)
    if USE_PG:
        conn.execute("""INSERT INTO user_configs (user_id, config_json) VALUES (%s,%s)
                        ON CONFLICT (user_id) DO UPDATE SET config_json=EXCLUDED.config_json""",
                     (uid, data))
    else:
        conn.execute("INSERT OR REPLACE INTO user_configs (user_id, config_json) VALUES (?,?)",
                     (uid, data))
    conn.commit()
    conn.close()

@app.route("/admin/users/<int:uid>/feature/<feat>", methods=["POST"])
@require_admin
def admin_toggle_feature(uid, feat):
    """Ativa ou desativa uma feature para um usuário."""
    ALLOWED_FEATURES = {"grupos", "instagram", "wa_status", "relatorio"}
    if feat not in ALLOWED_FEATURES:
        return jsonify({"ok": False, "error": "Feature inválida"}), 400
    conn = db()
    row = conn.execute("SELECT features FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Usuário não encontrado"}), 404
    try:
        features = json.loads(row["features"] or "[]")
    except Exception:
        features = []
    if feat in features:
        features.remove(feat)
        active = False
    else:
        features.append(feat)
        active = True
    conn.execute("UPDATE users SET features=? WHERE id=?", (json.dumps(features), uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "active": active, "features": features})


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

@app.route("/api/admin/create-user", methods=["POST"])
@require_admin
def api_admin_create_user():
    """Admin cria usuário diretamente sem passar pela tela de registro."""
    data     = request.get_json(force=True) or {}
    email    = (data.get("email", "") or "").strip().lower()
    name     = (data.get("name",  "") or "").strip()
    password = (data.get("password", "") or "").strip()
    plan     = data.get("plan", "trial")
    days     = int(data.get("trial_days", 3))
    phone    = (data.get("phone", "") or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if not email or not password:
        return jsonify({"ok": False, "error": "Email e senha são obrigatórios"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Senha deve ter ao menos 6 caracteres"}), 400

    from datetime import datetime as _dt, timedelta as _td
    trial_exp = (_dt.utcnow() + _td(days=days)).strftime("%Y-%m-%dT%H:%M:%S") if plan == "trial" else None

    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, plan, is_admin, trial_expires_at, must_change_password) VALUES (?,?,?,?,0,?,1)",
            (email, _hash_pw(password), name, plan, trial_exp)
        )
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        uid  = user["id"]
        conn.close()
        # Salva phone e senha temp no config do cliente para reenvio futuro
        if phone:
            save_config(uid, {"welcome_phone": phone, "welcome_password": password,
                              "welcome_name": name or email.split("@")[0]})
    except Exception as _ie:
        conn.close()
        return jsonify({"ok": False, "error": "Email já cadastrado"}), 400

    # Envia mensagem de boas-vindas via WhatsApp (em background, sem bloquear)
    wa_sent = False
    if phone:
        try:
            admin_cfg = _admin_evo_cfg() or {}
            evo_url   = (admin_cfg.get("evo_url") or "").rstrip("/")
            evo_tok   = admin_cfg.get("evo_token", "")
            instance  = admin_cfg.get("evo_instance", f"zapshot_u{session.get('user_id',1)}")
            app_url   = (admin_cfg.get("app_url") or request.host_url.rstrip("/"))

            nome_cli  = name or email.split("@")[0]
            trial_txt = f"Seu acesso é válido por *{days} dias* de teste gratuito." if plan == "trial" else "Seu acesso está ativo."

            msg = (
                f"👋 Olá, *{nome_cli}*! Seja bem-vindo(a) ao *⚡ ZapShot*!\n\n"
                f"Sua conta foi criada com sucesso. Aqui estão seus dados de acesso:\n\n"
                f"🔗 *Link do app:* {app_url.rstrip('/')}/login\n"
                f"📧 *Email:* {email}\n"
                f"🔑 *Senha temporária:* {password}\n\n"
                f"⚠️ No primeiro acesso você será solicitado a criar uma senha pessoal.\n\n"
                f"📲 *Como instalar no celular:*\n"
                f"1. Abra o link acima no navegador (Chrome ou Safari)\n"
                f"2. Toque no menu do navegador ⋮\n"
                f"3. Selecione *\"Adicionar à tela inicial\"*\n"
                f"4. Pronto! O app fica salvo como ícone no seu celular 📱\n\n"
                f"{trial_txt}\n\n"
                f"🚧 *AVISO IMPORTANTE — Versão BETA:*\n"
                f"O ZapShot está em fase de testes. Por ser BETA, sempre que houver uma atualização do sistema, "
                f"*todas as configurações serão perdidas* (API, token, grupos e posts agendados) e precisarão ser refeitas. "
                f"Agradecemos sua compreensão e paciência nessa fase! 🙏\n\n"
                f"Qualquer dúvida, me chame aqui! 🚀"
            )

            import threading
            def _enviar():
                try:
                    requests.post(
                        f"{evo_url}/message/sendText/{instance}",
                        headers={"apikey": evo_tok, "Content-Type": "application/json"},
                        json={"number": phone, "text": msg},
                        timeout=15
                    )
                    print(f"[create-user] boas-vindas WA enviado para {phone}")
                except Exception as exc:
                    print(f"[create-user] erro ao enviar WA: {exc}")
            threading.Thread(target=_enviar, daemon=True).start()
            wa_sent = True
        except Exception as exc:
            print(f"[create-user] erro ao preparar WA: {exc}")

    return jsonify({"ok": True, "uid": uid, "trial_expires_at": trial_exp, "wa_sent": wa_sent})

@app.route("/api/admin/users/<int:uid>/send-welcome", methods=["POST"])
@require_admin
def api_admin_send_welcome(uid):
    """Reenvia mensagem de boas-vindas WA para o cliente."""
    data  = request.get_json(force=True) or {}
    phone = (data.get("phone", "") or "").strip().replace(" ","").replace("-","").replace("(","").replace(")","")

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not user:
        return jsonify({"ok": False, "error": "Usuário não encontrado"}), 404

    # Tenta pegar phone salvo no config se não foi passado
    if not phone:
        cfg = load_config(user_id=uid)
        phone = cfg.get("welcome_phone", "")
    if not phone:
        return jsonify({"ok": False, "error": "Nenhum telefone cadastrado para este usuário"}), 400

    cfg = load_config(user_id=uid)
    nome_cli = cfg.get("welcome_name") or user["name"] or user["email"].split("@")[0]
    password = cfg.get("welcome_password", "****")
    email    = user["email"]
    plan     = user["plan"]

    admin_cfg = _admin_evo_cfg() or {}
    evo_url   = (admin_cfg.get("evo_url") or "").rstrip("/")
    evo_tok   = admin_cfg.get("evo_token", "")
    instance  = admin_cfg.get("evo_instance", "")
    app_url   = admin_cfg.get("app_url") or "https://social-midia.onrender.com"
    days      = 30

    if not evo_url or not instance:
        return jsonify({"ok": False, "error": "EVO API não configurada no admin"}), 400

    trial_txt = f"Seu acesso é válido por *{days} dias* de teste gratuito." if plan == "trial" else "Seu acesso está ativo."

    msg = (
        f"👋 Olá, *{nome_cli}*! Seja bem-vindo(a) ao *⚡ ZapShot*!\n\n"
        f"Sua conta foi criada com sucesso. Aqui estão seus dados de acesso:\n\n"
        f"🔗 *Link do app:* {app_url.rstrip('/')}/login\n"
        f"📧 *Email:* {email}\n"
        f"🔑 *Senha temporária:* {password}\n\n"
        f"⚠️ No primeiro acesso você será solicitado a criar uma senha pessoal.\n\n"
        f"📲 *Como instalar no celular:*\n"
        f"1. Abra o link acima no navegador (Chrome ou Safari)\n"
        f"2. Toque no menu do navegador ⋮\n"
        f"3. Selecione *\"Adicionar à tela inicial\"*\n"
        f"4. Pronto! O app fica salvo como ícone no seu celular 📱\n\n"
        f"{trial_txt}\n\n"
        f"🚧 *AVISO IMPORTANTE — Versão BETA:*\n"
        f"O ZapShot está em fase de testes. Por ser BETA, sempre que houver uma atualização do sistema, "
        f"*todas as configurações serão perdidas* (API, token, grupos e posts agendados) e precisarão ser refeitas. "
        f"Agradecemos sua compreensão e paciência nessa fase! 🙏\n\n"
        f"Qualquer dúvida, me chame aqui! 🚀"
    )
    try:
        import requests as _req
        r = _req.post(
            f"{evo_url}/message/sendText/{instance}",
            headers={"apikey": evo_tok, "Content-Type": "application/json"},
            json={"number": phone, "text": msg},
            timeout=15
        )
        print(f"[send-welcome] {r.status_code} para {phone}")
        return jsonify({"ok": True, "phone": phone})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/change-password", methods=["GET", "POST"])
@require_login
def change_password():
    u = get_current_user()
    error = ""
    if request.method == "POST":
        new_pw  = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm", "").strip()
        if len(new_pw) < 6:
            error = "A senha deve ter ao menos 6 caracteres."
        elif new_pw != confirm:
            error = "As senhas não coincidem."
        else:
            conn = db()
            conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                         (_hash_pw(new_pw), u["id"]))
            conn.commit()
            conn.close()
            session["show_welcome"] = True
            return redirect("/")
    return render_template("change_password.html", error=error, user_name=u.get("name") or u.get("email"))

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
    u = get_current_user()
    # Calcula dias restantes do trial
    trial_days_left = None
    if u and u.get("plan") == "trial" and u.get("trial_expires_at"):
        from datetime import datetime as _dt
        try:
            diff = _dt.fromisoformat(u["trial_expires_at"]) - _dt.utcnow()
            trial_days_left = max(0, diff.days)
        except Exception:
            pass
    show_welcome = session.pop("show_welcome", False)
    return render_template("index.html", current_user=u,
                           has_grupos=user_has_feature(u, "grupos") if u else False,
                           has_instagram=user_has_feature(u, "instagram") if u else False,
                           has_wa_status=user_has_feature(u, "wa_status") if u else False,
                           has_relatorio=user_has_feature(u, "relatorio") if u else False,
                           trial_days_left=trial_days_left,
                           show_welcome=show_welcome)

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
def _salvar_grupos_silencioso(cfg, uid, groups):
    """Salva grupos no banco em background sem travar a resposta.
    Prioriza o invite_link já vindo do fetchAllGroups.
    Para grupos sem link, tenta /group/inviteCode (funciona p/ qualquer membro).
    """
    base    = cfg.get("evo_url", "").rstrip("/")
    inst    = cfg.get("evo_instance", "")
    headers = _evo_headers(cfg)

    # JIDs que retornaram not-authorized (admin bloqueou link) — não tentar de novo
    _no_auth = set()

    def _get_invite(g):
        # Se já veio no fetchAllGroups, usa direto
        if g.get("invite_link"):
            return g["invite_link"]
        jid = g["id"]
        if jid in _no_auth:
            return ""
        try:
            r = requests.get(f"{base}/group/inviteCode/{inst}?groupJid={jid}",
                             headers=headers, timeout=8)
            d = r.json()
            url = (d.get("inviteUrl") or d.get("invite_url") or "")
            if url:
                return url
            code = (d.get("inviteCode") or d.get("code") or
                    d.get("invite") or d.get("link") or "")
            if code and code.startswith("https://"):
                return code
            if code:
                return f"https://chat.whatsapp.com/{code}"
            # Verifica se é not-authorized (admin desativou link)
            msgs = str(d)
            if "not-authorized" in msgs:
                _no_auth.add(jid)
            return ""
        except Exception:
            return ""

    conn = db()
    try:
        # Busca links SEQUENCIALMENTE com delay para não bater rate limit do WhatsApp
        links = []
        for g in groups:
            links.append(_get_invite(g))
            time.sleep(0.4)   # 400ms entre cada request → ~12s para 29 grupos

        for g, link in zip(groups, links):
            jid  = g["id"]
            name = g["name"]
            existing = conn.execute(
                "SELECT id, invite_link FROM wa_imported_groups WHERE user_id=? AND group_jid=?", (uid, jid)
            ).fetchone()
            if existing:
                # Só atualiza o link se agora temos um e antes não tinha
                new_link = link or (existing["invite_link"] or "")
                conn.execute(
                    "UPDATE wa_imported_groups SET name=?, invite_link=?, imported_at=datetime('now') WHERE user_id=? AND group_jid=?",
                    (name, new_link, uid, jid)
                )
            else:
                if USE_PG:
                    conn.execute(
                        "INSERT INTO wa_imported_groups (user_id, group_jid, name, invite_link) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, group_jid) DO NOTHING",
                        (uid, jid, name, link)
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO wa_imported_groups (user_id, group_jid, name, invite_link) VALUES (?,?,?,?)",
                        (uid, jid, name, link)
                    )
        conn.commit()
        print(f"[grupos_silencioso] uid={uid} grupos={len(groups)} com_link={sum(1 for l in links if l)}")
    except Exception as e:
        print(f"[grupos_silencioso] erro: {e}")
    finally:
        conn.close()


@app.route("/api/wa/groups")
@require_login
def api_wa_groups():
    cfg    = load_config()
    groups, err = wa_get_groups(cfg)
    if err and not groups:
        return jsonify({"ok": False, "error": err})

    # Salva grupos em background (não bloqueia a resposta)
    uid = session["user_id"]
    t = threading.Thread(target=_salvar_grupos_silencioso, args=(cfg, uid, groups), daemon=True)
    t.start()

    return jsonify({"ok": True, "groups": groups})

@app.route("/api/admin/users/<int:uid>/importar-grupos", methods=["POST"])
@require_admin
def admin_importar_grupos_usuario(uid):
    """Admin força importação dos grupos WA de um cliente."""
    cfg = load_config(user_id=uid)
    groups, err = wa_get_groups(cfg)
    if err and not groups:
        return jsonify({"ok": False, "error": err})
    threading.Thread(target=_salvar_grupos_silencioso, args=(cfg, uid, groups), daemon=True).start()
    return jsonify({"ok": True, "grupos": len(groups)})

@app.route("/api/admin/reset-stuck-posts", methods=["GET", "POST"])
@require_admin
def api_reset_stuck_posts():
    """Reseta posts presos em 'queued' ou 'sending' de volta para 'pending'."""
    conn = db()
    # Só reseta 'queued' — nunca 'sending' (pode estar rodando agora)
    n = conn.execute(
        "UPDATE posts SET status='pending' WHERE status='queued'"
    ).rowcount
    conn.commit(); conn.close()
    return jsonify({"ok": True, "resetados": n})

@app.route("/api/admin/migrar-db", methods=["GET", "POST"])
@require_admin
def api_migrar_db():
    """Força execução das migrations pendentes (adiciona colunas que faltam)."""
    import traceback
    resultados = []
    try:
        conn = db()
        migrations = [
            ("posts", "send_all_groups", "INTEGER DEFAULT 0"),
            ("posts", "wa_status",       "INTEGER DEFAULT 0"),
            ("posts", "suspend_from",    "TEXT DEFAULT ''"),
            ("posts", "suspend_to",      "TEXT DEFAULT ''"),
            ("posts", "batch_title",     "TEXT DEFAULT ''"),
            ("posts", "client_phone",    "TEXT DEFAULT ''"),
        ]
        for table, col, defn in migrations:
            if USE_PG:
                cur = conn._conn.cursor()
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name=%s AND column_name=%s
                """, (table, col))
                existe = cur.fetchone() is not None
            else:
                existe = True  # SQLite não precisa
            if not existe:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                    conn.commit()
                    resultados.append({"coluna": col, "status": "ADICIONADA"})
                except Exception as e:
                    resultados.append({"coluna": col, "status": "ERRO", "error": str(e)})
            else:
                resultados.append({"coluna": col, "status": "ja_existe"})
        conn.close()
        return jsonify({"ok": True, "migrations": resultados})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "traceback": traceback.format_exc()})

@app.route("/api/admin/diagnostico-posts", methods=["GET"])
@require_admin
def api_diagnostico_posts():
    """Diagnóstico: tenta inserir 1 post de teste e retorna erro detalhado."""
    import traceback, uuid as _uuid
    uid = session["user_id"]
    batch_id = str(_uuid.uuid4())
    now_str = datetime.now().isoformat(timespec="minutes")
    try:
        conn = db()
        # Verifica colunas da tabela posts
        if USE_PG:
            cur = conn._conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='posts' ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
        else:
            cur = conn._conn.execute("PRAGMA table_info(posts)")
            cols = [r[1] for r in cur.fetchall()]

        # Tenta inserir 1 post de teste
        SQL = """INSERT INTO posts
            (user_id,caption,filename,media_type,wa_groups,ig_feed,ig_stories,ig_reels,wa_status,
             scheduled_at,status,created_at,batch_id,batch_title,client_phone,suspend_from,suspend_to,send_all_groups)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)"""
        row = (uid, "TESTE_DIAG", "__test__", "image", "[]", 0, 0, 0, 0,
               now_str, now_str, batch_id, "TESTE", "", "", "", 0)
        conn.executemany(SQL, [row])
        conn.commit()
        # Remove o post de teste
        conn.execute("DELETE FROM posts WHERE batch_id=?", (batch_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "colunas": cols, "msg": "INSERT funcionou normalmente"})
    except Exception as exc:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "colunas": cols if 'cols' in dir() else [], "error": str(exc), "traceback": tb})

@app.route("/api/wa/debug-invite")
@require_admin
def api_wa_debug_invite():
    """Debug: testa busca de invite link de um grupo. Retorna raw da Evolution API."""
    cfg = load_config()
    base     = (cfg.get("evo_url") or "").rstrip("/")
    instance = cfg.get("evo_instance") or ""
    headers  = _evo_headers(cfg)
    groups, err = wa_get_groups(cfg)
    if err or not groups:
        return jsonify({"ok": False, "error": err or "Sem grupos"})
    results = []
    for g in groups[:5]:  # Testa só os 5 primeiros
        jid = g["id"]
        try:
            r = requests.get(f"{base}/group/inviteCode/{instance}?groupJid={jid}",
                             headers=headers, timeout=8)
            results.append({"group": g["name"], "jid": jid,
                            "http": r.status_code, "raw": r.text[:300]})
        except Exception as e:
            results.append({"group": g["name"], "jid": jid, "error": str(e)})
    return jsonify({"ok": True, "base": base, "instance": instance, "results": results})

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
    wa_groups      = data.get("wa_groups", [])
    send_all_groups = int(bool(data.get("send_all_groups", False)))
    u = get_current_user()
    ig_feed    = int(bool(data.get("ig_feed")))    if user_has_feature(u, "instagram") else 0
    ig_stories = int(bool(data.get("ig_stories"))) if user_has_feature(u, "instagram") else 0
    ig_reels   = int(bool(data.get("ig_reels")))   if user_has_feature(u, "instagram") else 0
    wa_status  = int(bool(data.get("wa_status")))  if user_has_feature(u, "wa_status")  else 0
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
    # Celular do cliente só salvo se o usuário tiver a feature "relatorio"
    _raw_phone    = data.get("client_phone", "") or ""
    client_phone  = re.sub(r"\D", "", _raw_phone) if user_has_feature(u, "relatorio") else ""
    # Janela de suspensão (só faz sentido para lotes com repeat_days > 1)
    suspend_from = ""
    suspend_to   = ""
    if repeat_days > 1 and data.get("suspend_enabled"):
        _sf = (data.get("suspend_from") or "").strip()
        _st = (data.get("suspend_to")   or "").strip()
        # Valida formato HH:MM
        import re as _re
        if _re.match(r"^\d{2}:\d{2}$", _sf) and _re.match(r"^\d{2}:\d{2}$", _st) and _sf != _st:
            suspend_from = _sf
            suspend_to   = _st

    try:
        base_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        return jsonify({"ok": False, "error": "Horário inválido"})

    interval_minutes = int(24 * 60 / times_per_day)
    total    = repeat_days * times_per_day
    batch_id = str(uuid.uuid4())
    created_at     = datetime.now().isoformat(timespec="seconds")
    wa_groups_json = json.dumps(wa_groups)

    # Monta todos os registros de uma vez para inserir em batch (muito mais rápido no PostgreSQL)
    rows = []
    for i in range(total):
        sched = (base_dt + timedelta(minutes=i * interval_minutes)).isoformat(timespec="minutes")
        rows.append((uid, caption, filename, media_type, wa_groups_json,
                     ig_feed, ig_stories, ig_reels, wa_status, sched, "pending", created_at,
                     batch_id, batch_title, client_phone, suspend_from, suspend_to, send_all_groups))

    try:
        conn = db()
        SQL = """INSERT INTO posts
            (user_id,caption,filename,media_type,wa_groups,ig_feed,ig_stories,ig_reels,wa_status,
             scheduled_at,status,created_at,batch_id,batch_title,client_phone,suspend_from,suspend_to,send_all_groups)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        conn.executemany(SQL, rows)
        conn.commit()
        first = conn.execute(
            "SELECT id FROM posts WHERE batch_id=? ORDER BY scheduled_at LIMIT 1", (batch_id,)
        ).fetchone()
        first_id = first["id"] if first else None
        conn.close()
        print(f"[create_post] batch {batch_id} ok: {len(rows)} posts")
        return jsonify({"ok": True, "id": first_id, "count": total, "batch_id": batch_id})
    except Exception as exc:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)})

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

@app.route("/api/posts/batch/<batch_id>", methods=["PATCH"])
@require_login
def api_edit_batch(batch_id):
    """Edita caption e/ou reagenda todos os posts pendentes de um lote."""
    uid  = session["user_id"]
    data = request.get_json() or {}
    conn = db()
    # Só edita posts ainda pendentes do usuário
    rows = conn.execute(
        "SELECT id, scheduled_at FROM posts WHERE batch_id=? AND user_id=? AND status='pending' ORDER BY scheduled_at",
        (batch_id, uid)
    ).fetchall()
    if not rows:
        conn.close()
        return jsonify({"ok": False, "error": "Nenhum post pendente neste lote"})

    updates = []
    new_caption   = data.get("caption")
    new_start_str = data.get("scheduled_at")  # novo horário do 1º post

    if new_start_str:
        try:
            new_start = datetime.fromisoformat(new_start_str)
            # Mantém o intervalo original entre os posts
            original_start = datetime.fromisoformat(rows[0]["scheduled_at"])
            for row in rows:
                original_dt = datetime.fromisoformat(row["scheduled_at"])
                delta = original_dt - original_start
                new_dt = new_start + delta
                updates.append((new_dt.strftime("%Y-%m-%dT%H:%M"), row["id"]))
        except Exception as e:
            conn.close()
            return jsonify({"ok": False, "error": f"Horário inválido: {e}"})

    if updates:
        for new_sched, pid in updates:
            if new_caption is not None:
                conn.execute("UPDATE posts SET scheduled_at=?, caption=? WHERE id=?", (new_sched, new_caption, pid))
            else:
                conn.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (new_sched, pid))
    elif new_caption is not None:
        conn.execute("UPDATE posts SET caption=? WHERE batch_id=? AND user_id=? AND status='pending'",
                     (new_caption, batch_id, uid))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "editados": len(rows)})

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

@app.route("/api/admin/users/<int:uid>/config", methods=["GET", "POST"])
@require_admin
def api_admin_user_config(uid):
    """Admin lê ou salva config de qualquer usuário."""
    if request.method == "POST":
        save_config(request.get_json() or {}, user_id=uid)
        return jsonify({"ok": True})
    return jsonify(load_config(user_id=uid))

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

# ── WA Connect (rotas do cliente) ────────────────────────────────────────────
def _get_user_evo_cfg():
    """Retorna (base, token, instance) da config do usuário logado."""
    cfg      = load_config()
    base     = (cfg.get("evo_url") or "").rstrip("/")
    token    = cfg.get("evo_token") or ""
    instance = cfg.get("evo_instance") or ""
    return base, token, instance

@app.route("/api/wa/status")
@require_login
def api_wa_status_user():
    base, token, instance = _get_user_evo_cfg()
    if not all([base, token, instance]):
        return jsonify({"ok": False, "error": "Não configurado"})
    try:
        r = requests.get(f"{base}/instance/connectionState/{instance}",
                         headers={"apikey": token}, timeout=10)
        if r.status_code == 200:
            state = r.json().get("instance", {}).get("state", "unknown")
            return jsonify({"ok": True, "state": state})
        return jsonify({"ok": False, "error": f"HTTP {r.status_code}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/wa/qr")
@require_login
def api_wa_qr_user():
    base, token, instance = _get_user_evo_cfg()
    if not all([base, token, instance]):
        return jsonify({"ok": False, "error": "Não configurado"})
    try:
        r = requests.get(f"{base}/instance/connect/{instance}",
                         headers={"apikey": token}, timeout=15)
        if r.status_code == 200:
            d = r.json()
            qr = d.get("base64") or d.get("qrcode", {}).get("base64", "")
            if qr and qr.startswith("data:image"):
                qr = qr.split(",", 1)[1]
            return jsonify({"ok": True, "qr_base64": qr})
        return jsonify({"ok": False, "error": f"HTTP {r.status_code}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/wa/pairing-code", methods=["POST"])
@require_login
def api_wa_pairing_user():
    base, token, instance = _get_user_evo_cfg()
    if not all([base, token, instance]):
        return jsonify({"ok": False, "error": "Não configurado"})
    data  = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip().replace(" ","").replace("-","").replace("(","").replace(")","")
    if not phone:
        return jsonify({"ok": False, "error": "Informe o número"})
    try:
        # Tenta endpoint v2
        r = requests.post(f"{base}/instance/pairingCode/{instance}",
                          headers={"apikey": token, "Content-Type": "application/json"},
                          json={"number": phone}, timeout=15)
        print(f"[pairing-code user] {r.status_code} {r.text[:300]}")
        if r.status_code == 404:
            # Tenta endpoint alternativo
            r = requests.post(f"{base}/instance/pairingCode",
                              headers={"apikey": token, "Content-Type": "application/json"},
                              json={"number": phone, "instanceName": instance}, timeout=15)
            print(f"[pairing-code user alt] {r.status_code} {r.text[:300]}")
        d = r.json() if r.content else {}
        code = d.get("code") or d.get("pairingCode") or d.get("pairing_code") or ""
        if code:
            return jsonify({"ok": True, "code": code})
        return jsonify({"ok": False, "error": d.get("message") or d.get("error") or f"HTTP {r.status_code}: {r.text[:200]}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/wa/disconnect", methods=["POST"])
@require_login
def api_wa_disconnect_user():
    base, token, instance = _get_user_evo_cfg()
    if not all([base, token, instance]):
        return jsonify({"ok": False, "error": "Não configurado"})
    try:
        requests.delete(f"{base}/instance/delete/{instance}",
                        headers={"apikey": token}, timeout=15)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

@app.route("/api/grupos/debug", methods=["POST"])
@require_login
@require_feature("grupos")
def api_grupos_debug():
    """Diagnóstico completo: ScaleSerp + raspagem direta."""
    data    = request.get_json() or {}
    local   = data.get("local", "Campinas SP")
    tema    = data.get("tema", "")
    cfg     = load_config()
    api_key = cfg.get("google_api_key", "") or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "sem chave api"})

    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    WA = re.compile(r'https://chat\.whatsapp\.com/invite/[A-Za-z0-9_-]+')

    out = {}

    # 1. Query site:gruposwhats.app
    q1 = f"site:gruposwhats.app {local} {tema}".strip()
    try:
        r1 = requests.get("https://api.scaleserp.com/search",
            params={"api_key": api_key, "q": q1, "num": 10, "gl": "br", "hl": "pt"}, timeout=15)
        org1 = r1.json().get("organic_results", [])
        out["q_site_gruposwhats"] = {"query": q1, "count": len(org1),
            "results": [{"title": it.get("title","")[:80], "link": it.get("link","")} for it in org1]}
    except Exception as e:
        out["q_site_gruposwhats"] = {"error": str(e)}

    # 2. Testa API interna do gruposwhats.app
    test_id = "695038"
    base_url = f"https://gruposwhats.app/group/{test_id}"
    api_results = {}
    try:
        # Pega CSRF token da página
        pg2 = requests.get(base_url, timeout=8, headers=UA)
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', pg2.text)
        csrf_token = csrf.group(1) if csrf else ""

        # Procura qualquer padrão de link/invite no HTML completo
        invite_codes = re.findall(r'[A-Za-z0-9_-]{20,}', pg2.text)
        wa_matches = [c for c in invite_codes if len(c) >= 20]

        # Tenta endpoints comuns de API Laravel
        endpoints = [
            ("GET",  f"https://gruposwhats.app/api/group/{test_id}"),
            ("GET",  f"https://gruposwhats.app/api/grupos/{test_id}"),
            ("POST", f"https://gruposwhats.app/group/{test_id}/redirect"),
            ("POST", f"https://gruposwhats.app/group/{test_id}/participar"),
            ("POST", f"https://gruposwhats.app/group/{test_id}/link"),
            ("GET",  f"https://gruposwhats.app/group/{test_id}/go"),
        ]
        ep_results = []
        hdrs = {**UA, "X-CSRF-TOKEN": csrf_token, "X-Requested-With": "XMLHttpRequest",
                "Referer": base_url, "Accept": "application/json, text/plain, */*"}
        for method, ep_url in endpoints:
            try:
                if method == "POST":
                    rr = requests.post(ep_url, timeout=5, headers=hdrs,
                                       data={"_token": csrf_token}, allow_redirects=False)
                else:
                    rr = requests.get(ep_url, timeout=5, headers=hdrs, allow_redirects=False)
                wa_in_resp = WA.findall(rr.text)
                ep_results.append({
                    "url": ep_url, "method": method,
                    "status": rr.status_code,
                    "location": rr.headers.get("Location",""),
                    "wa_links": wa_in_resp,
                    "body_snippet": rr.text[:200],
                })
            except Exception as ee:
                ep_results.append({"url": ep_url, "error": str(ee)})

        out["group_page_test"] = {
            "csrf_found": bool(csrf_token),
            "html_long_codes_sample": wa_matches[:10],
            "endpoints": ep_results,
        }
    except Exception as e:
        out["group_page_test"] = {"error": str(e)}

    # 3. Acesso direto ao gruposwhats.app/estado
    uf_set = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG",
              "PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"}
    parts = local.split()
    uf    = next((p.upper() for p in parts if p.upper() in uf_set), "")
    city  = " ".join(p for p in parts if p.upper() != uf).strip()
    slug  = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
    du    = f"https://gruposwhats.app/estado/{uf}/{slug}" if slug and uf else f"https://gruposwhats.app/estado/{uf}"
    try:
        pg = requests.get(du, timeout=8, headers=UA)
        wa_links = WA.findall(pg.text)
        out["direto"] = {"url": du, "status": pg.status_code,
                         "wa_links_found": len(wa_links),
                         "wa_links_sample": wa_links[:5],
                         "html_snippet": pg.text[:800]}
    except Exception as e:
        out["direto"] = {"url": du, "error": str(e)}

    return jsonify({"ok": True, "debug": out})

@app.route("/api/grupos/nome", methods=["POST"])
@require_login
@require_feature("grupos")
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
@require_feature("grupos")
def api_buscar_grupos():
    data           = request.get_json() or {}
    tema           = data.get("tema", "").strip()
    local          = data.get("local", "").strip()
    cidades_regiao = data.get("cidades_regiao")   # lista de nomes de cidades da microrregião
    cfg            = load_config()

    api_key = cfg.get("google_api_key", "") or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "no_key"})

    WA_PATTERN = re.compile(r'https://chat\.whatsapp\.com/invite/[A-Za-z0-9_-]+')
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # Extrai UF e cidade do local (ex: "Campinas SP" → uf=SP city=Campinas)
    uf_set = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG",
              "PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"}
    parts_local = local.split()
    uf        = next((p.upper() for p in parts_local if p.upper() in uf_set), "")
    city      = " ".join(p for p in parts_local if p.upper() != uf).strip()

    def _scaleserp(q, num=100):
        try:
            r = requests.get("https://api.scaleserp.com/search",
                params={"api_key": api_key, "q": q, "num": num, "gl": "br", "hl": "pt"},
                timeout=20)
            return r.json().get("organic_results", [])
        except Exception:
            return []

    # Agrupa lista de cidades em lotes de N para usar OR no Google
    def _batches(lst, size):
        for i in range(0, len(lst), size):
            yield lst[i:i+size]

    def _or_expr(cities):
        """Monta '(Campinas OR Sumaré OR Valinhos)' ou só 'Campinas'."""
        if len(cities) == 1:
            return cities[0]
        return "(" + " OR ".join(cities) + ")"

    def _build_lote_queries():
        """
        Retorna lista de (label, queries_diretas, queries_gw).
        Com região: agrupa 8 cidades por lote via OR → menos chamadas à ScaleSerp.
        Sem região: usa local normal.
        """
        if cidades_regiao and isinstance(cidades_regiao, list):
            lotes = []
            todas = cidades_regiao[:60]   # aceita até 60 cidades (≈8 lotes de 8)
            for lote in _batches(todas, 8):
                or_expr = _or_expr(lote)
                loc_or  = f"{or_expr} {uf}".strip() if uf else or_expr
                q_local_or = f"{loc_or} {tema}".strip()
                qs_dir = [
                    f'"chat.whatsapp.com/invite" {q_local_or}',
                    f'grupos whatsapp {q_local_or} "chat.whatsapp.com"',
                ]
                if tema:
                    qs_dir.insert(0, f'"chat.whatsapp.com/invite" {tema} {or_expr}')
                qs_gw = [
                    f"site:gruposwhats.app {q_local_or}",
                    f"site:gruposwhats.app {or_expr}",
                ]
                lotes.append((or_expr, qs_dir, qs_gw))
            return lotes
        else:
            q_local = f"{local} {tema}".strip()
            qs_dir  = [
                f'"chat.whatsapp.com/invite" {q_local}',
                f'grupos whatsapp {q_local} "chat.whatsapp.com"',
            ]
            if tema:
                qs_dir.insert(0, f'"chat.whatsapp.com/invite" {tema} {city}')
            qs_gw = [f"site:gruposwhats.app {q_local}", f"site:gruposwhats.app {city}"]
            return [(city, qs_dir, qs_gw)]

    def _parse_serp_items(items, seen_codes, seen_gw_urls, results, gw_results, pages_to_scrape, mode):
        """Processa resultados ScaleSerp (thread-safe via lock externo)."""
        for it in items:
            url     = it.get("link", "")
            title   = it.get("title", "")
            snippet = it.get("snippet", "")
            if mode == "dir":
                m = WA_PATTERN.search(url)
                if m:
                    code = m.group(0).split("/")[-1]
                    if code not in seen_codes:
                        seen_codes.add(code)
                        results.append({"link": m.group(0), "name": title,
                                        "title": title, "snippet": snippet})
                    continue
                for m in WA_PATTERN.finditer(snippet):
                    code = m.group(0).split("/")[-1]
                    if code not in seen_codes:
                        seen_codes.add(code)
                        results.append({"link": m.group(0), "name": title,
                                        "title": title, "snippet": snippet})
                if ("chat.whatsapp" in snippet or "chat.whatsapp" in title) and \
                   not any(d in url for d in ("whatsapp.com", "wa.me")):
                    pages_to_scrape.append((url, title))
            else:  # gw
                url = url.rstrip("/")
                if url and url not in seen_gw_urls and "/group/" in url:
                    seen_gw_urls.add(url)
                    nome = re.sub(r'\s*[|–\-].*$', '', title).strip()
                    nome = re.sub(r'^Grupo de WhatsApp\s*', '', nome).strip() or "Grupo WhatsApp"
                    gw_results.append({"link": url, "name": nome, "title": nome,
                                       "snippet": snippet, "type": "group_page"})

    try:
        seen_codes   = set()
        seen_gw_urls = set()
        results      = []
        gw_results   = []
        pages_to_scrape = []
        lock         = threading.Lock()

        lotes = _build_lote_queries()

        # Monta lista completa de tarefas: (query, mode)
        tasks = []
        for _, qs_dir, qs_gw in lotes:
            for q in qs_dir:
                tasks.append((q, "dir"))
            for q in qs_gw:
                tasks.append((q, "gw"))

        def _run_task(args):
            q, mode = args
            items = _scaleserp(q, num=100)
            return (items, mode)

        # ── Executa todas as queries ScaleSerp em paralelo (máx 8 threads) ──
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(_run_task, t) for t in tasks]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    items, mode = fut.result()
                    with lock:
                        _parse_serp_items(items, seen_codes, seen_gw_urls,
                                          results, gw_results, pages_to_scrape, mode)
                except Exception:
                    pass

        # ── Raspa páginas promissoras em paralelo (máx 8 threads) ──
        def _scrape(args):
            url, title = args
            try:
                pg = requests.get(url, timeout=5, headers=UA)
                found = []
                for m in WA_PATTERN.finditer(pg.text):
                    found.append((m.group(0), title))
                return found
            except Exception:
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_scrape, t) for t in pages_to_scrape[:15]]
            for fut in concurrent.futures.as_completed(futs):
                for wa_link, title in fut.result():
                    code = wa_link.split("/")[-1]
                    with lock:
                        if code not in seen_codes:
                            seen_codes.add(code)
                            results.append({"link": wa_link, "name": title,
                                            "title": title, "snippet": ""})

        for gr in gw_results:
            results.append(gr)

        # ── Grupos pinados (aparecem primeiro quando critérios batem) ──────────
        PINNED_GROUPS = [
            {
                "keywords": ["hortolândia", "hortolandia", "hortolândia sp", "hortolandia sp"],
                "temas":    [],  # vazio = qualquer tema
                "link":  "https://chat.whatsapp.com/IvArtQuj9kW9vmi3NJ58tA",
                "name":  "Feira do Rolo Hortolândia Campinas e região",
            },
        ]
        local_lower = local.lower()
        tema_lower  = tema.lower()
        pinned_to_add = []
        for pin in PINNED_GROUPS:
            kw_match    = any(k in local_lower for k in pin["keywords"])
            tema_match  = not pin["temas"] or any(t in tema_lower for t in pin["temas"])
            if kw_match and tema_match:
                code = pin["link"].split("/")[-1]
                if code not in seen_codes:
                    seen_codes.add(code)
                    pinned_to_add.append({
                        "link": pin["link"], "name": pin["name"],
                        "title": pin["name"], "snippet": ""
                    })
        results = pinned_to_add + results

        print(f"[buscar_grupos] lotes={len(lotes)} tasks={len(tasks)} diretos={len(seen_codes)} gw={len(gw_results)} pinned={len(pinned_to_add)} total={len(results)}")
        return jsonify({"ok": True, "results": results})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

# ── Importar grupos do WhatsApp do usuário ──────────────────────────────────────
@app.route("/api/grupos/importar-wa", methods=["POST"])
@require_login
def api_importar_grupos_wa():
    """Busca todos os grupos via Evolution API e salva no banco."""
    cfg     = load_config()
    evo_url = (cfg.get("evo_url") or os.getenv("EVO_URL", "")).rstrip("/")
    evo_tok = cfg.get("evo_token") or os.getenv("EVO_TOKEN", "")
    inst    = cfg.get("evo_instance") or os.getenv("EVO_INSTANCE", "")
    if not evo_url or not evo_tok or not inst:
        return jsonify({"ok": False, "error": "Configure a Evolution API primeiro (URL, Token e Instância)"})

    headers = {"apikey": evo_tok, "Content-Type": "application/json"}

    # 1. Busca todos os grupos da instância
    try:
        r = requests.get(f"{evo_url}/group/fetchAllGroups/{inst}?getParticipants=false",
                         headers=headers, timeout=20)
        groups_raw = r.json()
        if isinstance(groups_raw, dict) and "groups" in groups_raw:
            groups_raw = groups_raw["groups"]
        if not isinstance(groups_raw, list):
            return jsonify({"ok": False, "error": f"Resposta inesperada: {str(groups_raw)[:200]}"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao buscar grupos: {e}"})

    uid      = session["user_id"]
    conn     = db()
    saved    = 0
    updated  = 0

    for g in groups_raw:
        jid   = g.get("id", "")
        name  = g.get("subject", "") or g.get("name", "") or "Grupo sem nome"
        if not jid:
            continue

        # 2. Tenta pegar o invite link (pode falhar se não for admin do grupo)
        invite_link = ""
        try:
            ri = requests.get(f"{evo_url}/group/inviteCode/{inst}?groupJid={jid}",
                              headers=headers, timeout=8)
            rd = ri.json()
            code = rd.get("inviteCode") or rd.get("code") or ""
            if code:
                invite_link = f"https://chat.whatsapp.com/{code}"
        except Exception:
            pass

        # 3. Salva/atualiza no banco
        try:
            existing = conn.execute(
                "SELECT id FROM wa_imported_groups WHERE user_id=? AND group_jid=?", (uid, jid)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE wa_imported_groups SET name=?, invite_link=?, imported_at=datetime('now') WHERE user_id=? AND group_jid=?",
                    (name, invite_link, uid, jid)
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO wa_imported_groups (user_id, group_jid, name, invite_link) VALUES (?,?,?,?)",
                    (uid, jid, name, invite_link)
                )
                saved += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "novos": saved, "atualizados": updated, "total": saved + updated})


@app.route("/api/grupos/meus-wa", methods=["GET"])
@require_login
def api_meus_grupos_wa():
    """Retorna grupos WA importados pelo usuário logado."""
    uid  = session["user_id"]
    conn = db()
    rows = conn.execute(
        "SELECT id, group_jid, name, invite_link, imported_at FROM wa_imported_groups WHERE user_id=? ORDER BY name",
        (uid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/grupos-wa", methods=["GET"])
@require_admin
def api_admin_grupos_wa():
    """Admin: retorna todos os grupos importados de todos os usuários."""
    conn = db()
    rows = conn.execute("""
        SELECT g.id, g.group_jid, g.name, g.invite_link, g.imported_at,
               u.email as user_email, u.name as user_name
        FROM wa_imported_groups g
        JOIN users u ON u.id = g.user_id
        ORDER BY u.email, g.name
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/grupos-wa/<int:gid>", methods=["DELETE", "POST"])
@require_admin
def api_admin_delete_grupo_wa(gid):
    conn = db()
    conn.execute("DELETE FROM wa_imported_groups WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Proxy IBGE (evita bloqueio CORS no cliente) ──────────────────────────────────
@app.route("/api/ibge/municipios/<uf>")
def api_ibge_municipios(uf):
    try:
        r = requests.get(
            f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios?orderBy=nome",
            timeout=10
        )
        return Response(r.content, content_type="application/json")
    except Exception as e:
        return jsonify([]), 200

@app.route("/api/ibge/municipio/<int:ibge_id>")
def api_ibge_municipio(ibge_id):
    try:
        r = requests.get(
            f"https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{ibge_id}",
            timeout=10
        )
        return Response(r.content, content_type="application/json")
    except Exception as e:
        return jsonify({}), 200

@app.route("/api/ibge/microrregiao/<int:micro_id>/municipios")
def api_ibge_microrregiao(micro_id):
    try:
        r = requests.get(
            f"https://servicodados.ibge.gov.br/api/v1/localidades/microrregioes/{micro_id}/municipios",
            timeout=10
        )
        return Response(r.content, content_type="application/json")
    except Exception as e:
        return jsonify([]), 200

# ── Keep-alive (evita cold start no Render free) ────────────────────────────────
@app.route("/ping")
def ping():
    return "pong", 200

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
