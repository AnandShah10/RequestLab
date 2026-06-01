"""
RequestLab - A Postman Alternative built with Python + Flask
Run: pip install flask requests && python app.py
Then open: http://localhost:5000
"""

import json
import os
import time
import sqlite3
import hashlib
import traceback
import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, Response, session, redirect

app = Flask(__name__, static_folder='media', static_url_path='/media')
app.secret_key = os.environ.get('SECRET_KEY', 'requestlab-secret-' + hashlib.md5(os.path.abspath(__file__).encode()).hexdigest())
DB_PATH = "RequestLab.db"

# Load .env file if it exists
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                val = v.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                os.environ[k.strip()] = val

SMTP_CONFIG = {
    "server": os.environ.get("SMTP_SERVER", "smtp.gmail.com").strip('"').strip("'"),
    "port": int(str(os.environ.get("SMTP_PORT", "587")).strip('"').strip("'")),
    "user": os.environ.get("SMTP_USER", "").strip('"').strip("'"),
    "pass": os.environ.get("SMTP_PASS", "").strip('"').strip("'"),
    "use_tls": str(os.environ.get("SMTP_TLS", "True")).lower().strip('"').strip("'") == "true",
    "sender": os.environ.get("SMTP_SENDER", "noreply@requestlab.com").strip('"').strip("'")
}

# ─── Script Execution Engine ─────────────────────────────────────────────────

class AssertionBuilder:
    """Builder for assertions similar to Postman's expect API"""
    def __init__(self, name, value, assertions_list):
        self.name = name
        self.value = value
        self.assertions = assertions_list
    
    def to_equal(self, expected):
        passed = self.value == expected
        self.assertions.append({"name": self.name, "passed": passed, "expected": expected, "actual": self.value})
        return self
    
    def to_be_truthy(self):
        passed = bool(self.value)
        self.assertions.append({"name": self.name, "passed": passed, "actual": self.value})
        return self
    
    def to_exist(self):
        passed = self.value is not None
        self.assertions.append({"name": self.name, "passed": passed, "actual": self.value})
        return self
    
    def to_contain(self, substring):
        passed = substring in str(self.value)
        self.assertions.append({"name": self.name, "passed": passed, "expected": substring, "actual": self.value})
        return self
    
    def to_have_status(self, status_code):
        passed = self.value == status_code
        self.assertions.append({"name": self.name, "passed": passed, "expected": status_code, "actual": self.value})
        return self

class PostmanLikeAPI:
    """Implements pm.* API similar to Postman for scripting"""
    def __init__(self, context):
        self.environment = EnvironmentAPI(context.get("environment", {}))
        self.globals = GlobalsAPI(context.get("globals", {}))
        self.request = context.get("request", {})
        self.response = context.get("response", {})
        self.assertions = context.get("assertions", [])
    
    def expect(self, name, value=None):
        """Create an expectation/ assertion"""
        return AssertionBuilder(name, value, self.assertions)
    
    def test(self, name, test_fn):
        """Run a test function and record result"""
        try:
            result = test_fn()
            self.assertions.append({"name": name, "passed": bool(result)})
        except Exception as e:
            self.assertions.append({"name": name, "passed": False, "error": str(e)})

class EnvironmentAPI:
    """Environment variables API"""
    def __init__(self, vars_dict):
        self._vars = vars_dict if isinstance(vars_dict, dict) else {}
    
    def get(self, key, default=None):
        return self._vars.get(key, default)
    
    def set(self, key, value):
        self._vars[key] = value
    
    def unset(self, key):
        self._vars.pop(key, None)

class GlobalsAPI:
    """Global variables API"""
    def __init__(self, vars_dict):
        self._vars = vars_dict if isinstance(vars_dict, dict) else {}
    
    def get(self, key, default=None):
        return self._vars.get(key, default)
    
    def set(self, key, value):
        self._vars[key] = value
    
    def unset(self, key):
        self._vars.pop(key, None)

def execute_python_script(script, context):
    """Execute Python pre-request/test script with safe context"""
    if not script or not script.strip():
        return context
    
    try:
        pm = PostmanLikeAPI(context)
        safe_globals = {"__builtins__": {"len": len, "str": str, "int": int, "float": float, 
                                          "bool": bool, "dict": dict, "list": list, 
                                          "print": print, "range": range, "isinstance": isinstance}}
        safe_locals = {"pm": pm}
        exec(script, safe_globals, safe_locals)
        
        # Update context with modified environment/globals
        context["environment"] = pm.environment._vars
        context["globals"] = pm.globals._vars
        context["assertions"] = pm.assertions
    except Exception as e:
        context["script_error"] = str(e)
    
    return context


# ─── Database Setup ───────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email    TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                avatar_color TEXT DEFAULT '#00d4ff',
                created  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS collections (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name    TEXT NOT NULL,
                created TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS folders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id    INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                parent_folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                name             TEXT NOT NULL,
                created          TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                method        TEXT DEFAULT 'GET',
                url           TEXT DEFAULT '',
                params        TEXT DEFAULT '[]',
                headers       TEXT DEFAULT '[]',
                body_type     TEXT DEFAULT 'none',
                body_content  TEXT DEFAULT '',
                auth_type     TEXT DEFAULT 'none',
                auth_data     TEXT DEFAULT '{}',
                created       TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
                method        TEXT,
                url           TEXT,
                status_code   INTEGER,
                duration_ms   REAL,
                request_data  TEXT,
                response_data TEXT,
                timestamp     TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS environments (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name    TEXT NOT NULL,
                vars    TEXT DEFAULT '{}',
                active  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                token TEXT PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
        """)
        # Migration: add folder_id to requests if missing
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")
            conn.commit()
        except Exception:
            pass
            
        # Migration: add parent_folder_id to folders if missing
        try:
            conn.execute("ALTER TABLE folders ADD COLUMN parent_folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE")
            conn.commit()
        except Exception:
            pass

        # Migration: add user_id to collections if missing
        try:
            conn.execute("ALTER TABLE collections ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            conn.commit()
        except Exception:
            pass

        # Migration: add user_id to history if missing
        try:
            conn.execute("ALTER TABLE history ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            conn.commit()
        except Exception:
            pass

        # Migration: add user_id to environments if missing
        try:
            conn.execute("ALTER TABLE environments ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            conn.commit()
        except Exception:
            pass
        
        # Migration: add script columns to requests
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN pre_request_script TEXT DEFAULT ''")
            conn.execute("ALTER TABLE requests ADD COLUMN test_script TEXT DEFAULT ''")
            conn.execute("ALTER TABLE requests ADD COLUMN script_language TEXT DEFAULT 'javascript'")
            conn.commit()
        except Exception:
            pass
        
        # Create mock_endpoints table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mock_endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                method TEXT DEFAULT 'GET',
                response_body TEXT DEFAULT '{}',
                content_type TEXT DEFAULT 'application/json',
                status_code INTEGER DEFAULT 200,
                delay_ms INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created TEXT DEFAULT (datetime('now'))
            );
        """)
        
        # Create collection_runs table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                iterations INTEGER DEFAULT 1,
                status TEXT DEFAULT 'running',
                start_time TEXT DEFAULT (datetime('now')),
                end_time TEXT,
                total_requests INTEGER DEFAULT 0,
                passed INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0
            );
        """)

        # Migration: add avatar_color to users if missing
        try:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_color TEXT DEFAULT '#00d4ff'")
            conn.commit()
        except Exception:
            pass

        # Migration: add global_vars to users if missing
        try:
            conn.execute("ALTER TABLE users ADD COLUMN global_vars TEXT DEFAULT '{}'")
            conn.commit()
        except Exception:
            pass

        # Migration: add vars to collections if missing
        try:
            conn.execute("ALTER TABLE collections ADD COLUMN vars TEXT DEFAULT '{}'")
            conn.commit()
        except Exception:
            pass

        # ── Team Collaboration tables ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS team_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT DEFAULT 'viewer',
                invited_at TEXT DEFAULT (datetime('now')),
                joined_at TEXT,
                UNIQUE(team_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS shared_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                shared_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                shared_at TEXT DEFAULT (datetime('now')),
                UNIQUE(team_id, collection_id)
            );
        """)

        # ── Advanced Testing / CI/CD tables ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS test_suites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                collection_ids TEXT DEFAULT '[]',
                schedule_cron TEXT DEFAULT '',
                last_run TEXT,
                status TEXT DEFAULT 'idle',
                created TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suite_id INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
                run_id INTEGER,
                status TEXT DEFAULT 'running',
                total INTEGER DEFAULT 0,
                passed INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                duration_ms REAL DEFAULT 0,
                triggered_by TEXT DEFAULT 'manual',
                details TEXT DEFAULT '[]',
                started_at TEXT DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ci_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created TEXT DEFAULT (datetime('now')),
                last_used TEXT
            );
        """)

        # ── API Documentation table ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                content TEXT DEFAULT '{}',
                updated TEXT DEFAULT (datetime('now')),
                UNIQUE(collection_id)
            );
        """)

        # ── API Monitors table ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                method TEXT DEFAULT 'GET',
                headers TEXT DEFAULT '[]',
                body_type TEXT DEFAULT 'none',
                body_content TEXT DEFAULT '',
                interval_seconds INTEGER DEFAULT 300,
                expected_status INTEGER DEFAULT 200,
                expected_body TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                last_status TEXT DEFAULT 'never',
                last_check TEXT,
                last_duration_ms REAL,
                uptime_pct REAL DEFAULT 100.0,
                total_checks INTEGER DEFAULT 0,
                total_failures INTEGER DEFAULT 0,
                created TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS monitor_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                status TEXT DEFAULT 'success',
                status_code INTEGER,
                duration_ms REAL,
                response_body TEXT DEFAULT '',
                error TEXT DEFAULT '',
                checked_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # Migration: add pre_processor / post_processor to requests
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN pre_processor TEXT DEFAULT ''")
            conn.execute("ALTER TABLE requests ADD COLUMN post_processor TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            pass

        # Migration: add cookies column to requests
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN cookies TEXT DEFAULT '[]'")
            conn.commit()
        except Exception:
            pass

init_db()


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user_id():
    return session.get('user_id')

def require_auth():
    uid = get_current_user_id()
    if not uid:
        return None
    return uid


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    d = request.json or {}
    username = d.get("username", "").strip()
    email = d.get("email", "").strip().lower()
    password = d.get("password", "")
    if not username or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
    pw_hash = hash_password(password)
    colors = ['#00d4ff','#3dd68c','#f0883e','#d2a8ff','#f47067','#e3b341','#79c0ff']
    import random
    avatar_color = random.choice(colors)
    try:
        with get_db() as conn:
            uid = conn.execute(
                "INSERT INTO users (username, email, password, avatar_color) VALUES (?,?,?,?)",
                (username, email, pw_hash, avatar_color)
            ).lastrowid
        session['user_id'] = uid
        session['username'] = username
        return jsonify({"ok": True, "user": {"id": uid, "username": username, "email": email, "avatar_color": avatar_color}})
    except sqlite3.IntegrityError as e:
        if 'username' in str(e):
            return jsonify({"error": "Username already exists"}), 409
        return jsonify({"error": "Email already registered"}), 409

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    d = request.json or {}
    login = d.get("login", "").strip()  # can be username or email
    password = d.get("password", "")
    if not login or not password:
        return jsonify({"error": "Username/email and password required"}), 400
    pw_hash = hash_password(password)
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE (username=? OR email=?) AND password=?",
            (login, login.lower(), pw_hash)
        ).fetchone()
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    session['user_id'] = user['id']
    session['username'] = user['username']
    return jsonify({"ok": True, "user": {"id": user['id'], "username": user['username'], "email": user['email'], "avatar_color": user['avatar_color']}})

@app.route("/api/auth/forgot-password", methods=["POST"])
def auth_forgot_password():
    data = request.json or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    with get_db() as conn:
        user = conn.execute("SELECT id, username FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            # Prevent email enumeration by returning success anyway
            return jsonify({"ok": True, "message": "Check your inbox! If an account exists, a reset link has been sent."})

        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        conn.execute("INSERT INTO password_resets (token, user_id, expires_at) VALUES (?, ?, ?)", (token, user['id'], expires))
        
    reset_link = f"http://localhost:5000/app?reset_token={token}"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ margin: 0; padding: 0; background-color: #0d1117; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
        .wrapper {{ width: 100%; table-layout: fixed; background-color: #0d1117; padding: 40px 0; }}
        .main {{ background-color: #161b22; margin: 0 auto; width: 100%; max-width: 600px; border-spacing: 0; border-radius: 12px; border: 1px solid #30363d; color: #cdd9e5; overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #00d4ff 0%, #d2a8ff 100%); padding: 40px; text-align: center; }}
        .content {{ padding: 40px; text-align: left; }}
        h1 {{ color: #ffffff; font-size: 26px; font-weight: 700; margin: 0; letter-spacing: -0.5px; }}
        p {{ font-size: 16px; line-height: 24px; margin: 20px 0; color: #8b9eb5; }}
        .btn-container {{ text-align: center; margin: 35px 0; }}
        .btn {{ background-color: #00d4ff; color: #000000 !important; text-decoration: none; padding: 16px 36px; border-radius: 30px; font-weight: 700; font-size: 16px; display: inline-block; box-shadow: 0 4px 20px rgba(0, 212, 255, 0.4); }}
        .footer {{ padding: 25px 40px; text-align: center; border-top: 1px solid #30363d; background-color: #0d1117; }}
        .footer p {{ font-size: 13px; color: #4d6377; margin: 0; }}
        .logo-img {{ width: 64px; height: 64px; margin-bottom: 15px; filter: drop-shadow(0 0 10px rgba(255,255,255,0.2)); }}
      </style>
    </head>
    <body>
      <div class="wrapper">
        <table class="main" align="center">
          <tr>
            <td class="header">
              <img src="cid:logo" class="logo-img" alt="RequestLab">
              <h1>Reset Your Password</h1>
            </td>
          </tr>
          <tr>
            <td class="content">
              <p>Hello <strong>{user['username']}</strong>,</p>
              <p>We received a request to reset your password for RequestLab. If you didn't request this, you can safely ignore this email. No changes have been made to your account yet.</p>
              <div class="btn-container">
                <a href="{reset_link}" class="btn">Set New Password</a>
              </div>
              <p>This link will remain active for <strong>1 hour</strong>. For security reasons, the link can only be used once.</p>
              <p>Happy coding!<br>The RequestLab Team</p>
            </td>
          </tr>
          <tr>
            <td class="footer">
              <p>&copy; 2026 RequestLab &bull; Modern API Client</p>
            </td>
          </tr>
        </table>
      </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg['Subject'] = 'RequestLab Password Reset'
    msg['From'] = SMTP_CONFIG['sender']
    msg['To'] = email

    part1 = MIMEText(f"Hello {user['username']},\n\nClick the link below to reset your password:\n{reset_link}\n\nIf you didn't request this, ignore this email.\nThis link expires in 1 hour.", "plain")
    part2 = MIMEText(html_content, "html")

    msg.attach(part1)
    msg.attach(part2)

    # Inline logo
    logo_path = os.path.join(app.static_folder, 'logo.png')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<logo>')
            msg.attach(img)

    try:
        with smtplib.SMTP(SMTP_CONFIG['server'], SMTP_CONFIG['port']) as server:
            if SMTP_CONFIG['use_tls']:
                server.starttls()
            if SMTP_CONFIG['user'] and SMTP_CONFIG['pass']:
                server.login(SMTP_CONFIG['user'], SMTP_CONFIG['pass'])
            server.send_message(msg)
    except Exception as e:
        print("SMTP Error:", e)
        return jsonify({"error": f"Failed to send email. Check SMTP configuration in postman.py. Details: {e}"}), 500

    return jsonify({"ok": True, "message": "If an account exists, a reset link has been sent."})

@app.route("/api/auth/reset-password", methods=["POST"])
def auth_reset_password():
    data = request.json or {}
    token = data.get("token", "").strip()
    new_pw   = data.get("new_password", "").strip()

    if not token or not new_pw:
        return jsonify({"error": "Token and new password required"}), 400

    with get_db() as conn:
        reset = conn.execute("SELECT user_id, expires_at FROM password_resets WHERE token=?", (token,)).fetchone()
        if not reset:
            return jsonify({"error": "Invalid or expired token"}), 400
        
        # Check expiry
        if datetime.fromisoformat(reset['expires_at']) < datetime.utcnow():
            conn.execute("DELETE FROM password_resets WHERE token=?", (token,))
            return jsonify({"error": "Token has expired"}), 400

        hashed_pw = hashlib.sha256(new_pw.encode()).hexdigest()
        conn.execute("UPDATE users SET password=? WHERE id=?", (hashed_pw, reset['user_id']))
        conn.execute("DELETE FROM password_resets WHERE token=?", (token,))

    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    uid = get_current_user_id()
    if not uid:
        return jsonify({"authenticated": False}), 200
    with get_db() as conn:
        user = conn.execute("SELECT id,username,email,avatar_color FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        session.clear()
        return jsonify({"authenticated": False}), 200
    return jsonify({"authenticated": True, "user": dict(user)})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/globals", methods=["GET"])
def get_globals():
    uid = get_current_user_id()
    if not uid:
        return jsonify({"vars": "{}"}), 200
    with get_db() as conn:
        row = conn.execute("SELECT global_vars FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row["global_vars"]:
        return jsonify({"vars": "{}"}), 200
    return jsonify({"vars": row["global_vars"]}), 200

@app.route("/api/globals", methods=["PUT"])
def update_globals():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    vars_str = json.dumps((request.json or {}).get("vars", {}))
    with get_db() as conn:
        conn.execute("UPDATE users SET global_vars=? WHERE id=?", (vars_str, uid))
    return jsonify({"ok": True})

# ─── Mock Server ──────────────────────────────────────────────────────────────

@app.route("/api/mocks", methods=["GET"])
def list_mocks():
    uid = get_current_user_id()
    with get_db() as conn:
        if uid:
            rows = conn.execute("SELECT * FROM mock_endpoints WHERE user_id=? ORDER BY created DESC", (uid,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM mock_endpoints WHERE user_id IS NULL ORDER BY created DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/mocks", methods=["POST"])
def create_mock():
    uid = get_current_user_id()
    d = request.json or {}
    path = d.get("path", "/mock-endpoint")
    method = d.get("method", "GET").upper()
    response_body = d.get("response_body", "{}")
    content_type = d.get("content_type", "application/json")
    status_code = d.get("status_code", 200)
    delay_ms = d.get("delay_ms", 0)
    
    with get_db() as conn:
        mid = conn.execute(
            "INSERT INTO mock_endpoints (path,method,response_body,content_type,status_code,delay_ms,user_id) VALUES (?,?,?,?,?,?,?)",
            (path, method, response_body, content_type, status_code, delay_ms, uid)
        ).lastrowid
    return jsonify({"id": mid, "ok": True})

@app.route("/api/mocks/<int:mid>", methods=["PUT"])
def update_mock(mid):
    d = request.json or {}
    with get_db() as conn:
        conn.execute(
            "UPDATE mock_endpoints SET path=?,method=?,response_body=?,content_type=?,status_code=?,delay_ms=?,enabled=? WHERE id=?",
            (d.get("path"), d.get("method"), d.get("response_body"), d.get("content_type"),
             d.get("status_code", 200), d.get("delay_ms", 0), d.get("enabled", 1), mid)
        )
    return jsonify({"ok": True})

@app.route("/api/mocks/<int:mid>", methods=["DELETE"])
def delete_mock(mid):
    with get_db() as conn:
        conn.execute("DELETE FROM mock_endpoints WHERE id=?", (mid,))
    return jsonify({"ok": True})

@app.route("/mock/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def serve_mock(path):
    method = request.method
    with get_db() as conn:
        mock = conn.execute(
            "SELECT * FROM mock_endpoints WHERE path=? AND method=? AND enabled=1",
            (path, method)
        ).fetchone()
    
    if not mock:
        return jsonify({"error": "Mock endpoint not found"}), 404
    
    # Apply delay
    if mock["delay_ms"] > 0:
        time.sleep(mock["delay_ms"] / 1000.0)
    
    return Response(
        mock["response_body"],
        status=mock["status_code"],
        content_type=mock["content_type"]
    )


# ─── Proxy / Execute Request ──────────────────────────────────────────────────

@app.route("/api/execute", methods=["POST"])
def execute_request():
    if request.is_json:
        data = request.json or {}
    else:
        try:
            data = json.loads(request.form.get("payload", "{}"))
        except:
            data = {}
    method      = data.get("method", "GET").upper()
    url         = data.get("url", "").strip()
    params_list = data.get("params", [])
    headers_list= data.get("headers", [])
    cookies_list= data.get("cookies", [])
    body_type   = data.get("body_type", "none")
    body_content= data.get("body_content", "")
    auth_type   = data.get("auth_type", "none")
    auth_data   = data.get("auth_data", {})
    timeout     = data.get("timeout", 30)
    protocol    = data.get("protocol", "http")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    params  = {p["key"]: p["value"] for p in params_list  if p.get("key") and p.get("enabled", True)}
    headers = {h["key"]: h["value"] for h in headers_list if h.get("key") and h.get("enabled", True)}
    cookies = {c["key"]: c["value"] for c in cookies_list if c.get("key") and c.get("enabled", True)}

    auth = None
    if auth_type == "basic":
        auth = (auth_data.get("username",""), auth_data.get("password",""))
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth_data.get('token','')}"
    elif auth_type == "oauth2":
        prefix = auth_data.get("prefix", "Bearer")
        headers["Authorization"] = f"{prefix} {auth_data.get('token','')}".strip()
    elif auth_type == "awsv4":
        try:
            from requests_aws4auth import AWS4Auth
            access_key = auth_data.get("access_key", "")
            secret_key = auth_data.get("secret_key", "")
            region = auth_data.get("region", "us-east-1")
            service = auth_data.get("service", "execute-api")
            session_token = auth_data.get("session_token", "") or None
            auth = AWS4Auth(access_key, secret_key, region, service, session_token=session_token)
        except ImportError:
            return jsonify({"error": "requests-aws4auth is required for AWS Signature. Install it with: pip install requests-aws4auth"}), 400
    elif auth_type == "apikey":
        key_loc = auth_data.get("location","header")
        key_name= auth_data.get("key","X-API-Key")
        key_val = auth_data.get("value","")
        if key_loc == "header": headers[key_name] = key_val
        else:                   params[key_name]  = key_val

    req_body = None; req_json = None; form_data = None

    if body_type in ("json", "graphql"):
        try:
            req_json = json.loads(body_content) if body_content.strip() else None
            if "Content-Type" not in headers: headers["Content-Type"] = "application/json"
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Invalid {body_type.upper()} body: {e}"}), 400
    elif body_type in ("raw", "soap", "xml"):
        req_body = body_content.encode("utf-8")
        if protocol == "soap" or body_type == "soap":
            if "Content-Type" not in headers: headers["Content-Type"] = "text/xml; charset=utf-8"
            soap_action = auth_data.get("soap_action", "")
            if soap_action:
                headers["SOAPAction"] = f'"{soap_action}"'
        elif body_type == "xml" and "Content-Type" not in headers:
            headers["Content-Type"] = "application/xml"
    elif body_type in ("form", "urlencoded"):
        try:   form_data = json.loads(body_content) if body_content.strip() else {}
        except: form_data = {}
        if body_type == "urlencoded":
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req_files = None
    if not request.is_json and request.files:
        req_files = {}
        for k, v in request.files.items():
            if k.startswith("file_"):
                req_files[k[5:]] = (v.filename, v.stream.read(), v.mimetype)
    
    # Get script and environment data
    pre_request_script = data.get("pre_request_script", "")
    test_script = data.get("test_script", "")
    script_language = data.get("script_language", "javascript")
    pre_processor = data.get("pre_processor", "")
    post_processor = data.get("post_processor", "")
    
    # Load active environment variables
    env_vars = {}
    global_vars = {}
    uid = get_current_user_id()
    with get_db() as conn:
        active_env = conn.execute("SELECT vars FROM environments WHERE user_id=? AND active=1", (uid,)).fetchone()
        if active_env and active_env["vars"]:
            try:
                env_vars = json.loads(active_env["vars"]) if isinstance(active_env["vars"], str) else active_env["vars"]
            except:
                env_vars = {}
        
        user_row = conn.execute("SELECT global_vars FROM users WHERE id=?", (uid,)).fetchone()
        if user_row and user_row["global_vars"]:
            try:
                global_vars = json.loads(user_row["global_vars"]) if isinstance(user_row["global_vars"], str) else user_row["global_vars"]
            except:
                global_vars = {}
    
    # Execute pre-request script (Python only for server-side)
    if pre_request_script and script_language == "python":
        pre_context = {
            "request": {"method": method, "url": url, "headers": headers, "params": params},
            "environment": env_vars.copy(),
            "globals": global_vars.copy(),
            "assertions": []
        }
        pre_context = execute_python_script(pre_request_script, pre_context)
        env_vars = pre_context.get("environment", env_vars)
        global_vars = pre_context.get("globals", global_vars)
        # Update headers/params if script modified them
        if "modified_headers" in pre_context:
            headers.update(pre_context["modified_headers"])
        if "modified_params" in pre_context:
            params.update(pre_context["modified_params"])

    start = time.time()
    try:
        if protocol == "soap":
            method = "POST"
            if "Content-Type" not in headers: headers["Content-Type"] = "text/xml; charset=utf-8"
            
        if protocol == "grpc":
            from grpc_requests import Client
            # format: host:port/service/method
            parts = url.split('/')
            if len(parts) < 3:
                return jsonify({"error": "Invalid gRPC URL. Expected format: host:port/service/method"}), 400
            host_port = parts[0]
            service = parts[1]
            method_name = parts[2]
            
            client = Client.get_by_endpoint(host_port)
            grpc_req_json = req_json if req_json else {}
            grpc_resp = client.request(service, method_name, grpc_req_json)
            
            duration_ms = (time.time() - start) * 1000
            result = {
                "status_code": 200, "status_text": "OK",
                "duration_ms": round(duration_ms, 2), "size_bytes": len(str(grpc_resp)),
                "headers": {"content-type": "application/grpc+json"},
                "body_text": json.dumps(grpc_resp, indent=2),
                "body_json": grpc_resp
            }
            return jsonify(result)

        # Standard HTTP Request
        resp = requests.request(
            method=method, url=url, params=params, headers=headers,
            json=req_json, data=form_data or req_body, files=req_files,
            auth=auth, timeout=timeout, allow_redirects=True, verify=True,
            cookies=cookies if cookies else None,
        )
        duration_ms = (time.time() - start) * 1000
        try:    resp_body = resp.text
        except: resp_body = "<binary content>"
        resp_json = None
        try: resp_json = resp.json()
        except: pass

        result = {
            "status_code": resp.status_code, "status_text": resp.reason,
            "duration_ms": round(duration_ms, 2), "size_bytes": len(resp.content),
            "headers": dict(resp.headers),
            "cookies": {c.name: c.value for c in resp.cookies},
            "body": resp_body, "body_json": resp_json,
            "url": resp.url, "redirects": len(resp.history),
        }
        
        # Execute test script (Python only for server-side)
        if test_script and script_language == "python":
            test_context = {
                "request": {"method": method, "url": url},
                "response": result,
                "environment": env_vars.copy(),
                "globals": global_vars.copy(),
                "assertions": []
            }
            test_context = execute_python_script(test_script, test_context)
            result["test_results"] = test_context.get("assertions", [])
            result["script_error"] = test_context.get("script_error")
            # Update environment/globals if modified
            if test_context.get("environment") != env_vars:
                try:
                    with get_db() as conn:
                        conn.execute("UPDATE environments SET vars=? WHERE user_id=? AND active=1",
                                   (json.dumps(test_context["environment"]), uid))
                except:
                    pass
        else:
            result["test_results"] = []

        # Execute pre-processor (before request - can modify request data)
        if pre_processor and script_language == "python":
            pre_proc_context = {
                "request": {"method": method, "url": url, "headers": headers, "params": params},
                "environment": env_vars.copy(),
                "globals": global_vars.copy(),
                "assertions": []
            }
            pre_proc_context = execute_python_script(pre_processor, pre_proc_context)
            result["pre_processor_output"] = pre_proc_context.get("script_error") or pre_proc_context.get("assertions", [])
            if pre_proc_context.get("environment") != env_vars:
                env_vars = pre_proc_context["environment"]

        # Execute post-processor (after response - can transform/validate response)
        if post_processor and script_language == "python":
            post_proc_context = {
                "request": {"method": method, "url": url},
                "response": result,
                "environment": env_vars.copy(),
                "globals": global_vars.copy(),
                "assertions": []
            }
            post_proc_context = execute_python_script(post_processor, post_proc_context)
            result["post_processor_output"] = post_proc_context.get("script_error") or post_proc_context.get("assertions", [])
            result["test_results"].extend(post_proc_context.get("assertions", []))
        with get_db() as conn:
            conn.execute(
                "INSERT INTO history (user_id,method,url,status_code,duration_ms,request_data,response_data) VALUES (?,?,?,?,?,?,?)",
                (get_current_user_id(), method, url, resp.status_code, round(duration_ms,2),
                 json.dumps(data), json.dumps({"status_code": resp.status_code, "body_preview": resp_body[:500]}))
            )
        return jsonify(result)
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": f"Connection error: {e}"}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": f"Request timed out after {timeout}s"}), 504
    except requests.exceptions.SSLError as e:
        return jsonify({"error": f"SSL error: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ─── Collection Runner ───────────────────────────────────────────────────────

@app.route("/api/runner/execute", methods=["POST"])
def execute_collection_runner():
    """Execute all requests in a collection sequentially"""
    uid = require_auth() or get_current_user_id()
    data = request.json or {}
    collection_id = data.get("collection_id")
    iterations = data.get("iterations", 1)
    delay_ms = data.get("delay_ms", 0)
    
    if not collection_id:
        return jsonify({"error": "collection_id is required"}), 400
    
    with get_db() as conn:
        requests_list = conn.execute(
            "SELECT * FROM requests WHERE collection_id=? ORDER BY id",
            (collection_id,)
        ).fetchall()
        
        if not requests_list:
            return jsonify({"error": "No requests found in collection"}), 404
        
        run_id = conn.execute(
            "INSERT INTO collection_runs (user_id,collection_id,iterations,total_requests) VALUES (?,?,?,?)",
            (uid, collection_id, iterations, len(requests_list))
        ).lastrowid
    
    results = []
    total_passed = 0
    total_failed = 0
    
    env_vars = {}
    global_vars = {}
    with get_db() as conn:
        active_env = conn.execute("SELECT vars FROM environments WHERE user_id=? AND active=1", (uid,)).fetchone()
        if active_env and active_env["vars"]:
            try:
                env_vars = json.loads(active_env["vars"]) if isinstance(active_env["vars"], str) else active_env["vars"]
            except:
                pass
        
        user_row = conn.execute("SELECT global_vars FROM users WHERE id=?", (uid,)).fetchone()
        if user_row and user_row["global_vars"]:
            try:
                global_vars = json.loads(user_row["global_vars"]) if isinstance(user_row["global_vars"], str) else user_row["global_vars"]
            except:
                pass
    
    for iteration in range(iterations):
        for req in requests_list:
            req_data = {
                "method": req["method"],
                "url": req["url"],
                "params": json.loads(req["params"]) if req["params"] else [],
                "headers": json.loads(req["headers"]) if req["headers"] else [],
                "body_type": req["body_type"],
                "body_content": req["body_content"],
                "auth_type": req["auth_type"],
                "auth_data": json.loads(req["auth_data"]) if req["auth_data"] else {},
                "pre_request_script": req["pre_request_script"] if req["pre_request_script"] else "",
                "test_script": req["test_script"] if req["test_script"] else "",
                "script_language": req["script_language"] if req["script_language"] else "javascript",
            }
            
            try:
                result = execute_single_request(req_data, env_vars, global_vars)
                results.append({
                    "request_name": req["name"],
                    "request_id": req["id"],
                    "iteration": iteration + 1,
                    **result
                })
                
                test_results = result.get("test_results", [])
                for test in test_results:
                    if test.get("passed"):
                        total_passed += 1
                    else:
                        total_failed += 1
                
                if "updated_env" in result:
                    env_vars = result["updated_env"]
                
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
            except Exception as e:
                results.append({
                    "request_name": req["name"],
                    "request_id": req["id"],
                    "iteration": iteration + 1,
                    "error": str(e)
                })
    
    with get_db() as conn:
        conn.execute(
            "UPDATE collection_runs SET status='completed',end_time=datetime('now'),passed=?,failed=? WHERE id=?",
            (total_passed, total_failed, run_id)
        )
    
    return jsonify({
        "run_id": run_id,
        "results": results,
        "summary": {
            "total_requests": len(results),
            "total_passed": total_passed,
            "total_failed": total_failed
        }
    })

def execute_single_request(req_data, env_vars, global_vars):
    """Execute a single request with scripts (used by runner)"""
    method = req_data["method"]
    url = req_data["url"]
    params = {p["key"]: p["value"] for p in req_data.get("params", []) if p.get("key") and p.get("enabled", True)}
    headers = {h["key"]: h["value"] for h in req_data.get("headers", []) if h.get("key") and h.get("enabled", True)}
    
    if req_data.get("pre_request_script") and req_data.get("script_language") == "python":
        pre_context = {
            "request": {"method": method, "url": url},
            "environment": env_vars.copy(),
            "globals": global_vars.copy(),
            "assertions": []
        }
        pre_context = execute_python_script(req_data["pre_request_script"], pre_context)
        env_vars = pre_context.get("environment", env_vars)
    
    body_type = req_data.get("body_type", "none")
    body_content = req_data.get("body_content", "")
    req_json = None
    req_body = None
    
    if body_type in ("json", "graphql"):
        try:
            req_json = json.loads(body_content) if body_content.strip() else None
            headers.setdefault("Content-Type", "application/json")
        except:
            pass
    elif body_type in ("raw", "xml", "soap"):
        req_body = body_content.encode("utf-8")
    
    start = time.time()
    resp = requests.request(
        method=method, url=url, params=params, headers=headers,
        json=req_json, data=req_body,
        timeout=30, allow_redirects=True, verify=True,
    )
    duration_ms = (time.time() - start) * 1000
    
    try:
        resp_body = resp.text
    except:
        resp_body = "<binary>"
    
    resp_json = None
    try:
        resp_json = resp.json()
    except:
        pass
    
    result = {
        "status_code": resp.status_code,
        "duration_ms": round(duration_ms, 2),
        "test_results": []
    }
    
    if req_data.get("test_script") and req_data.get("script_language") == "python":
        test_context = {
            "request": {"method": method, "url": url},
            "response": result,
            "environment": env_vars.copy(),
            "globals": global_vars.copy(),
            "assertions": []
        }
        test_context = execute_python_script(req_data["test_script"], test_context)
        result["test_results"] = test_context.get("assertions", [])
        result["updated_env"] = test_context.get("environment", env_vars)
    
    return result


# ─── Collections ──────────────────────────────────────────────────────────────

@app.route("/api/collections", methods=["GET"])
def list_collections():
    uid = get_current_user_id()
    with get_db() as conn:
        if uid:
            cols = conn.execute("SELECT * FROM collections WHERE user_id=? ORDER BY created DESC", (uid,)).fetchall()
        else:
            cols = conn.execute("SELECT * FROM collections WHERE user_id IS NULL ORDER BY created DESC").fetchall()
        all_folders = conn.execute("SELECT * FROM folders ORDER BY name").fetchall()
        all_reqs    = conn.execute(
            "SELECT id,collection_id,folder_id,name,method FROM requests ORDER BY id"
        ).fetchall()

    result = []
    for c in cols:
        cid = c["id"]
        folders = [dict(f) for f in all_folders if f["collection_id"] == cid]
        reqs    = [dict(r) for r in all_reqs    if r["collection_id"] == cid]

        def build_tree(parent_id):
            nodes = []
            for f in folders:
                if f["parent_folder_id"] == parent_id:
                    fd = {
                        "id": f["id"], "name": f["name"],
                        "collection_id": cid, "parent_folder_id": parent_id,
                        "folders":   build_tree(f["id"]),
                        "requests":  [{"id": r["id"], "name": r["name"], "method": r["method"]}
                                      for r in reqs if r.get("folder_id") == f["id"]],
                    }
                    nodes.append(fd)
            return nodes

        col_dict = dict(c)
        col_dict["folders"]        = build_tree(None)
        col_dict["requests"]       = [{"id": r["id"], "name": r["name"], "method": r["method"]}
                                       for r in reqs if not r.get("folder_id")]
        col_dict["total_requests"] = len(reqs)
        result.append(col_dict)
    return jsonify(result)


@app.route("/api/collections", methods=["POST"])
def create_collection():
    name = (request.json or {}).get("name", "New Collection")
    uid = get_current_user_id()
    with get_db() as conn:
        cur = conn.execute("INSERT INTO collections (name, user_id) VALUES (?,?)", (name, uid))
        cid = cur.lastrowid
    return jsonify({"id": cid, "name": name, "requests": [], "folders": []})

@app.route("/api/collections/<int:cid>", methods=["PUT"])
def rename_collection(cid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    name = (request.json or {}).get("name", "")
    with get_db() as conn:
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (cid,)).fetchone()
        if not col or col["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))
    return jsonify({"ok": True})

@app.route("/api/collections/<int:cid>", methods=["DELETE"])
def delete_collection(cid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (cid,)).fetchone()
        if not col or col["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute("DELETE FROM collections WHERE id=?", (cid,))
    return jsonify({"ok": True})

@app.route("/api/collections/<int:cid>/vars", methods=["PUT"])
def update_collection_vars(cid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    vars_str = json.dumps((request.json or {}).get("vars", {}))
    with get_db() as conn:
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (cid,)).fetchone()
        if not col or col["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute("UPDATE collections SET vars=? WHERE id=?", (vars_str, cid))
    return jsonify({"ok": True})


# ─── Collection Folders (for dropdown) ───────────────────────────────────────

@app.route("/api/collections/<int:cid>/folders", methods=["GET"])
def get_collection_folders(cid):
    with get_db() as conn:
        folders = conn.execute(
            "SELECT * FROM folders WHERE collection_id=? ORDER BY name", (cid,)
        ).fetchall()

    folders_list = [dict(f) for f in folders]

    def build_flat(parent_id=None, depth=0):
        result = []
        for f in folders_list:
            if f["parent_folder_id"] == parent_id:
                result.append({**f, "depth": depth})
                result.extend(build_flat(f["id"], depth + 1))
        return result

    return jsonify(build_flat())


# ─── Export Collection ────────────────────────────────────────────────────────

@app.route("/api/collections/<int:cid>/export", methods=["GET"])
def export_collection(cid):
    with get_db() as conn:
        col = conn.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
        if not col: return jsonify({"error": "Not found"}), 404
        reqs    = conn.execute("SELECT * FROM requests WHERE collection_id=?", (cid,)).fetchall()
        folders = conn.execute("SELECT * FROM folders  WHERE collection_id=?", (cid,)).fetchall()

    reqs_out = []
    for r in reqs:
        d = dict(r)
        d["params"]    = json.loads(d["params"]    or "[]")
        d["headers"]   = json.loads(d["headers"]   or "[]")
        d["auth_data"] = json.loads(d["auth_data"] or "{}")
        del d["collection_id"]
        reqs_out.append(d)

    export_data = {
        "RequestLab_export": True, "version": "1.1",
        "exported_at": datetime.utcnow().isoformat(),
        "collection": {
            "name": col["name"], "created": col["created"],
            "folders":  [dict(f) for f in folders],
            "requests": reqs_out,
        }
    }
    return Response(
        json.dumps(export_data, indent=2), mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{col["name"]}.json"'}
    )


# ─── Import Collection ────────────────────────────────────────────────────────

@app.route("/api/collections/import", methods=["POST"])
def import_collection():
    try:
        uid = get_current_user_id()
        data = request.json or {}

        if data.get("RequestLab_export"):
            col_data = data.get("collection", {})
            name = col_data.get("name", "Imported Collection")
            with get_db() as conn:
                cid = conn.execute("INSERT INTO collections (name, user_id) VALUES (?, ?)", (name, uid)).lastrowid

                # Recreate folders, mapping old ids → new ids
                folder_id_map = {}
                raw_folders   = col_data.get("folders", [])

                def import_folders(parent_old_id=None):
                    for f in raw_folders:
                        if f.get("parent_folder_id") == parent_old_id:
                            new_parent = folder_id_map.get(parent_old_id) if parent_old_id else None
                            new_fid = conn.execute(
                                "INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)",
                                (cid, new_parent, f["name"])
                            ).lastrowid
                            folder_id_map[f["id"]] = new_fid
                            import_folders(f["id"])

                import_folders(None)

                for r in col_data.get("requests", []):
                    old_fid = r.get("folder_id")
                    new_fid = folder_id_map.get(old_fid) if old_fid else None
                    conn.execute(
                        "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, new_fid, r.get("name","Untitled"), r.get("method","GET"),
                         r.get("url",""), json.dumps(r.get("params",[])), json.dumps(r.get("headers",[])),
                         r.get("body_type","none"), r.get("body_content",""),
                         r.get("auth_type","none"), json.dumps(r.get("auth_data",{})))
                    )
            return jsonify({"ok": True, "id": cid, "name": name})

        # Postman v2 / v2.1
        postman_schema = ""
        if "info" in data: postman_schema = data["info"].get("schema","")
        elif "collection" in data and "info" in data.get("collection",{}):
            data = data["collection"]; postman_schema = data["info"].get("schema","")

        if "schema.getpostman.com" in postman_schema or "item" in data:
            name = data.get("info",{}).get("name","Imported Collection")

            with get_db() as conn:
                cid = conn.execute("INSERT INTO collections (name, user_id) VALUES (?, ?)", (name, uid)).lastrowid
                
                def process_items(items, parent_folder_id=None):
                    for item in (items or []):
                        item_name = item.get("name","Untitled")
                        if "item" in item:
                            # It is a folder
                            fid = conn.execute(
                                "INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)",
                                (cid, parent_folder_id, item_name)
                            ).lastrowid
                            process_items(item["item"], fid)
                        elif "request" in item:
                            # It is a request
                            req       = item.get("request",{})
                            method    = req.get("method","GET").upper()
                            url_raw   = req.get("url","")
                            url       = url_raw.get("raw","") if isinstance(url_raw, dict) else url_raw
                            params    = []
                            if isinstance(url_raw, dict):
                                for q in url_raw.get("query",[]):
                                    if not q.get("disabled",False):
                                        params.append({"key":q.get("key",""),"value":q.get("value",""),"enabled":True})
                            headers = [{"key":h.get("key",""),"value":h.get("value",""),"enabled":True}
                                       for h in req.get("header",[]) if not h.get("disabled",False)]
                            body_type="none"; body_content=""
                            body_obj = req.get("body") or {}
                            mode = body_obj.get("mode","none")
                            if mode=="raw":
                                body_type = "json" if "json" in body_obj.get("options",{}).get("raw",{}).get("language","") else "raw"
                                body_content = body_obj.get("raw","")
                            elif mode=="urlencoded":
                                body_type="urlencoded"
                                body_content=json.dumps({x["key"]:x.get("value","") for x in body_obj.get("urlencoded",[]) if not x.get("disabled")})
                            elif mode=="formdata":
                                body_type="form"
                                body_content=json.dumps({x["key"]:x.get("value","") for x in body_obj.get("formdata",[]) if not x.get("disabled")})
                            auth_type="none"; auth_data={}
                            auth_obj  = req.get("auth") or {}
                            a_type    = auth_obj.get("type","noauth")
                            if a_type=="basic":
                                auth_type="basic"; auth_data={x["key"]:x.get("value","") for x in auth_obj.get("basic",[])}
                            elif a_type=="bearer":
                                auth_type="bearer"; auth_data={"token":next((x.get("value","") for x in auth_obj.get("bearer",[]) if x["key"]=="token"),"")}
                            elif a_type=="apikey":
                                auth_type="apikey"; auth_data={x["key"]:x.get("value","") for x in auth_obj.get("apikey",[])}
                            conn.execute(
                                "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (cid,parent_folder_id,item_name,method,url,json.dumps(params),json.dumps(headers),body_type,body_content,auth_type,json.dumps(auth_data))
                            )
                
                process_items(data.get("item",[]))
            return jsonify({"ok":True,"id":cid,"name":name})

        # OpenAPI / Swagger
        if "openapi" in data or "swagger" in data:
            name = data.get("info", {}).get("title", "Imported OpenAPI")
            with get_db() as conn:
                cid = conn.execute("INSERT INTO collections (name, user_id) VALUES (?, ?)", (name, uid)).lastrowid
                tag_folders = {}
                for tag in data.get("tags", []):
                    tname = tag.get("name")
                    if tname:
                        fid = conn.execute("INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)", (cid, None, tname)).lastrowid
                        tag_folders[tname] = fid

                base_url = ""
                if "servers" in data and data["servers"]:
                    base_url = data["servers"][0].get("url", "")
                elif "host" in data:
                    scheme = data.get("schemes", ["http"])[0]
                    base_url = f"{scheme}://{data['host']}{data.get('basePath', '')}"

                paths = data.get("paths", {})
                for path, methods in paths.items():
                    for method, op in methods.items():
                        if method.lower() not in ["get", "post", "put", "delete", "patch", "options", "head"]:
                            continue
                        req_name = op.get("summary", op.get("operationId", path))
                        
                        folder_id = None
                        if op.get("tags"):
                            tname = op["tags"][0]
                            if tname not in tag_folders:
                                fid = conn.execute("INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)", (cid, None, tname)).lastrowid
                                tag_folders[tname] = fid
                            folder_id = tag_folders[tname]

                        req_url = base_url + path
                        params = []
                        headers = []
                        for param in op.get("parameters", []):
                            pin = param.get("in", "")
                            pname = param.get("name", "")
                            if pin == "query": params.append({"key": pname, "value": "", "enabled": True})
                            elif pin == "header": headers.append({"key": pname, "value": "", "enabled": True})
                            elif pin == "path": req_url = req_url.replace(f"{{{pname}}}", f":{pname}")

                        body_type = "none"
                        body_content = ""
                        if "requestBody" in op:
                            content = op["requestBody"].get("content", {})
                            if "application/json" in content: body_type = "json"; body_content = "{}"
                            elif "application/x-www-form-urlencoded" in content: body_type = "urlencoded"; body_content = "{}"
                            elif "multipart/form-data" in content: body_type = "form"; body_content = "{}"
                        elif "parameters" in op:
                            for param in op["parameters"]:
                                if param.get("in") == "body": body_type = "json"; body_content = "{}"; break

                        conn.execute(
                            "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (cid, folder_id, req_name, method.upper(), req_url, json.dumps(params), json.dumps(headers), body_type, body_content, "none", "{}")
                        )
            return jsonify({"ok": True, "id": cid, "name": name})

        return jsonify({"error":"Unrecognised file. Export from Postman, OpenAPI JSON, or RequestLab."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── Folder CRUD ──────────────────────────────────────────────────────────────

@app.route("/api/folders", methods=["POST"])
def create_folder():
    d = request.json or {}
    collection_id    = d.get("collection_id")
    parent_folder_id = d.get("parent_folder_id")
    name             = d.get("name", "New Folder")
    if not collection_id:
        return jsonify({"error": "collection_id required"}), 400
    with get_db() as conn:
        fid = conn.execute(
            "INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)",
            (collection_id, parent_folder_id, name)
        ).lastrowid
    return jsonify({"id": fid, "ok": True, "name": name})

@app.route("/api/folders/<int:fid>", methods=["PUT"])
def update_folder(fid):
    name = (request.json or {}).get("name", "")
    with get_db() as conn:
        conn.execute("UPDATE folders SET name=? WHERE id=?", (name, fid))
    return jsonify({"ok": True})

@app.route("/api/folders/<int:fid>", methods=["DELETE"])
def delete_folder(fid):
    with get_db() as conn:
        conn.execute("DELETE FROM folders WHERE id=?", (fid,))
    return jsonify({"ok": True})

@app.route("/api/folders/<int:fid>/duplicate", methods=["POST"])
def duplicate_folder(fid):
    with get_db() as conn:
        folder = conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not folder: return jsonify({"error": "Not found"}), 404

        def copy_recursive(src_id, new_parent_id):
            src = conn.execute("SELECT * FROM folders WHERE id=?", (src_id,)).fetchone()
            if not src: return None
            new_fid = conn.execute(
                "INSERT INTO folders (collection_id,parent_folder_id,name) VALUES (?,?,?)",
                (src["collection_id"], new_parent_id, src["name"] + " Copy")
            ).lastrowid
            for r in conn.execute("SELECT * FROM requests WHERE folder_id=?", (src_id,)).fetchall():
                conn.execute(
                    "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r["collection_id"], new_fid, r["name"], r["method"], r["url"],
                     r["params"], r["headers"], r["body_type"], r["body_content"], r["auth_type"], r["auth_data"])
                )
            for sf in conn.execute("SELECT * FROM folders WHERE parent_folder_id=?", (src_id,)).fetchall():
                copy_recursive(sf["id"], new_fid)
            return new_fid

        new_id = copy_recursive(fid, folder["parent_folder_id"])
    return jsonify({"id": new_id, "ok": True})


# ─── Saved Requests ───────────────────────────────────────────────────────────

@app.route("/api/requests", methods=["POST"])
def save_request():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    cid = d.get("collection_id")
    with get_db() as conn:
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (cid,)).fetchone()
        if not col or col["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        rid = conn.execute(
            "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data,cookies,pre_processor,post_processor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, d.get("folder_id"), d.get("name","Untitled"), d.get("method","GET"),
             d.get("url",""), json.dumps(d.get("params",[])), json.dumps(d.get("headers",[])),
             d.get("body_type","none"), d.get("body_content",""),
             d.get("auth_type","none"), json.dumps(d.get("auth_data",{})),
             json.dumps(d.get("cookies",[])), d.get("pre_processor",""), d.get("post_processor",""))
        ).lastrowid
    return jsonify({"id": rid, "ok": True})

@app.route("/api/requests/<int:rid>", methods=["GET"])
def get_request(rid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        r = conn.execute(
            "SELECT r.* FROM requests r JOIN collections c ON r.collection_id = c.id WHERE r.id=? AND c.user_id=?", 
            (rid, uid)
        ).fetchone()
    if not r: return jsonify({"error": "Not found"}), 404
    d = dict(r)
    d["params"]    = json.loads(d["params"])
    d["headers"]   = json.loads(d["headers"])
    d["auth_data"] = json.loads(d["auth_data"])
    d["cookies"]   = json.loads(d.get("cookies") or "[]")
    return jsonify(d)

@app.route("/api/requests/<int:rid>", methods=["PUT"])
def update_request(rid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    with get_db() as conn:
        r = conn.execute("SELECT c.user_id FROM requests r JOIN collections c ON r.collection_id = c.id WHERE r.id=?", (rid,)).fetchone()
        if not r or r["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute(
            "UPDATE requests SET name=?,method=?,url=?,params=?,headers=?,body_type=?,body_content=?,auth_type=?,auth_data=?,folder_id=?,cookies=?,pre_processor=?,post_processor=? WHERE id=?",
            (d.get("name","Untitled"), d.get("method","GET"), d.get("url",""),
             json.dumps(d.get("params",[])), json.dumps(d.get("headers",[])),
             d.get("body_type","none"), d.get("body_content",""),
             d.get("auth_type","none"), json.dumps(d.get("auth_data",{})),
             d.get("folder_id"), json.dumps(d.get("cookies",[])),
             d.get("pre_processor",""), d.get("post_processor",""), rid)
        )
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>/move", methods=["PUT"])
def move_request(rid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    new_collection_id = data.get("collection_id")
    new_folder_id = data.get("folder_id")
    with get_db() as conn:
        r = conn.execute("SELECT c.user_id FROM requests r JOIN collections c ON r.collection_id = c.id WHERE r.id=?", (rid,)).fetchone()
        if not r or r["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        
        c = conn.execute("SELECT user_id FROM collections WHERE id=?", (new_collection_id,)).fetchone()
        if not c or c["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403

        conn.execute("UPDATE requests SET collection_id=?, folder_id=? WHERE id=?", (new_collection_id, new_folder_id, rid))
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>/rename", methods=["PUT"])
def rename_request(rid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    name = (request.json or {}).get("name","Untitled")
    with get_db() as conn:
        r = conn.execute("SELECT c.user_id FROM requests r JOIN collections c ON r.collection_id = c.id WHERE r.id=?", (rid,)).fetchone()
        if not r or r["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute("UPDATE requests SET name=? WHERE id=?", (name, rid))
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>", methods=["DELETE"])
def delete_request(rid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        r = conn.execute("SELECT c.user_id FROM requests r JOIN collections c ON r.collection_id = c.id WHERE r.id=?", (rid,)).fetchone()
        if not r or r["user_id"] != uid: return jsonify({"error": "Unauthorized"}), 403
        conn.execute("DELETE FROM requests WHERE id=?", (rid,))
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>/duplicate", methods=["POST"])
def duplicate_request(rid):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r: return jsonify({"error": "Not found"}), 404
        new_id = conn.execute(
            "INSERT INTO requests (collection_id,folder_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["collection_id"], r["folder_id"], r["name"] + " Copy", r["method"], r["url"],
             r["params"], r["headers"], r["body_type"], r["body_content"], r["auth_type"], r["auth_data"])
        ).lastrowid
    return jsonify({"id": new_id, "ok": True})


# ─── History ──────────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    uid = get_current_user_id()
    limit = int(request.args.get("limit", 50))
    with get_db() as conn:
        if uid:
            rows = conn.execute(
                "SELECT id,method,url,status_code,duration_ms,timestamp FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,method,url,status_code,duration_ms,timestamp FROM history WHERE user_id IS NULL ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/history/<int:hid>", methods=["GET"])
def get_history_item(hid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM history WHERE id=?", (hid,)).fetchone()
    if not row: return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["request_data"]  = json.loads(d["request_data"]  or "{}")
    d["response_data"] = json.loads(d["response_data"] or "{}")
    return jsonify(d)

@app.route("/api/history", methods=["DELETE"])
def clear_history():
    with get_db() as conn: conn.execute("DELETE FROM history")
    return jsonify({"ok": True})


# ─── Environments ─────────────────────────────────────────────────────────────

@app.route("/api/environments", methods=["GET"])
def list_environments():
    uid = get_current_user_id()
    with get_db() as conn:
        if uid:
            rows = conn.execute("SELECT * FROM environments WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM environments WHERE user_id IS NULL ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/environments", methods=["POST"])
def create_environment():
    d = request.json or {}
    name = d.get("name", "New Environment")
    vars_ = json.dumps(d.get("vars", {}))
    uid = get_current_user_id()
    with get_db() as conn:
        eid = conn.execute("INSERT INTO environments (name,vars,user_id) VALUES (?,?,?)", (name, vars_, uid)).lastrowid
    return jsonify({"id": eid, "name": name, "vars": {}, "active": 0})

@app.route("/api/environments/<int:eid>", methods=["PUT"])
def update_environment(eid):
    d = request.json or {}
    with get_db() as conn:
        conn.execute("UPDATE environments SET name=?,vars=? WHERE id=?",
                     (d.get("name",""), json.dumps(d.get("vars",{})), eid))
    return jsonify({"ok": True})

@app.route("/api/environments/<int:eid>/activate", methods=["POST"])
def activate_environment(eid):
    uid = get_current_user_id()
    with get_db() as conn:
        if uid:
            conn.execute("UPDATE environments SET active=0 WHERE user_id=?", (uid,))
            conn.execute("UPDATE environments SET active=1 WHERE id=? AND user_id=?", (eid, uid))
        else:
            conn.execute("UPDATE environments SET active=0 WHERE user_id IS NULL")
            conn.execute("UPDATE environments SET active=1 WHERE id=? AND user_id IS NULL", (eid,))
    return jsonify({"ok": True})

@app.route("/api/environments/<int:eid>", methods=["DELETE"])
def delete_environment(eid):
    with get_db() as conn:
        conn.execute("DELETE FROM environments WHERE id=?", (eid,))
    return jsonify({"ok": True})


# ─── Team Collaboration ──────────────────────────────────────────────────────────

@app.route("/api/teams", methods=["POST"])
def create_team():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    name = d.get("name", "New Team").strip()
    if not name: return jsonify({"error": "Team name required"}), 400
    with get_db() as conn:
        tid = conn.execute("INSERT INTO teams (name, owner_id) VALUES (?,?)", (name, uid)).lastrowid
        conn.execute("INSERT INTO team_members (team_id,user_id,role,joined_at) VALUES (?,?,?,datetime('now'))",
                     (tid, uid, 'owner'))
    return jsonify({"id": tid, "name": name, "role": "owner"})

@app.route("/api/teams", methods=["GET"])
def list_teams():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.id, t.name, t.owner_id, t.created, tm.role,
                   (SELECT COUNT(*) FROM team_members WHERE team_id=t.id) as member_count
            FROM teams t JOIN team_members tm ON t.id=tm.team_id
            WHERE tm.user_id=?
            ORDER BY t.created DESC
        """, (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/teams/<int:tid>", methods=["GET"])
def get_team(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        if not team: return jsonify({"error": "Not found"}), 404
        is_member = conn.execute("SELECT 1 FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not is_member: return jsonify({"error": "Forbidden"}), 403
        members = conn.execute("""
            SELECT u.id, u.username, u.email, u.avatar_color, tm.role, tm.joined_at
            FROM team_members tm JOIN users u ON tm.user_id=u.id
            WHERE tm.team_id=?
        """, (tid,)).fetchall()
        shared = conn.execute("""
            SELECT sc.collection_id, c.name, sc.shared_at, u.username as shared_by_name
            FROM shared_collections sc
            JOIN collections c ON sc.collection_id=c.id
            LEFT JOIN users u ON sc.shared_by=u.id
            WHERE sc.team_id=?
        """, (tid,)).fetchall()
    return jsonify({
        "team": dict(team),
        "members": [dict(m) for m in members],
        "shared_collections": [dict(s) for s in shared]
    })

@app.route("/api/teams/<int:tid>", methods=["PUT"])
def update_team(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    with get_db() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        if not team or team["owner_id"] != uid:
            return jsonify({"error": "Only owner can update team"}), 403
        conn.execute("UPDATE teams SET name=? WHERE id=?", (d.get("name", team["name"]), tid))
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>", methods=["DELETE"])
def delete_team(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        if not team or team["owner_id"] != uid:
            return jsonify({"error": "Only owner can delete team"}), 403
        conn.execute("DELETE FROM teams WHERE id=?", (tid,))
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>/invite", methods=["POST"])
def invite_team_member(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    email = d.get("email", "").strip()
    role = d.get("role", "viewer")
    if role not in ('admin', 'editor', 'viewer'):
        return jsonify({"error": "Invalid role"}), 400
    if not email: return jsonify({"error": "Email required"}), 400
    with get_db() as conn:
        my_role = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not my_role or my_role["role"] not in ('owner', 'admin'):
            return jsonify({"error": "Insufficient permissions"}), 403
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return jsonify({"error": "User not found with that email"}), 404
        existing = conn.execute("SELECT id FROM team_members WHERE team_id=? AND user_id=?", (tid, user["id"])).fetchone()
        if existing:
            return jsonify({"error": "User already a member"}), 400
        conn.execute("INSERT INTO team_members (team_id,user_id,role,joined_at) VALUES (?,?,?,datetime('now'))",
                     (tid, user["id"], role))
    return jsonify({"ok": True, "user_id": user["id"]})

@app.route("/api/teams/<int:tid>/members/<int:member_uid>", methods=["PUT"])
def update_team_member_role(tid, member_uid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    role = d.get("role", "viewer")
    if role not in ('admin', 'editor', 'viewer'):
        return jsonify({"error": "Invalid role"}), 400
    with get_db() as conn:
        my_role = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not my_role or my_role["role"] != 'owner':
            return jsonify({"error": "Only owner can change roles"}), 403
        conn.execute("UPDATE team_members SET role=? WHERE team_id=? AND user_id=?", (role, tid, member_uid))
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>/members/<int:member_uid>", methods=["DELETE"])
def remove_team_member(tid, member_uid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        my_role = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        team = conn.execute("SELECT owner_id FROM teams WHERE id=?", (tid,)).fetchone()
        if not team: return jsonify({"error": "Not found"}), 404
        if uid != member_uid and (not my_role or my_role["role"] not in ('owner', 'admin')):
            return jsonify({"error": "Insufficient permissions"}), 403
        if member_uid == team["owner_id"]:
            return jsonify({"error": "Cannot remove owner"}), 400
        conn.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (tid, member_uid))
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>/leave", methods=["POST"])
def leave_team(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        team = conn.execute("SELECT owner_id FROM teams WHERE id=?", (tid,)).fetchone()
        if not team: return jsonify({"error": "Not found"}), 404
        if team["owner_id"] == uid:
            return jsonify({"error": "Owner must delete team, not leave"}), 400
        conn.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (tid, uid))
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>/collections", methods=["POST"])
def share_collection_with_team(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    collection_id = d.get("collection_id")
    if not collection_id: return jsonify({"error": "collection_id required"}), 400
    with get_db() as conn:
        my_role = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not my_role or my_role["role"] not in ('owner', 'admin', 'editor'):
            return jsonify({"error": "Insufficient permissions"}), 403
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (collection_id,)).fetchone()
        if not col or col["user_id"] != uid:
            return jsonify({"error": "You don't own this collection"}), 403
        try:
            conn.execute("INSERT INTO shared_collections (team_id,collection_id,shared_by) VALUES (?,?,?)",
                         (tid, collection_id, uid))
        except sqlite3.IntegrityError:
            return jsonify({"error": "Already shared"}), 400
    return jsonify({"ok": True})

@app.route("/api/teams/<int:tid>/collections", methods=["GET"])
def list_team_collections(tid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        is_member = conn.execute("SELECT 1 FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not is_member: return jsonify({"error": "Forbidden"}), 403
        rows = conn.execute("""
            SELECT c.id, c.name, sc.shared_at, u.username as shared_by_name
            FROM shared_collections sc
            JOIN collections c ON sc.collection_id=c.id
            LEFT JOIN users u ON sc.shared_by=u.id
            WHERE sc.team_id=?
        """, (tid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/teams/<int:tid>/collections/<int:cid>", methods=["DELETE"])
def unshare_collection(tid, cid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        my_role = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, uid)).fetchone()
        if not my_role or my_role["role"] not in ('owner', 'admin'):
            return jsonify({"error": "Insufficient permissions"}), 403
        conn.execute("DELETE FROM shared_collections WHERE team_id=? AND collection_id=?", (tid, cid))
    return jsonify({"ok": True})


# ─── Advanced Testing / CI/CD ──────────────────────────────────────────────────

def require_ci_token():
    """Authenticate via CI token from X-CI-Token header"""
    token = request.headers.get("X-CI-Token", "")
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT user_id FROM ci_tokens WHERE token=?", (token,)).fetchone()
        if row:
            conn.execute("UPDATE ci_tokens SET last_used=datetime('now') WHERE token=?", (token,))
            return row["user_id"]
    return None

@app.route("/api/test-suites", methods=["POST"])
def create_test_suite():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    name = d.get("name", "New Test Suite").strip()
    collection_ids = json.dumps(d.get("collection_ids", []))
    with get_db() as conn:
        sid = conn.execute("INSERT INTO test_suites (user_id,name,collection_ids) VALUES (?,?,?)",
                           (uid, name, collection_ids)).lastrowid
    return jsonify({"id": sid, "name": name, "collection_ids": d.get("collection_ids", [])})

@app.route("/api/test-suites", methods=["GET"])
def list_test_suites():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM test_suites WHERE user_id=? ORDER BY created DESC", (uid,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["collection_ids"] = json.loads(d["collection_ids"]) if isinstance(d["collection_ids"], str) else []
        result.append(d)
    return jsonify(result)

@app.route("/api/test-suites/<int:sid>", methods=["PUT"])
def update_test_suite(sid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    with get_db() as conn:
        suite = conn.execute("SELECT user_id FROM test_suites WHERE id=?", (sid,)).fetchone()
        if not suite or suite["user_id"] != uid:
            return jsonify({"error": "Forbidden"}), 403
        conn.execute("UPDATE test_suites SET name=?, collection_ids=? WHERE id=?",
                     (d.get("name", ""), json.dumps(d.get("collection_ids", [])), sid))
    return jsonify({"ok": True})

@app.route("/api/test-suites/<int:sid>", methods=["DELETE"])
def delete_test_suite(sid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        suite = conn.execute("SELECT user_id FROM test_suites WHERE id=?", (sid,)).fetchone()
        if not suite or suite["user_id"] != uid:
            return jsonify({"error": "Forbidden"}), 403
        conn.execute("DELETE FROM test_suites WHERE id=?", (sid,))
    return jsonify({"ok": True})

@app.route("/api/test-suites/<int:sid>/run", methods=["POST"])
def run_test_suite(sid):
    uid = require_auth() or require_ci_token()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    triggered_by = request.json.get("triggered_by", "manual") if request.json else "manual"
    with get_db() as conn:
        suite = conn.execute("SELECT * FROM test_suites WHERE id=?", (sid,)).fetchone()
        if not suite: return jsonify({"error": "Not found"}), 404
        if suite["user_id"] != uid: return jsonify({"error": "Forbidden"}), 403
        col_ids = json.loads(suite["collection_ids"]) if isinstance(suite["collection_ids"], str) else []
        if not col_ids:
            return jsonify({"error": "No collections in suite"}), 400
        conn.execute("UPDATE test_suites SET status='running', last_run=datetime('now') WHERE id=?", (sid,))
        result_id = conn.execute(
            "INSERT INTO test_results (suite_id,status,triggered_by) VALUES (?,?,?)",
            (sid, 'running', triggered_by)
        ).lastrowid
    total_passed = 0
    total_failed = 0
    total_requests = 0
    all_results = []
    env_vars, global_vars = {}, {}
    with get_db() as conn:
        active_env = conn.execute("SELECT vars FROM environments WHERE user_id=? AND active=1", (uid,)).fetchone()
        if active_env and active_env["vars"]:
            try: env_vars = json.loads(active_env["vars"])
            except: pass
        user_row = conn.execute("SELECT global_vars FROM users WHERE id=?", (uid,)).fetchone()
        if user_row and user_row["global_vars"]:
            try: global_vars = json.loads(user_row["global_vars"])
            except: pass
    start_time = time.time()
    for col_id in col_ids:
        with get_db() as conn:
            reqs = conn.execute("SELECT * FROM requests WHERE collection_id=? ORDER BY id", (col_id,)).fetchall()
            col = conn.execute("SELECT name FROM collections WHERE id=?", (col_id,)).fetchone()
        col_name = col["name"] if col else f"Collection {col_id}"
        for req in reqs:
            req_data = {
                "method": req["method"], "url": req["url"],
                "params": json.loads(req["params"]) if req["params"] else [],
                "headers": json.loads(req["headers"]) if req["headers"] else [],
                "body_type": req["body_type"], "body_content": req["body_content"],
                "auth_type": req["auth_type"], "auth_data": json.loads(req["auth_data"]) if req["auth_data"] else {},
                "pre_request_script": req["pre_request_script"] or "",
                "test_script": req["test_script"] or "",
                "script_language": req["script_language"] or "javascript",
            }
            try:
                result = execute_single_request(req_data, env_vars, global_vars)
                test_results = result.get("test_results", [])
                passed = sum(1 for t in test_results if t.get("passed"))
                failed = sum(1 for t in test_results if not t.get("passed"))
                total_passed += passed
                total_failed += failed
                total_requests += 1
                all_results.append({
                    "collection": col_name, "request": req["name"],
                    "status_code": result.get("status_code"),
                    "duration_ms": result.get("duration_ms"),
                    "passed": passed, "failed": failed,
                    "tests": test_results
                })
                if "updated_env" in result:
                    env_vars = result["updated_env"]
            except Exception as e:
                total_failed += 1
                total_requests += 1
                all_results.append({
                    "collection": col_name, "request": req["name"],
                    "error": str(e), "passed": 0, "failed": 1, "tests": []
                })
    duration_ms = (time.time() - start_time) * 1000
    status = "passed" if total_failed == 0 else "failed"
    with get_db() as conn:
        conn.execute("""
            UPDATE test_results SET status=?, total=?, passed=?, failed=?, duration_ms=?,
            details=?, finished_at=datetime('now') WHERE id=?
        """, (status, total_requests, total_passed, total_failed, round(duration_ms, 2),
              json.dumps(all_results), result_id))
        conn.execute("UPDATE test_suites SET status=? WHERE id=?", (status, sid))
    return jsonify({
        "result_id": result_id, "status": status,
        "summary": {"total": total_requests, "passed": total_passed, "failed": total_failed,
                     "duration_ms": round(duration_ms, 2)},
        "results": all_results
    })

@app.route("/api/test-results", methods=["GET"])
def list_test_results():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    suite_id = request.args.get("suite_id")
    limit = request.args.get("limit", 50, type=int)
    with get_db() as conn:
        if suite_id:
            rows = conn.execute(
                "SELECT tr.* FROM test_results tr JOIN test_suites ts ON tr.suite_id=ts.id WHERE ts.user_id=? AND tr.suite_id=? ORDER BY tr.started_at DESC LIMIT ?",
                (uid, suite_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT tr.* FROM test_results tr JOIN test_suites ts ON tr.suite_id=ts.id WHERE ts.user_id=? ORDER BY tr.started_at DESC LIMIT ?",
                (uid, limit)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"]) if isinstance(d["details"], str) else []
        result.append(d)
    return jsonify(result)

def _generate_junit_xml(test_result):
    """Generate JUnit XML from a test result row"""
    details = json.loads(test_result["details"]) if isinstance(test_result["details"], str) else test_result["details"]
    total = test_result["total"]
    failures = test_result["failed"]
    duration = (test_result["duration_ms"] or 0) / 1000.0
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append(f'<testsuites tests="{total}" failures="{failures}" time="{duration:.3f}">')
    xml.append(f'  <testsuite name="RequestLab Test Suite" tests="{total}" failures="{failures}" time="{duration:.3f}">')
    for r in details:
        req_name = r.get("request", "Unknown")
        col_name = r.get("collection", "Default")
        req_time = (r.get("duration_ms") or 0) / 1000.0
        xml.append(f'    <testcase classname="{col_name}" name="{req_name}" time="{req_time:.3f}">')
        if r.get("error"):
            xml.append(f'      <failure message="{r["error"]}">{r["error"]}</failure>')
        elif r.get("failed", 0) > 0:
            failed_tests = [t for t in r.get("tests", []) if not t.get("passed")]
            msgs = "; ".join(t.get("name", "") + ": " + str(t.get("error", t.get("expected", ""))) for t in failed_tests)
            xml.append(f'      <failure message="{msgs}">{msgs}</failure>')
        xml.append('    </testcase>')
    xml.append('  </testsuite>')
    xml.append('</testsuites>')
    return "\n".join(xml)

@app.route("/api/ci/tokens", methods=["POST"])
def create_ci_token():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    name = d.get("name", "CI Token").strip()
    token = secrets.token_urlsafe(48)
    with get_db() as conn:
        tid = conn.execute("INSERT INTO ci_tokens (user_id,token,name) VALUES (?,?,?)",
                           (uid, token, name)).lastrowid
    return jsonify({"id": tid, "token": token, "name": name})

@app.route("/api/ci/tokens", methods=["GET"])
def list_ci_tokens():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, created, last_used FROM ci_tokens WHERE user_id=?", (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/ci/tokens/<int:token_id>", methods=["DELETE"])
def delete_ci_token(token_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        tok = conn.execute("SELECT user_id FROM ci_tokens WHERE id=?", (token_id,)).fetchone()
        if not tok or tok["user_id"] != uid:
            return jsonify({"error": "Forbidden"}), 403
        conn.execute("DELETE FROM ci_tokens WHERE id=?", (token_id,))
    return jsonify({"ok": True})

@app.route("/api/ci/run", methods=["POST"])
def ci_run_suite():
    """CI endpoint - authenticates via X-CI-Token header"""
    uid = require_ci_token()
    if not uid: return jsonify({"error": "Invalid CI token"}), 401
    d = request.json or {}
    suite_id = d.get("suite_id")
    if not suite_id: return jsonify({"error": "suite_id required"}), 400
    with get_db() as conn:
        suite = conn.execute("SELECT * FROM test_suites WHERE id=? AND user_id=?", (suite_id, uid)).fetchone()
        if not suite: return jsonify({"error": "Suite not found"}), 404
    # Run suite inline (reuse logic)
    col_ids = json.loads(suite["collection_ids"]) if isinstance(suite["collection_ids"], str) else []
    with get_db() as conn:
        conn.execute("UPDATE test_suites SET status='running', last_run=datetime('now') WHERE id=?", (suite_id,))
        result_id = conn.execute(
            "INSERT INTO test_results (suite_id,status,triggered_by) VALUES (?,?,?)",
            (suite_id, 'running', 'ci')
        ).lastrowid
    total_passed = 0
    total_failed = 0
    total_requests = 0
    all_results = []
    env_vars, global_vars = {}, {}
    with get_db() as conn:
        active_env = conn.execute("SELECT vars FROM environments WHERE user_id=? AND active=1", (uid,)).fetchone()
        if active_env and active_env["vars"]:
            try: env_vars = json.loads(active_env["vars"])
            except: pass
    start_time = time.time()
    for col_id in col_ids:
        with get_db() as conn:
            reqs = conn.execute("SELECT * FROM requests WHERE collection_id=? ORDER BY id", (col_id,)).fetchall()
            col = conn.execute("SELECT name FROM collections WHERE id=?", (col_id,)).fetchone()
        col_name = col["name"] if col else f"Collection {col_id}"
        for req in reqs:
            req_data = {
                "method": req["method"], "url": req["url"],
                "params": json.loads(req["params"]) if req["params"] else [],
                "headers": json.loads(req["headers"]) if req["headers"] else [],
                "body_type": req["body_type"], "body_content": req["body_content"],
                "auth_type": req["auth_type"], "auth_data": json.loads(req["auth_data"]) if req["auth_data"] else {},
                "pre_request_script": req["pre_request_script"] or "",
                "test_script": req["test_script"] or "",
                "script_language": req["script_language"] or "javascript",
            }
            try:
                result = execute_single_request(req_data, env_vars, global_vars)
                test_results = result.get("test_results", [])
                passed = sum(1 for t in test_results if t.get("passed"))
                failed = sum(1 for t in test_results if not t.get("passed"))
                total_passed += passed
                total_failed += failed
                total_requests += 1
                all_results.append({"collection": col_name, "request": req["name"],
                    "status_code": result.get("status_code"), "duration_ms": result.get("duration_ms"),
                    "passed": passed, "failed": failed, "tests": test_results})
                if "updated_env" in result: env_vars = result["updated_env"]
            except Exception as e:
                total_failed += 1
                total_requests += 1
                all_results.append({"collection": col_name, "request": req["name"], "error": str(e), "passed": 0, "failed": 1, "tests": []})
    duration_ms = (time.time() - start_time) * 1000
    status = "passed" if total_failed == 0 else "failed"
    with get_db() as conn:
        conn.execute("UPDATE test_results SET status=?, total=?, passed=?, failed=?, duration_ms=?, details=?, finished_at=datetime('now') WHERE id=?",
                     (status, total_requests, total_passed, total_failed, round(duration_ms, 2), json.dumps(all_results), result_id))
        conn.execute("UPDATE test_suites SET status=? WHERE id=?", (status, suite_id))
    fmt = d.get("format", "json")
    if fmt == "junit":
        tr = {"details": json.dumps(all_results), "total": total_requests, "failed": total_failed, "duration_ms": round(duration_ms, 2)}
        xml = _generate_junit_xml(tr)
        return Response(xml, mimetype="application/xml", headers={"Content-Disposition": "attachment; filename=results.xml"})
    return jsonify({"result_id": result_id, "status": status,
                    "summary": {"total": total_requests, "passed": total_passed, "failed": total_failed, "duration_ms": round(duration_ms, 2)},
                    "results": all_results})

@app.route("/api/ci/run/<int:rid>/junit", methods=["GET"])
def ci_export_junit(rid):
    uid = require_auth() or require_ci_token()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        tr = conn.execute("SELECT tr.* FROM test_results tr JOIN test_suites ts ON tr.suite_id=ts.id WHERE tr.id=? AND ts.user_id=?", (rid, uid)).fetchone()
        if not tr: return jsonify({"error": "Not found"}), 404
    xml = _generate_junit_xml(dict(tr))
    return Response(xml, mimetype="application/xml", headers={"Content-Disposition": f"attachment; filename=result_{rid}.xml"})

@app.route("/api/ci/run/<int:rid>/json", methods=["GET"])
def ci_export_json(rid):
    uid = require_auth() or require_ci_token()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        tr = conn.execute("SELECT tr.* FROM test_results tr JOIN test_suites ts ON tr.suite_id=ts.id WHERE tr.id=? AND ts.user_id=?", (rid, uid)).fetchone()
        if not tr: return jsonify({"error": "Not found"}), 404
    d = dict(tr)
    d["details"] = json.loads(d["details"]) if isinstance(d["details"], str) else []
    return jsonify(d)


# ─── API Documentation Generator ────────────────────────────────────────────────

def _generate_openapi_from_collection(collection_id, uid):
    """Generate OpenAPI 3.0 spec from collection requests"""
    with get_db() as conn:
        col = conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
        if not col: return None
        reqs = conn.execute("SELECT * FROM requests WHERE collection_id=? ORDER BY id", (collection_id,)).fetchall()
    paths = {}
    for req in reqs:
        url = req["url"] or "/"
        # Extract path from URL
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path or "/"
        except:
            path = "/"
        method = (req["method"] or "GET").lower()
        if path not in paths:
            paths[path] = {}
        headers_list = json.loads(req["headers"]) if req["headers"] else []
        params_list = json.loads(req["params"]) if req["params"] else []
        op = {
            "summary": req["name"],
            "operationId": f"{method}_{req['id']}",
            "parameters": [],
            "responses": {"200": {"description": "Successful response"}}
        }
        for p in params_list:
            if p.get("key"):
                op["parameters"].append({"name": p["key"], "in": "query", "schema": {"type": "string"},
                                          "example": p.get("value", "")})
        for h in headers_list:
            if h.get("key") and h["key"].lower() not in ("content-type", "accept"):
                op["parameters"].append({"name": h["key"], "in": "header", "schema": {"type": "string"},
                                          "example": h.get("value", "")})
        if req["body_type"] in ("json", "graphql", "raw", "xml", "soap") and req["body_content"]:
            body = req["body_content"]
            schema = {"type": "string"}
            if req["body_type"] == "json":
                try:
                    parsed_body = json.loads(body)
                    schema = _infer_json_schema(parsed_body)
                except:
                    pass
            ct = "application/json" if req["body_type"] == "json" else "text/plain"
            op["requestBody"] = {"content": {ct: {"schema": schema}}}
        if req["auth_type"] and req["auth_type"] != "none":
            op["security"] = [{req["auth_type"]: []}]
        paths[path][method] = op
    spec = {
        "openapi": "3.0.3",
        "info": {"title": col["name"], "version": "1.0.0", "description": f"API documentation for {col['name']}"},
        "paths": paths
    }
    return spec

def _infer_json_schema(obj):
    """Infer JSON schema from a sample object"""
    if isinstance(obj, dict):
        props = {}
        for k, v in obj.items():
            props[k] = _infer_json_schema(v)
        return {"type": "object", "properties": props}
    elif isinstance(obj, list):
        item_schema = {"type": "string"}
        if obj:
            item_schema = _infer_json_schema(obj[0])
        return {"type": "array", "items": item_schema}
    elif isinstance(obj, bool):
        return {"type": "boolean"}
    elif isinstance(obj, int):
        return {"type": "integer"}
    elif isinstance(obj, float):
        return {"type": "number"}
    else:
        return {"type": "string"}

@app.route("/api/docs/generate", methods=["POST"])
def generate_docs():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    collection_id = d.get("collection_id")
    if not collection_id: return jsonify({"error": "collection_id required"}), 400
    spec = _generate_openapi_from_collection(collection_id, uid)
    if not spec: return jsonify({"error": "Collection not found"}), 404
    with get_db() as conn:
        col = conn.execute("SELECT user_id FROM collections WHERE id=?", (collection_id,)).fetchone()
        if not col or col["user_id"] != uid:
            return jsonify({"error": "Forbidden"}), 403
        existing = conn.execute("SELECT id FROM api_docs WHERE collection_id=?", (collection_id,)).fetchone()
        if existing:
            conn.execute("UPDATE api_docs SET content=?, updated=datetime('now') WHERE collection_id=?",
                         (json.dumps(spec), collection_id))
        else:
            conn.execute("INSERT INTO api_docs (user_id, collection_id, content) VALUES (?,?,?)",
                         (uid, collection_id, json.dumps(spec)))
    return jsonify(spec)

@app.route("/api/docs/<int:collection_id>", methods=["GET"])
def get_docs(collection_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        doc = conn.execute("SELECT * FROM api_docs WHERE collection_id=? AND user_id=?", (collection_id, uid)).fetchone()
        if not doc:
            # Auto-generate
            spec = _generate_openapi_from_collection(collection_id, uid)
            if not spec: return jsonify({"error": "Not found"}), 404
            return jsonify(spec)
        return json.loads(doc["content"])

@app.route("/api/docs/<int:collection_id>", methods=["PUT"])
def update_docs(collection_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM api_docs WHERE collection_id=? AND user_id=?", (collection_id, uid)).fetchone()
        if existing:
            conn.execute("UPDATE api_docs SET content=?, updated=datetime('now') WHERE collection_id=?",
                         (json.dumps(d), collection_id))
        else:
            conn.execute("INSERT INTO api_docs (user_id, collection_id, content) VALUES (?,?,?)",
                         (uid, collection_id, json.dumps(d)))
    return jsonify({"ok": True})

@app.route("/api/docs/<int:collection_id>/openapi", methods=["GET"])
def export_openapi(collection_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    spec = _generate_openapi_from_collection(collection_id, uid)
    if not spec: return jsonify({"error": "Not found"}), 404
    return jsonify(spec)

@app.route("/api/docs/<int:collection_id>/markdown", methods=["GET"])
def export_markdown(collection_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    spec = _generate_openapi_from_collection(collection_id, uid)
    if not spec: return jsonify({"error": "Not found"}), 404
    md = [f"# {spec['info']['title']}\n"]
    md.append(f"{spec['info'].get('description', '')}\n")
    md.append(f"**Version:** {spec['info']['version']}\n\n")
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            md.append(f"## {method.upper()} `{path}`\n")
            md.append(f"**{op.get('summary', '')}**\n\n")
            if op.get("parameters"):
                md.append("### Parameters\n")
                md.append("| Name | In | Example |")
                md.append("|------|----|---------|")
                for p in op["parameters"]:
                    md.append(f"| {p['name']} | {p['in']} | {p.get('example', '')} |")
                md.append("")
            if op.get("requestBody"):
                md.append("### Request Body\n")
                for ct, ct_data in op["requestBody"].get("content", {}).items():
                    md.append(f"Content-Type: `{ct}`\n")
                    if ct_data.get("schema"):
                        md.append(f"```json\n{json.dumps(ct_data['schema'], indent=2)}\n```\n")
            md.append("---\n")
    text = "\n".join(md)
    return Response(text, mimetype="text/markdown", headers={"Content-Disposition": f"attachment; filename=api_docs.md"})

@app.route("/api/docs/<int:collection_id>/html", methods=["GET"])
def export_html_docs(collection_id):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    spec = _generate_openapi_from_collection(collection_id, uid)
    if not spec: return jsonify({"error": "Not found"}), 404
    h = [f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{spec['info']['title']} - API Docs</title>
    <style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;background:#0d1117;color:#cdd9e5}}
    h1{{color:#00d4ff}}h2{{color:#79c0ff;border-bottom:1px solid #1e2d3d;padding-bottom:8px}}
    .method{{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;margin-right:8px}}
    .get{{background:#1a4a2e;color:#3dd68c}}.post{{background:#1a3a5c;color:#79c0ff}}
    .put{{background:#4a3a1a;color:#e3b341}}.delete{{background:#4a1a1a;color:#f47067}}
    .patch{{background:#3a1a4a;color:#d2a8ff}}
    table{{width:100%;border-collapse:collapse;margin:12px 0}}th,td{{text-align:left;padding:8px 12px;border:1px solid #1e2d3d;font-size:13px}}
    th{{background:#131920}}code{{background:#131920;padding:2px 6px;border-radius:3px;font-size:13px}}
    pre{{background:#131920;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px}}
    .endpoint{{margin:20px 0;padding:16px;border:1px solid #1e2d3d;border-radius:8px}}</style></head><body>"""]
    h.append(f"<h1>{spec['info']['title']}</h1>")
    h.append(f"<p>{spec['info'].get('description', '')}</p>")
    h.append(f"<p><strong>Version:</strong> {spec['info']['version']}</p>")
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            h.append(f'<div class="endpoint">')
            h.append(f'<h2><span class="method {method}">{method.upper()}</span> <code>{path}</code></h2>')
            h.append(f"<p><strong>{op.get('summary', '')}</strong></p>")
            if op.get("parameters"):
                h.append("<h3>Parameters</h3><table><tr><th>Name</th><th>In</th><th>Example</th></tr>")
                for p in op["parameters"]:
                    h.append(f"<tr><td>{p['name']}</td><td>{p['in']}</td><td>{p.get('example','')}</td></tr>")
                h.append("</table>")
            if op.get("requestBody"):
                h.append("<h3>Request Body</h3>")
                for ct, ct_data in op["requestBody"].get("content", {}).items():
                    h.append(f"<p>Content-Type: <code>{ct}</code></p>")
                    if ct_data.get("schema"):
                        h.append(f"<pre>{json.dumps(ct_data['schema'], indent=2)}</pre>")
            h.append('</div>')
    h.append("</body></html>")
    html_out = "\n".join(h)
    return Response(html_out, mimetype="text/html", headers={"Content-Disposition": f"attachment; filename=api_docs.html"})


# ─── Code Generation ──────────────────────────────────────────────────────────

def _generate_code(data, target):
    """Generate code snippet for given target language"""
    method = data.get("method", "GET").upper()
    url = data.get("url", "https://api.example.com/endpoint")
    headers = {h["key"]: h["value"] for h in data.get("headers", []) if h.get("key") and h.get("enabled", True)}
    params = {p["key"]: p["value"] for p in data.get("params", []) if p.get("key") and p.get("enabled", True)}
    body_type = data.get("body_type", "none")
    body_content = data.get("body_content", "")
    auth_type = data.get("auth_type", "none")
    auth_data = data.get("auth_data", {})

    if auth_type == "basic":
        headers["Authorization"] = "Basic <base64_encoded_credentials>"
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth_data.get('token', '<token>')}"
    elif auth_type == "apikey":
        loc = auth_data.get("location", "header")
        kn = auth_data.get("key", "X-API-Key")
        kv = auth_data.get("value", "<api_key>")
        if loc == "header": headers[kn] = kv
        else: params[kn] = kv

    generators = {
        "curl": _gen_curl, "python": _gen_python, "javascript_fetch": _gen_js_fetch,
        "javascript_axios": _gen_js_axios, "php": _gen_php, "ruby": _gen_ruby,
        "go": _gen_go, "java": _gen_java, "csharp": _gen_csharp, "powershell": _gen_powershell
    }
    gen = generators.get(target, _gen_curl)
    return gen(method, url, headers, params, body_type, body_content)

def _gen_curl(method, url, headers, params, body_type, body_content):
    parts = [f"curl -X {method}"]
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sep = "&" if "?" in url else "?"
        full_url = url + sep + qs
    parts.append(f"  '{full_url}'")
    for k, v in headers.items():
        parts.append(f"  -H '{k}: {v}'")
    if body_type in ("json", "graphql") and body_content:
        parts.append(f"  -H 'Content-Type: application/json'")
        parts.append(f"  -d '{body_content}'")
    elif body_type in ("raw", "xml", "soap") and body_content:
        parts.append(f"  -d '{body_content}'")
    return " \\\n".join(parts)

def _gen_python(method, url, headers, params, body_type, body_content):
    lines = ["import requests", "", f"url = \"{url}\""]
    if params: lines.append(f"params = {json.dumps(params, indent=4)}")
    if headers: lines.append(f"headers = {json.dumps(headers, indent=4)}")
    if body_type in ("json", "graphql") and body_content:
        try: lines.append(f"payload = {json.dumps(json.loads(body_content), indent=4)}")
        except: lines.append(f"payload = {repr(body_content)}")
        lines.append(f"response = requests.{method.lower()}(url, json=payload{', headers=headers' if headers else ''}{', params=params' if params else ''})")
    elif body_type in ("raw", "xml", "soap") and body_content:
        lines.append(f"data = {repr(body_content)}")
        lines.append(f"response = requests.{method.lower()}(url, data=data{', headers=headers' if headers else ''}{', params=params' if params else ''})")
    else:
        lines.append(f"response = requests.{method.lower()}(url{', headers=headers' if headers else ''}{', params=params' if params else ''})")
    lines.extend(["", "print(response.status_code)", "print(response.text)"])
    return "\n".join(lines)

def _gen_js_fetch(method, url, headers, params, body_type, body_content):
    lines = []
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sep = "&" if "?" in url else "?"
        url_expr = f"`{url}{sep}" + "${new URLSearchParams(" + json.dumps(params) + ")}`"
    else:
        url_expr = f"'{url}'"
    opts = [f"  method: '{method}'"]
    if headers: opts.append(f"  headers: {json.dumps(headers, indent=4).replace(chr(10), chr(10)+'  ')}")
    if body_type in ("json", "graphql") and body_content:
        headers.setdefault("Content-Type", "application/json")
        opts[-1] = f"  headers: {json.dumps(headers, indent=4).replace(chr(10), chr(10)+'  ')}"
        opts.append(f"  body: JSON.stringify({body_content})")
    elif body_type in ("raw", "xml", "soap") and body_content:
        opts.append(f"  body: {repr(body_content)}")
    lines.append(f"const response = await fetch({url_expr}, {{")
    lines.append(",\n".join(opts))
    lines.append("});\n")
    lines.append("const data = await response.json();")
    lines.append("console.log(data);")
    return "\n".join(lines)

def _gen_js_axios(method, url, headers, params, body_type, body_content):
    lines = ["import axios from 'axios';", ""]
    config_parts = [f"  method: '{method}'", f"  url: '{url}'"]
    if params: config_parts.append(f"  params: {json.dumps(params)}")
    if headers: config_parts.append(f"  headers: {json.dumps(headers)}")
    if body_type in ("json", "graphql") and body_content:
        config_parts.append(f"  data: {body_content}")
    elif body_type in ("raw", "xml", "soap") and body_content:
        config_parts.append(f"  data: {repr(body_content)}")
    lines.append(f"const response = await axios({{\n" + ",\n".join(config_parts) + "\n});")
    lines.append("console.log(response.data);")
    return "\n".join(lines)

def _gen_php(method, url, headers, params, body_type, body_content):
    lines = ["<?php", "$ch = curl_init();", ""]
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sep = "&" if "?" in url else "?"
        full_url = url + sep + qs
    lines.append(f"curl_setopt($ch, CURLOPT_URL, '{full_url}');")
    lines.append(f"curl_setopt($ch, CURLOPT_CUSTOMREQUEST, '{method}');")
    lines.append("curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);")
    if headers:
        h_arr = ", ".join(f"'{k}: {v}'" for k, v in headers.items())
        lines.append(f"curl_setopt($ch, CURLOPT_HTTPHEADER, [{h_arr}]);")
    if body_type in ("json", "graphql", "raw", "xml", "soap") and body_content:
        lines.append(f"curl_setopt($ch, CURLOPT_POSTFIELDS, {repr(body_content)});")
    lines.extend(["", "$response = curl_exec($ch);", "curl_close($ch);", "echo $response;", "?>"])
    return "\n".join(lines)

def _gen_ruby(method, url, headers, params, body_type, body_content):
    lines = ["require 'net/http'", "require 'uri'", "require 'json'", ""]
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sep = "&" if "?" in url else "?"
        full_url = url + sep + qs
    lines.append(f"uri = URI.parse('{full_url}')")
    lines.append(f"http = Net::HTTP.new(uri.host, uri.port)")
    lines.append("http.use_ssl = uri.scheme == 'https'")
    lines.append(f"request = Net::HTTP::{method.capitalize()}.new(uri.request_uri)")
    for k, v in headers.items():
        lines.append(f"request['{k}'] = '{v}'")
    if body_type in ("json", "graphql") and body_content:
        lines.append(f"request.body = {repr(body_content)}")
    elif body_type in ("raw", "xml", "soap") and body_content:
        lines.append(f"request.body = {repr(body_content)}")
    lines.extend(["", "response = http.request(request)", "puts response.code", "puts response.body"])
    return "\n".join(lines)

def _gen_go(method, url, headers, params, body_type, body_content):
    lines = ['package main', '', 'import (', '\t"fmt"', '\t"io"', '\t"net/http"']
    has_body = body_type in ("json", "graphql", "raw", "xml", "soap") and body_content
    if has_body:
        lines.append('\t"strings"')
    lines.extend([')', '', 'func main() {'])
    if has_body:
        lines.append('\tbody := strings.NewReader(`' + body_content + '`)')
        lines.append(f'\treq, err := http.NewRequest("{method}", "{url}", body)')
    else:
        lines.append(f'\treq, err := http.NewRequest("{method}", "{url}", nil)')
    lines.append('\tif err != nil { panic(err) }')
    for k, v in headers.items():
        lines.append(f'\treq.Header.Set("{k}", "{v}")')
    if has_body and body_type in ("json", "graphql"):
        lines.append('\treq.Header.Set("Content-Type", "application/json")')
    lines.extend(['\tresp, err := http.DefaultClient.Do(req)', '\tif err != nil { panic(err) }',
                  '\tdefer resp.Body.Close()', '\tb, _ := io.ReadAll(resp.Body)',
                  '\tfmt.Println(string(b))', '}'])
    return "\n".join(lines)

def _gen_java(method, url, headers, params, body_type, body_content):
    lines = ['OkHttpClient client = new OkHttpClient();', '']
    has_body = body_type in ("json", "graphql", "raw", "xml", "soap") and body_content
    if has_body:
        ct = "application/json" if body_type in ("json", "graphql") else "text/plain"
        lines.append(f'MediaType mediaType = MediaType.parse("{ct}");')
        escaped = body_content.replace('"', '\\"')
        lines.append(f'RequestBody body = RequestBody.create(mediaType, "{escaped}");')
    lines.append('Request request = new Request.Builder()')
    lines.append(f'    .url("{url}")')
    if has_body:
        lines.append(f'    .method("{method}", body)')
    elif method != "GET":
        lines.append(f'    .method("{method}", null)')
    for k, v in headers.items():
        lines.append(f'    .addHeader("{k}", "{v}")')
    lines.extend(['    .build();', '', 'Response response = client.newCall(request).execute();',
                  'System.out.println(response.body().string());'])
    return "\n".join(lines)

def _gen_csharp(method, url, headers, params, body_type, body_content):
    lines = ['using var client = new HttpClient();', '']
    has_body = body_type in ("json", "graphql", "raw", "xml", "soap") and body_content
    for k, v in headers.items():
        lines.append(f'client.DefaultRequestHeaders.Add("{k}", "{v}");')
    if has_body:
        ct = "application/json" if body_type in ("json", "graphql") else "text/plain"
        escaped = body_content.replace('"', '""')
        lines.append(f'var content = new StringContent("{escaped}", System.Text.Encoding.UTF8, "{ct}");')
        method_map = {"POST": "PostAsync", "PUT": "PutAsync", "PATCH": "PatchAsync"}
        if method in method_map:
            lines.append(f'var response = await client.{method_map[method]}("{url}", content);')
        else:
            lines.append(f'var request = new HttpRequestMessage(HttpMethod.{method.capitalize()}, "{url}") {{ Content = content }};')
            lines.append('var response = await client.SendAsync(request);')
    else:
        if method == "GET":
            lines.append(f'var response = await client.GetAsync("{url}");')
        elif method == "DELETE":
            lines.append(f'var response = await client.DeleteAsync("{url}");')
        else:
            lines.append(f'var request = new HttpRequestMessage(HttpMethod.{method.capitalize()}, "{url}");')
            lines.append('var response = await client.SendAsync(request);')
    lines.extend(['var body = await response.Content.ReadAsStringAsync();',
                  'Console.WriteLine($"Status: {{response.StatusCode}}");', 'Console.WriteLine(body);'])
    return "\n".join(lines)

def _gen_powershell(method, url, headers, params, body_type, body_content):
    lines = []
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sep = "&" if "?" in url else "?"
        url = url + sep + qs
    lines.append(f"$uri = '{url}'")
    if headers:
        lines.append("$headers = @{")
        for k, v in headers.items():
            lines.append(f"    '{k}' = '{v}'")
        lines.append("}")
    has_body = body_type in ("json", "graphql", "raw", "xml", "soap") and body_content
    if has_body:
        lines.append(f"$body = @'")
        lines.append(body_content)
        lines.append("'@")
    cmd = f"Invoke-RestMethod -Uri $uri -Method {method}"
    if headers: cmd += " -Headers $headers"
    if has_body:
        ct = "application/json" if body_type in ("json", "graphql") else "text/plain"
        cmd += f" -Body $body -ContentType '{ct}'"
    lines.extend(["", cmd + " | ConvertTo-Json -Depth 10"])
    return "\n".join(lines)

@app.route("/api/code/generate", methods=["POST"])
def generate_code():
    d = request.json or {}
    target = d.get("target", "curl")
    req_data = d.get("request", {})
    code = _generate_code(req_data, target)
    return jsonify({"code": code, "target": target})


# ─── Backup & Restore ─────────────────────────────────────────────────────────

BACKUP_SCHEMA_VERSION = 2

@app.route("/api/backup", methods=["GET"])
def export_backup():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        collections = [dict(r) for r in conn.execute("SELECT * FROM collections WHERE user_id=?", (uid,)).fetchall()]
        col_ids = [c["id"] for c in collections]
        folders = []
        requests_data = []
        if col_ids:
            placeholders = ",".join("?" * len(col_ids))
            folders = [dict(r) for r in conn.execute(f"SELECT * FROM folders WHERE collection_id IN ({placeholders})", col_ids).fetchall()]
            requests_data = [dict(r) for r in conn.execute(f"SELECT * FROM requests WHERE collection_id IN ({placeholders})", col_ids).fetchall()]
        environments = [dict(r) for r in conn.execute("SELECT * FROM environments WHERE user_id=?", (uid,)).fetchall()]
        history = [dict(r) for r in conn.execute("SELECT * FROM history WHERE user_id=? ORDER BY timestamp DESC LIMIT 1000", (uid,)).fetchall()]
        mock_endpoints = [dict(r) for r in conn.execute("SELECT * FROM mock_endpoints WHERE user_id=?", (uid,)).fetchall()]
        test_suites = [dict(r) for r in conn.execute("SELECT * FROM test_suites WHERE user_id=?", (uid,)).fetchall()]
        user = conn.execute("SELECT username, email, avatar_color, global_vars FROM users WHERE id=?", (uid,)).fetchone()
    backup = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "user": dict(user) if user else {},
        "collections": collections,
        "folders": folders,
        "requests": requests_data,
        "environments": environments,
        "history": history,
        "mock_endpoints": mock_endpoints,
        "test_suites": test_suites
    }
    return jsonify(backup)

@app.route("/api/backup/download", methods=["GET"])
def download_backup():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        collections = [dict(r) for r in conn.execute("SELECT * FROM collections WHERE user_id=?", (uid,)).fetchall()]
        col_ids = [c["id"] for c in collections]
        folders, requests_data = [], []
        if col_ids:
            ph = ",".join("?" * len(col_ids))
            folders = [dict(r) for r in conn.execute(f"SELECT * FROM folders WHERE collection_id IN ({ph})", col_ids).fetchall()]
            requests_data = [dict(r) for r in conn.execute(f"SELECT * FROM requests WHERE collection_id IN ({ph})", col_ids).fetchall()]
        environments = [dict(r) for r in conn.execute("SELECT * FROM environments WHERE user_id=?", (uid,)).fetchall()]
        history = [dict(r) for r in conn.execute("SELECT * FROM history WHERE user_id=? ORDER BY timestamp DESC LIMIT 1000", (uid,)).fetchall()]
        mock_endpoints = [dict(r) for r in conn.execute("SELECT * FROM mock_endpoints WHERE user_id=?", (uid,)).fetchall()]
        test_suites = [dict(r) for r in conn.execute("SELECT * FROM test_suites WHERE user_id=?", (uid,)).fetchall()]
        user = conn.execute("SELECT username, email, avatar_color, global_vars FROM users WHERE id=?", (uid,)).fetchone()
    backup = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "user": dict(user) if user else {},
        "collections": collections, "folders": folders, "requests": requests_data,
        "environments": environments, "history": history,
        "mock_endpoints": mock_endpoints, "test_suites": test_suites
    }
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"requestlab_backup_{ts}.json"
    return Response(json.dumps(backup, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/api/restore", methods=["POST"])
def restore_backup():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    mode = d.get("mode", "merge")  # merge or replace
    backup = d.get("data", {})
    if not backup: return jsonify({"error": "No backup data provided"}), 400
    id_map = {}  # old_id -> new_id for collections
    with get_db() as conn:
        if mode == "replace":
            # Delete existing data
            conn.execute("DELETE FROM shared_collections WHERE team_id IN (SELECT id FROM teams WHERE owner_id=?)", (uid,))
            conn.execute("DELETE FROM test_results WHERE suite_id IN (SELECT id FROM test_suites WHERE user_id=?)", (uid,))
            conn.execute("DELETE FROM test_suites WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM api_docs WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM mock_endpoints WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM history WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM environments WHERE user_id=?", (uid,))
            # Get collection IDs to delete requests/folders
            col_ids = [r["id"] for r in conn.execute("SELECT id FROM collections WHERE user_id=?", (uid,)).fetchall()]
            if col_ids:
                ph = ",".join("?" * len(col_ids))
                conn.execute(f"DELETE FROM requests WHERE collection_id IN ({ph})", col_ids)
                conn.execute(f"DELETE FROM folders WHERE collection_id IN ({ph})", col_ids)
                conn.execute(f"DELETE FROM shared_collections WHERE collection_id IN ({ph})", col_ids)
            conn.execute("DELETE FROM collections WHERE user_id=?", (uid,))
        # Import collections
        for col in backup.get("collections", []):
            old_id = col["id"]
            new_id = conn.execute("INSERT INTO collections (name, user_id, vars) VALUES (?,?,?)",
                                  (col["name"], uid, col.get("vars", "{}"))).lastrowid
            id_map[old_id] = new_id
        # Import folders
        folder_map = {}
        for f in backup.get("folders", []):
            new_col_id = id_map.get(f["collection_id"])
            if not new_col_id: continue
            new_fid = conn.execute("INSERT INTO folders (collection_id, parent_folder_id, name) VALUES (?,?,?)",
                                   (new_col_id, None, f["name"])).lastrowid
            folder_map[f["id"]] = new_fid
        # Update folder parent references
        for f in backup.get("folders", []):
            if f.get("parent_folder_id") and f["parent_folder_id"] in folder_map:
                new_fid = folder_map[f["id"]]
                new_parent = folder_map[f["parent_folder_id"]]
                conn.execute("UPDATE folders SET parent_folder_id=? WHERE id=?", (new_parent, new_fid))
        # Import requests
        for r in backup.get("requests", []):
            new_col_id = id_map.get(r["collection_id"])
            if not new_col_id: continue
            new_folder_id = folder_map.get(r.get("folder_id"))
            conn.execute("""INSERT INTO requests (collection_id, folder_id, name, method, url, params, headers,
                         body_type, body_content, auth_type, auth_data, pre_request_script, test_script, script_language)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (new_col_id, new_folder_id, r["name"], r.get("method","GET"), r.get("url",""),
                          r.get("params","[]"), r.get("headers","[]"), r.get("body_type","none"),
                          r.get("body_content",""), r.get("auth_type","none"), r.get("auth_data","{}"),
                          r.get("pre_request_script",""), r.get("test_script",""), r.get("script_language","javascript")))
        # Import environments
        for e in backup.get("environments", []):
            conn.execute("INSERT INTO environments (name, vars, active, user_id) VALUES (?,?,0,?)",
                         (e["name"], e.get("vars","{}"), uid))
        # Import mock endpoints
        for m in backup.get("mock_endpoints", []):
            conn.execute("INSERT INTO mock_endpoints (user_id, path, method, response_body, content_type, status_code, delay_ms, enabled) VALUES (?,?,?,?,?,?,?,?)",
                         (uid, m["path"], m.get("method","GET"), m.get("response_body","{}"),
                          m.get("content_type","application/json"), m.get("status_code",200),
                          m.get("delay_ms",0), m.get("enabled",1)))
        # Import test suites
        for ts_item in backup.get("test_suites", []):
            conn.execute("INSERT INTO test_suites (user_id, name, collection_ids, status) VALUES (?,?,?,?)",
                         (uid, ts_item["name"], ts_item.get("collection_ids","[]"), "idle"))
        # Import history (limited)
        for h in backup.get("history", [])[:500]:
            conn.execute("INSERT INTO history (user_id, method, url, status_code, duration_ms, request_data, response_data) VALUES (?,?,?,?,?,?,?)",
                         (uid, h.get("method"), h.get("url"), h.get("status_code"), h.get("duration_ms"),
                          h.get("request_data",""), h.get("response_data","")))
    return jsonify({"ok": True, "mode": mode,
                    "imported": {"collections": len(backup.get("collections",[])),
                                 "environments": len(backup.get("environments",[])),
                                 "mock_endpoints": len(backup.get("mock_endpoints",[]))}})

@app.route("/api/backup/upload", methods=["POST"])
def upload_backup():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        data = json.loads(f.read().decode("utf-8"))
    except:
        return jsonify({"error": "Invalid JSON file"}), 400
    mode = request.form.get("mode", "merge")
    # Reuse restore logic
    request._json = {"data": data, "mode": mode}
    return restore_backup()


# ─── API Monitors ──────────────────────────────────────────────────────────────

def _run_single_monitor(monitor_id, uid):
    """Execute a single monitor check and record result"""
    with get_db() as conn:
        mon = conn.execute("SELECT * FROM monitors WHERE id=? AND user_id=?", (monitor_id, uid)).fetchone()
        if not mon: return {"error": "Monitor not found"}
    url = mon["url"]
    method = mon["method"] or "GET"
    headers_list = json.loads(mon["headers"]) if mon["headers"] else []
    headers = {h["key"]: h["value"] for h in headers_list if h.get("key") and h.get("enabled", True)}
    body_type = mon["body_type"] or "none"
    body_content = mon["body_content"] or ""
    req_json = None
    req_body = None
    if body_type in ("json", "graphql"):
        try: req_json = json.loads(body_content) if body_content.strip() else None
        except: pass
        headers.setdefault("Content-Type", "application/json")
    elif body_type in ("raw", "xml", "soap"):
        req_body = body_content.encode("utf-8")
    expected_status = mon["expected_status"] or 200
    expected_body = mon["expected_body"] or ""
    start = time.time()
    status = "success"
    status_code = None
    resp_body_preview = ""
    error_msg = ""
    try:
        resp = requests.request(method=method, url=url, headers=headers, json=req_json, data=req_body,
                                timeout=30, allow_redirects=True, verify=True)
        status_code = resp.status_code
        resp_body_preview = resp.text[:500]
        duration_ms = (time.time() - start) * 1000
        if status_code != expected_status:
            status = "failure"
            error_msg = f"Expected status {expected_status}, got {status_code}"
        elif expected_body and expected_body not in resp.text:
            status = "failure"
            error_msg = f"Expected body text not found"
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        status = "failure"
        error_msg = str(e)
    # Record log
    with get_db() as conn:
        conn.execute("INSERT INTO monitor_logs (monitor_id,status,status_code,duration_ms,response_body,error) VALUES (?,?,?,?,?,?)",
                     (monitor_id, status, status_code, round(duration_ms, 2), resp_body_preview, error_msg))
        # Update monitor stats
        total_checks = mon["total_checks"] + 1
        total_failures = mon["total_failures"] + (1 if status == "failure" else 0)
        uptime_pct = round(((total_checks - total_failures) / total_checks) * 100, 2) if total_checks > 0 else 100.0
        conn.execute("UPDATE monitors SET last_status=?,last_check=datetime('now'),last_duration_ms=?,total_checks=?,total_failures=?,uptime_pct=? WHERE id=?",
                     (status, round(duration_ms, 2), total_checks, total_failures, uptime_pct, monitor_id))
    return {"status": status, "status_code": status_code, "duration_ms": round(duration_ms, 2), "error": error_msg}

@app.route("/api/monitors", methods=["POST"])
def create_monitor():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    name = d.get("name", "New Monitor").strip()
    url = d.get("url", "").strip()
    if not url: return jsonify({"error": "URL required"}), 400
    with get_db() as conn:
        mid = conn.execute("""INSERT INTO monitors (user_id,name,url,method,headers,body_type,body_content,
                           interval_seconds,expected_status,expected_body,enabled)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (uid, name, url, d.get("method","GET"), json.dumps(d.get("headers",[])),
                           d.get("body_type","none"), d.get("body_content",""),
                           d.get("interval_seconds",300), d.get("expected_status",200),
                           d.get("expected_body",""), d.get("enabled",1))).lastrowid
    return jsonify({"id": mid, "name": name, "url": url})

@app.route("/api/monitors", methods=["GET"])
def list_monitors():
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM monitors WHERE user_id=? ORDER BY created DESC", (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/monitors/<int:mid>", methods=["PUT"])
def update_monitor(mid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    with get_db() as conn:
        mon = conn.execute("SELECT user_id FROM monitors WHERE id=?", (mid,)).fetchone()
        if not mon or mon["user_id"] != uid: return jsonify({"error": "Forbidden"}), 403
        conn.execute("""UPDATE monitors SET name=?,url=?,method=?,headers=?,body_type=?,body_content=?,
                     interval_seconds=?,expected_status=?,expected_body=?,enabled=? WHERE id=?""",
                    (d.get("name",""), d.get("url",""), d.get("method","GET"),
                     json.dumps(d.get("headers",[])), d.get("body_type","none"), d.get("body_content",""),
                     d.get("interval_seconds",300), d.get("expected_status",200),
                     d.get("expected_body",""), d.get("enabled",1), mid))
    return jsonify({"ok": True})

@app.route("/api/monitors/<int:mid>", methods=["DELETE"])
def delete_monitor(mid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        mon = conn.execute("SELECT user_id FROM monitors WHERE id=?", (mid,)).fetchone()
        if not mon or mon["user_id"] != uid: return jsonify({"error": "Forbidden"}), 403
        conn.execute("DELETE FROM monitors WHERE id=?", (mid,))
    return jsonify({"ok": True})

@app.route("/api/monitors/<int:mid>/run", methods=["POST"])
def run_monitor(mid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    result = _run_single_monitor(mid, uid)
    return jsonify(result)

@app.route("/api/monitors/<int:mid>/logs", methods=["GET"])
def get_monitor_logs(mid):
    uid = require_auth()
    if not uid: return jsonify({"error": "Unauthorized"}), 401
    limit = request.args.get("limit", 50, type=int)
    with get_db() as conn:
        mon = conn.execute("SELECT user_id FROM monitors WHERE id=?", (mid,)).fetchone()
        if not mon or mon["user_id"] != uid: return jsonify({"error": "Forbidden"}), 403
        rows = conn.execute("SELECT * FROM monitor_logs WHERE monitor_id=? ORDER BY checked_at DESC LIMIT ?", (mid, limit)).fetchall()
    return jsonify([dict(r) for r in rows])


# ─── Frontend ─────────────────────────────────────────────────────────────────

LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RequestLab - Next-Gen API Client</title>
<link rel="icon" type="image/png" href="/media/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #080c10;
  --bg-glass: rgba(13, 17, 23, 0.7);
  --acc: #00d4ff;
  --acc-glow: #00d4ff60;
  --acc2: #79c0ff;
  --txt: #cdd9e5;
  --txt-dim: #8b9eb5;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Space Grotesk', sans-serif;
  background: var(--bg);
  color: var(--txt);
  min-height: 100vh;
  overflow-x: hidden;
  position: relative;
}
.blob { position: absolute; border-radius: 50%; filter: blur(80px); z-index: 0; opacity: 0.5; animation: float 12s infinite alternate ease-in-out; }
.blob.one { top: -10%; left: -10%; width: 400px; height: 400px; background: #00d4ff; }
.blob.two { bottom: 10%; right: -5%; width: 500px; height: 500px; background: #d2a8ff; animation-delay: -5s; }
.blob.three { top: 40%; left: 40%; width: 300px; height: 300px; background: #3dd68c; animation-delay: -2s; opacity: 0.3;}

@keyframes float {
  0% { transform: translate(0, 0) scale(1); }
  100% { transform: translate(50px, 80px) scale(1.1); }
}

.noise {
  position: fixed; inset: 0; z-index: 1; opacity: 0.04; pointer-events: none;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='1'/%3E%3C/svg%3E");
}

.nav {
  position: relative; z-index: 10; display: flex; align-items: center; justify-content: space-between;
  padding: 24px 60px; max-width: 1400px; margin: 0 auto;
}
.logo { font-size: 24px; font-weight: 700; display: flex; align-items: center; gap: 12px; color: #fff; text-decoration: none;}
.logo-icon { width: 44px; height: 44px; border-radius: 10px; box-shadow: 0 0 30px var(--acc-glow); filter: drop-shadow(0 0 12px var(--acc-glow)) brightness(1.1); transition: transform 0.3s; }
.logo:hover .logo-icon { transform: scale(1.1) rotate(5deg); }

.nav-links a { color: var(--txt-dim); text-decoration: none; font-weight: 500; margin-left: 32px; transition: color 0.2s; }
.nav-links a:hover { color: #fff; }

.btn-launch {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  padding: 14px 32px; font-size: 16px; font-weight: 600; color: #000; text-decoration: none;
  background: var(--acc); border-radius: 30px; transition: all 0.3s cubic-bezier(0.2, 0.8, 0.2, 1);
  box-shadow: 0 0 20px var(--acc-glow), inset 0 -2px 0 rgba(0,0,0,0.1); overflow: hidden;
}
.btn-launch::before {
  content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent);
  transition: left 0.5s;
}
.btn-launch:hover { transform: translateY(-3px) scale(1.02); box-shadow: 0 10px 30px var(--acc-glow); }
.btn-launch:hover::before { left: 100%; }

.hero {
  position: relative; z-index: 10; max-width: 1200px; margin: 0 auto; padding: 120px 20px;
  text-align: center; display: flex; flex-direction: column; align-items: center;
}
.badge {
  display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 20px;
  background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
  font-size: 13px; font-weight: 600; color: var(--acc2); margin-bottom: 24px;
  backdrop-filter: blur(10px);
}
.badge span { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--acc2); box-shadow: 0 0 8px var(--acc2); }

.hero h1 {
  font-size: clamp(48px, 6vw, 84px); font-weight: 700; line-height: 1.05; letter-spacing: -2px;
  color: #fff; margin-bottom: 30px; max-width: 900px;
}
.hero h1 span {
  background: linear-gradient(135deg, var(--acc), #d2a8ff); -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.hero p {
  font-size: 20px; color: var(--txt-dim); max-width: 600px; line-height: 1.6; margin-bottom: 48px;
}

.dashboard-preview {
  position: relative; z-index: 10; width: 90%; max-width: 1100px; margin: 0 auto 100px;
  height: 600px; border-radius: 16px; background: rgba(13, 17, 23, 0.8); backdrop-filter: blur(20px);
  border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 40px 100px rgba(0,0,0,0.5), 0 0 0 1px rgba(0,212,255,0.1);
  overflow: hidden; display: flex; flex-direction: column;
}
.dp-header { height: 40px; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; align-items: center; padding: 0 16px; gap: 8px;}
.dp-dot { width: 12px; height: 12px; border-radius: 50%; background: #f47067; }
.dp-dot:nth-child(2) { background: #e3b341; }
.dp-dot:nth-child(3) { background: #3dd68c; }
.dp-content {
  flex: 1; display: flex; flex-direction: column; padding: 20px; background: url("data:image/svg+xml,%3Csvg width='20' height='20' viewBox='0 0 20 20' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='2' cy='2' r='1' fill='rgba(255,255,255,0.05)'/%3E%3C/svg%3E");
}

.features {
  position: relative; z-index: 10; max-width: 1200px; margin: 0 auto 120px;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 30px; padding: 0 20px;
}
.feature-card {
  background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.05); border-radius: 16px;
  padding: 40px 30px; transition: all 0.3s; backdrop-filter: blur(10px);
}
.feature-card:hover {
  background: rgba(255,255,255,0.04); border-color: rgba(0,212,255,0.2); transform: translateY(-5px);
}
.fc-icon { font-size: 32px; margin-bottom: 20px; display: inline-block; }
.fc-title { font-size: 22px; font-weight: 600; color: #fff; margin-bottom: 12px; }
.fc-desc { font-size: 15px; color: var(--txt-dim); line-height: 1.6; }
</style>
</head>
<body>
  <div class="blob one"></div>
  <div class="blob two"></div>
  <div class="blob three"></div>
  <div class="noise"></div>

  <nav class="nav">
    <a href="/" class="logo">
      <img src="/media/logo.png" alt="Logo" class="logo-icon">
      RequestLab
    </a>
    <div class="nav-links">
      <a href="#features">Features</a>
      <a href="https://github.com/AnandShah10/RequestLab">GitHub</a>
      <a href="/app">Go to App</a>
    </div>
  </nav>

  <section class="hero">
    <div class="badge"><span></span> v3.0 is live</div>
    <h1>The API Workspace for the <span>Modern Web</span></h1>
    <p>Ditch the bloated tools. RequestLab is a blazingly fast, self-hosted API client with team collaboration, CI/CD, monitoring, code generation, and more — all in one lightweight package.</p>
    <a href="/app" class="btn-launch">Launch Workspace</a>
  </section>

  <div class="dashboard-preview">
    <div class="dp-header">
      <div class="dp-dot"></div><div class="dp-dot"></div><div class="dp-dot"></div>
    </div>
    <div class="dp-content">
      <div style="width: 100%; height: 60px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; margin-bottom: 16px; display: flex; align-items: center; padding: 0 16px; gap: 12px;">
        <div style="padding: 4px 10px; background: rgba(0,212,255,0.1); color: #00d4ff; border-radius: 4px; font-size: 12px; font-weight: bold;">GET</div>
        <div style="font-family: monospace; color: #fff;">{{base_url}}/api/v1/users</div>
        <div style="margin-left: auto; padding: 8px 24px; background: #00d4ff; color: #000; border-radius: 4px; font-weight: bold; font-size: 13px;">Send</div>
      </div>
      <div style="flex: 1; display: flex; gap: 16px;">
        <div style="flex: 1; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.03); border-radius: 8px;"></div>
        <div style="flex: 2; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.03); border-radius: 8px;"></div>
      </div>
    </div>
  </div>

  <section id="features" class="features">
    <div class="feature-card">
      <div class="fc-icon">⚡</div>
      <div class="fc-title">Lightning Fast</div>
      <div class="fc-desc">2-second startup, &lt;100MB RAM. Built with a lightweight Python + Flask stack — no Electron, no heavy frameworks, zero lag.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">🔒</div>
      <div class="fc-title">100% Private & Self-Hosted</div>
      <div class="fc-desc">Complete data ownership with local SQLite storage. No cloud dependency, no telemetry, no data leaving your machine.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">👥</div>
      <div class="fc-title">Team Collaboration</div>
      <div class="fc-desc">Create teams, invite members with role-based access (Admin/Editor/Viewer), and share collections for collaborative API testing.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">🐍</div>
      <div class="fc-title">Python Scripting</div>
      <div class="fc-desc">Write pre-request and test scripts in Python with a Postman-compatible <code>pm.*</code> API. Pre-processors and post-processors on every request.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">🔄</div>
      <div class="fc-title">CI/CD & Test Suites</div>
      <div class="fc-desc">Group collections into test suites, generate CI tokens, and integrate with Jenkins, GitHub Actions, and GitLab CI pipelines.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">📡</div>
      <div class="fc-title">API Monitor</div>
      <div class="fc-desc">Track uptime with configurable health checks, interval scheduling, failure tracking, and detailed check history logs.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">💻</div>
      <div class="fc-title">Code Generation</div>
      <div class="fc-desc">Generate ready-to-use code in 10+ languages — cURL, Python, JavaScript, Go, Java, C#, PHP, Ruby, Rust, and Node.js.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">📝</div>
      <div class="fc-title">API Documentation</div>
      <div class="fc-desc">Auto-generate professional docs from saved requests. Export as OpenAPI 3.0, Markdown, or standalone HTML.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">📱</div>
      <div class="fc-title">Fully Responsive</div>
      <div class="fc-desc">Works on desktop, tablet, and mobile with 4 adaptive breakpoints. The only API client with full mobile support.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">🎯</div>
      <div class="fc-title">Smart Variables</div>
      <div class="fc-desc">Environment and global variables with <code>{{variable}}</code> substitution, color-coded highlighting, and autocomplete.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">🎭</div>
      <div class="fc-title">Mock Server</div>
      <div class="fc-desc">Create mock API endpoints with custom responses, status codes, and delays — test without a backend.</div>
    </div>
    <div class="feature-card">
      <div class="fc-icon">💾</div>
      <div class="fc-title">Backup & Restore</div>
      <div class="fc-desc">Export your entire workspace as JSON — collections, requests, environments, monitors, teams, and more. Restore with one click.</div>
    </div>
  </section>
</body>
</html>"""

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RequestLab</title>
<link rel="icon" type="image/png" href="/media/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://unpkg.com/mqtt/dist/mqtt.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#080c10;--bg1:#0d1117;--bg2:#131920;--bg3:#1a2230;--bg4:#212d40;
  --border:#1e2d3d;--border2:#253548;--border3:#2d4060;
  --txt:#cdd9e5;--txt2:#8b9eb5;--txt3:#4d6377;
  --acc:#00d4ff;--acc2:#00b8e0;--acc-dim:#00d4ff12;--acc-glow:#00d4ff30;
  --green:#3dd68c;--red:#f47067;--blue:#79c0ff;--yellow:#e3b341;--purple:#d2a8ff;--orange:#f0883e;
  --mono:'IBM Plex Mono',monospace;--sans:'Space Grotesk',sans-serif;
  --radius:5px;--radius-lg:8px;--radius-xl:12px;
  --shadow:0 4px 24px rgba(0,0,0,.4);--glow:0 0 20px var(--acc-glow);
}
[data-theme="light"] {
  --bg0:#f0f2f5;--bg1:#ffffff;--bg2:#f8f9fa;--bg3:#e1e4e8;--bg4:#d1d5da;
  --border:#e1e4e8;--border2:#d1d5da;--border3:#c6cbd1;
  --txt:#24292e;--txt2:#586069;--txt3:#6a737d;
  --shadow:0 4px 24px rgba(0,0,0,.1);
}
html,body{height:100%;overflow:hidden;background:var(--bg0);color:var(--txt);font-family:var(--sans)}
body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");pointer-events:none;z-index:0;opacity:.4}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--border3)}

/* ── Layout ── */
#app{display:grid;grid-template-columns:260px 1fr;grid-template-rows:52px 1fr;height:100vh;position:relative;z-index:1}
#topbar{grid-column:1/-1;display:flex;align-items:center;background:var(--bg1);border-bottom:1px solid var(--border);z-index:20;padding:0}
#sidebar{background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#main{display:flex;flex-direction:column;overflow:hidden;background:var(--bg0);position:relative}
#view-builder{flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0}
#view-environments,#view-mocks,#view-runner,#view-testsuites,#view-docs,#view-settings,#view-monitor{flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0}
/* ── Topbar ── */
.logo-area{display:flex;align-items:center;gap:10px;padding:0 18px;width:260px;border-right:1px solid var(--border);height:100%;flex-shrink:0}
.logo-mark{width:26px;height:26px;background:var(--acc);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#000;box-shadow:0 0 12px var(--acc-glow);flex-shrink:0}
.logo-text{font-weight:700;font-size:15px;letter-spacing:-.3px;color:var(--txt)}
.logo-text span{color:var(--acc)}
.top-nav{display:flex;height:100%;flex:1;padding:0 12px;gap:2px;align-items:center}
.top-tab{padding:7px 14px;border-radius:var(--radius);font-size:12px;font-weight:600;cursor:pointer;color:var(--txt3);border:none;background:transparent;transition:all .15s;font-family:var(--sans);letter-spacing:.2px}
.top-tab:hover{color:var(--txt2);background:var(--bg3)}
.top-tab.active{color:var(--acc);background:var(--acc-dim)}
.top-right{display:flex;align-items:center;gap:8px;padding:0 16px;margin-left:auto}
.env-select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:5px 10px;font-size:11px;color:var(--txt2);outline:none;cursor:pointer;font-family:var(--mono);transition:border-color .15s}
.env-select:hover,.env-select:focus{border-color:var(--border3)}
.conn-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}

/* ── Sidebar ── */
.sidebar-tabs{display:flex;border-bottom:1px solid var(--border)}
.s-tab{flex:1;padding:10px 0;font-size:11px;font-weight:600;cursor:pointer;color:var(--txt3);border-bottom:2px solid transparent;transition:all .15s;text-align:center;letter-spacing:.5px;text-transform:uppercase;background:none;border-left:none;border-right:none;border-top:none;font-family:var(--sans)}
.s-tab:hover{color:var(--txt2)}
.s-tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.sidebar-inner{flex:1;display:flex;flex-direction:column;overflow:hidden}
.sidebar-toolbar{display:flex;align-items:center;gap:6px;padding:10px 10px 6px}
.sidebar-search{flex:1;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:6px 10px;font-size:11px;color:var(--txt);font-family:var(--mono);outline:none;transition:border-color .15s}
.sidebar-search:focus{border-color:var(--acc)}
.sidebar-search::placeholder{color:var(--txt3)}
.script-editor{width:100%;min-height:280px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;font-size:12.5px;line-height:1.6;color:var(--txt);font-family:var(--mono);resize:vertical;outline:none;tab-size:4;transition:border-color .15s}
.script-editor:focus{border-color:var(--acc)}
.script-editor::placeholder{color:var(--txt3);font-style:italic}
#help-content h1{margin:0 0 14px;font-size:22px;font-weight:700;color:var(--txt)}
#help-content h2{margin:0 0 14px;font-size:18px;font-weight:700;color:var(--txt)}
#help-content h3{margin:18px 0 8px;font-size:14px;font-weight:600;color:var(--txt)}
#help-content p{margin:0 0 10px}
#help-content ul,#help-content ol{margin:0 0 12px;padding-left:22px}
#help-content ul,#help-content ol{margin:0 0 12px;padding-left:22px}
#help-content li{margin-bottom:4px}
#help-content code{background:var(--bg1);padding:1px 5px;border-radius:3px;font-size:12px;color:var(--acc)}
#help-content pre{margin:0 0 12px}
.help-nav-item:hover{background:var(--bg2) !important}
.icon-btn{background:none;border:1px solid var(--border);color:var(--txt3);cursor:pointer;padding:5px 8px;border-radius:var(--radius);display:flex;align-items:center;font-size:12px;transition:all .15s;font-family:var(--mono)}
.icon-btn:hover{color:var(--txt);background:var(--bg3);border-color:var(--border2)}
.icon-btn.accent{border-color:var(--acc-dim);color:var(--acc)}
.icon-btn.accent:hover{background:var(--acc-dim)}
.sidebar-scroll{flex:1;overflow-y:auto;padding:4px 8px 12px}

/* ── Collection tree ── */
.coll-group{margin-bottom:2px}
.coll-header{display:flex;align-items:center;gap:6px;padding:7px 8px;cursor:pointer;font-size:12px;font-weight:600;color:var(--txt2);user-select:none;border-radius:var(--radius);transition:all .15s}
.coll-header:hover{background:var(--bg3);color:var(--txt)}
.coll-arrow{font-size:9px;transition:transform .2s;flex-shrink:0;opacity:.5}
.coll-arrow.open{transform:rotate(90deg);opacity:1}
.coll-icon{font-size:13px;opacity:.7}
.coll-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.coll-count{font-size:10px;color:var(--txt3);background:var(--bg3);padding:1px 6px;border-radius:10px;font-family:var(--mono)}
.coll-actions{display:none;gap:3px;align-items:center}
.coll-header:hover .coll-actions{display:flex}
.coll-act-btn, .req-act-btn{background:transparent;border:1px solid transparent;color:var(--txt3);cursor:pointer;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;border-radius:4px;transition:all .15s ease;padding:0}
.coll-act-btn:hover, .req-act-btn:hover{background:var(--bg4);color:var(--txt);border-color:var(--border2);box-shadow:0 2px 5px rgba(0,0,0,0.2)}
.coll-act-btn.danger:hover, .req-act-btn.danger:hover{background:rgba(244,112,103,.15);color:var(--red);border-color:rgba(244,112,103,.4)}
.coll-act-btn.accent-btn:hover, .req-act-btn.dup:hover{background:var(--acc-dim);color:var(--acc);border-color:rgba(0,212,255,.3)}
.req-list{margin-left:11px; border-left:1px solid var(--border); padding-left:4px; display:none;flex-direction:column;gap:1px;padding-top:2px;padding-bottom:4px}
.req-list.open{display:flex}

/* ── Folder nodes ── */
.folder-node{margin-bottom:1px}
.folder-hdr{display:flex;align-items:center;gap:5px;padding:5px 8px;cursor:pointer;font-size:11.5px;font-weight:600;color:var(--txt2);user-select:none;border-radius:var(--radius);transition:all .15s;position:relative}
.folder-hdr::before{content:'';position:absolute;left:-5px;top:14px;width:5px;height:1px;background:var(--border);pointer-events:none}
.folder-hdr:hover{background:var(--bg3);color:var(--txt)}
.folder-nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fold-count{font-size:9.5px;color:var(--txt3);background:var(--bg3);padding:1px 5px;border-radius:10px;font-family:var(--mono)}
.fold-acts{display:none;gap:3px;align-items:center}
.folder-hdr:hover .fold-acts{display:flex}
.folder-children{border-left:1px solid var(--border);margin-left:15px;padding-left:4px;display:none;flex-direction:column;gap:1px;padding-top:2px;padding-bottom:2px}
.folder-children.open{display:flex}
.f-arrow{font-size:8px;transition:transform .2s;flex-shrink:0;opacity:.5;min-width:10px}
.f-arrow.open{transform:rotate(90deg);opacity:1}

/* ── Request items ── */
.req-item{display:flex;align-items:center;gap:7px;padding:5px 8px;border-radius:var(--radius);cursor:pointer;font-size:11px;color:var(--txt3);transition:all .12s;border:1px solid transparent;position:relative}
.req-item::before{content:'';position:absolute;left:-5px;top:14px;width:5px;height:1px;background:var(--border);pointer-events:none}
.req-item:hover{background:var(--bg3);color:var(--txt2);border-color:var(--border)}
.req-item.active{background:var(--acc-dim);color:var(--acc);border-color:var(--acc-dim)}
.req-method{font-family:var(--mono);font-size:9.5px;font-weight:600;min-width:36px;letter-spacing:.3px}
.req-name-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.req-item-actions{display:none;gap:3px;margin-left:auto;flex-shrink:0;align-items:center}
.req-item:hover .req-item-actions{display:flex}

/* ── History ── */
.hist-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--radius);cursor:pointer;font-size:11px;color:var(--txt2);transition:all .12s;margin-bottom:1px}
.hist-item:hover{background:var(--bg3);color:var(--txt)}
.hist-url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;font-family:var(--mono);font-size:10.5px}

/* ── Method colors ── */
.m-GET{color:#3dd68c}.m-POST{color:#f0883e}.m-PUT{color:#79c0ff}
.m-PATCH{color:#d2a8ff}.m-DELETE{color:#f47067}.m-HEAD{color:#e3b341}.m-OPTIONS{color:#00d4ff}

/* ── Request tab bar ── */
#req-tabs-bar{display:flex;align-items:stretch;background:var(--bg1);border-bottom:1px solid var(--border);min-height:36px;overflow-x:auto;overflow-y:hidden;flex-shrink:0}
#req-tabs-bar::-webkit-scrollbar{height:3px}
.req-tab-pill{display:flex;align-items:center;gap:6px;padding:0 14px;font-size:11px;font-weight:600;font-family:var(--mono);color:var(--txt3);cursor:pointer;border-right:1px solid var(--border);background:transparent;border-top:none;border-left:none;border-bottom:none;transition:all .15s;white-space:nowrap;min-width:120px;max-width:200px;position:relative;flex-shrink:0}
.req-tab-pill:hover{background:var(--bg2);color:var(--txt2)}
.req-tab-pill.active{background:var(--bg0);color:var(--txt);border-bottom:2px solid var(--acc)}
.req-tab-method{font-size:9px;font-weight:700;min-width:30px}
.req-tab-name{overflow:hidden;text-overflow:ellipsis;flex:1;text-align:left}
.req-tab-close{opacity:0;background:none;border:none;color:var(--txt3);cursor:pointer;font-size:11px;padding:0 2px;border-radius:3px;line-height:1;transition:all .1s;flex-shrink:0}
.req-tab-pill:hover .req-tab-close,.req-tab-pill.active .req-tab-close{opacity:1}
.req-tab-close:hover{color:var(--red);background:rgba(244,112,103,.15)}
.req-tab-pill.unsaved .req-tab-name::after{content:'●';margin-left:5px;font-size:8px;color:var(--acc);opacity:.8}
#new-tab-btn{padding:0 12px;font-size:16px;color:var(--txt3);cursor:pointer;background:none;border:none;transition:all .15s;flex-shrink:0;align-self:center}
#new-tab-btn:hover{color:var(--acc)}

/* ── URL bar ── */
.url-bar{display:flex;gap:8px;align-items:center;padding:10px 16px;background:var(--bg1);border-bottom:1px solid var(--border);flex-shrink:0}
.req-name-display{display:flex;align-items:center;gap:6px;padding:4px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);font-size:11px;color:var(--txt2);font-family:var(--mono);cursor:pointer;transition:all .15s;white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis}
.req-name-display:hover{border-color:var(--border2);color:var(--txt)}
.method-select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:7px 10px;font-size:12px;font-weight:700;color:var(--green);cursor:pointer;outline:none;font-family:var(--mono);transition:border-color .15s}
.method-select:hover,.method-select:focus{border-color:var(--border3)}
.url-input-wrap{flex:1;position:relative;overflow:hidden}
.url-input{width:100%;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:8px 14px;font-size:12px;color:var(--txt);font-family:var(--mono);outline:none;transition:all .15s;position:relative;z-index:1}
.url-input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-dim)}
.url-input::placeholder{color:var(--txt3)}
.url-input.has-vars{/* removed transparent text hack, input renders normally */}
.url-highlight-layer{position:absolute;top:0;left:0;right:0;bottom:0;padding:8px 14px;font-size:12px;font-family:var(--mono);pointer-events:none;white-space:pre;overflow:hidden;border-radius:var(--radius);z-index:2;color:transparent;border:1px solid transparent;line-height:16px;letter-spacing:normal;word-spacing:normal}
.url-input{line-height:16px;letter-spacing:normal;word-spacing:normal}
.var-badge{background:var(--bg2);color:#ff8c32;border-radius:3px;padding:0 2px;font-weight:600;position:relative;cursor:default;box-shadow:inset 0 0 0 1px rgba(255,140,50,.25)}
.var-badge.resolved{background:var(--bg2);color:#3dd68c;box-shadow:inset 0 0 0 1px rgba(60,200,120,.25)}
.var-badge.unresolved{background:var(--bg2);color:#f47067;box-shadow:inset 0 0 0 1px rgba(244,112,103,.25)}
.var-tooltip{position:absolute;background:var(--bg2);border:1px solid var(--border2);border-radius:6px;padding:4px 10px;font-size:10px;font-family:var(--mono);white-space:nowrap;z-index:99999;pointer-events:none;box-shadow:0 4px 16px rgba(0,0,0,.4);display:none}
.var-tooltip::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:5px solid transparent;border-top-color:var(--border2)}
.var-tooltip .vt-key{color:var(--txt3)}
.var-tooltip .vt-arrow{color:var(--txt3);margin:0 4px}
.var-tooltip .vt-val{color:#3dd68c;font-weight:600}
.var-tooltip .vt-unresolved{color:#f47067;font-style:italic}
.kv-input.has-var{box-shadow:inset 0 0 0 1px rgba(255,140,50,.35);background:rgba(255,140,50,.06)!important;border-radius:4px}
.code-editor.has-var{box-shadow:inset 0 0 0 1px rgba(255,140,50,.3)}
.auth-field input.has-var{box-shadow:inset 0 0 0 1px rgba(255,140,50,.35);background:rgba(255,140,50,.06)!important}
.var-indicator{display:inline-flex;align-items:center;gap:4px;font-size:9px;color:#ff8c32;padding:2px 6px;background:rgba(255,140,50,.1);border-radius:4px;border:1px solid rgba(255,140,50,.2);margin-left:6px;font-family:var(--mono);letter-spacing:.3px}
.btn-group{display:flex;gap:6px;flex-shrink:0}
.save-btn{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:8px 14px;font-size:12px;font-weight:600;color:var(--txt2);cursor:pointer;font-family:var(--sans);transition:all .15s}
.save-btn:hover{border-color:var(--border3);color:var(--txt)}
.send-btn{background:var(--acc);border:none;border-radius:var(--radius);padding:8px 22px;font-size:12px;font-weight:700;color:#000;cursor:pointer;font-family:var(--sans);letter-spacing:.3px;transition:all .2s;box-shadow:0 0 12px var(--acc-glow)}
.send-btn:hover{background:var(--acc2);box-shadow:0 0 20px var(--acc-glow);transform:translateY(-1px)}
.send-btn:active{transform:translateY(0)}
.send-btn:disabled{opacity:.5;cursor:wait;transform:none}
.cancel-btn{background:transparent;border:1px solid var(--red);border-radius:var(--radius);padding:8px 14px;font-size:12px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--sans);transition:all .2s}
.cancel-btn:hover{background:var(--red);color:#fff}

/* ── Tabs ── */
.tab-bar{display:flex;background:var(--bg1);border-bottom:1px solid var(--border);padding:0 16px;gap:0}
.tab{padding:9px 14px;font-size:11px;font-weight:600;cursor:pointer;color:var(--txt3);border-bottom:2px solid transparent;transition:all .15s;letter-spacing:.3px;white-space:nowrap}
.tab:hover{color:var(--txt2)}
.tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.tab-badge{background:var(--bg3);color:var(--txt3);font-size:9.5px;padding:1px 5px;border-radius:10px;margin-left:4px;font-family:var(--mono)}
.tab-badge.has{background:var(--acc-dim);color:var(--acc)}
.tab-content{padding:14px 16px;overflow-y:auto;flex:1}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* ── KV Table ── */
.kv-wrap{overflow:hidden;border-radius:var(--radius-lg);border:1px solid var(--border)}
.kv-table{width:100%;border-collapse:collapse;font-size:11.5px;font-family:var(--mono)}
.kv-table thead tr{background:var(--bg2)}
.kv-table th{text-align:left;padding:7px 10px;font-size:9.5px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--txt3);border-bottom:1px solid var(--border)}
.kv-table td{padding:2px 4px;vertical-align:middle;border-bottom:1px solid var(--border)}
.kv-table tr:last-child td{border-bottom:none}
.kv-table tbody tr:hover td{background:var(--bg2)}
.kv-input{width:100%;background:transparent;border:none;padding:5px 8px;font-size:11.5px;color:var(--txt);font-family:var(--mono);outline:none;border-radius:4px}
.kv-input:focus{background:var(--bg3)}
.kv-input::placeholder{color:var(--txt3)}
.kv-cb{accent-color:var(--acc);width:13px;height:13px;cursor:pointer;margin:0 4px}
.add-row-btn{margin-top:8px;background:none;border:1px dashed var(--border2);border-radius:var(--radius);padding:6px 14px;font-size:11px;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:all .15s}
.add-row-btn:hover{border-color:var(--acc);color:var(--acc)}
.del-row-btn{background:none;border:none;color:var(--txt3);cursor:pointer;padding:3px 6px;border-radius:4px;font-size:11px;opacity:0;transition:all .15s}
.kv-table tbody tr:hover .del-row-btn{opacity:1}
.del-row-btn:hover{color:var(--red);background:rgba(244,112,103,.1)}
.form-type-select{background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:3px 6px;font-size:10px;color:var(--txt3);outline:none;cursor:pointer;font-family:var(--mono);transition:border-color .15s;max-width:80px}
.form-type-select:focus{border-color:var(--acc);color:var(--txt)}
.file-cell-wrap{display:flex;align-items:center;gap:4px;flex:1}
.file-pick-btn{background:var(--bg3);border:1px solid var(--border2);border-radius:3px;padding:3px 8px;font-size:10px;color:var(--txt3);cursor:pointer;font-family:var(--mono);white-space:nowrap;transition:all .15s}
.file-pick-btn:hover{border-color:var(--acc);color:var(--acc)}
.file-name-txt{font-size:10px;color:var(--txt3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px}

/* ── Body ── */
.body-type-bar{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.body-type-btn{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;color:var(--txt3);font-family:var(--mono);transition:all .15s}
.body-type-btn:hover{border-color:var(--border2);color:var(--txt2)}
.body-type-btn.active{background:var(--acc-dim);border-color:var(--acc);color:var(--acc)}
.code-editor{width:100%;min-height:220px;max-height:50vh;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;font-size:13px;color:var(--txt);font-family:var(--mono);resize:vertical;outline:none;line-height:1.75;tab-size:2;transition:border-color .15s,box-shadow .15s}
.code-editor:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(0,212,255,.08)}
.code-editor::placeholder{color:var(--txt3)}
#body-editor-wrap{position:relative}
.body-editor-toolbar{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.body-editor-toolbar .editor-hint{font-size:10px;color:var(--txt3);font-family:var(--mono);margin-left:auto;opacity:.6}
.body-editor-toolbar .editor-hint kbd{background:var(--bg3);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:9px;font-family:var(--mono)}

/* ── Auth ── */
.auth-type-select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:7px 12px;font-size:12px;color:var(--txt);outline:none;cursor:pointer;font-family:var(--mono);margin-bottom:14px;transition:border-color .15s}
.auth-type-select:focus{border-color:var(--acc)}
.auth-field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
.auth-field label{font-size:10px;font-weight:700;color:var(--txt3);letter-spacing:.8px;text-transform:uppercase}
.auth-field input,.auth-field select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:8px 12px;font-size:12px;color:var(--txt);font-family:var(--mono);outline:none;transition:border-color .15s}
.auth-field input:focus,.auth-field select:focus{border-color:var(--acc)}
.auth-field input::placeholder{color:var(--txt3)}

/* ── Response panel ── */
#response-panel{background:var(--bg1);border-top:1px solid var(--border);display:flex;flex-direction:column;min-height:40px;max-height:48vh;transition:max-height .2s ease,min-height .2s ease}
#response-panel.collapsed{min-height:40px;max-height:40px;overflow:hidden}
#response-panel.collapsed .tab-bar,#response-panel.collapsed .resp-body{display:none !important}
.resp-topbar{display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--bg1);border-bottom:1px solid var(--border);flex-shrink:0}
.resp-close-btn{background:none;border:1px solid var(--border);border-radius:var(--radius);color:var(--txt3);cursor:pointer;padding:3px 8px;font-size:11px;font-family:var(--mono);transition:all .15s;display:flex;align-items:center;gap:4px}
.resp-close-btn:hover{color:var(--txt);border-color:var(--border2);background:var(--bg3)}
.resp-close-btn svg{width:12px;height:12px}
.resp-label{font-size:10px;font-weight:700;letter-spacing:1px;color:var(--txt3);text-transform:uppercase}
.status-badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;font-family:var(--mono)}
.s-2xx{background:#3dd68c20;color:#3dd68c}.s-3xx{background:#79c0ff20;color:#79c0ff}
.s-4xx{background:#e3b34120;color:#e3b341}.s-5xx{background:#f4706720;color:#f47067}.s-err{background:#f4706720;color:#f47067}
.resp-meta{font-size:11px;color:var(--txt3);font-family:var(--mono);display:flex;gap:14px}
.resp-meta .val{color:var(--txt2)}
.resp-topbar-right{margin-left:auto;display:flex;gap:6px;align-items:center}
.copy-btn{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:4px 10px;font-size:11px;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:all .15s}
.copy-btn:hover{color:var(--txt);border-color:var(--border2)}
.resp-body{flex:1;overflow:auto;padding:12px 16px}
.resp-body pre{font-size:12px;font-family:var(--mono);line-height:1.65;white-space:pre-wrap;word-break:break-all;color:var(--txt)}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--txt3);font-size:12px;gap:10px}
.empty-icon{font-size:32px;opacity:.2}
.empty-state p{opacity:.5;font-family:var(--mono)}
.j-key{color:#79c0ff}.j-str{color:#a5d6a7}.j-num{color:#f0883e}.j-bool{color:#e3b341}.j-null{color:#d2a8ff}
.resp-headers-table{width:100%;font-size:11.5px;font-family:var(--mono);border-collapse:collapse}
.resp-headers-table tr:nth-child(even) td{background:var(--bg2)}
.resp-headers-table td{padding:5px 10px;border-bottom:1px solid var(--border);vertical-align:top}
.resp-headers-table td:first-child{color:var(--txt3);width:38%;white-space:nowrap}
.resp-headers-table td:last-child{color:var(--txt);word-break:break-all}

/* ── Response view mode tabs ── */
.resp-view-bar{display:flex;gap:2px;align-items:center;margin-left:12px}
.resp-view-btn{background:transparent;border:1px solid var(--border);border-radius:var(--radius);padding:3px 10px;font-size:10px;font-weight:600;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:all .15s;letter-spacing:.3px}
.resp-view-btn:hover{color:var(--txt2);border-color:var(--border2)}
.resp-view-btn.active{background:var(--acc-dim);border-color:var(--acc);color:var(--acc)}
.resp-preview-iframe{width:100%;border:none;background:#fff;border-radius:var(--radius);min-height:200px}
.resp-raw-pre{font-size:12px;font-family:var(--mono);line-height:1.65;white-space:pre-wrap;word-break:break-all;color:var(--txt3)}

/* ── Var autocomplete dropdown ── */
.var-autocomplete{position:absolute;z-index:500;background:var(--bg2);border:1px solid var(--border2);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.5);max-height:180px;overflow-y:auto;min-width:200px;padding:4px;display:none}
.var-ac-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-family:var(--mono);transition:background .1s}
.var-ac-item:hover,.var-ac-item.selected{background:var(--bg4)}
.var-ac-key{color:var(--acc);font-weight:600}
.var-ac-val{color:var(--txt3);font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.var-ac-empty{padding:8px 10px;font-size:11px;color:var(--txt3);font-family:var(--mono);font-style:italic}

/* ── Beautify button ── */
.beautify-btn{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:3px 10px;font-size:10px;font-weight:600;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:all .15s;margin-left:auto}
.beautify-btn:hover{border-color:var(--acc);color:var(--acc)}

/* ── Environments ── */
.env-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-xl);padding:16px;margin-bottom:10px;transition:border-color .15s}
.env-card:hover{border-color:var(--border2)}
.env-card-header{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.env-name-input{background:transparent;border:none;font-size:14px;font-weight:700;color:var(--txt);outline:none;flex:1;font-family:var(--sans);border-bottom:1px solid transparent;padding-bottom:2px;transition:border-color .15s}
.env-name-input:focus{border-bottom-color:var(--acc)}
.env-active-badge{background:#3dd68c15;color:#3dd68c;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;font-family:var(--mono);border:1px solid #3dd68c30}
.activate-btn{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:4px 10px;font-size:11px;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:all .15s}
.activate-btn:hover{border-color:var(--acc);color:var(--acc)}
.env-save-btn{background:var(--acc);border:none;border-radius:var(--radius);padding:5px 14px;font-size:11px;font-weight:700;color:#000;cursor:pointer;font-family:var(--sans);transition:all .15s}
.env-save-btn:hover{background:var(--acc2)}

/* ── Modals ── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.modal-overlay.open{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:var(--radius-xl);padding:24px;min-width:360px;max-width:500px;width:100%;box-shadow:var(--shadow);animation:modalIn .15s ease-out}
@keyframes modalIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.modal h3{font-size:15px;font-weight:700;color:var(--txt)}
.modal-close{background:none;border:none;color:var(--txt3);cursor:pointer;font-size:18px;padding:2px 6px;border-radius:4px;transition:all .15s}
.modal-close:hover{color:var(--txt);background:var(--bg3)}
.form-group{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.form-label{font-size:10px;font-weight:700;color:var(--txt3);letter-spacing:.8px;text-transform:uppercase}
.modal input,.modal select,.modal textarea{width:100%;background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:9px 12px;font-size:12px;color:var(--txt);font-family:var(--mono);outline:none;transition:border-color .15s}
.modal input:focus,.modal select:focus{border-color:var(--acc)}
.modal input::placeholder{color:var(--txt3)}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}
.btn-primary{background:var(--acc);border:none;border-radius:var(--radius);padding:8px 20px;font-size:12px;font-weight:700;color:#000;cursor:pointer;font-family:var(--sans);transition:all .15s}
.btn-primary:hover{background:var(--acc2)}
.btn-secondary{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:8px 16px;font-size:12px;font-weight:600;color:var(--txt2);cursor:pointer;font-family:var(--sans);transition:all .15s}
.btn-secondary:hover{border-color:var(--border3);color:var(--txt)}

/* ── Toast ── */
#toast-container{position:fixed;bottom:20px;right:20px;z-index:10000;display:flex;flex-direction:column;gap:8px}
.toast{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius-lg);padding:10px 16px;font-size:12px;color:var(--txt);font-family:var(--sans);box-shadow:var(--shadow);animation:toastIn .2s ease-out;display:flex;align-items:center;gap:8px;min-width:220px}
.toast.success{border-left:3px solid var(--green)}.toast.error{border-left:3px solid var(--red)}.toast.info{border-left:3px solid var(--acc)}
@keyframes toastIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
@keyframes toastOut{to{opacity:0;transform:translateX(20px)}}

/* ── Resize handle ── */
#resize-handle{height:4px;background:transparent;cursor:ns-resize;flex-shrink:0;border-top:1px solid var(--border);transition:background .15s}
#resize-handle:hover,#resize-handle.dragging{background:var(--acc-dim)}

/* ── Spinner ── */
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{width:13px;height:13px;border:2px solid rgba(0,0,0,.3);border-top-color:#000;border-radius:50%;animation:spin .7s linear infinite;display:inline-block}

/* ── Import area ── */
.import-drop{border:2px dashed var(--border2);border-radius:var(--radius-lg);padding:24px;text-align:center;color:var(--txt3);font-size:12px;cursor:pointer;transition:all .15s;margin-bottom:12px}
.import-drop:hover,.import-drop.over{border-color:var(--acc);color:var(--acc);background:var(--acc-dim)}
.import-drop .import-icon{font-size:28px;margin-bottom:8px;opacity:.5}

/* ── Quick action separator ── */
.tree-empty{color:var(--txt3);font-size:11px;padding:6px 10px;font-family:var(--mono);font-style:italic}

/* ── User Menu ── */
.user-menu{position:relative;display:flex;align-items:center}
.user-avatar-btn{background:none;border:none;cursor:pointer;padding:0;display:flex;align-items:center}
.user-avatar{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#000;text-transform:uppercase;box-shadow:0 0 8px rgba(0,0,0,.3);transition:box-shadow .15s}
.user-avatar:hover{box-shadow:0 0 14px rgba(0,212,255,.4)}
.user-dropdown{position:absolute;top:calc(100% + 8px);right:0;background:var(--bg2);border:1px solid var(--border2);border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.5);min-width:200px;padding:6px;z-index:100;display:none}
.user-dropdown.open{display:block}
.user-dd-header{padding:10px 12px}
.user-dd-name{display:block;font-size:13px;font-weight:700;color:var(--txt)}
.user-dd-email{display:block;font-size:11px;color:var(--txt3);margin-top:2px}
.user-dd-divider{height:1px;background:var(--border);margin:4px 0}
.user-dd-item{display:block;width:100%;text-align:left;background:none;border:none;color:var(--txt2);padding:8px 12px;border-radius:6px;font-size:12px;cursor:pointer;font-family:var(--sans);transition:all .12s}
.user-dd-item:hover{background:var(--bg4);color:var(--red)}

/* ── Auth Gate ── */
.auth-gate{position:fixed;inset:0;background:var(--bg0);z-index:9999;display:flex;align-items:center;justify-content:center}
.auth-card{background:var(--bg1);border:1px solid var(--border2);border-radius:16px;padding:36px 32px;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.auth-logo{display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:24px}
.auth-logo .logo-mark{width:36px;height:36px;font-size:17px}
.auth-logo .logo-text{font-size:20px}
.auth-tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid var(--border)}
.auth-tab{flex:1;padding:10px 0;text-align:center;font-size:12px;font-weight:600;color:var(--txt3);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;background:none;border-top:none;border-left:none;border-right:none;font-family:var(--sans);letter-spacing:.5px;text-transform:uppercase}
.auth-tab:hover{color:var(--txt2)}
.auth-tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.auth-form{display:flex;flex-direction:column;gap:12px}
.auth-form input{width:100%;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;font-size:13px;color:var(--txt);font-family:var(--mono);outline:none;transition:border-color .15s;box-sizing:border-box}
.auth-form input:focus{border-color:var(--acc)}
.auth-form input::placeholder{color:var(--txt3)}
.pw-wrap{position:relative;display:flex;width:100%}
.pw-wrap input{padding-right:36px;width:100%}
.pw-toggle{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--txt3);cursor:pointer;font-size:14px;padding:4px;display:flex;align-items:center;justify-content:center;transition:color .15s}
.pw-toggle:hover{color:var(--acc)}
.auth-submit{background:var(--acc);border:none;border-radius:var(--radius);padding:10px;font-size:13px;font-weight:700;color:#000;cursor:pointer;font-family:var(--sans);transition:all .15s;letter-spacing:.3px}
.auth-submit:hover{background:var(--acc2);box-shadow:0 0 16px var(--acc-glow)}
.auth-submit:disabled{opacity:.5;cursor:wait}
.auth-error{color:var(--red);font-size:11px;font-family:var(--mono);text-align:center;min-height:16px}
.auth-skip{display:block;text-align:center;margin-top:14px;font-size:11px;color:var(--txt3);cursor:pointer;font-family:var(--mono);transition:color .15s;background:none;border:none;width:100%}
.auth-skip:hover{color:var(--acc)}
.coll-header.drag-over, .folder-hdr.drag-over { background: rgba(0, 212, 255, 0.15); border-radius: 4px; }
.req-item.dragging { opacity: 0.5; }

/* ── Sidebar Collapse ── */
.sidebar-toggle{background:none;border:none;color:var(--txt3);cursor:pointer;padding:4px 6px;border-radius:var(--radius);transition:all .15s;display:flex;align-items:center;justify-content:center;margin-left:auto;flex-shrink:0}
.sidebar-toggle:hover{color:var(--acc);background:var(--bg3)}
#app{transition:grid-template-columns .2s ease}
#app.sidebar-collapsed{grid-template-columns:0px 1fr !important}
#app.sidebar-collapsed #sidebar{overflow:hidden;width:0;min-width:0;border-right:none;padding:0;opacity:0;pointer-events:none}
#app.sidebar-collapsed .logo-area{width:auto !important;min-width:auto;border-right:1px solid var(--border)}
#app.sidebar-collapsed #sidebar-drag{display:none !important}

/* ── Sidebar Resize Handle ── */
#sidebar-drag{position:absolute;top:52px;width:5px;cursor:col-resize;z-index:50;bottom:0;transition:background .15s}
#sidebar-drag:hover,#sidebar-drag.dragging{background:rgba(0,212,255,.25)}

/* ── Body Error Bar ── */
.body-error-bar{display:none;padding:7px 14px;font-size:11px;font-family:var(--mono);color:#f47067;background:linear-gradient(135deg,rgba(244,112,103,.06),rgba(244,112,103,.12));border:1px solid rgba(244,112,103,.25);border-radius:var(--radius);margin-top:8px;align-items:center;gap:8px;animation:errSlideIn .2s ease}
@keyframes errSlideIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.body-error-bar.visible{display:flex}
.body-error-bar .err-icon{font-weight:700;flex-shrink:0;font-size:13px}
.body-error-bar .err-msg{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.body-error-bar .err-line{flex-shrink:0;color:var(--txt3);font-size:10px;background:rgba(244,112,103,.15);padding:2px 8px;border-radius:10px}
.body-editor-invalid{border-color:rgba(244,112,103,.5) !important;box-shadow:0 0 0 3px rgba(244,112,103,.1) !important}

/* ── JSON Tree View ── */
.json-tree{font-family:var(--mono);font-size:12.5px;line-height:1.8;padding:8px 0;margin:0}
.jt-row{display:flex;align-items:center;white-space:pre;min-height:24px;padding:1px 8px;border-radius:4px;transition:background .1s}
.jt-row:hover{background:rgba(0,212,255,.04)}
.jt-toggle{cursor:pointer;user-select:none;display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;flex-shrink:0;color:var(--txt3);transition:all .15s;font-size:9px;border-radius:3px;margin-right:2px}
.jt-toggle:hover{color:var(--acc);background:var(--bg3)}
.jt-toggle.closed{transform:rotate(-90deg)}
.jt-bracket{color:var(--txt3);font-weight:600}
.jt-ell{color:var(--acc);cursor:pointer;padding:1px 8px;background:var(--acc-dim);border-radius:4px;font-size:10px;margin:0 4px;font-style:normal;font-weight:600;transition:all .15s}
.jt-ell:hover{background:var(--acc);color:#000}
.jt-ln{color:var(--txt3);opacity:.3;min-width:36px;text-align:right;padding-right:12px;user-select:none;font-size:11px}
.jt-colon{color:var(--txt3);margin:0 2px}
.jt-comma{color:var(--txt3)}
.jt-count{color:var(--txt3);font-size:10px;font-style:italic;margin-left:8px;opacity:.5;background:var(--bg3);padding:1px 8px;border-radius:10px}
.jt-children{margin-left:4px;padding-left:16px;border-left:1px solid var(--border)}
.jt-children:hover{border-left-color:var(--acc-dim)}
#resp-body-tree{display:none;overflow:auto;padding:12px 20px}
/* ── Responsive Design ── */
/* Tablet: 768px - 1024px */
@media (max-width: 1024px) {
  #app {
    grid-template-columns: 200px 1fr !important;
  }
  
  .logo-area {
    width: 200px !important;
    padding: 0 12px;
  }
  
  .logo-text {
    font-size: 13px;
  }
  
  .top-tab {
    padding: 6px 12px;
    font-size: 11px;
  }
  
  .req-name-display {
    max-width: 120px;
  }
}

/* Mobile Landscape: 768px */
@media (max-width: 768px) {
  #app {
    grid-template-columns: 1fr !important;
  }
  
  #sidebar {
    display: none !important;
  }
  
  #sidebar.sidebar-open {
    display: flex !important;
    position: fixed;
    left: 0;
    top: 52px;
    bottom: 0;
    width: 280px;
    z-index: 100;
    box-shadow: 2px 0 8px rgba(0,0,0,0.3);
  }
  
  .logo-area {
    width: auto !important;
    padding: 0 10px;
  }
  
  .logo-mark {
    width: 22px;
    height: 22px;
    font-size: 11px;
  }
  
  .logo-text {
    font-size: 12px;
  }
  
  .top-nav {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    padding: 0 6px;
    gap: 1px;
  }
  
  .top-tab {
    white-space: nowrap;
    padding: 5px 8px;
    font-size: 10px;
  }
  
  .top-right {
    gap: 4px;
    padding: 0 8px;
  }
  
  .env-select {
    font-size: 10px;
    padding: 4px 6px;
  }
  
  .req-name-display {
    max-width: 100px;
    font-size: 10px;
    padding: 3px 8px;
  }
  
  .url-bar {
    flex-wrap: wrap;
    gap: 6px;
    padding: 8px 10px;
  }
  
  .method-select {
    width: 100% !important;
    font-size: 11px;
    padding: 6px 8px;
  }
  
  .url-input-wrap {
    width: 100%;
    order: 3;
  }
  
  .url-input {
    font-size: 11px;
    padding: 7px 10px;
  }
  
  .btn-group {
    width: 100%;
    order: 4;
    gap: 4px;
  }
  
  .save-btn, .send-btn, .cancel-btn {
    flex: 1;
    padding: 7px 10px;
    font-size: 11px;
  }
  
  .tab-bar {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    padding: 0 8px;
  }
  
  .tab {
    padding: 8px 10px;
    font-size: 10px;
  }
  
  .body-type-bar {
    flex-wrap: wrap;
    gap: 3px;
  }
  
  .body-type-btn {
    font-size: 9px;
    padding: 3px 6px;
  }
  
  .beautify-btn {
    font-size: 9px;
    padding: 2px 6px;
  }
  
  .code-editor {
    min-height: 180px;
    font-size: 11px;
    padding: 10px;
  }
  
  .kv-table {
    font-size: 10px;
  }
  
  .kv-table th {
    font-size: 8px;
    padding: 5px 6px;
  }
  
  .kv-table td {
    padding: 2px 3px;
  }
  
  .kv-input {
    font-size: 10px;
    padding: 4px 5px;
  }
  
  .add-row-btn {
    font-size: 10px;
    padding: 5px 10px;
  }
  
  .env-card {
    padding: 10px;
  }
  
  .env-card-header {
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }
  
  .env-name-input {
    width: 100% !important;
    font-size: 12px;
  }
  
  .env-save-btn, .activate-btn {
    font-size: 10px;
    padding: 4px 10px;
  }
  
  #runner-panel > div {
    max-width: 100% !important;
    padding: 0 4px;
  }
  
  .resp-topbar {
    flex-wrap: wrap;
    gap: 6px;
    padding: 6px 10px;
  }
  
  .resp-label {
    font-size: 9px;
  }
  
  .status-badge {
    font-size: 10px;
    padding: 2px 8px;
  }
  
  .resp-meta {
    font-size: 10px;
    gap: 8px;
  }
  
  .copy-btn {
    font-size: 10px;
    padding: 3px 8px;
  }
  
  .resp-body {
    padding: 8px 10px;
  }
  
  .resp-body pre {
    font-size: 11px;
  }
  
  .modal {
    min-width: auto;
    max-width: 95%;
    margin: 0 10px;
  }
}

/* Mobile Portrait: 480px */
@media (max-width: 480px) {
  .logo-text {
    display: none;
  }
  
  .logo-area {
    padding: 0 6px;
  }
  
  .logo-mark {
    width: 20px;
    height: 20px;
    font-size: 10px;
  }
  
  .top-tab {
    padding: 4px 6px;
    font-size: 9px;
  }
  
  .env-select {
    font-size: 9px;
    padding: 3px 5px;
  }
  
  .req-name-display {
    max-width: 80px;
    font-size: 9px;
    padding: 2px 6px;
  }
  
  .url-bar {
    padding: 6px 8px;
  }
  
  .method-select {
    font-size: 10px;
    padding: 5px 6px;
  }
  
  .url-input {
    font-size: 10px;
    padding: 6px 8px;
  }
  
  .save-btn, .send-btn, .cancel-btn {
    padding: 6px 8px;
    font-size: 10px;
  }
  
  .tab {
    padding: 6px 8px;
    font-size: 9px;
  }
  
  .body-type-btn {
    padding: 2px 5px;
    font-size: 8px;
  }
  
  .code-editor {
    min-height: 150px;
    font-size: 10px;
    padding: 8px;
  }
  
  .kv-table {
    font-size: 9px;
  }
  
  .kv-table th {
    font-size: 7px;
    padding: 4px 4px;
  }
  
  .kv-table td {
    padding: 1px 2px;
  }
  
  .kv-input {
    font-size: 9px;
    padding: 3px 4px;
  }
  
  .env-card {
    padding: 8px;
  }
  
  .env-name-input {
    font-size: 11px;
  }
  
  .env-save-btn, .activate-btn, .btn-primary, .btn-secondary {
    font-size: 9px;
    padding: 4px 8px;
  }
  
  .modal {
    padding: 16px;
  }
  
  .modal h3 {
    font-size: 13px;
  }
  
  .modal input, .modal select, .modal textarea {
    font-size: 11px;
    padding: 7px 10px;
  }
  
  #toast-container {
    bottom: 10px;
    right: 10px;
    left: 10px;
  }
  
  .toast {
    font-size: 11px;
    padding: 8px 12px;
    min-width: auto;
  }
}

/* Very small screens: 360px */
@media (max-width: 360px) {
  .top-nav {
    padding: 0 4px;
  }
  
  .top-tab {
    padding: 3px 5px;
    font-size: 8px;
  }
  
  .logo-area {
    padding: 0 4px;
  }
  
  .logo-mark {
    width: 18px;
    height: 18px;
    font-size: 9px;
  }
  
  .url-input {
    font-size: 9px;
    padding: 5px 6px;
  }
  
  .method-select {
    font-size: 9px;
    padding: 4px 5px;
  }
  
  .save-btn, .send-btn, .cancel-btn {
    padding: 5px 6px;
    font-size: 9px;
  }
  
  .kv-table {
    font-size: 8px;
  }
  
  .kv-table th {
    font-size: 6px;
    padding: 3px 3px;
  }
  
  .kv-table td {
    padding: 1px 1px;
  }
  
  .kv-input {
    font-size: 8px;
    padding: 2px 3px;
  }
  
  .code-editor {
    font-size: 9px;
    padding: 6px;
    min-height: 120px;
  }
  
  .env-card {
    padding: 6px;
  }
  
  .env-name-input {
    font-size: 10px;
  }
}

/* ── Runner Cards Base CSS ── */
.runner-config-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 20px;
  margin-bottom: 20px;
}

.runner-result-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  text-align: center;
}

/* ── Mock Method Select ── */
.mock-method-select {
  width: 80px;
}
.env-card-content {
  transition: max-height 0.3s ease-out;
  overflow: hidden;
}
</style>
</head>
<body>

<!-- Auth Gate -->
<div class="auth-gate" id="auth-gate" style="display:none">
  <div class="auth-card">
    <div class="auth-logo">
      <img src="/media/logo.png" alt="Logo" style="width: 52px; height: 52px; border-radius: 12px; filter: drop-shadow(0 0 15px rgba(0, 212, 255, 0.3)) brightness(1.1);">
      <div class="logo-text" style="font-size: 28px;">Request<span>Lab</span></div>
    </div>
    <div class="auth-tabs">
      <button class="auth-tab active" onclick="authTab('login')" id="at-login">Sign In</button>
      <button class="auth-tab" onclick="authTab('register')" id="at-register">Create Account</button>
    </div>
    <div class="auth-error" id="auth-error"></div>
    <!-- Login form -->
    <form class="auth-form" id="login-form" onsubmit="doLogin(event)">
      <input id="login-field" type="text" placeholder="Username or Email" autocomplete="username" required>
      <div class="pw-wrap">
        <input id="login-pw" type="password" placeholder="Password" autocomplete="current-password" required>
        <button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" title="Show password"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg></button>
      </div>
      <button type="submit" class="auth-submit" id="login-btn">Sign In</button>
      <a href="#" onclick="authTab('forgot'); return false;" style="font-size:11px;color:var(--txt3);text-align:right;margin-top:-6px;text-decoration:none;">Forgot password?</a>
    </form>
    <!-- Forgot password form -->
    <form class="auth-form" id="forgot-form" style="display:none" onsubmit="doForgotPassword(event)">
      <div style="font-size:12px;color:var(--txt2);text-align:center;margin-bottom:12px">Enter your email to receive a password reset link.</div>
      <input id="forgot-email" type="email" placeholder="Email Address" required>
      <button type="submit" class="auth-submit" id="forgot-btn">Send Reset Link</button>
      <a href="#" onclick="authTab('login'); return false;" style="font-size:11px;color:var(--txt3);text-align:center;text-decoration:none;margin-top:6px;">← Back to Sign In</a>
    </form>
    <!-- Reset password form (shown via link) -->
    <form class="auth-form" id="reset-form" style="display:none" onsubmit="doResetPassword(event)">
      <div style="font-size:12px;color:var(--txt2);text-align:center;margin-bottom:12px">Set a new password.</div>
      <input id="reset-token" type="hidden">
      <div class="pw-wrap">
        <input id="reset-pw" type="password" placeholder="New Password" required minlength="4">
        <button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" title="Show password"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg></button>
      </div>
      <button type="submit" class="auth-submit" id="reset-btn">Save New Password</button>
      <a href="#" onclick="authTab('login'); return false;" style="font-size:11px;color:var(--txt3);text-align:center;text-decoration:none;margin-top:6px;">← Back to Sign In</a>
    </form>
    <!-- Register form -->
    <form class="auth-form" id="register-form" style="display:none" onsubmit="doRegister(event)">
      <input id="reg-username" type="text" placeholder="Username" autocomplete="username" required>
      <input id="reg-email" type="email" placeholder="Email" autocomplete="email" required>
      <div class="pw-wrap">
        <input id="reg-pw" type="password" placeholder="Password (min 4 chars)" autocomplete="new-password" required minlength="4">
        <button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" title="Show password"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg></button>
      </div>
      <button type="submit" class="auth-submit" id="reg-btn">Create Account</button>
    </form>
    <button class="auth-skip" onclick="skipAuth()">Continue without account →</button>
  </div>
</div>

<div id="app" style="display:none">
<div id="sidebar-drag" style="left:259px"></div>

  <!-- Topbar -->
  <header id="topbar">
    <div class="logo-area">
      <div class="logo-mark">R</div>
      <div class="logo-text">Request<span>Lab</span></div>
      <button class="sidebar-toggle" id="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar (Ctrl+\\)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg></button>
    </div>
    <div class="top-nav">
      <button class="top-tab active" onclick="switchView('builder')">Builder</button>
      <button class="top-tab" onclick="switchView('environments')">Envs</button>
      <button class="top-tab" onclick="switchView('mocks')">Mocks</button>
      <button class="top-tab" onclick="switchView('runner')">Runner</button>
      <button class="top-tab" onclick="switchView('testsuites')">Test Suites</button>
      <button class="top-tab" onclick="switchView('docs')">Docs</button>
      <button class="top-tab" onclick="switchView('monitor')">Monitor</button>
      <button class="top-tab" onclick="switchView('settings')">Settings</button>
    </div>
    <div class="top-right">
      <button class="icon-btn" onclick="openHelpModal()" title="Documentation & Shortcuts" style="border:none;font-size:15px;font-weight:700;color:var(--acc)">?</button>
      <button class="icon-btn" onclick="toggleTheme()" title="Toggle Theme" id="theme-btn" style="border:none;font-size:16px;">☀️</button>
      <select id="env-selector" class="env-select" onchange="selectEnv(this.value)">
        <option value="">No Environment</option>
      </select>
      <div class="conn-dot" title="Server connected"></div>
      <div id="user-menu" class="user-menu" style="display:none">
        <button class="user-avatar-btn" id="user-avatar-btn" onclick="toggleUserDropdown()">
          <span class="user-avatar" id="user-avatar">?</span>
        </button>
        <div class="user-dropdown" id="user-dropdown">
          <div class="user-dd-header">
            <span class="user-dd-name" id="user-dd-name"></span>
            <span class="user-dd-email" id="user-dd-email"></span>
          </div>
          <div class="user-dd-divider"></div>
          <button class="user-dd-item" onclick="attemptLogout()">Sign Out</button>
        </div>
      </div>
    </div>
  </header>

  <!-- Sidebar -->
  <aside id="sidebar">
    <div class="sidebar-tabs">
      <button class="s-tab active" id="st-collections" onclick="sidebarTab('collections')">Collections</button>
      <button class="s-tab" id="st-history" onclick="sidebarTab('history')">History</button>
      <button class="s-tab" id="st-teams" onclick="sidebarTab('teams')">Teams</button>
    </div>
    <div id="sp-collections" class="sidebar-inner">
      <div class="sidebar-toolbar">
        <input class="sidebar-search" id="coll-search" placeholder="Search…" oninput="filterCollections(this.value)">
        <button class="icon-btn accent" title="Import Collection" onclick="openImportModal()">⬆</button>
        <button class="icon-btn accent" title="New Collection" onclick="openNewCollModal()">＋</button>
      </div>
      <div class="sidebar-scroll" id="collections-tree"></div>
    </div>
    <div id="sp-history" class="sidebar-inner" style="display:none">
      <div class="sidebar-toolbar">
        <span style="font-size:11px;color:var(--txt3);font-family:var(--mono);flex:1">Recent Requests</span>
        <button class="icon-btn" title="Clear history" onclick="clearHistory()">🗑</button>
      </div>
      <div class="sidebar-scroll" id="history-list"></div>
    </div>
    <div id="sp-teams" class="sidebar-inner" style="display:none">
      <div class="sidebar-toolbar">
        <input class="sidebar-search" id="team-search" placeholder="Search teams..." style="flex:1">
        <button class="icon-btn accent" title="Create team" onclick="showCreateTeamModal()">+</button>
      </div>
      <div class="sidebar-scroll" id="teams-list"></div>
    </div>
  </aside>

  <!-- Main -->
  <main id="main">
    <div id="view-builder">

      <!-- Request tab bar -->
      <div id="req-tabs-bar">
        <button id="new-tab-btn" onclick="newTab()" title="New tab">＋</button>
      </div>

      <div id="request-panel" style="display:flex;flex-direction:column;overflow:hidden;flex:1">
        <!-- URL bar -->
        <div class="url-bar">
          <div class="req-name-display" id="req-name-display" onclick="openRenameReqModal()" title="Click to rename">
            <span id="req-name-text">Untitled Request</span>
            <span style="font-size:9px;opacity:.4">✎</span>
          </div>
          <select id="protocol-select" class="method-select" style="width:105px; border-right: 1px solid var(--border2);" onchange="updateProtocolUI(); markTabDirty()">
            <option value="http">HTTP</option>
            <option value="soap">SOAP</option>
            <option value="ws">WebSocket</option>
            <option value="socketio">Socket.io</option>
            <option value="mqtt">MQTT</option>
            <option value="grpc">gRPC</option>
          </select>
          <select id="method-select" class="method-select" onchange="updateMethodColor();markTabDirty()">
            <option>GET</option><option>POST</option><option>PUT</option>
            <option>PATCH</option><option>DELETE</option><option>HEAD</option><option>OPTIONS</option>
          </select>
          <div class="url-input-wrap">
            <input id="url-input" class="url-input" type="text" placeholder="https://api.example.com/endpoint"
              oninput="markTabDirty();highlightUrlVars()" onkeydown="if(event.key==='Enter')sendRequest()" onscroll="syncHighlightScroll()">
            <div class="url-highlight-layer" id="url-highlight-layer"></div>
          </div>
          <div class="btn-group">
            <button class="save-btn" onclick="handleSave()">Save</button>
            <button class="icon-btn" onclick="openCodeGen()" title="Generate Code" style="padding:6px 10px;font-size:11px">&lt;/&gt;</button>
            <button class="send-btn" id="send-btn" onclick="sendRequest()">Send</button>
            <button class="cancel-btn" id="cancel-btn" onclick="cancelRequest()" style="display:none">✕ Cancel</button>
          </div>
        </div>

        <!-- HTTP Panel -->
        <div id="http-panel" style="display:flex;flex-direction:column;flex:1;overflow:hidden">
          <!-- Request section tabs -->
          <div class="tab-bar">
          <div class="tab active" onclick="reqTab('params')" id="rt-params">Params <span class="tab-badge" id="tc-params">0</span></div>
          <div class="tab" onclick="reqTab('headers')" id="rt-headers">Headers <span class="tab-badge" id="tc-headers">0</span></div>
          <div class="tab" onclick="reqTab('body')" id="rt-body">Body</div>
          <div class="tab" onclick="reqTab('auth')" id="rt-auth">Auth</div>
          <div class="tab" onclick="reqTab('cookies')" id="rt-cookies">Cookies <span class="tab-badge" id="tc-cookies">0</span></div>
          <div class="tab" onclick="reqTab('preproc')" id="rt-preproc">Pre-Processor</div>
          <div class="tab" onclick="reqTab('postproc')" id="rt-postproc">Post-Processor</div>
        </div>

        <div style="flex:1;overflow-y:auto;background:var(--bg0)">
          <!-- Params -->
          <div class="tab-content tab-pane active" id="pane-params">
            <div class="kv-wrap"><table class="kv-table">
              <thead><tr><th style="width:28px"></th><th>Key</th><th>Value</th><th>Description</th><th style="width:36px"></th></tr></thead>
              <tbody id="params-body"></tbody>
            </table></div>
            <button class="add-row-btn" onclick="addKVRow('params')">+ Add Parameter</button>
          </div>
          <!-- Headers -->
          <div class="tab-content tab-pane" id="pane-headers">
            <div class="kv-wrap"><table class="kv-table">
              <thead><tr><th style="width:28px"></th><th>Key</th><th>Value</th><th>Description</th><th style="width:36px"></th></tr></thead>
              <tbody id="headers-body"></tbody>
            </table></div>
            <button class="add-row-btn" onclick="addKVRow('headers')">+ Add Header</button>
          </div>
          <!-- Body -->
          <div class="tab-content tab-pane" id="pane-body">
            <div class="body-type-bar">
              <button class="body-type-btn active" onclick="setBodyType('none')">none</button>
              <button class="body-type-btn" onclick="setBodyType('json')">JSON</button>
              <button class="body-type-btn" onclick="setBodyType('graphql')">GraphQL</button>
              <button class="body-type-btn" onclick="setBodyType('raw')">raw</button>
              <button class="body-type-btn" onclick="setBodyType('form')">form-data</button>
              <button class="body-type-btn" onclick="setBodyType('urlencoded')">urlencoded</button>
              <button class="body-type-btn" onclick="setBodyType('soap')">soap</button>
              <button class="body-type-btn" onclick="setBodyType('xml')">xml</button>
              <button class="beautify-btn" id="beautify-btn" style="display:none" onclick="beautifyBody()">✨ Beautify</button>
            </div>
            <div id="body-none-msg" style="color:var(--txt3);font-size:12px;font-family:var(--mono);padding:8px 0">This request does not have a body.</div>
            <div id="body-editor-wrap" style="display:none; position:relative; width:100%;">
              <div class="body-editor-toolbar">
                <span class="editor-hint"><kbd>Ctrl</kbd>+<kbd>/</kbd> comment • <kbd>Ctrl</kbd>+<kbd>B</kbd> beautify • <kbd>Tab</kbd> indent</span>
              </div>
              <textarea class="code-editor" id="body-editor" style="position:relative; z-index:1; background:transparent; color:transparent; caret-color:var(--txt); white-space:pre;" placeholder="Enter request body…" spellcheck="false" oninput="markTabDirty(); updateBodyHighlight()" onscroll="document.getElementById('body-highlight-layer').scrollTop = this.scrollTop; document.getElementById('body-highlight-layer').scrollLeft = this.scrollLeft;" onkeydown="handleBodyKeydown(event)"></textarea>
              <pre id="body-highlight-layer" class="code-editor" style="position:absolute; top:33px; left:0; right:0; bottom:0; z-index:0; margin:0; pointer-events:none; white-space:pre; overflow:hidden; border-color:transparent; background:var(--bg2);"></pre>
              <div class="body-error-bar" id="body-error-bar"><span class="err-icon">✕</span><span class="err-msg" id="body-error-msg"></span></div>
            </div>
            <div id="body-graphql-wrap" style="display:none; width:100%;">
              <div style="font-size:11px;font-weight:700;color:var(--txt3);margin-bottom:4px;text-transform:uppercase;">Query</div>
              <textarea class="code-editor" id="graphql-query" style="min-height:140px; margin-bottom:12px; font-family:var(--mono);" placeholder="query { ... }" oninput="markTabDirty()"></textarea>
              <div style="font-size:11px;font-weight:700;color:var(--txt3);margin-bottom:4px;text-transform:uppercase;">Variables (JSON)</div>
              <textarea class="code-editor" id="graphql-vars" style="min-height:80px; font-family:var(--mono);" placeholder="{}" oninput="markTabDirty()"></textarea>
            </div>
            <div id="body-kv-wrap" style="display:none">
              <div class="kv-wrap"><table class="kv-table">
                <thead><tr><th style="width:28px"></th><th>Type</th><th>Key</th><th>Value / File</th><th style="width:36px"></th></tr></thead>
                <tbody id="body-kv-body"></tbody>
              </table></div>
              <button class="add-row-btn" onclick="addFormRow()">+ Add Field</button>
            </div>
          </div>
          <!-- Auth -->
          <div class="tab-content tab-pane" id="pane-auth">
            <select class="auth-type-select" id="auth-type" onchange="renderAuthFields();markTabDirty()">
              <option value="none">No Auth</option>
              <option value="basic">Basic Auth</option>
              <option value="bearer">Bearer Token</option>
              <option value="oauth2">OAuth 2.0</option>
              <option value="awsv4">AWS Signature</option>
              <option value="apikey">API Key</option>
            </select>
            <div id="auth-fields"></div>
          </div>
          <!-- Cookies -->
          <div class="tab-content tab-pane" id="pane-cookies">
            <div class="kv-wrap"><table class="kv-table">
              <thead><tr><th style="width:28px"></th><th>Key</th><th>Value</th><th>Description</th><th style="width:36px"></th></tr></thead>
              <tbody id="cookies-body"></tbody>
            </table></div>
            <button class="add-row-btn" onclick="addKVRow('cookies')">+ Add Cookie</button>
          </div>
          <!-- Pre-Processor -->
          <div class="tab-content tab-pane" id="pane-preproc">
            <div style="padding:8px 10px;font-size:11px;color:var(--txt3)">Runs before the request. Modify headers, params, or set environment variables.</div>
            <textarea id="preproc-editor" class="script-editor" placeholder="# Pre-Processor (Python)
# Available: pm.environment, pm.globals, pm.request
# Example:
pm.environment.set('timestamp', str(int(__import__('time').time())))" spellcheck="false" oninput="markTabDirty()"></textarea>
          </div>
          <!-- Post-Processor -->
          <div class="tab-content tab-pane" id="pane-postproc">
            <div style="padding:8px 10px;font-size:11px;color:var(--txt3)">Runs after the response. Validate, transform, or extract data from the response.</div>
            <textarea id="postproc-editor" class="script-editor" placeholder="# Post-Processor (Python)
# Available: pm.environment, pm.globals, pm.response, pm.expect, pm.test
# Example:
pm.test('Status is 200', lambda: pm.response.get('status_code') == 200)" spellcheck="false" oninput="markTabDirty()"></textarea>
          </div>
        </div>
      </div>

      <div id="resize-handle"></div>

      <!-- Response panel -->
      <div id="response-panel">
        <div class="resp-topbar">
          <span class="resp-label">Response</span>
          <div id="resp-status-wrap" style="display:none;align-items:center;gap:10px">
            <span class="status-badge" id="resp-status-badge"></span>
            <div class="resp-meta" id="resp-meta"></div>
          </div>
          <div class="resp-topbar-right">
            <button class="copy-btn" id="copy-resp-btn" onclick="copyResponse()">Copy</button>
            <button class="copy-btn" onclick="downloadResponse()">Download</button>
            <button class="resp-close-btn" id="resp-collapse-btn" onclick="toggleResponsePanel()" title="Toggle response panel">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
          </div>
        </div>
        <div class="tab-bar">
          <div class="tab active" onclick="respTab('body')" id="rst-body">Body</div>
          <div class="tab" onclick="respTab('headers')" id="rst-headers">Headers</div>
          <div class="tab" onclick="respTab('cookies')" id="rst-cookies">Cookies</div>
          <div class="resp-view-bar" id="resp-view-bar" style="display:none">
            <button class="resp-view-btn active" onclick="setRespView('pretty')" id="rv-pretty">Pretty</button>
            <button class="resp-view-btn" onclick="setRespView('tree')" id="rv-tree">Tree</button>
            <button class="resp-view-btn" onclick="setRespView('raw')" id="rv-raw">Raw</button>
            <button class="resp-view-btn" onclick="setRespView('preview')" id="rv-preview">Preview</button>
          </div>
        </div>
        <div class="resp-body" id="resp-body-pane">
          <div class="empty-state" id="resp-empty">
            <div class="empty-icon">◈</div>
            <p>Hit <strong>Send</strong> to fire a request</p>
          </div>
          <pre id="resp-body-content" style="display:none"></pre>
          <pre id="resp-body-raw" class="resp-raw-pre" style="display:none"></pre>
          <iframe id="resp-body-preview" class="resp-preview-iframe" style="display:none" sandbox="allow-same-origin"></iframe>
          <div id="resp-body-tree" class="json-tree"></div>
        </div>
        <div class="resp-body" id="resp-headers-pane" style="display:none">
          <table class="resp-headers-table"><tbody id="resp-headers-tbody"></tbody></table>
        </div>
        <div class="resp-body" id="resp-cookies-pane" style="display:none">
          <table class="resp-headers-table"><tbody id="resp-cookies-tbody"></tbody></table>
        </div>
      </div> <!-- End HTTP Panel -->

      <!-- Realtime Panel -->
      <div id="realtime-panel" style="display:none;flex-direction:column;flex:1;overflow:hidden;background:var(--bg0);">
        <div style="padding:16px; border-bottom:1px solid var(--border); display:flex; gap:12px; align-items:center; background:var(--bg1);">
          <div id="realtime-status-dot" style="width:10px;height:10px;border-radius:50%;background:var(--txt3);"></div>
          <span id="realtime-status-text" style="font-size:12px;font-family:var(--mono);color:var(--txt2);font-weight:700;">Disconnected</span>
          <button class="btn-primary" id="realtime-connect-btn" onclick="toggleRealtimeConnection()" style="margin-left:auto;">Connect</button>
        </div>
        <div id="realtime-config-bar" style="padding:12px 16px; border-bottom:1px solid var(--border); display:none; gap:10px; align-items:center;">
          <!-- Socket.io / MQTT config like Event Name or Topic will go here -->
        </div>
        <div id="realtime-log" style="flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:8px; font-family:var(--mono); font-size:12px;">
          <div style="color:var(--txt3);text-align:center;margin-top:20px;font-style:italic;">Enter a URL and connect to start session.</div>
        </div>
        <div style="padding:12px; border-top:1px solid var(--border); background:var(--bg1); display:flex; gap:8px;">
          <textarea id="realtime-msg-input" class="code-editor" style="min-height:40px; height:60px; flex:1;" placeholder="Type message to send..."></textarea>
          <button class="btn-primary" onclick="sendRealtimeMessage()" style="align-self:flex-end;">Send</button>
        </div>
      </div> <!-- End Realtime Panel -->

    </div>

    <!-- Environments View -->
    <div id="view-environments" style="flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">Environments</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Use <code style="font-family:var(--mono);color:var(--acc)">{{variable}}</code> in your requests</p>
        </div>
        <button class="btn-primary" onclick="createEnvironment()">+ New Environment</button>
      </div>
      <div id="envs-panel" style="overflow-y:auto;padding:16px;flex:1"></div>
    </div>
    
    <!-- Mock Server View -->
    <div id="view-mocks" style="flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">Mock Server</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Base URL: <code style="font-family:var(--mono);color:var(--acc)">http://localhost:5000/mock/</code></p>
        </div>
        <button class="btn-primary" onclick="createMockEndpoint()">+ New Mock</button>
      </div>
      <div id="mocks-panel" style="overflow-y:auto;padding:16px;flex:1"></div>
    </div>
    
    <!-- Collection Runner View -->
    <div id="view-runner" style="flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">Collection Runner</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Run collections with iterations and delays</p>
        </div>
      </div>
      <div id="runner-panel" style="overflow-y:auto;padding:16px;flex:1">
        <div style="max-width:600px;margin:0 auto">
          <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-xl);padding:20px;margin-bottom:20px">
            <h3 style="font-size:13px;font-weight:700;margin-bottom:12px">Run Configuration</h3>
            <div style="margin-bottom:12px">
              <label style="font-size:11px;color:var(--txt3);display:block;margin-bottom:4px">Select Collection</label>
              <select id="runner-collection" class="kv-input" style="width:100%;padding:8px"></select>
            </div>
            <div style="display:flex;gap:12px;margin-bottom:12px">
              <div style="flex:1">
                <label style="font-size:11px;color:var(--txt3);display:block;margin-bottom:4px">Iterations</label>
                <input id="runner-iterations" type="number" class="kv-input" value="1" min="1" max="100" style="width:100%;padding:8px">
              </div>
              <div style="flex:1">
                <label style="font-size:11px;color:var(--txt3);display:block;margin-bottom:4px">Delay (ms)</label>
                <input id="runner-delay" type="number" class="kv-input" value="0" min="0" style="width:100%;padding:8px">
              </div>
            </div>
            <button class="btn-primary" onclick="runCollection()" style="width:100%">Run Collection</button>
          </div>
          <div id="runner-results" style="display:none"></div>
        </div>
      </div>
    </div>

    <!-- Test Suites View -->
    <div id="view-testsuites" style="display:none;flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">Test Suites</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Group collections and run automated tests</p>
        </div>
        <button class="icon-btn accent" onclick="showCreateSuiteModal()" style="padding:6px 14px;font-size:11px">+ New Suite</button>
      </div>
      <div style="flex:1;overflow-y:auto;padding:16px 20px">
        <div id="suites-list" style="display:flex;flex-direction:column;gap:10px"></div>
        <div id="suite-results-panel" style="display:none;margin-top:20px">
          <h3 style="font-size:13px;font-weight:700;margin-bottom:10px">Run History</h3>
          <div id="suite-results-list" style="display:flex;flex-direction:column;gap:8px"></div>
        </div>
      </div>
    </div>

    <!-- API Docs View -->
    <div id="view-docs" style="display:none;flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">API Documentation</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Generate and export API docs from collections</p>
        </div>
        <div style="display:flex;gap:6px">
          <select id="docs-collection-select" class="env-select" style="min-width:160px" onchange="loadDocsForCollection()">
            <option value="">Select collection...</option>
          </select>
          <button class="icon-btn" onclick="exportDocs('openapi')" title="Export OpenAPI">JSON</button>
          <button class="icon-btn" onclick="exportDocs('markdown')" title="Export Markdown">MD</button>
          <button class="icon-btn" onclick="exportDocs('html')" title="Export HTML">HTML</button>
        </div>
      </div>
      <div style="flex:1;overflow-y:auto;padding:16px 20px" id="docs-content-area">
        <p style="color:var(--txt3);font-size:12px">Select a collection to generate API documentation.</p>
      </div>
    </div>

    <!-- Settings View (CI/CD + Backup) -->
    <div id="view-settings" style="display:none;flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);background:var(--bg1)">
        <h2 style="font-size:14px;font-weight:700">Settings</h2>
        <p style="font-size:11px;color:var(--txt3);margin-top:2px">CI/CD integration, backup &amp; restore</p>
      </div>
      <div style="flex:1;overflow-y:auto;padding:16px 20px">
        <div style="margin-bottom:24px">
          <h3 style="font-size:13px;font-weight:700;margin-bottom:12px">CI/CD Integration</h3>
          <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
            <input id="ci-token-name" class="sidebar-search" placeholder="Token name..." style="max-width:200px">
            <button class="icon-btn accent" onclick="createCIToken()" style="padding:6px 14px;font-size:11px">Generate Token</button>
          </div>
          <div id="ci-tokens-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:16px"></div>
          <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px;font-size:12px">
            <h4 style="font-size:12px;font-weight:700;margin-bottom:8px">CI/CD Usage</h4>
            <p style="color:var(--txt3);font-size:11px;margin-bottom:8px">Run test suites from your CI pipeline:</p>
            <pre style="background:var(--bg0);padding:10px;border-radius:var(--radius);font-size:11px;overflow-x:auto;color:var(--txt2);font-family:var(--mono)">curl -X POST http://localhost:5000/api/ci/run \
  -H "X-CI-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"suite_id": 1, "format": "junit"}'</pre>
            <p style="color:var(--txt3);font-size:11px;margin-top:8px">Get results as JUnit XML for CI integration or JSON for custom processing.</p>
          </div>
        </div>
        <div>
          <h3 style="font-size:13px;font-weight:700;margin-bottom:12px">Backup &amp; Restore</h3>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button class="icon-btn accent" onclick="downloadBackup()" style="padding:8px 18px;font-size:11px">Export Backup</button>
            <label class="icon-btn" style="padding:8px 18px;font-size:11px;cursor:pointer">
              Import Backup
              <input type="file" accept=".json" onchange="uploadBackup(this.files[0])" style="display:none">
            </label>
            <select id="restore-mode" class="env-select" style="min-width:100px">
              <option value="merge">Merge</option>
              <option value="replace">Replace All</option>
            </select>
          </div>
          <div id="backup-status" style="margin-top:10px;font-size:11px;color:var(--txt3)"></div>
        </div>
      </div>
    </div>

    <!-- Monitor View -->
    <div id="view-monitor" style="display:none;flex-direction:column;overflow:hidden;flex:1;min-height:0">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">API Monitor</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Track uptime and performance of your APIs</p>
        </div>
        <button class="icon-btn accent" onclick="showCreateMonitorModal()" style="padding:6px 14px;font-size:11px">+ New Monitor</button>
      </div>
      <div style="flex:1;overflow-y:auto;padding:16px 20px">
        <div id="monitors-list" style="display:flex;flex-direction:column;gap:10px"></div>
      </div>
    </div>

  </main>
</div>

<div id="toast-container"></div>

<!-- ══ Save Request Modal ══ -->
<div class="modal-overlay" id="save-modal">
  <div class="modal">
    <div class="modal-header"><h3>Save Request</h3><button class="modal-close" onclick="closeModal('save-modal')">×</button></div>
    <div class="form-group"><label class="form-label">Request Name</label><input id="save-name" placeholder="My Request" type="text"></div>
    <div class="form-group"><label class="form-label">Collection</label>
      <select id="save-collection" onchange="loadFoldersForCollection(this.value)">
        <option value="">Select collection…</option>
      </select>
    </div>
    <div class="form-group"><label class="form-label">Folder (Optional)</label>
      <select id="save-folder"><option value="">No Folder (Top Level)</option></select>
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('save-modal')">Cancel</button>
      <button class="btn-primary" onclick="saveRequest()">Save Request</button>
    </div>
  </div>
</div>

<!-- ══ New Collection Modal ══ -->
<div class="modal-overlay" id="coll-modal">
  <div class="modal">
    <div class="modal-header"><h3>New Collection</h3><button class="modal-close" onclick="closeModal('coll-modal')">×</button></div>
    <div class="form-group"><label class="form-label">Collection Name</label>
      <input id="coll-name-input" placeholder="My API Collection" type="text" onkeydown="if(event.key==='Enter')createCollection()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('coll-modal')">Cancel</button>
      <button class="btn-primary" onclick="createCollection()">Create</button>
    </div>
  </div>
</div>

<!-- ══ Rename Collection Modal ══ -->
<div class="modal-overlay" id="rename-coll-modal">
  <div class="modal">
    <div class="modal-header"><h3>Rename Collection</h3><button class="modal-close" onclick="closeModal('rename-coll-modal')">×</button></div>
    <div class="form-group"><label class="form-label">New Name</label>
      <input id="rename-coll-input" type="text" onkeydown="if(event.key==='Enter')confirmRenameCollection()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('rename-coll-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmRenameCollection()">Rename</button>
    </div>
  </div>
</div>

<!-- ══ New Folder Modal ══ -->
<div class="modal-overlay" id="folder-modal">
  <div class="modal">
    <div class="modal-header"><h3>New Folder</h3><button class="modal-close" onclick="closeModal('folder-modal')">×</button></div>
    <div class="form-group"><label class="form-label">Folder Name</label>
      <input id="folder-name-input" placeholder="e.g. Auth Endpoints" type="text" onkeydown="if(event.key==='Enter')confirmNewFolder()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('folder-modal')">Cancel</button>
      <button class="btn-primary" type="button" onclick="confirmNewFolder()">Create Folder</button>
    </div>
  </div>
</div>

<!-- ══ Collection Variables Modal ══ -->
<div class="modal-overlay" id="coll-vars-modal">
  <div class="modal" style="width: 500px">
    <div class="modal-header"><h3>Collection Variables <span id="coll-vars-name" style="color:var(--txt3);font-size:12px;font-weight:400;margin-left:8px"></span></h3><button class="modal-close" onclick="closeModal('coll-vars-modal')">×</button></div>
    <div class="kv-wrap" style="max-height: 400px; overflow-y: auto;">
      <table class="kv-table">
        <thead><tr><th>Variable</th><th>Value</th><th style="width:36px"></th></tr></thead>
        <tbody id="coll-vars-body"></tbody>
      </table>
    </div>
    <button class="add-row-btn" onclick="addCollVarRow()">+ Add Variable</button>
    <div class="modal-actions" style="margin-top:20px">
      <button class="btn-secondary" onclick="closeModal('coll-vars-modal')">Cancel</button>
      <button class="btn-primary" onclick="saveCollectionVars()">Save Variables</button>
    </div>
  </div>
</div>

<!-- ══ Rename Folder Modal ══ -->
<div class="modal-overlay" id="rename-folder-modal">
  <div class="modal">
    <div class="modal-header"><h3>Rename Folder</h3><button class="modal-close" onclick="closeModal('rename-folder-modal')">×</button></div>
    <div class="form-group"><label class="form-label">New Name</label>
      <input id="rename-folder-input" type="text" onkeydown="if(event.key==='Enter')confirmRenameFolder()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('rename-folder-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmRenameFolder()">Rename</button>
    </div>
  </div>
</div>

<!-- ══ Quick Add Request Modal ══ -->
<div class="modal-overlay" id="quick-req-modal">
  <div class="modal">
    <div class="modal-header"><h3>New Request</h3><button class="modal-close" onclick="closeModal('quick-req-modal')">×</button></div>
    <div class="form-group"><label class="form-label">Request Name</label>
      <input id="quick-req-name" placeholder="e.g. Get Users" type="text" onkeydown="if(event.key==='Enter')confirmQuickAddReq()">
    </div>
    <div class="form-group"><label class="form-label">Method</label>
      <select id="quick-req-method">
        <option>GET</option><option>POST</option><option>PUT</option>
        <option>PATCH</option><option>DELETE</option><option>HEAD</option><option>OPTIONS</option>
      </select>
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('quick-req-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmQuickAddReq()">Create &amp; Open</button>
    </div>
  </div>
</div>

<!-- ══ Rename Request Modal ══ -->
<div class="modal-overlay" id="rename-req-modal">
  <div class="modal">
    <div class="modal-header"><h3>Rename Request</h3><button class="modal-close" onclick="closeModal('rename-req-modal')">×</button></div>
    <div class="form-group"><label class="form-label">New Name</label>
      <input id="rename-req-input" type="text" onkeydown="if(event.key==='Enter')confirmRenameRequest()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('rename-req-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmRenameRequest()">Rename</button>
    </div>
  </div>
</div>

<!-- ══ Import Modal ══ -->
<div class="modal-overlay" id="import-modal">
  <div class="modal">
    <div class="modal-header"><h3>Import Collection</h3><button class="modal-close" onclick="closeModal('import-modal')">×</button></div>
    <div class="import-drop" id="import-drop"
      onclick="document.getElementById('import-file').click()"
      ondragover="importDragOver(event)" ondragleave="importDragLeave(event)" ondrop="importDrop(event)">
      <div class="import-icon">📂</div>
      <div>Drop a <strong>Postman</strong> or <strong>RequestLab</strong> export <strong>.json</strong> here</div>
      <div style="margin-top:4px;font-size:11px;opacity:.6">Supports Postman Collection v2 / v2.1 — or click to browse</div>
    </div>
    <input type="file" id="import-file" accept=".json" style="display:none" onchange="importFile(this)">
    <div id="import-status" style="font-size:12px;color:var(--txt3);font-family:var(--mono);min-height:20px"></div>
    <div class="modal-actions"><button class="btn-secondary" onclick="closeModal('import-modal')">Cancel</button></div>
  </div>
</div>

<!-- ══ Save Before Close Modal ══ -->
<div class="modal-overlay" id="save-close-modal">
  <div class="modal">
    <div class="modal-header"><h3>Unsaved Changes</h3><button class="modal-close" onclick="closeModal('save-close-modal')">×</button></div>
    <p style="font-size:12px;color:var(--txt2);font-family:var(--mono);line-height:1.6;margin-bottom:4px">This request has unsaved changes. What would you like to do?</p>
    <div class="modal-actions" style="gap:8px">
      <button class="btn-secondary" onclick="discardAndClose()" style="color:var(--red);border-color:var(--red)">Don't Save</button>
      <button class="btn-secondary" onclick="closeModal('save-close-modal')">Cancel</button>
      <button class="btn-primary" onclick="saveAndClose()">Save & Close</button>
    </div>
  </div>
</div>

<!-- ══ Logout Confirm Modal ══ -->
<div class="modal-overlay" id="logout-confirm-modal">
  <div class="modal">
    <div class="modal-header"><h3>Unsaved Changes</h3><button class="modal-close" onclick="closeModal('logout-confirm-modal')">×</button></div>
    <p style="font-size:12px;color:var(--txt2);font-family:var(--mono);line-height:1.6;margin-bottom:4px">You have unsaved tabs. Logging out will discard these changes. What would you like to do?</p>
    <div class="modal-actions" style="gap:8px">
      <button class="btn-secondary" onclick="closeModal('logout-confirm-modal')">Cancel</button>
      <button class="btn-primary" onclick="closeModal('logout-confirm-modal'); doLogout();" style="background:var(--red);color:#fff;border-color:var(--red)">Discard & Log Out</button>
    </div>
  </div>
</div>

<!-- ══ Create Team Modal ══ -->
<div class="modal-overlay" id="create-team-modal">
  <div class="modal">
    <div class="modal-header"><h3>Create Team</h3><button class="modal-close" onclick="closeModal('create-team-modal')">&times;</button></div>
    <div style="padding:16px">
      <input id="new-team-name" class="sidebar-search" placeholder="Team name..." style="width:100%;margin-bottom:12px">
      <div class="modal-actions"><button class="btn-secondary" onclick="closeModal('create-team-modal')">Cancel</button><button class="btn-primary" onclick="createTeam()">Create</button></div>
    </div>
  </div>
</div>

<!-- ══ Team Details Modal ══ -->
<div class="modal-overlay" id="team-details-modal">
  <div class="modal" style="width:580px">
    <div class="modal-header"><h3 id="team-details-name">Team</h3><button class="modal-close" onclick="closeModal('team-details-modal')">&times;</button></div>
    <div style="padding:16px">
      <div style="margin-bottom:16px">
        <h4 style="font-size:12px;font-weight:700;margin-bottom:8px">Members</h4>
        <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
          <input id="invite-email" class="sidebar-search" placeholder="Enter email address..." style="flex:1;min-width:0;padding:9px 12px;font-size:12.5px">
          <select id="invite-role" class="env-select" style="width:95px;flex-shrink:0;padding:9px 10px;font-size:12.5px"><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="admin">Admin</option></select>
          <button class="icon-btn accent" style="padding:9px 14px;font-size:12.5px;white-space:nowrap;flex-shrink:0" onclick="inviteMember()">Invite</button>
        </div>
        <div id="team-members-list" style="max-height:200px;overflow-y:auto"></div>
      </div>
      <div>
        <h4 style="font-size:12px;font-weight:700;margin-bottom:8px">Shared Collections</h4>
        <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
          <select id="share-collection-select" class="env-select" style="flex:1;padding:9px 10px;font-size:12.5px"><option value="">Select collection...</option></select>
          <button class="icon-btn accent" style="padding:9px 14px;font-size:12.5px" onclick="shareCollection()">Share</button>
        </div>
        <div id="team-shared-collections" style="max-height:150px;overflow-y:auto"></div>
      </div>
    </div>
  </div>
</div>

<!-- ══ Create Test Suite Modal ══ -->
<div class="modal-overlay" id="create-suite-modal">
  <div class="modal" style="width:480px">
    <div class="modal-header"><h3>New Test Suite</h3><button class="modal-close" onclick="closeModal('create-suite-modal')">&times;</button></div>
    <div style="padding:16px">
      <input id="new-suite-name" class="sidebar-search" placeholder="Suite name..." style="width:100%;margin-bottom:12px">
      <h4 style="font-size:12px;font-weight:700;margin-bottom:8px">Select Collections</h4>
      <div id="suite-collections-picker" style="max-height:200px;overflow-y:auto;margin-bottom:12px"></div>
      <div class="modal-actions"><button class="btn-secondary" onclick="closeModal('create-suite-modal')">Cancel</button><button class="btn-primary" onclick="createTestSuite()">Create Suite</button></div>
    </div>
  </div>
</div>

<!-- ══ Code Generation Modal ══ -->
<div class="modal-overlay" id="code-gen-modal">
  <div class="modal" style="width:600px">
    <div class="modal-header"><h3>Code Generator</h3><button class="modal-close" onclick="closeModal('code-gen-modal')">&times;</button></div>
    <div style="padding:16px">
      <div style="display:flex;gap:8px;margin-bottom:12px">
        <select id="code-target" class="env-select" style="min-width:180px" onchange="generateCode()">
          <option value="curl">cURL</option><option value="python">Python (requests)</option>
          <option value="javascript_fetch">JavaScript (fetch)</option><option value="javascript_axios">JavaScript (axios)</option>
          <option value="php">PHP</option><option value="ruby">Ruby</option>
          <option value="go">Go</option><option value="java">Java (OkHttp)</option>
          <option value="csharp">C# (HttpClient)</option><option value="powershell">PowerShell</option>
        </select>
        <button class="icon-btn" onclick="copyGeneratedCode()" style="padding:6px 14px;font-size:11px">Copy</button>
      </div>
      <pre id="code-output" style="background:var(--bg0);padding:14px;border-radius:var(--radius-lg);font-size:12px;font-family:var(--mono);max-height:400px;overflow:auto;white-space:pre-wrap;color:var(--txt2);border:1px solid var(--border)"></pre>
    </div>
  </div>
</div>

<!-- ══ Create Monitor Modal ══ -->
<div class="modal-overlay" id="create-monitor-modal">
  <div class="modal" style="width:580px">
    <div class="modal-header"><h3 id="monitor-modal-title">New Monitor</h3><button class="modal-close" onclick="closeModal('create-monitor-modal')">&times;</button></div>
    <div style="padding:16px;display:flex;flex-direction:column;gap:10px">
      <input id="mon-name" class="sidebar-search" placeholder="Monitor name..." style="width:100%;padding:9px 12px;font-size:12.5px">
      <div style="display:flex;gap:8px;align-items:center">
        <select id="mon-method" class="env-select" style="width:100px;flex-shrink:0;padding:9px 10px;font-size:12.5px"><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option><option>PATCH</option></select>
        <input id="mon-url" class="sidebar-search" placeholder="https://api.example.com/health" style="flex:1;min-width:0;padding:9px 12px;font-size:12.5px">
      </div>
      <div style="display:flex;gap:8px">
        <div style="flex:1"><label style="font-size:11px;color:var(--txt3)">Expected Status</label><input id="mon-expected-status" class="sidebar-search" value="200" style="width:100%"></div>
        <div style="flex:1"><label style="font-size:11px;color:var(--txt3)">Interval (sec)</label><input id="mon-interval" class="sidebar-search" value="300" style="width:100%"></div>
      </div>
      <div><label style="font-size:11px;color:var(--txt3)">Expected Body Contains (optional)</label><input id="mon-expected-body" class="sidebar-search" placeholder="ok" style="width:100%"></div>
      <input type="hidden" id="mon-edit-id" value="">
      <div class="modal-actions"><button class="btn-secondary" onclick="closeModal('create-monitor-modal')">Cancel</button><button class="btn-primary" onclick="saveMonitor()">Save Monitor</button></div>
    </div>
  </div>
</div>

<!-- ══ Monitor Logs Modal ══ -->
<div class="modal-overlay" id="monitor-logs-modal">
  <div class="modal" style="width:600px">
    <div class="modal-header"><h3 id="monitor-logs-title">Monitor Logs</h3><button class="modal-close" onclick="closeModal('monitor-logs-modal')">&times;</button></div>
    <div style="padding:16px;max-height:400px;overflow-y:auto" id="monitor-logs-content"></div>
  </div>
</div>

<!-- ══ Help / Documentation Modal ══ -->
<div class="modal-overlay" id="help-modal">
  <div class="modal" style="width:780px;max-height:85vh;display:flex;flex-direction:column">
    <div class="modal-header"><h3>RequestLab Documentation</h3><button class="modal-close" onclick="closeModal('help-modal')">&times;</button></div>
    <div style="display:flex;flex:1;overflow:hidden">
      <div id="help-nav" style="width:180px;min-width:180px;border-right:1px solid var(--border);overflow-y:auto;padding:8px 0;background:var(--bg1)"></div>
      <div id="help-content" style="flex:1;overflow-y:auto;padding:20px 24px;font-size:13px;line-height:1.7;color:var(--txt2)"></div>
    </div>
  </div>
</div>

<div id="global-var-tooltip" class="var-tooltip"></div>

<script>
const ICONS = {
  filePlus: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>`,
  folderPlus: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><line x1="12" y1="11" x2="12" y2="17"></line><line x1="9" y1="14" x2="15" y2="14"></line></svg>`,
  download: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>`,
  edit: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>`,
  copy: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`,
  trash: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`,
  cross: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`,
  folder: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`,
  folderOpen: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><polyline points="10 13 14 13 14 17"></polyline></svg>`,
  eye: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`,
  eyeOff: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`
};

// ══════════════════════════════════════════════════════════
//  TAB SYSTEM
// ══════════════════════════════════════════════════════════
let tabCounter = 0;
function makeTabState(o={}) {
  return Object.assign({
    id:++tabCounter, name:'Untitled Request', savedReqId:null, dirty:false,
    protocol:'http', method:'GET', url:'', params:[], headers:[], cookies:[],
    bodyType:'none', bodyContent:'', bodyKV:[],
    authType:'none', authData:{}, response:null,
    preProcessor:'', postProcessor:'',
    isRequesting: false, abortController: null,
  }, o);
}
let tabs=[], activeTabIdx=-1;

function currentTab(){ return tabs[activeTabIdx]||null; }

function snapshotTab(){
  const t=currentTab(); if(!t) return;
  t.protocol=document.getElementById('protocol-select').value;
  t.method=document.getElementById('method-select').value;
  t.url=document.getElementById('url-input').value;
  t.params=getKVRows('params-body');
  t.headers=getKVRows('headers-body');
  t.cookies=getKVRows('cookies-body');
  t.bodyType=S.bodyType;
  t.bodyContent=['json','raw'].includes(S.bodyType)?document.getElementById('body-editor').value:'';
  if(S.bodyType==='graphql') t.bodyContent=JSON.stringify({query:document.getElementById('graphql-query').value,variables:document.getElementById('graphql-vars').value});
  t.bodyKV=['form','urlencoded'].includes(S.bodyType)?getFormRows():[];
  t.authType=document.getElementById('auth-type').value;
  t.authData=getAuthData();
  t.name=document.getElementById('req-name-text').textContent;
  t.preProcessor=document.getElementById('preproc-editor')?document.getElementById('preproc-editor').value:'';
  t.postProcessor=document.getElementById('postproc-editor')?document.getElementById('postproc-editor').value:'';
}

function restoreTab(t){
  document.getElementById('protocol-select').value=t.protocol||'http';
  updateProtocolUI();
  document.getElementById('method-select').value=t.method;
  document.getElementById('url-input').value=t.url;
  document.getElementById('req-name-text').textContent=t.name;
  updateMethodColor();
  document.getElementById('params-body').innerHTML='';
  if(t.params.length) t.params.forEach(p=>addKVRow('params',p.key,p.value,p.desc||'',p.enabled));
  else addKVRow('params');
  document.getElementById('headers-body').innerHTML='';
  if(t.headers.length) t.headers.forEach(h=>addKVRow('headers',h.key,h.value,h.desc||'',h.enabled));
  else addKVRow('headers');
  const cookiesBody=document.getElementById('cookies-body');
  if(cookiesBody){cookiesBody.innerHTML='';if(t.cookies&&t.cookies.length) t.cookies.forEach(c=>addKVRow('cookies',c.key,c.value,c.desc||'',c.enabled));else addKVRow('cookies');}
  S.bodyType=t.bodyType; setBodyType(t.bodyType);
  if(t.bodyType==='graphql'){
    try{ const j=JSON.parse(t.bodyContent||'{}'); document.getElementById('graphql-query').value=j.query||''; document.getElementById('graphql-vars').value=j.variables||''; }
    catch(e){ document.getElementById('graphql-query').value=''; document.getElementById('graphql-vars').value=''; }
  } else {
    document.getElementById('body-editor').value=t.bodyContent||'';
    if(typeof updateBodyHighlight === 'function') updateBodyHighlight();
  }
  document.getElementById('body-kv-body').innerHTML='';
  if(t.bodyKV&&t.bodyKV.length) t.bodyKV.forEach(r=>addFormRow(r.type||'text',r.key,r.value,r.fileName));
  document.getElementById('auth-type').value=t.authType;
  S.authType=t.authType; S.authData=t.authData||{};
  renderAuthFields();
  if(t.authType==='basic'){ setIV('auth-username',t.authData.username||''); setIV('auth-password',t.authData.password||''); }
  else if(t.authType==='bearer') setIV('auth-token',t.authData.token||'');
  else if(t.authType==='oauth2'){ setIV('auth-token',t.authData.token||''); setIV('auth-prefix',t.authData.prefix||'Bearer'); }
  else if(t.authType==='awsv4'){ setIV('auth-access-key',t.authData.access_key||''); setIV('auth-secret-key',t.authData.secret_key||''); setIV('auth-region',t.authData.region||'us-east-1'); setIV('auth-service',t.authData.service||'execute-api'); setIV('auth-session-token',t.authData.session_token||''); }
  else if(t.authType==='apikey'){ setIV('auth-key',t.authData.key||''); setIV('auth-value',t.authData.value||''); setIV('auth-location',t.authData.location||'header'); }
  setIV('preproc-editor',t.preProcessor||'');
  setIV('postproc-editor',t.postProcessor||'');
  if(t.response){ S.response=t.response; renderResponse(t.response); }
  else { S.response=null; document.getElementById('resp-empty').style.display='flex'; document.getElementById('resp-body-content').style.display='none'; document.getElementById('resp-status-wrap').style.display='none'; document.getElementById('resp-view-bar').style.display='none'; document.getElementById('resp-body-tree').style.display='none'; document.getElementById('resp-body-raw').style.display='none'; document.getElementById('resp-body-preview').style.display='none'; }
  updateTabBadge('params'); updateTabBadge('headers');
  const btn=document.getElementById('send-btn');
  const cancelBtn=document.getElementById('cancel-btn');
  if(t.isRequesting){btn.innerHTML='<span class="spinner"></span>'; btn.disabled=true; cancelBtn.style.display='block';}
  else{btn.innerHTML='Send'; btn.disabled=false; cancelBtn.style.display='none';}
}
function setIV(id,val){ const e=document.getElementById(id); if(e) e.value=val; }

function renderTabBar(){
  const bar=document.getElementById('req-tabs-bar');
  bar.querySelectorAll('.req-tab-pill').forEach(e=>e.remove());
  const nb=document.getElementById('new-tab-btn');
  tabs.forEach((t,i)=>{
    const pill=document.createElement('button');
    pill.className='req-tab-pill'+(i===activeTabIdx?' active':'')+(t.dirty?' unsaved':'');
    pill.dataset.idx=i;
    pill.innerHTML=`<span class="req-tab-method" style="color:${methodColor(t.method)}">${t.method}</span><span class="req-tab-name">${esc(t.name)}</span><button class="req-tab-close" onclick="closeTab(event,${i})">×</button>`;
    pill.addEventListener('click',e=>{ if(e.target.classList.contains('req-tab-close')) return; switchTab(i); });
    bar.insertBefore(pill,nb);
  });
  saveWorkspace();
}

function saveWorkspace() {
  const safeTabs = tabs.map(t => ({...t, response: null}));
  localStorage.setItem('requestlab_workspace', JSON.stringify({tabs: safeTabs, activeTabIdx}));
}

function methodColor(m){ return {GET:'#3dd68c',POST:'#f0883e',PUT:'#79c0ff',PATCH:'#d2a8ff',DELETE:'#f47067',HEAD:'#e3b341',OPTIONS:'#00d4ff'}[m]||'#cdd9e5'; }

function switchTab(idx){
  if(idx===activeTabIdx) return;
  if(activeTabIdx>=0) snapshotTab();
  activeTabIdx=idx; restoreTab(tabs[idx]); renderTabBar();
  document.querySelectorAll('.req-item').forEach(el=>el.classList.toggle('active',el.id==='ri-'+tabs[idx].savedReqId));
}

function newTab(o={}){
  if(activeTabIdx>=0) snapshotTab();
  const t=makeTabState(o); tabs.push(t);
  activeTabIdx=tabs.length-1; restoreTab(t); renderTabBar();
}

function closeTab(e,idx){
  closeTabSafe(e,idx);
}

function markTabDirty(){ const t=currentTab(); if(t&&!t.dirty){t.dirty=true;renderTabBar();} }

// ══════════════════════════════════════════════════════════
//  GLOBAL STATE & THEME
// ══════════════════════════════════════════════════════════
function toggleTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  document.documentElement.setAttribute('data-theme', isLight ? 'dark' : 'light');
  document.getElementById('theme-btn').textContent = isLight ? '☀️' : '🌙';
  localStorage.setItem('requestlab_theme', isLight ? 'dark' : 'light');
}

if(localStorage.getItem('requestlab_theme') === 'light') {
  document.documentElement.setAttribute('data-theme', 'light');
}

const S = {
  globalVars:{}, bodyType:'none', authType:'none', authData:{}, response:null,
  collections:[], history:[], environments:[],
  renameCollId:null, renameReqId:null, renameFolderId:null,
  newFolderCollId:null, newFolderParentId:null,
  quickAddCollId:null, quickAddFolderId:null,
  editCollVarsId:null,
  currentUser:null, pendingCloseIdx:null,
};

// ══════════════════════════════════════════════════════════
//  AUTH UI
// ══════════════════════════════════════════════════════════
function togglePw(btn) {
  const inp = btn.previousElementSibling;
  if (inp.type === 'password') {
    inp.type = 'text';
    btn.innerHTML = ICONS.eyeOff;
    btn.title = 'Hide password';
  } else {
    inp.type = 'password';
    btn.innerHTML = ICONS.eye;
    btn.title = 'Show password';
  }
}

function authTab(tab){
  document.getElementById('at-login').classList.toggle('active',tab==='login'||tab==='reset'||tab==='forgot');
  document.getElementById('at-register').classList.toggle('active',tab==='register');
  document.getElementById('login-form').style.display=tab==='login'?'flex':'none';
  document.getElementById('register-form').style.display=tab==='register'?'flex':'none';
  document.getElementById('forgot-form').style.display=tab==='forgot'?'flex':'none';
  document.getElementById('reset-form').style.display=tab==='reset'?'flex':'none';
  document.getElementById('auth-error').textContent='';
}

async function doLogin(e){
  e.preventDefault();
  const btn=document.getElementById('login-btn'); btn.disabled=true;
  document.getElementById('auth-error').textContent='';
  try {
    const res=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:document.getElementById('login-field').value,password:document.getElementById('login-pw').value})});
    const data=await res.json();
    if(data.error){ document.getElementById('auth-error').textContent=data.error; return; }
    S.currentUser=data.user;
    showApp();
  } finally { btn.disabled=false; }
}

async function doRegister(e){
  e.preventDefault();
  const btn=document.getElementById('reg-btn'); btn.disabled=true;
  document.getElementById('auth-error').textContent='';
  try {
    const res=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('reg-username').value,email:document.getElementById('reg-email').value,password:document.getElementById('reg-pw').value})});
    const data=await res.json();
    if(data.error){ document.getElementById('auth-error').textContent=data.error; return; }
    S.currentUser=data.user;
    showApp();
  } finally { btn.disabled=false; }
}

async function doForgotPassword(e){
  e.preventDefault();
  const btn=document.getElementById('forgot-btn'); btn.disabled=true;
  document.getElementById('auth-error').textContent='';
  try {
    const res=await fetch('/api/auth/forgot-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('forgot-email').value})});
    const data=await res.json();
    if(data.error){ document.getElementById('auth-error').textContent=data.error; return; }
    toast(data.message, 'success');
    authTab('login');
  } finally { btn.disabled=false; }
}

async function doResetPassword(e){
  e.preventDefault();
  const btn=document.getElementById('reset-btn'); btn.disabled=true;
  document.getElementById('auth-error').textContent='';
  try {
    const res=await fetch('/api/auth/reset-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('reset-token').value,new_password:document.getElementById('reset-pw').value})});
    const data=await res.json();
    if(data.error){ document.getElementById('auth-error').textContent=data.error; return; }
    toast('Password reset successfully! Please sign in.','success');
    // Clear URL query parameters
    window.history.replaceState({}, document.title, window.location.pathname);
    authTab('login');
  } finally { btn.disabled=false; }
}

function skipAuth(){
  S.currentUser=null;
  showApp();
}

function attemptLogout(){
  document.getElementById('user-dropdown').classList.remove('open');
  if(activeTabIdx >= 0) snapshotTab();
  if(tabs.some(t => t.dirty)) {
    openModal('logout-confirm-modal');
  } else {
    doLogout();
  }
}

async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  S.currentUser=null;
  // Clear open tabs so they don't leak to the next user
  tabs = [makeTabState()];
  activeTabIdx = 0;
  restoreTab(tabs[0]);
  renderTabBar();
  
  document.getElementById('app').style.display='none';
  document.getElementById('auth-gate').style.display='flex';
  document.getElementById('user-menu').style.display='none';
  document.getElementById('auth-error').textContent='';
  document.getElementById('login-field').value='';
  document.getElementById('login-pw').value='';
  authTab('login');
}

function toggleUserDropdown(){
  document.getElementById('user-dropdown').classList.toggle('open');
}

document.addEventListener('click',function(e){
  const dd=document.getElementById('user-dropdown');
  const btn=document.getElementById('user-avatar-btn');
  if(dd&&btn&&!dd.contains(e.target)&&!btn.contains(e.target)) dd.classList.remove('open');
});

function showApp(){
  document.getElementById('auth-gate').style.display='none';
  document.getElementById('app').style.display='grid';
  if(S.currentUser){
    document.getElementById('user-menu').style.display='flex';
    document.getElementById('user-avatar').textContent=S.currentUser.username.charAt(0).toUpperCase();
    document.getElementById('user-avatar').style.background=S.currentUser.avatar_color||'#00d4ff';
    document.getElementById('user-dd-name').textContent=S.currentUser.username;
    document.getElementById('user-dd-email').textContent=S.currentUser.email||'';
  } else {
    document.getElementById('user-menu').style.display='none';
  }
  loadCollections(); loadHistory(); loadEnvironments(); loadGlobals();
  S.mocks = [];
  
  // Initialize: hide all views except builder
  const envView = document.getElementById('view-environments');
  const mocksView = document.getElementById('view-mocks');
  const runnerView = document.getElementById('view-runner');
  
  if(envView) envView.style.display = 'none';
  if(mocksView) mocksView.style.display = 'none';
  if(runnerView) runnerView.style.display = 'none';
}

async function loadGlobals(){
  try {
    const res = await fetch('/api/globals');
    const data = await res.json();
    try {
      S.globalVars = typeof data.vars === 'string' ? JSON.parse(data.vars) : (data.vars || {});
    } catch(pe) { S.globalVars = {}; }
    if(!S.globalVars || typeof S.globalVars !== 'object') S.globalVars = {};
    renderEnvironmentsView();
  } catch(e) { console.error('loadGlobals error:', e); S.globalVars = {}; }
}

// ══════════════════════════════════════════════════════════
//  SAVE BEFORE CLOSE
// ══════════════════════════════════════════════════════════
function closeTabSafe(e,idx){
  e.stopPropagation();
  // If tab has unsaved changes, show confirmation
  if(idx===activeTabIdx) snapshotTab();
  const t=tabs[idx];
  if(t&&t.dirty){
    S.pendingCloseIdx=idx;
    openModal('save-close-modal');
    return;
  }
  doCloseTab(idx);
}

function doCloseTab(idx){
  if(tabs.length===1){ tabs[0]=makeTabState(); activeTabIdx=0; restoreTab(tabs[0]); renderTabBar(); return; }
  tabs.splice(idx,1);
  if(activeTabIdx>=tabs.length) activeTabIdx=tabs.length-1;
  else if(activeTabIdx>idx) activeTabIdx--;
  restoreTab(tabs[activeTabIdx]); renderTabBar();
}

async function saveAndClose(){
  const idx=S.pendingCloseIdx;
  closeModal('save-close-modal');
  if(idx==null) return;
  // Switch to the tab to save it
  if(idx!==activeTabIdx) switchTab(idx);
  await handleSave();
  doCloseTab(idx>=tabs.length?tabs.length-1:idx);
  S.pendingCloseIdx=null;
}

function discardAndClose(){
  const idx=S.pendingCloseIdx;
  closeModal('save-close-modal');
  if(idx==null) return;
  tabs[idx].dirty=false;
  doCloseTab(idx);
  S.pendingCloseIdx=null;
}

// Browser close/refresh warning
window.addEventListener('beforeunload',function(e){
  if(tabs.some(t=>t.dirty)){
    e.preventDefault();
    e.returnValue='You have unsaved changes. Are you sure you want to leave?';
  }
});

// ══════════════════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', async ()=>{
  if(localStorage.getItem('requestlab_theme') === 'light') document.getElementById('theme-btn').textContent = '🌙';
  try {
    const ws = JSON.parse(localStorage.getItem('requestlab_workspace'));
    if(ws && ws.tabs && ws.tabs.length) {
      tabs = ws.tabs.map(t => makeTabState(t));
      activeTabIdx = ws.activeTabIdx || 0;
      if (activeTabIdx >= tabs.length) activeTabIdx = 0;
    } else {
      tabs = [makeTabState()]; activeTabIdx=0;
    }
  } catch(e) {
    tabs = [makeTabState()]; activeTabIdx=0;
  }
  renderTabBar(); restoreTab(tabs[activeTabIdx]);
  setupResizeHandle(); updateMethodColor();
  // Check auth
  try {
    const res=await fetch('/api/auth/me');
    const data=await res.json();
    
    const params = new URLSearchParams(window.location.search);
    const resetToken = params.get('reset_token');

    if(data.authenticated){
      S.currentUser=data.user;
      showApp();
    } else if (resetToken) {
      document.getElementById('reset-token').value = resetToken;
      document.getElementById('auth-gate').style.display='flex';
      authTab('reset');
    } else {
      document.getElementById('auth-gate').style.display='flex';
    }
  } catch(e){
    document.getElementById('auth-gate').style.display='flex';
  }
});

// ══════════════════════════════════════════════════════════
//  TOAST
// ══════════════════════════════════════════════════════════
function toast(msg,type='info',dur=2800){
  const icons={success:'✓',error:'✕',info:'ℹ'};
  const el=document.createElement('div');
  el.className=`toast ${type}`;
  el.innerHTML=`<span style="font-weight:700">${icons[type]||''}</span>${msg}`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(()=>{ el.style.animation='toastOut .2s forwards'; setTimeout(()=>el.remove(),200); },dur);
}

// ══════════════════════════════════════════════════════════
//  VIEW / SIDEBAR SWITCHING
// ══════════════════════════════════════════════════════════
function switchView(v){
  const views = ['builder','environments','mocks','runner','testsuites','docs','monitor','settings'];
  document.querySelectorAll('.top-tab').forEach((t,i)=>t.classList.toggle('active',views[i]===v));
  // Hide all sub-views inside view-builder (never hide view-builder itself)
  const subViews = ['view-environments','view-mocks','view-runner','view-testsuites','view-docs','view-settings','view-monitor'];
  subViews.forEach(id => { const el = document.getElementById(id); if(el) el.style.display = 'none'; });
  const reqTabsBar = document.getElementById('req-tabs-bar');
  const reqPanel = document.getElementById('request-panel');
  if(reqTabsBar) reqTabsBar.style.display = 'none';
  if(reqPanel) reqPanel.style.display = 'none';
  if(v==='builder') {
    if(reqTabsBar) reqTabsBar.style.display = 'flex';
    if(reqPanel) reqPanel.style.display = 'flex';
  } else if(v==='environments') {
    document.getElementById('view-environments').style.display = 'flex';
    document.getElementById('envs-panel').style.display = 'block';
    loadEnvironments(); setTimeout(() => renderEnvironmentsView(), 100);
  } else if(v==='mocks') {
    document.getElementById('view-mocks').style.display = 'flex';
    loadMocks();
  } else if(v==='runner') {
    document.getElementById('view-runner').style.display = 'flex';
    loadRunnerCollections();
  } else if(v==='testsuites') {
    document.getElementById('view-testsuites').style.display = 'flex';
    loadTestSuites();
  } else if(v==='docs') {
    document.getElementById('view-docs').style.display = 'flex';
    loadDocsCollections();
  } else if(v==='settings') {
    document.getElementById('view-settings').style.display = 'flex';
    loadCITokens();
  } else if(v==='monitor') {
    document.getElementById('view-monitor').style.display = 'flex';
    loadMonitors();
  }
}

function sidebarTab(t){
  document.getElementById('st-collections').classList.toggle('active',t==='collections');
  document.getElementById('st-history').classList.toggle('active',t==='history');
  document.getElementById('st-teams').classList.toggle('active',t==='teams');
  document.getElementById('sp-collections').style.display=t==='collections'?'flex':'none';
  document.getElementById('sp-history').style.display=t==='history'?'flex':'none';
  document.getElementById('sp-teams').style.display=t==='teams'?'flex':'none';
  if(t==='history') loadHistory();
  if(t==='teams') loadTeams();
}

function reqTab(name){
  ['params','headers','body','auth','cookies','preproc','postproc'].forEach(t=>{
    document.getElementById('pane-'+t).classList.toggle('active',t===name);
    const e=document.getElementById('rt-'+t); if(e) e.classList.toggle('active',t===name);
  });
}

function respTab(name){
  ['body','headers','cookies'].forEach(t=>{
    document.getElementById('resp-'+t+'-pane').style.display=t===name?'block':'none';
    document.getElementById('rst-'+t).classList.toggle('active',t===name);
  });
}

function updateProtocolUI() {
  const protocol = document.getElementById('protocol-select').value;
  const isReqRes = ['http', 'grpc', 'soap'].includes(protocol);
  
  document.getElementById('http-panel').style.display = isReqRes ? 'flex' : 'none';
  document.getElementById('realtime-panel').style.display = isReqRes ? 'none' : 'flex';
  
  const sendBtn = document.getElementById('send-btn');
  const cancelBtn = document.getElementById('cancel-btn');
  const methodSelect = document.getElementById('method-select');
  
  if (isReqRes) {
    sendBtn.style.display = '';
    methodSelect.style.display = protocol === 'http' ? '' : 'none'; // hide method for grpc and soap
    document.getElementById('realtime-config-bar').style.display = 'none';
  } else {
    sendBtn.style.display = 'none';
    cancelBtn.style.display = 'none';
    methodSelect.style.display = 'none';
    // Show specific realtime config bars if needed
    const configBar = document.getElementById('realtime-config-bar');
    if (protocol === 'socketio') {
      configBar.innerHTML = `<input type="text" class="url-input" id="sio-event" placeholder="Event Name (e.g. 'message')" style="width:200px">`;
      configBar.style.display = 'flex';
    } else if (protocol === 'mqtt') {
      configBar.innerHTML = `<input type="text" class="url-input" id="mqtt-topic" placeholder="Topic (e.g. 'home/sensor')" style="width:200px">`;
      configBar.style.display = 'flex';
    } else {
      configBar.style.display = 'none';
    }
  }
}

// ══════════════════════════════════════════════════════════
//  KV ROWS
// ══════════════════════════════════════════════════════════
function addKVRow(type,key='',value='',desc='',enabled=true){
  const tbody=document.getElementById(type+'-body');
  const tr=document.createElement('tr');
  tr.innerHTML=`<td style="text-align:center"><input type="checkbox" class="kv-cb" ${enabled?'checked':''} onchange="updateTabBadge('${type}');markTabDirty()"></td>
    <td><input class="kv-input" placeholder="key" value="${esc(key)}" oninput="updateTabBadge('${type}');markTabDirty()"></td>
    <td><input class="kv-input" placeholder="value" value="${esc(value)}" oninput="markTabDirty()"></td>
    <td><input class="kv-input" placeholder="description" value="${esc(desc)}"></td>
    <td><button class="del-row-btn" onclick="this.closest('tr').remove();updateTabBadge('${type}');markTabDirty()">✕</button></td>`;
  tbody.appendChild(tr); updateTabBadge(type);
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function updateTabBadge(type){
  const map={params:'tc-params',headers:'tc-headers',cookies:'tc-cookies'};
  const bid=map[type]; if(!bid) return;
  const n=[...document.querySelectorAll('#'+type+'-body tr')].filter(r=>{
    const cb=r.querySelector('input[type=checkbox]');
    const inp=r.querySelectorAll('input:not([type=checkbox])')[0];
    return cb?.checked&&inp?.value.trim();
  }).length;
  const badge=document.getElementById(bid);
  badge.textContent=n; badge.classList.toggle('has',n>0);
}

function getKVRows(tbodyId){
  return [...document.querySelectorAll('#'+tbodyId+' tr')].map(tr=>{
    const inputs=tr.querySelectorAll('input:not([type=checkbox])');
    const cb=tr.querySelector('input[type=checkbox]');
    return {key:inputs[0]?.value||'',value:inputs[1]?.value||'',enabled:cb?.checked!==false};
  }).filter(r=>r.key.trim());
}

// ══════════════════════════════════════════════════════════
//  FORM-DATA ROWS
// ══════════════════════════════════════════════════════════
const formFileMap=new WeakMap();

function addFormRow(type='text',key='',value='',fileName=''){
  const tbody=document.getElementById('body-kv-body');
  const tr=document.createElement('tr');
  tr.innerHTML=`<td style="text-align:center"><input type="checkbox" class="kv-cb" checked onchange="markTabDirty()"></td>
    <td style="min-width:72px"><select class="form-type-select" onchange="onFormTypeChange(this)">
      <option value="text" ${type==='text'?'selected':''}>Text</option>
      <option value="file" ${type==='file'?'selected':''}>File</option>
    </select></td>
    <td><input class="kv-input" placeholder="key" value="${esc(key)}" oninput="markTabDirty()"></td>
    <td>${type==='file'?`<div class="file-cell-wrap"><label class="file-pick-btn">Choose File<input type="file" style="display:none" onchange="onFilePicked(this)"></label><span class="file-name-txt">${esc(fileName||'No file chosen')}</span></div>`:`<input class="kv-input" placeholder="value" value="${esc(value)}" oninput="markTabDirty()">`}</td>
    <td><button class="del-row-btn" onclick="this.closest('tr').remove();markTabDirty()" style="opacity:1">✕</button></td>`;
  tbody.appendChild(tr);
}

function onFormTypeChange(sel){
  const tr=sel.closest('tr'); const vc=tr.querySelector('td:nth-child(4)'); const type=sel.value;
  if(type==='file'){ vc.innerHTML=`<div class="file-cell-wrap"><label class="file-pick-btn">Choose File<input type="file" style="display:none" onchange="onFilePicked(this)"></label><span class="file-name-txt">No file chosen</span></div>`; formFileMap.delete(tr); }
  else { vc.innerHTML=`<input class="kv-input" placeholder="value" oninput="markTabDirty()">`; formFileMap.delete(tr); }
  markTabDirty();
}

function onFilePicked(input){
  const file=input.files[0]; if(!file) return;
  const tr=input.closest('tr'); formFileMap.set(tr,file);
  const ns=tr.querySelector('.file-name-txt'); if(ns) ns.textContent=file.name;
  markTabDirty();
}

function getFormRows(){
  return [...document.querySelectorAll('#body-kv-body tr')].map(tr=>{
    const typeEl=tr.querySelector('.form-type-select');
    const keyEl=tr.querySelectorAll('input:not([type=checkbox]):not([type=file])')[0];
    const type=typeEl?.value||'text'; const key=keyEl?.value||'';
    if(type==='file'){ const file=formFileMap.get(tr)||null; const ns=tr.querySelector('.file-name-txt'); return {type:'file',key,value:'',fileName:file?file.name:(ns?.textContent||''),fileObj:file}; }
    const valEl=tr.querySelector('td:nth-child(4) input.kv-input');
    return {type:'text',key,value:valEl?.value||''};
  }).filter(r=>r.key.trim());
}

// ══════════════════════════════════════════════════════════
//  BODY TYPE
// ══════════════════════════════════════════════════════════
function setBodyType(type){
  S.bodyType=type;
  document.querySelectorAll('.body-type-btn').forEach(b=>b.classList.toggle('active',b.textContent.trim()===type));
  document.getElementById('body-none-msg').style.display=type==='none'?'block':'none';
  const ew = document.getElementById('body-editor-wrap');
  if(ew) ew.style.display=['json','raw','soap','xml'].includes(type)?'block':'none';
  const gw = document.getElementById('body-graphql-wrap');
  if(gw) gw.style.display=type==='graphql'?'block':'none';
  document.getElementById('beautify-btn').style.display=['json','soap','xml'].includes(type)?'':'none';
  document.getElementById('body-kv-wrap').style.display=['form','urlencoded'].includes(type)?'block':'none';
  if(type==='json'&&!document.getElementById('body-editor').value.trim()){
    document.getElementById('body-editor').value='{\n  \n}';
    if(typeof updateBodyHighlight === 'function') updateBodyHighlight();
  } else if((type==='soap'||type==='xml')&&!document.getElementById('body-editor').value.trim()){
    document.getElementById('body-editor').value = type==='soap' ? '<?xml version="1.0" encoding="utf-8"?>\n<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">\n  <soap:Body>\n    \n  </soap:Body>\n</soap:Envelope>' : '<?xml version="1.0" encoding="utf-8"?>\n<root>\n  \n</root>';
    if(typeof updateBodyHighlight === 'function') updateBodyHighlight();
  }
}

// ══════════════════════════════════════════════════════════
//  AUTH
// ══════════════════════════════════════════════════════════
function renderAuthFields(){
  const t=document.getElementById('auth-type').value; S.authType=t;
  const c=document.getElementById('auth-fields'); c.innerHTML='';
  if(t==='basic') c.innerHTML=af('Username','auth-username',S.authData.username||'')+af('Password','auth-password',S.authData.password||'','password');
  else if(t==='bearer') c.innerHTML=af('Token','auth-token',S.authData.token||'','password');
  else if(t==='oauth2') c.innerHTML=af('Access Token','auth-token',S.authData.token||'','password')+af('Header Prefix','auth-prefix',S.authData.prefix||'Bearer');
  else if(t==='awsv4') c.innerHTML=af('Access Key','auth-access-key',S.authData.access_key||'')+af('Secret Key','auth-secret-key',S.authData.secret_key||'','password')+af('AWS Region','auth-region',S.authData.region||'us-east-1')+af('Service Name','auth-service',S.authData.service||'execute-api')+af('Session Token','auth-session-token',S.authData.session_token||'','password');
  else if(t==='apikey') c.innerHTML=af('Key Name','auth-key',S.authData.key||'X-API-Key')+af('Key Value','auth-value',S.authData.value||'','password')+`<div class="auth-field"><label>Add to</label><select class="auth-type-select" id="auth-location" style="margin-bottom:0"><option value="header" ${S.authData.location==='header'?'selected':''}>Header</option><option value="query" ${S.authData.location==='query'?'selected':''}>Query Param</option></select></div>`;
}
function af(label,id,val='',type='text'){ 
  let inp = `<input id="${id}" type="${type}" value="${esc(val)}" placeholder="${label}" oninput="markTabDirty()">`;
  if(type==='password') inp = `<div class="pw-wrap">${inp}<button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" title="Show password">${ICONS.eye}</button></div>`;
  return `<div class="auth-field"><label>${label}</label>${inp}</div>`; 
}
function getAuthData(){
  const t=document.getElementById('auth-type').value;
  if(t==='basic') return {username:gv('auth-username'),password:gv('auth-password')};
  if(t==='bearer') return {token:gv('auth-token')};
  if(t==='oauth2') return {token:gv('auth-token'),prefix:gv('auth-prefix')};
  if(t==='awsv4') return {access_key:gv('auth-access-key'),secret_key:gv('auth-secret-key'),region:gv('auth-region'),service:gv('auth-service'),session_token:gv('auth-session-token')};
  if(t==='apikey') return {key:gv('auth-key'),value:gv('auth-value'),location:gv('auth-location')};
  return {};
}
function gv(id){ return document.getElementById(id)?.value||''; }

function updateMethodColor(){
  const sel=document.getElementById('method-select');
  sel.style.color=methodColor(sel.value);
  const t=currentTab(); if(t){t.method=sel.value;renderTabBar();}
}

// ══════════════════════════════════════════════════════════
//  ENVIRONMENT VARIABLE SUBSTITUTION
// ══════════════════════════════════════════════════════════
function getActiveEnv(){ return S.environments.find(e=>e.active)||null; }
function getEnvVars(){
  let gVars = {};
  if (S.globalVars && typeof S.globalVars === 'object') gVars = S.globalVars;
  else if (typeof S.globalVars === 'string') { 
    try { 
      const parsed = JSON.parse(S.globalVars); 
      if(parsed && typeof parsed === 'object') gVars = parsed; 
    } catch(e) {} 
  }
  let cVars = {};
  const t = currentTab();
  if(t && t.savedReqId && S.collections) {
    const coll = S.collections.find(c => {
       const reqs = c.requests || [];
       const fReqs = (c.folders || []).flatMap(f => f.requests || []);
       return reqs.some(r => r.id === t.savedReqId) || fReqs.some(r => r.id === t.savedReqId);
    });
    if (coll && coll.vars) {
      try { cVars = typeof coll.vars === 'string' ? JSON.parse(coll.vars) : coll.vars; } catch(e){}
    }
  }
  let eVars = {};
  const env = getActiveEnv();
  if(env && env.vars) {
    try { 
      const parsed = typeof env.vars==='string'?JSON.parse(env.vars):env.vars; 
      if(parsed && typeof parsed === 'object') eVars = parsed;
    }catch(e){}
  }
  return Object.assign({}, gVars, cVars, eVars);
}
function substituteVars(str){
  if(typeof str!=='string') return str;
  const vars=getEnvVars(); if(!Object.keys(vars).length) return str;
  return str.replace(/\{\{(\w+)\}\}/g,(_,k)=>vars[k]!==undefined?vars[k]:`{{${k}}}`);
}
function substituteKVList(list){ return list.map(item=>({...item,key:substituteVars(item.key),value:substituteVars(item.value)})); }
function substituteAuthData(authType,authData){ const out={}; for(const [k,v] of Object.entries(authData)) out[k]=typeof v==='string'?substituteVars(v):v; return out; }

// ══════════════════════════════════════════════════════════
//  VARIABLE HIGHLIGHTING (Postman-style)
// ══════════════════════════════════════════════════════════
const VAR_RE=/\{\{(\w+)\}\}/g;

function highlightUrlVars(){
  const input=document.getElementById('url-input');
  const layer=document.getElementById('url-highlight-layer');
  if(!input||!layer) return;
  const val=input.value;
  const vars=getEnvVars();
  let html='';
  let last=0;
  let m;
  const re=new RegExp(VAR_RE.source,'g');
  while((m=re.exec(val))!==null){
    html+=escHtml(val.slice(last,m.index));
    const varName=m[1];
    const resolved=vars[varName];
    const cls=resolved!==undefined?'resolved':'unresolved';
    const ttVal=resolved!==undefined?`<span class="vt-key">${escHtml(varName)}</span><span class="vt-arrow">→</span><span class="vt-val">${escHtml(String(resolved))}</span>`:`<span class="vt-unresolved">undefined</span>`;
    html+=`<span class="var-badge ${cls}" style="pointer-events:auto" onmouseenter="showGlobalTooltip(this, '${escHtml(ttVal)}')" onmouseleave="hideGlobalTooltip()">{{${escHtml(varName)}}}</span>`;
    last=re.lastIndex;
  }
  html+=escHtml(val.slice(last));
  const hasVars=last>0;
  input.classList.toggle('has-vars',hasVars);
  if(hasVars){
    layer.innerHTML=html;
    layer.scrollLeft=input.scrollLeft;
    layer.style.display='';
  } else {
    layer.innerHTML='';
    layer.style.display='none';
  }
}

let tooltipTimer;
function showGlobalTooltip(el, htmlVal){
  clearTimeout(tooltipTimer);
  const tt=document.getElementById('global-var-tooltip');
  if(!tt) return;
  tt.innerHTML=htmlVal;
  tt.style.display='block';
  const rect=el.getBoundingClientRect();
  const ttRect=tt.getBoundingClientRect();
  tt.style.left=(rect.left+rect.width/2-ttRect.width/2)+'px';
  tt.style.top=(rect.top-ttRect.height-6)+'px';
}
function hideGlobalTooltip(){
  tooltipTimer=setTimeout(()=>{
    const tt=document.getElementById('global-var-tooltip');
    if(tt) tt.style.display='none';
  },50);
}

function syncHighlightScroll(){
  const input=document.getElementById('url-input');
  const layer=document.getElementById('url-highlight-layer');
  if(input&&layer) layer.scrollLeft=input.scrollLeft;
}

function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function scanInputVars(){
  const vars=getEnvVars();
  const hasVars=Object.keys(vars).length>0;
  // KV inputs in params and headers
  document.querySelectorAll('.kv-input').forEach(inp=>{
    const v=inp.value;
    if(VAR_RE.test(v)){ inp.classList.add('has-var'); inp.title=v.replace(new RegExp(VAR_RE.source,'g'),(_,k)=>vars[k]!==undefined?`{{${k}}} → ${vars[k]}`:`{{${k}}} → [undefined]`); }
    else { inp.classList.remove('has-var'); if(inp.title&&inp.title.includes('→')) inp.title=''; }
  });
  // Body editor
  const bodyEd=document.getElementById('body-editor');
  if(bodyEd){ if(VAR_RE.test(bodyEd.value)) bodyEd.classList.add('has-var'); else bodyEd.classList.remove('has-var'); }
  // Auth fields
  document.querySelectorAll('.auth-field input').forEach(inp=>{
    if(VAR_RE.test(inp.value)) inp.classList.add('has-var'); else inp.classList.remove('has-var');
  });
}

// Run variable scan periodically and on input
setInterval(scanInputVars,800);
setInterval(highlightUrlVars,800);

// ══════════════════════════════════════════════════════════
//  VARIABLE AUTOCOMPLETE
// ══════════════════════════════════════════════════════════
let acDropdown=null, acTarget=null, acItems=[], acIdx=-1;

function createACDropdown(){
  if(acDropdown) return acDropdown;
  acDropdown=document.createElement('div');
  acDropdown.className='var-autocomplete';
  acDropdown.id='var-autocomplete';
  document.body.appendChild(acDropdown);
  return acDropdown;
}

function showVarAutocomplete(input){
  const vars=getEnvVars();
  const keys=Object.keys(vars);
  if(!keys.length){ hideVarAutocomplete(); return; }
  const cursorPos=input.selectionStart;
  const textBefore=input.value.substring(0,cursorPos);
  const match=textBefore.match(/\{\{(\w*)$/);
  if(!match){ hideVarAutocomplete(); return; }
  const partial=match[1].toLowerCase();
  const filtered=keys.filter(k=>k.toLowerCase().startsWith(partial));
  if(!filtered.length){ hideVarAutocomplete(); return; }
  acTarget=input; acItems=filtered; acIdx=0;
  const dd=createACDropdown();
  dd.innerHTML=filtered.map((k,i)=>
    `<div class="var-ac-item${i===0?' selected':''}" data-key="${escHtml(k)}" onmousedown="pickACVar('${escHtml(k)}',event)">
      <span class="var-ac-key">{{${escHtml(k)}}}</span>
      <span class="var-ac-val">${escHtml(String(vars[k]))}</span>
    </div>`
  ).join('');
  // Position near cursor
  const rect=input.getBoundingClientRect();
  dd.style.left=rect.left+'px';
  dd.style.top=(rect.bottom+4)+'px';
  dd.style.minWidth=Math.min(rect.width,280)+'px';
  dd.style.display='block';
}

function hideVarAutocomplete(){
  if(acDropdown) acDropdown.style.display='none';
  acTarget=null; acItems=[]; acIdx=-1;
}

function pickACVar(key,evt){
  if(evt) evt.preventDefault();
  if(!acTarget) return;
  const cursorPos=acTarget.selectionStart;
  const val=acTarget.value;
  const before=val.substring(0,cursorPos);
  const after=val.substring(cursorPos);
  const match=before.match(/\{\{(\w*)$/);
  if(!match) return;
  const start=before.length-match[0].length;
  const newVal=val.substring(0,start)+'{{'+key+'}}'+after;
  acTarget.value=newVal;
  const newPos=start+key.length+4;
  acTarget.setSelectionRange(newPos,newPos);
  acTarget.focus();
  acTarget.dispatchEvent(new Event('input',{bubbles:true}));
  hideVarAutocomplete();
  highlightUrlVars();
}

function navAC(dir){
  if(!acItems.length) return;
  acIdx=Math.max(0,Math.min(acItems.length-1,acIdx+dir));
  acDropdown.querySelectorAll('.var-ac-item').forEach((el,i)=>el.classList.toggle('selected',i===acIdx));
  const sel=acDropdown.querySelector('.var-ac-item.selected');
  if(sel) sel.scrollIntoView({block:'nearest'});
}

// Global listeners for autocomplete
document.addEventListener('input',function(e){
  const t=e.target;
  if(t.tagName==='INPUT'&&(t.classList.contains('kv-input')||t.classList.contains('url-input')||t.closest('.auth-field'))){
    showVarAutocomplete(t);
  }
  if(t.tagName==='TEXTAREA'&&t.id==='body-editor'){
    showVarAutocomplete(t);
  }
},true);

document.addEventListener('keydown',function(e){
  if(!acDropdown||acDropdown.style.display==='none') return;
  if(e.key==='ArrowDown'){ e.preventDefault(); navAC(1); }
  else if(e.key==='ArrowUp'){ e.preventDefault(); navAC(-1); }
  else if(e.key==='Enter'||e.key==='Tab'){
    if(acItems.length&&acIdx>=0){ e.preventDefault(); pickACVar(acItems[acIdx]); }
  }
  else if(e.key==='Escape'){ hideVarAutocomplete(); }
},true);

document.addEventListener('click',function(e){
  if(acDropdown&&!acDropdown.contains(e.target)) hideVarAutocomplete();
},true);

// ══════════════════════════════════════════════════════════
//  JSON COMMENT STRIPPING
// ══════════════════════════════════════════════════════════
function stripJsonComments(str){
  let result='',i=0,inStr=false;
  while(i<str.length){
    if(inStr){if(str[i]==='\\'){result+=str[i]+(str[i+1]||'');i+=2;continue;}if(str[i]==='"')inStr=false;result+=str[i];i++;}
    else{if(str[i]==='"'){inStr=true;result+=str[i];i++;}
    else if(str[i]==='/'&&str[i+1]==='/'){while(i<str.length&&str[i]!=='\n')i++;}
    else if(str[i]==='/'&&str[i+1]==='*'){i+=2;while(i<str.length&&!(str[i]==='*'&&str[i+1]==='/'))i++;i+=2;}
    else{result+=str[i];i++;}}
  }
  return result;
}

// ══════════════════════════════════════════════════════════
//  JSON BEAUTIFY
// ══════════════════════════════════════════════════════════
function beautifyBody(){
  const editor=document.getElementById('body-editor');
  if(!editor) return;
  const val=editor.value.trim();
  if(!val){ toast('Body is empty','info'); return; }
  try {
    const stripped=stripJsonComments(val);
    const parsed=JSON.parse(stripped);
    editor.value=JSON.stringify(parsed,null,2);
    markTabDirty();
    if(typeof updateBodyHighlight === 'function') updateBodyHighlight();
    toast('JSON beautified','success');
  } catch(e){
    toast('Invalid JSON: '+e.message,'error');
  }
}

// ══════════════════════════════════════════════════════════
//  SEND REQUEST
// ══════════════════════════════════════════════════════════
async function sendRequest(){
  const rawUrl=document.getElementById('url-input').value.trim();
  if(!rawUrl){toast('Enter a URL first','error');return;}
  const url=substituteVars(rawUrl);
  const t=currentTab(); if(!t) return;
  if(t.isRequesting) return;
  t.isRequesting=true; t.abortController=new AbortController();
  const btn=document.getElementById('send-btn');
  const cancelBtn=document.getElementById('cancel-btn');
  if(t===currentTab()){ btn.innerHTML='<span class="spinner"></span>'; btn.disabled=true; cancelBtn.style.display='block'; }
  const bodyType=S.bodyType;
  const authType=document.getElementById('auth-type').value;
  const authData=substituteAuthData(authType,getAuthData());
  const params=substituteKVList(getKVRows('params-body'));
  const headers=substituteKVList(getKVRows('headers-body'));
  const cookies=substituteKVList(getKVRows('cookies-body'));
  const preProcessor=document.getElementById('preproc-editor')?document.getElementById('preproc-editor').value:'';
  const postProcessor=document.getElementById('postproc-editor')?document.getElementById('postproc-editor').value:'';
  try{
    let result;
    if(bodyType==='form'){
      const formRows=getFormRows();
      const payload={method:document.getElementById('method-select').value,url,params,headers,cookies,body_type:'form',body_content:'{}',auth_type:authType,auth_data:authData,pre_processor:preProcessor,post_processor:postProcessor};
      const fd = new FormData();
      const textFields = {};
      formRows.forEach(r => {
        if(r.type==='text') textFields[r.key] = substituteVars(r.value);
        else if(r.type==='file' && r.fileObj) fd.append('file_'+substituteVars(r.key), r.fileObj, r.fileObj.name);
      });
      payload.body_content = JSON.stringify(textFields);
      fd.append('payload', JSON.stringify(payload));
      const res=await fetch('/api/execute',{method:'POST',body:fd,signal:t.abortController.signal});
      result=await res.json();
    } else {
      let bodyContent='';
      if(['json','raw','soap','xml'].includes(bodyType)){
        let raw=document.getElementById('body-editor').value;
        if(bodyType==='json') raw=stripJsonComments(raw);
        bodyContent=substituteVars(raw);
      }
      else if(bodyType==='graphql'){
        const q=document.getElementById('graphql-query').value;
        const v=document.getElementById('graphql-vars').value;
        let varsObj={}; try{varsObj=JSON.parse(stripJsonComments(substituteVars(v))||'{}');}catch(e){}
        bodyContent=JSON.stringify({query:substituteVars(q),variables:varsObj});
      }
      else if(bodyType==='urlencoded'){ const kv=getFormRows(); const obj={}; kv.forEach(r=>{obj[substituteVars(r.key)]=substituteVars(r.value);}); bodyContent=JSON.stringify(obj); }
      const payload={method:document.getElementById('method-select').value,url,params,headers,cookies,body_type:bodyType,body_content:bodyContent,auth_type:authType,auth_data:authData,protocol:t.protocol,pre_processor:preProcessor,post_processor:postProcessor};
      const res=await fetch('/api/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:t.abortController.signal});
      result=await res.json();
    }
    if(t) t.response=result;
    if(t===currentTab()){ S.response=result; renderResponse(result); }
    loadHistory();
  }catch(e){
    if(e.name==='AbortError'){if(t===currentTab())toast('Request cancelled','info');}
    else{if(t===currentTab()){renderError(e.message);toast('Request failed: '+e.message,'error');}}
  }finally{
    if(t){t.isRequesting=false; t.abortController=null;}
    if(t===currentTab()){btn.innerHTML='Send';btn.disabled=false;cancelBtn.style.display='none';}
  }
}

function cancelRequest(){
  const t=currentTab();
  if(t&&t.abortController){t.abortController.abort();t.abortController=null;t.isRequesting=false;}
}

function renderResponse(data){
  const empty=document.getElementById('resp-empty');
  const content=document.getElementById('resp-body-content');
  const rawPre=document.getElementById('resp-body-raw');
  const previewFrame=document.getElementById('resp-body-preview');
  const statusWrap=document.getElementById('resp-status-wrap');
  const viewBar=document.getElementById('resp-view-bar');
  if(data.error){renderError(data.error);return;}
  const sc=data.status_code;
  const cls=sc>=500?'s-5xx':sc>=400?'s-4xx':sc>=300?'s-3xx':sc>=200?'s-2xx':'s-err';
  document.getElementById('resp-status-badge').className='status-badge '+cls;
  document.getElementById('resp-status-badge').textContent=`${sc} ${data.status_text||''}`;
  document.getElementById('resp-meta').innerHTML=`<span>Time: <span class="val">${data.duration_ms}ms</span></span><span>Size: <span class="val">${formatSize(data.size_bytes)}</span></span>${data.redirects>0?`<span>Redirects: <span class="val">${data.redirects}</span></span>`:''}`;
  statusWrap.style.display='flex'; empty.style.display='none';
  viewBar.style.display='flex';
  // Store raw response for view toggling
  S._respPrettyHtml = (data.body_json!==null&&data.body_json!==undefined) ? syntaxHighlight(JSON.stringify(data.body_json,null,2)) : null;
  S._respRawText = data.body||'';
  S._respIsHtml = (data.headers||{})['Content-Type']?.includes('text/html') || (data.headers||{})['content-type']?.includes('text/html');
  // Default to Pretty view (or Tree if JSON)
  setRespView(data.body_json ? 'tree' : 'pretty');
  document.getElementById('resp-headers-tbody').innerHTML=Object.entries(data.headers||{}).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('');
  const cookies=data.cookies||{};
  document.getElementById('resp-cookies-tbody').innerHTML=Object.keys(cookies).length?Object.entries(cookies).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join(''):'<tr><td colspan="2" style="color:var(--txt3);font-size:12px;padding:12px">No cookies</td></tr>';
}

function setRespView(mode){
  S._respViewMode=mode;
  ['pretty','tree','raw','preview'].forEach(v=>{
    const el=document.getElementById('rv-'+v);
    if(el) el.classList.toggle('active',v===mode);
  });
  const content=document.getElementById('resp-body-content');
  const rawPre=document.getElementById('resp-body-raw');
  const previewFrame=document.getElementById('resp-body-preview');
  const treePane=document.getElementById('resp-body-tree');
  content.style.display='none'; rawPre.style.display='none'; previewFrame.style.display='none'; treePane.style.display='none';
  if(mode==='pretty'){
    content.style.display='block';
    if(S._respPrettyHtml) content.innerHTML=S._respPrettyHtml;
    else content.textContent=S._respRawText||'';
  } else if(mode==='raw'){
    rawPre.style.display='block';
    rawPre.textContent=S._respRawText||'';
  } else if(mode==='preview'){
    previewFrame.style.display='block';
    const rawBody=S._respRawText||'';
    if(S._respIsHtml){
      previewFrame.srcdoc=rawBody;
    } else if(S._respPrettyHtml){
      previewFrame.srcdoc=`<html><body style="background:#1a1b26;color:#c0caf5;font-family:monospace;font-size:13px;padding:16px;margin:0"><pre>${S._respPrettyHtml}</pre></body></html>`;
    } else {
      previewFrame.srcdoc=`<html><body style="background:#1a1b26;color:#c0caf5;font-family:monospace;font-size:13px;padding:16px;margin:0"><pre>${escHtml(rawBody)}</pre></body></html>`;
    }
    previewFrame.style.height=(previewFrame.closest('.resp-body')?.clientHeight-10||300)+'px';
  } else if(mode==='tree'){
    treePane.style.display='block';
    if(S.response && S.response.body_json) {
      treePane.innerHTML = renderJsonTree(S.response.body_json);
    } else {
      treePane.innerHTML = '<div style="color:var(--txt3);padding:10px">Not a valid JSON response.</div>';
    }
  }
}

function renderJsonTree(obj, isLast=true) {
  let ln = 1;
  function indent(n){ return n > 0 ? `<span style="display:inline-block;width:${n*1.6}ch"></span>` : ''; }

  function build(val, depth, isLastItem) {
    const cm = isLastItem ? '' : '<span class="jt-comma">,</span>';
    if (val === null) return `<span class="j-null">null</span>${cm}`;
    if (typeof val === 'boolean') return `<span class="j-bool">${val}</span>${cm}`;
    if (typeof val === 'number') return `<span class="j-num">${val}</span>${cm}`;
    if (typeof val === 'string') return `<span class="j-str">"${escHtml(val)}"</span>${cm}`;

    if (Array.isArray(val)) {
      if (val.length === 0) return `<span class="jt-bracket">[ ]</span>${cm}`;
      const id = 'jtn_' + Math.random().toString(36).substr(2,9);
      let res = `<span class="jt-toggle" onclick="toggleJsonNode('${id}')">\u25BC</span><span class="jt-bracket">[</span><span class="jt-ell" id="${id}_ell" style="display:none" onclick="toggleJsonNode('${id}')">${val.length} items</span><span class="jt-count" id="${id}_cnt" style="display:none"></span>`;
      res += `<div id="${id}_ch" class="jt-children">`;
      for(let i=0; i<val.length; i++){
        const itemHtml = build(val[i], depth+1, i===val.length-1);
        res += `<div class="jt-row"><span class="jt-ln">${++ln}</span>${indent(depth+1)}${itemHtml}</div>`;
      }
      res += `</div><div class="jt-row"><span class="jt-ln">${++ln}</span>${indent(depth)}<span class="jt-bracket">]</span>${cm}</div>`;
      return res;
    }

    if (typeof val === 'object') {
      const keys = Object.keys(val);
      if (keys.length === 0) return `<span class="jt-bracket">{ }</span>${cm}`;
      const id = 'jtn_' + Math.random().toString(36).substr(2,9);
      let res = `<span class="jt-toggle" onclick="toggleJsonNode('${id}')">\u25BC</span><span class="jt-bracket">{</span><span class="jt-ell" id="${id}_ell" style="display:none" onclick="toggleJsonNode('${id}')">${keys.length} keys</span><span class="jt-count" id="${id}_cnt" style="display:none"></span>`;
      res += `<div id="${id}_ch" class="jt-children">`;
      for(let i=0; i<keys.length; i++){
        const k = keys[i];
        const vHtml = build(val[k], depth+1, i===keys.length-1);
        res += `<div class="jt-row"><span class="jt-ln">${++ln}</span>${indent(depth+1)}<span class="j-key">"${escHtml(k)}"</span><span class="jt-colon">: </span>${vHtml}</div>`;
      }
      res += `</div><div class="jt-row"><span class="jt-ln">${++ln}</span>${indent(depth)}<span class="jt-bracket">}</span>${cm}</div>`;
      return res;
    }
    return escHtml(String(val));
  }

  const rootHtml = build(obj, 0, true);
  return `<div class="jt-row"><span class="jt-ln">1</span>${rootHtml}</div>`;
}

window.toggleJsonNode = function(id) {
  const ch = document.getElementById(id+'_ch');
  const ell = document.getElementById(id+'_ell');
  if(!ch || !ell) return;
  const parent = ch.parentElement;
  const tgl = parent.querySelector('.jt-toggle');
  if(ch.style.display==='none'){
    ch.style.display=''; ell.style.display='none';
    if(tgl){ tgl.classList.remove('closed'); tgl.textContent='\u25BC'; }
    // show closing bracket row (next sibling of children div)
    const closingRow = ch.nextElementSibling;
    if(closingRow) closingRow.style.display='';
  } else {
    ch.style.display='none'; ell.style.display='inline-block';
    if(tgl){ tgl.classList.add('closed'); tgl.textContent='\u25B6'; }
    // hide closing bracket row
    const closingRow = ch.nextElementSibling;
    if(closingRow) closingRow.style.display='none';
  }
};

function toggleResponsePanel(){
  const panel = document.getElementById('response-panel');
  const btn = document.getElementById('resp-collapse-btn');
  const collapsed = panel.classList.toggle('collapsed');
  if(btn){
    const svg = btn.querySelector('svg');
    if(svg) svg.style.transform = collapsed ? 'rotate(180deg)' : '';
  }
}

function renderError(msg){
  document.getElementById('resp-empty').style.display='none';
  const c=document.getElementById('resp-body-content');
  c.style.display='block'; c.innerHTML=`<span style="color:var(--red)">✕ ${esc(msg)}</span>`;
  document.getElementById('resp-body-raw').style.display='none';
  document.getElementById('resp-body-preview').style.display='none';
  document.getElementById('resp-view-bar').style.display='none';
  document.getElementById('resp-status-badge').className='status-badge s-err';
  document.getElementById('resp-status-badge').textContent='Error';
  document.getElementById('resp-status-wrap').style.display='flex';
  document.getElementById('resp-meta').innerHTML='';
}

function syntaxHighlight(json){
  return json.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,m=>{
      let cls='j-num';
      if(/^"/.test(m)) cls=/:\s*$/.test(m)?'j-key':'j-str';
      else if(/true|false/.test(m)) cls='j-bool';
      else if(/null/.test(m)) cls='j-null';
      return `<span class="${cls}">${m}</span>`;
    });
}

function updateBodyHighlight() {
  const editor = document.getElementById('body-editor');
  const layer = document.getElementById('body-highlight-layer');
  if(!editor || !layer) return;
  const errBar = document.getElementById('body-error-bar');
  const errMsg = document.getElementById('body-error-msg');
  if(S.bodyType === 'json') {
    layer.innerHTML = syntaxHighlight(editor.value) + '<br>';
    // Validate JSON
    const val = editor.value.trim();
    if(val) {
      try {
        JSON.parse(stripJsonComments(val));
        editor.classList.remove('body-editor-invalid');
        if(errBar) errBar.classList.remove('visible');
      } catch(e) {
        editor.classList.add('body-editor-invalid');
        if(errBar && errMsg) {
          const m = e.message.replace(/^JSON\.parse:\s*/,'');
          errMsg.textContent = 'JSON Error: ' + m;
          errBar.classList.add('visible');
        }
      }
    } else {
      editor.classList.remove('body-editor-invalid');
      if(errBar) errBar.classList.remove('visible');
    }
  } else {
    layer.textContent = editor.value + '\n';
    editor.classList.remove('body-editor-invalid');
    if(errBar) errBar.classList.remove('visible');
  }
}

function handleBodyKeydown(e) {
  if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    const start = e.target.selectionStart;
    const end = e.target.selectionEnd;
    e.target.value = e.target.value.substring(0, start) + "  " + e.target.value.substring(end);
    e.target.selectionStart = e.target.selectionEnd = start + 2;
    markTabDirty();
    updateBodyHighlight();
  }
  // Ctrl+/ — toggle line comment(s) on one or multiple selected lines
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    const ta = e.target;
    const val = ta.value;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const lineStart = val.lastIndexOf('\n', start - 1) + 1;
    let lineEnd = val.indexOf('\n', end);
    if (lineEnd === -1) lineEnd = val.length;
    const lines = val.slice(lineStart, lineEnd).split('\n');
    // If every non-empty line is already commented, uncomment; else comment all
    const allCommented = lines.every(l => l.trimStart() === '' || l.trimStart().startsWith('//'));
    const toggled = lines.map(l => {
      if (l.trimStart() === '') return l;
      if (allCommented) return l.replace(/^(\s*)\/\/ ?/, '$1');
      else return l.replace(/^(\s*)/, '$1// ');
    }).join('\n');
    ta.value = val.slice(0, lineStart) + toggled + val.slice(lineEnd);
    ta.selectionStart = lineStart;
    ta.selectionEnd = lineStart + toggled.length;
    markTabDirty();
    updateBodyHighlight();
  }
}

document.addEventListener('keydown', function(e) {
  const tag = document.activeElement?.tagName;
  const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

  if (e.ctrlKey || e.metaKey) {
    // Ctrl+S — Save
    if (e.key === 's' || e.key === 'S') {
      e.preventDefault();
      handleSave();
    }
    // Ctrl+Enter — Send request
    else if (e.key === 'Enter') {
      e.preventDefault();
      sendRequest();
    }
    // Ctrl+T — New tab
    else if (e.key === 't' || e.key === 'T') {
      e.preventDefault();
      newTab();
    }
    // Ctrl+W — Close current tab
    else if (e.key === 'w' || e.key === 'W') {
      e.preventDefault();
      if (activeTabIdx >= 0) closeTabSafe({ stopPropagation: () => {} }, activeTabIdx);
    }
    // Ctrl+Tab / Ctrl+Shift+Tab — Cycle tabs
    else if (e.key === 'Tab') {
      e.preventDefault();
      if (!tabs.length) return;
      const next = e.shiftKey
        ? (activeTabIdx - 1 + tabs.length) % tabs.length
        : (activeTabIdx + 1) % tabs.length;
      switchTab(next);
    }
    // Ctrl+B — Beautify JSON body
    else if ((e.key === 'b' || e.key === 'B') && !inInput) {
      e.preventDefault();
      if (S.bodyType === 'json') beautifyBody();
    }
    // Ctrl+K — Focus URL bar
    else if (e.key === 'k' || e.key === 'K') {
      e.preventDefault();
      const urlInput = document.getElementById('url-input');
      if (urlInput) { urlInput.focus(); urlInput.select(); }
    }
    // Ctrl+\ — Toggle Sidebar
    else if (e.key === '\\') {
      e.preventDefault();
      toggleSidebar();
    }
  }

  // Escape — Cancel in-flight request
  if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey) {
    if (currentAbortController) {
      e.preventDefault();
      cancelRequest();
    }
  }
});

function formatSize(b){ if(b<1024) return b+'B'; if(b<1048576) return (b/1024).toFixed(1)+'KB'; return (b/1048576).toFixed(1)+'MB'; }

function copyResponse(){
  const data=S.response; if(!data) return;
  const txt=data.body_json?JSON.stringify(data.body_json,null,2):(data.body||'');
  navigator.clipboard.writeText(txt).then(()=>{ const btn=document.getElementById('copy-resp-btn'); btn.textContent='Copied!'; setTimeout(()=>btn.textContent='Copy',1500); toast('Response copied to clipboard','success'); });
}

function downloadResponse(){
  const data=S.response; if(!data) return;
  const txt=data.body_json?JSON.stringify(data.body_json,null,2):(data.body||'');
  const ext=data.body_json?'json':'txt';
  const blob=new Blob([txt],{type:'text/plain'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`response.${ext}`; a.click();
}

// ══════════════════════════════════════════════════════════
//  COLLECTIONS — RENDER TREE
// ══════════════════════════════════════════════════════════
async function loadCollections(){
  const res=await fetch('/api/collections');
  S.collections=await res.json();
  renderCollections(S.collections);
  renderSaveModal(S.collections);
}

function filterCollections(v){ renderCollections(S.collections,v); }

function renderCollections(colls,filter=''){
  const tree=document.getElementById('collections-tree');
  const filtered=filter?colls.filter(c=>c.name.toLowerCase().includes(filter.toLowerCase())):colls;
  if(!filtered.length){
    tree.innerHTML=`<div style="color:var(--txt3);font-size:11px;padding:16px;text-align:center;font-family:var(--mono)">${filter?'No matches':'No collections yet'}</div>`;
    return;
  }
  tree.innerHTML=filtered.map(c=>renderCollectionNode(c)).join('');
}

function renderCollectionNode(c){
  const allNodes=[
    ...(c.folders||[]).map(f=>({...f,_type:'folder'})),
    ...(c.requests||[]).map(r=>({...r,_type:'request'}))
  ];
  const totalCount=c.total_requests||(c.requests||[]).length;
  return `<div class="coll-group" id="coll-${c.id}">
    <div class="coll-header" onclick="toggleColl(${c.id})" ondragover="nodeDragOver(event)" ondragleave="nodeDragLeave(event)" ondrop="nodeDrop(event, 'collection', ${c.id})">
      <span class="coll-arrow" id="ca-${c.id}">▶</span>
      <span class="coll-icon">${ICONS.folder}</span>
      <span class="coll-name" title="${esc(c.name)}">${esc(c.name)}</span>
      <span class="coll-count">${totalCount}</span>
      <span class="coll-actions" onclick="event.stopPropagation()">
        <button class="coll-act-btn accent-btn" title="New Request" onclick="quickAddReq(${c.id},null)">${ICONS.filePlus}</button>
        <button class="coll-act-btn accent-btn" title="New Folder"  onclick="openNewFolderModal(${c.id},null)">${ICONS.folderPlus}</button>
        <button class="coll-act-btn accent-btn" title="Variables"   onclick="openCollVarsModal(${c.id},'${esc(c.name)}')">{v}</button>
        <button class="coll-act-btn" title="Export"  onclick="exportCollection(${c.id},'${esc(c.name)}')">${ICONS.download}</button>
        <button class="coll-act-btn" title="Rename"  onclick="openRenameCollModal(${c.id},'${esc(c.name)}')">${ICONS.edit}</button>
        <button class="coll-act-btn danger" title="Delete" onclick="deleteCollection(${c.id})">${ICONS.trash}</button>
      </span>
    </div>
    <div class="req-list" id="rl-${c.id}">
      ${allNodes.length
        ? allNodes.map(node=>renderTreeNode(node,c.id)).join('')
        : '<div class="tree-empty">Empty collection</div>'}
    </div>
  </div>`;
}

function renderTreeNode(item,collId){
  return item._type==='folder'
    ? renderFolderNode(item,collId)
    : renderRequestNode(item);
}

function renderFolderNode(f,collId){
  const allChildren=[
    ...(f.folders||[]).map(sf=>({...sf,_type:'folder'})),
    ...(f.requests||[]).map(r=>({...r,_type:'request'}))
  ];
  const childCount=(f.folders||[]).length+(f.requests||[]).length;
  return `<div class="folder-node" id="fn-${f.id}">
    <div class="folder-hdr" onclick="toggleFolder(${f.id})" ondragover="nodeDragOver(event)" ondragleave="nodeDragLeave(event)" ondrop="nodeDrop(event, 'folder', ${f.id}, ${collId})">
      <span class="f-arrow" id="farr-${f.id}">▶</span>
      <span class="folder-ic" id="fic-${f.id}" style="display:inline-flex">${ICONS.folder}</span>
      <span class="folder-nm" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="fold-count">${childCount}</span>
      <span class="fold-acts" onclick="event.stopPropagation()">
        <button class="coll-act-btn accent-btn" title="New Request"   onclick="quickAddReq(${collId},${f.id})">${ICONS.filePlus}</button>
        <button class="coll-act-btn accent-btn" title="New Subfolder" onclick="openNewFolderModal(${collId},${f.id})">${ICONS.folderPlus}</button>
        <button class="coll-act-btn dup"    title="Duplicate" onclick="duplicateFolder(${f.id})">${ICONS.copy}</button>
        <button class="coll-act-btn"        title="Rename"    onclick="openRenameFolderModal(${f.id},'${esc(f.name)}')">${ICONS.edit}</button>
        <button class="coll-act-btn danger" title="Delete"    onclick="deleteFolder(${f.id})">${ICONS.trash}</button>
      </span>
    </div>
    <div class="folder-children" id="fch-${f.id}">
      ${allChildren.length
        ? allChildren.map(ch=>renderTreeNode(ch,collId)).join('')
        : '<div class="tree-empty">Empty folder</div>'}
    </div>
  </div>`;
}

function renderRequestNode(r){
  return `<div class="req-item" id="ri-${r.id}" onclick="loadRequestInTab(${r.id})" draggable="true" ondragstart="reqDragStart(event, ${r.id})" ondragend="this.classList.remove('dragging')">
    <span class="req-method m-${r.method}">${r.method}</span>
    <span class="req-name-text" title="${esc(r.name)}">${esc(r.name)}</span>
    <span class="req-item-actions">
      <button class="req-act-btn dup"    title="Duplicate" onclick="event.stopPropagation();duplicateRequest(${r.id})">${ICONS.copy}</button>
      <button class="req-act-btn"        title="Rename"    onclick="event.stopPropagation();openRenameReqModalById(${r.id},'${esc(r.name)}')">${ICONS.edit}</button>
      <button class="req-act-btn danger" title="Delete"    onclick="event.stopPropagation();deleteReq(${r.id})">${ICONS.trash}</button>
    </span>
  </div>`;
}

// ── Drag & Drop ────────────────────────────
function reqDragStart(e, id) {
  e.dataTransfer.setData('text/plain', id);
  e.dataTransfer.effectAllowed = 'move';
  e.target.classList.add('dragging');
}
function nodeDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
function nodeDragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}
async function nodeDrop(e, type, targetId, collId) {
  e.preventDefault(); e.stopPropagation();
  e.currentTarget.classList.remove('drag-over');
  const reqId = e.dataTransfer.getData('text/plain');
  if(!reqId) return;
  const newCollId = type === 'collection' ? targetId : collId;
  const newFolderId = type === 'folder' ? targetId : null;
  try {
    const res = await fetch(`/api/requests/${reqId}/move`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collection_id: newCollId, folder_id: newFolderId})
    });
    if(!res.ok) throw new Error('Move failed');
    await loadCollections();
    if(type === 'collection') ensureCollOpen(targetId);
    else if(type === 'folder') ensureFolderOpen(targetId);
  } catch(err) {
    toast('Failed to move request', 'error');
  }
}

// ── Toggle collection / folder ────────────────────────────
function toggleColl(id){
  const rl=document.getElementById('rl-'+id);
  const arrow=document.getElementById('ca-'+id);
  const open=rl.classList.toggle('open');
  arrow.classList.toggle('open',open);
}

function toggleFolder(id){
  const fc=document.getElementById('fch-'+id);
  const arrow=document.getElementById('farr-'+id);
  const icon=document.getElementById('fic-'+id);
  if(!fc) return;
  const open=fc.classList.toggle('open');
  arrow.classList.toggle('open',open);
  if(icon) icon.innerHTML=open?ICONS.folderOpen:ICONS.folder;
}

// ── Expand helpers (used after create to reveal new items) ──
function ensureCollOpen(collId){
  const rl=document.getElementById('rl-'+collId);
  const arrow=document.getElementById('ca-'+collId);
  if(rl&&!rl.classList.contains('open')){ rl.classList.add('open'); if(arrow) arrow.classList.add('open'); }
}
function ensureFolderOpen(folderId){
  const fc=document.getElementById('fch-'+folderId);
  const arrow=document.getElementById('farr-'+folderId);
  const icon=document.getElementById('fic-'+folderId);
  if(fc&&!fc.classList.contains('open')){ fc.classList.add('open'); if(arrow) arrow.classList.add('open'); if(icon) icon.innerHTML=ICONS.folderOpen; }
}

// ══════════════════════════════════════════════════════════
//  FOLDER CRUD
// ══════════════════════════════════════════════════════════
function openNewFolderModal(collId, parentFolderId){
  S.newFolderCollId=collId; S.newFolderParentId=parentFolderId||null;
  document.getElementById('folder-name-input').value='';
  document.getElementById('folder-modal').classList.add('open');
  setTimeout(()=>document.getElementById('folder-name-input').focus(),50);
}

async function confirmNewFolder(){
  const name=document.getElementById('folder-name-input').value.trim()||'New Folder';
  if(!S.newFolderCollId){toast('No collection selected','error');return;}
  try{
    const res=await fetch('/api/folders',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({collection_id:S.newFolderCollId,parent_folder_id:S.newFolderParentId,name})});
    const data=await res.json();
    if(!res.ok||data.error){toast(data.error||'Failed to create folder','error');return;}
    closeModal('folder-modal');
    await loadCollections();
    ensureCollOpen(S.newFolderCollId);
    if(S.newFolderParentId) ensureFolderOpen(S.newFolderParentId);
    toast(`Folder "${name}" created`,'success');
  }catch(e){toast('Error: '+e.message,'error');}
}

function openRenameFolderModal(id,name){
  S.renameFolderId=id;
  document.getElementById('rename-folder-input').value=name;
  document.getElementById('rename-folder-modal').classList.add('open');
  setTimeout(()=>document.getElementById('rename-folder-input').focus(),50);
}

async function confirmRenameFolder(){
  const name=document.getElementById('rename-folder-input').value.trim();
  if(!name) return;
  await fetch('/api/folders/'+S.renameFolderId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  closeModal('rename-folder-modal');
  await loadCollections();
  toast(`Renamed to "${name}"`,'success');
}

async function deleteFolder(id){
  if(!confirm('Delete this folder and all its contents?')) return;
  await fetch('/api/folders/'+id,{method:'DELETE'});
  await loadCollections();
  toast('Folder deleted','info');
}

async function duplicateFolder(id){
  await fetch('/api/folders/'+id+'/duplicate',{method:'POST'});
  await loadCollections();
  toast('Folder duplicated','success');
}

// ══════════════════════════════════════════════════════════
//  QUICK ADD REQUEST (from collection/folder context)
// ══════════════════════════════════════════════════════════
function quickAddReq(collId,folderId){
  S.quickAddCollId=collId; S.quickAddFolderId=folderId||null;
  document.getElementById('quick-req-name').value='';
  document.getElementById('quick-req-method').value='GET';
  document.getElementById('quick-req-modal').classList.add('open');
  setTimeout(()=>document.getElementById('quick-req-name').focus(),50);
}

async function confirmQuickAddReq(){
  const name=document.getElementById('quick-req-name').value.trim()||'New Request';
  const method=document.getElementById('quick-req-method').value;
  const res=await fetch('/api/requests',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({collection_id:S.quickAddCollId,folder_id:S.quickAddFolderId,name,method,url:'',
      params:[],headers:[],body_type:'none',body_content:'',auth_type:'none',auth_data:{}})});
  const data=await res.json();
  closeModal('quick-req-modal');
  await loadCollections();
  ensureCollOpen(S.quickAddCollId);
  if(S.quickAddFolderId) ensureFolderOpen(S.quickAddFolderId);
  await loadRequestInTab(data.id);
  toast(`Request "${name}" created`,'success');
}

// ══════════════════════════════════════════════════════════
//  DUPLICATE REQUEST
// ══════════════════════════════════════════════════════════
async function duplicateRequest(id){
  const res=await fetch('/api/requests/'+id+'/duplicate',{method:'POST'});
  const data=await res.json();
  await loadCollections();
  toast('Request duplicated','success');
}

// ══════════════════════════════════════════════════════════
//  COLLECTION CRUD
// ══════════════════════════════════════════════════════════
function openNewCollModal(){
  document.getElementById('coll-name-input').value='';
  document.getElementById('coll-modal').classList.add('open');
  setTimeout(()=>document.getElementById('coll-name-input').focus(),50);
}

async function createCollection(){
  const name=document.getElementById('coll-name-input').value.trim()||'New Collection';
  await fetch('/api/collections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  closeModal('coll-modal'); await loadCollections();
  toast(`Collection "${name}" created`,'success');
}

async function deleteCollection(id){
  if(!confirm('Delete this collection and all its contents?')) return;
  await fetch('/api/collections/'+id,{method:'DELETE'});
  await loadCollections(); toast('Collection deleted','info');
}

function openRenameCollModal(id,currentName){
  S.renameCollId=id;
  document.getElementById('rename-coll-input').value=currentName;
  document.getElementById('rename-coll-modal').classList.add('open');
  setTimeout(()=>document.getElementById('rename-coll-input').focus(),50);
}

async function confirmRenameCollection(){
  const name=document.getElementById('rename-coll-input').value.trim(); if(!name) return;
  await fetch('/api/collections/'+S.renameCollId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  closeModal('rename-coll-modal'); await loadCollections();
  toast(`Renamed to "${name}"`,'success');
}

function exportCollection(id,name){
  const a=document.createElement('a'); a.href='/api/collections/'+id+'/export'; a.download=name+'.json'; a.click();
  toast(`Exporting "${name}"…`,'info');
}

function openCollVarsModal(id,name){
  S.editCollVarsId=id;
  document.getElementById('coll-vars-name').textContent=name;
  const coll=S.collections.find(c=>c.id===id);
  const tbody=document.getElementById('coll-vars-body');
  tbody.innerHTML='';
  let vars={};
  if(coll&&coll.vars) { try{ vars=typeof coll.vars==='string'?JSON.parse(coll.vars):coll.vars; }catch(e){} }
  const entries=Object.entries(vars);
  if(!entries.length) addCollVarRow();
  else entries.forEach(([k,v])=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td><input class="kv-input" value="${esc(k)}" placeholder="variable_name"></td><td><input class="kv-input" value="${esc(String(v))}" placeholder="value"></td><td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td>`;
    tbody.appendChild(tr);
  });
  openModal('coll-vars-modal');
}

function addCollVarRow(){
  const tbody=document.getElementById('coll-vars-body');
  const tr=document.createElement('tr');
  tr.innerHTML=`<td><input class="kv-input" placeholder="variable_name"></td><td><input class="kv-input" placeholder="value"></td><td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td>`;
  tbody.appendChild(tr);
}

async function saveCollectionVars(){
  if(!S.editCollVarsId) return;
  const tbody=document.getElementById('coll-vars-body');
  const rows=[...tbody.querySelectorAll('tr')];
  const vars={};
  rows.forEach(tr=>{ const inputs=tr.querySelectorAll('input'); const key=inputs[0]?.value.trim(); if(key) vars[key]=inputs[1]?.value||''; });
  const res=await fetch('/api/collections/'+S.editCollVarsId+'/vars',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({vars})});
  if(!res.ok){toast('Failed to save collection variables','error');return;}
  closeModal('coll-vars-modal');
  await loadCollections();
  toast('Collection variables saved','success');
}

function openImportModal(){
  document.getElementById('import-status').textContent='';
  document.getElementById('import-modal').classList.add('open');
}
function importDragOver(e){e.preventDefault();document.getElementById('import-drop').classList.add('over');}
function importDragLeave(e){document.getElementById('import-drop').classList.remove('over');}
function importDrop(e){e.preventDefault();document.getElementById('import-drop').classList.remove('over');const f=e.dataTransfer.files[0];if(f)processImportFile(f);}
function importFile(input){if(input.files[0])processImportFile(input.files[0]);}

async function processImportFile(file){
  const status=document.getElementById('import-status');
  status.style.color='var(--txt3)'; status.textContent=`Reading ${file.name}…`;
  try{
    const text=await file.text(); const data=JSON.parse(text);
    const res=await fetch('/api/collections/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const result=await res.json();
    if(result.error) throw new Error(result.error);
    status.style.color='var(--green)'; status.textContent=`✓ Imported "${result.name}" successfully`;
    await loadCollections(); toast(`Imported "${result.name}"`,'success');
    setTimeout(()=>closeModal('import-modal'),1200);
  }catch(e){ status.style.color='var(--red)'; status.textContent=`✕ ${e.message}`; }
}

// ══════════════════════════════════════════════════════════
//  LOAD / SAVE REQUESTS
// ══════════════════════════════════════════════════════════
async function loadRequestInTab(id){
  const existingIdx=tabs.findIndex(t=>t.savedReqId===id);
  if(existingIdx>=0){switchTab(existingIdx);return;}
  const res=await fetch('/api/requests/'+id);
  const r=await res.json();
  const t=currentTab();
  const reuse=t&&!t.dirty&&!t.savedReqId&&!t.url;
  const newState=makeTabState({savedReqId:id,name:r.name,method:r.method,url:r.url,
    params:r.params||[],headers:r.headers||[],cookies:r.cookies||[],bodyType:r.body_type||'none',bodyContent:r.body_content||'',
    authType:r.auth_type||'none',authData:r.auth_data||{},dirty:false,
    preProcessor:r.pre_processor||'',postProcessor:r.post_processor||''});
  if(reuse){ tabs[activeTabIdx]=newState; restoreTab(newState); }
  else { if(activeTabIdx>=0) snapshotTab(); tabs.push(newState); activeTabIdx=tabs.length-1; restoreTab(newState); }
  renderTabBar();
  document.querySelectorAll('.req-item').forEach(el=>el.classList.toggle('active',el.id==='ri-'+id));
  switchView('builder');
}

async function deleteReq(id){
  await fetch('/api/requests/'+id,{method:'DELETE'});
  const idx=tabs.findIndex(t=>t.savedReqId===id);
  if(idx>=0) closeTab({stopPropagation:()=>{}},idx);
  await loadCollections(); toast('Request deleted','info');
}

function openRenameReqModal(){
  const t=currentTab();
  document.getElementById('rename-req-input').value=t?t.name:'';
  S.renameReqId=t?t.savedReqId:null;
  document.getElementById('rename-req-modal').classList.add('open');
  setTimeout(()=>document.getElementById('rename-req-input').focus(),50);
}

function openRenameReqModalById(id,name){
  S.renameReqId=id;
  document.getElementById('rename-req-input').value=name;
  document.getElementById('rename-req-modal').classList.add('open');
  setTimeout(()=>document.getElementById('rename-req-input').focus(),50);
}

async function confirmRenameRequest(){
  const name=document.getElementById('rename-req-input').value.trim(); if(!name) return;
  if(S.renameReqId){
    await fetch('/api/requests/'+S.renameReqId+'/rename',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    await loadCollections();
    const idx=tabs.findIndex(t=>t.savedReqId===S.renameReqId);
    if(idx>=0){tabs[idx].name=name;if(idx===activeTabIdx)document.getElementById('req-name-text').textContent=name;renderTabBar();}
  } else {
    const t=currentTab(); if(t){t.name=name;document.getElementById('req-name-text').textContent=name;renderTabBar();}
  }
  closeModal('rename-req-modal'); toast(`Renamed to "${name}"`,'success');
}

// ── Save modal with folder support ───────────────────────
function renderSaveModal(colls){
  const sel=document.getElementById('save-collection');
  const cur=sel.value;
  sel.innerHTML='<option value="">Select collection…</option>'+
    colls.map(c=>`<option value="${c.id}" ${c.id==cur?'selected':''}>${esc(c.name)}</option>`).join('');
  if(cur) loadFoldersForCollection(cur);
}

async function loadFoldersForCollection(collId){
  const sel=document.getElementById('save-folder');
  sel.innerHTML='<option value="">No Folder (Top Level)</option>';
  if(!collId) return;
  try{
    const res=await fetch(`/api/collections/${collId}/folders`);
    const folders=await res.json();
    folders.forEach(f=>{
      const opt=document.createElement('option');
      opt.value=f.id;
      opt.textContent='\u00a0'.repeat(f.depth*4)+f.name;
      sel.appendChild(opt);
    });
  }catch(e){}
}

async function handleSave(){
  const t=currentTab();
  if(t&&t.savedReqId){ await performSave(t.name,null,null); }
  else { openSaveModal(); }
}

function openSaveModal(){
  const t=currentTab();
  document.getElementById('save-name').value=t?t.name:'Untitled';
  document.getElementById('save-folder').innerHTML='<option value="">No Folder (Top Level)</option>';
  document.getElementById('save-modal').classList.add('open');
  setTimeout(()=>document.getElementById('save-name').focus(),50);
}

async function saveRequest(){
  const name=document.getElementById('save-name').value.trim()||'Untitled';
  const collId=document.getElementById('save-collection').value;
  const folderId=document.getElementById('save-folder').value||null;
  if(!collId){toast('Select a collection first','error');return;}
  closeModal('save-modal');
  await performSave(name,parseInt(collId),folderId?parseInt(folderId):null);
}

async function performSave(name,collId,folderId){
  const bodyType=S.bodyType;
  let bodyContent='';
  if(['json','raw'].includes(bodyType)) bodyContent=document.getElementById('body-editor').value;
  else if(['form','urlencoded'].includes(bodyType)){
    const kv=getFormRows();
    bodyContent=JSON.stringify(Object.fromEntries(kv.filter(r=>r.type!=='file').map(r=>[r.key,r.value])));
  }
  const payload={
    name:name, method:document.getElementById('method-select').value,
    url:document.getElementById('url-input').value,
    params:getKVRows('params-body'), headers:getKVRows('headers-body'),
    cookies:getKVRows('cookies-body'),
    body_type:bodyType, body_content:bodyContent,
    auth_type:document.getElementById('auth-type').value, auth_data:getAuthData(),
    pre_processor:document.getElementById('preproc-editor')?document.getElementById('preproc-editor').value:'',
    post_processor:document.getElementById('postproc-editor')?document.getElementById('postproc-editor').value:'',
  };
  if(collId!==null&&collId!==undefined) payload.collection_id=collId;
  if(folderId!==null&&folderId!==undefined) payload.folder_id=folderId;

  const t=currentTab();
  if(t&&t.savedReqId){
    await fetch('/api/requests/'+t.savedReqId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(t){t.name=name;t.dirty=false;}
    document.getElementById('req-name-text').textContent=name;
    renderTabBar(); await loadCollections(); toast('Request updated','success');
  } else {
    const res=await fetch('/api/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await res.json();
    if(t){t.savedReqId=data.id;t.name=name;t.dirty=false;}
    document.getElementById('req-name-text').textContent=name;
    renderTabBar(); await loadCollections(); toast('Request saved','success');
  }
}

function openModal(id){ document.getElementById(id).classList.add('open'); }
function closeModal(id){ document.getElementById(id).classList.remove('open'); }

// ══════════════════════════════════════════════════════════
//  HELP / DOCUMENTATION MODAL
// ══════════════════════════════════════════════════════════
const HELP_SECTIONS = {
  'getting-started': {title:'Getting Started', content:`
<h2>Getting Started</h2>
<p><b>RequestLab</b> is a lightweight, self-hosted API testing client built with Python + Flask. No heavy frameworks, complete privacy, and powerful features.</p>
<h3>Quick Start</h3>
<ol>
<li>Run <code>pip install -r requirements.txt</code></li>
<li>Run <code>python postman.py</code></li>
<li>Open <code>http://localhost:5000</code> in your browser</li>
<li>Register an account and start testing APIs</li>
</ol>
<h3>Navigation</h3>
<p>The top navigation bar gives you access to all major views:</p>
<ul>
<li><b>Builder</b> — The main request builder (default view)</li>
<li><b>Envs</b> — Environment variable management</li>
<li><b>Mocks</b> — Mock server configuration</li>
<li><b>Runner</b> — Collection runner for automated testing</li>
<li><b>Test Suites</b> — CI/CD test suite management</li>
<li><b>Docs</b> — API documentation generator</li>
<li><b>Monitor</b> — API uptime monitoring</li>
<li><b>Settings</b> — Backup/restore, CI tokens, and preferences</li>
</ul>
<p>The <b>sidebar</b> has three tabs: <b>Collections</b>, <b>History</b>, and <b>Teams</b>.</p>
`},
  'shortcuts': {title:'Keyboard Shortcuts', content:`
<h2>Keyboard Shortcuts</h2>
<table style="width:100%;border-collapse:collapse;font-size:12.5px">
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Ctrl + Enter</b></td><td>Send request (from URL bar)</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Enter</b></td><td>Send request (from URL bar)</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Ctrl + /</b></td><td>Toggle comment in body editor</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Ctrl + B</b></td><td>Beautify/format JSON body</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Ctrl + S</b></td><td>Save current request</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Tab</b></td><td>Indent in body/script editor</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:6px 0"><b>Escape</b></td><td>Close active modal</td></tr>
<tr><td style="padding:6px 0"><b>Ctrl + K</b></td><td>Focus URL bar</td></tr>
</table>
`},
  'request-builder': {title:'Request Builder', content:`
<h2>Request Builder</h2>
<p>The request builder is the core of RequestLab. It lets you construct and send HTTP requests.</p>
<h3>URL Bar</h3>
<ul>
<li>Enter any URL with optional <code>{{variable}}</code> placeholders for substitution</li>
<li>Select HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS</li>
<li>Choose protocol: HTTP or HTTPS</li>
<li>Press <b>Enter</b> or click <b>Send</b> to execute</li>
</ul>
<h3>Params Tab</h3>
<p>Add query parameters as key-value pairs. Toggle enable/disable per row. Badges show active count.</p>
<h3>Headers Tab</h3>
<p>Set custom HTTP headers. Common headers like Content-Type are auto-set based on body type.</p>
<h3>Cookies Tab</h3>
<p>Add per-request cookies as key-value pairs. These are sent with the request automatically.</p>
<h3>Body Tab</h3>
<p>Supported body types:</p>
<ul>
<li><b>None</b> — No body (GET, HEAD)</li>
<li><b>JSON</b> — JSON editor with syntax highlighting and comment stripping</li>
<li><b>GraphQL</b> — Separate query and variables editors</li>
<li><b>Raw</b> — Plain text body</li>
<li><b>Form Data</b> — Multipart with file upload support</li>
<li><b>x-www-form-urlencoded</b> — URL-encoded key-value pairs</li>
<li><b>SOAP</b> — XML envelope editor</li>
<li><b>XML</b> — Raw XML body</li>
</ul>
<h3>Auth Tab</h3>
<p>Authentication types:</p>
<ul>
<li><b>No Auth</b> — No authentication</li>
<li><b>Basic Auth</b> — Username and password</li>
<li><b>Bearer Token</b> — JWT or API token</li>
<li><b>OAuth 2.0</b> — Token with optional prefix</li>
<li><b>API Key</b> — Key-value in header or query</li>
<li><b>AWS Signature v4</b> — Access key, secret, region, service</li>
</ul>
<h3>Pre-Processor Tab</h3>
<p>Python script that runs <b>before</b> the request. Modify headers, params, or set environment variables.</p>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto">pm.environment.set('timestamp', str(int(__import__('time').time())))</pre>
<h3>Post-Processor Tab</h3>
<p>Python script that runs <b>after</b> the response. Validate, transform, or extract data.</p>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto">pm.test('Status is 200', lambda: pm.response.get('status_code') == 200)</pre>
`},
  'response': {title:'Response Viewer', content:`
<h2>Response Viewer</h2>
<p>After sending a request, the response panel shows:</p>
<ul>
<li><b>Status Badge</b> — Color-coded: green (2xx), yellow (3xx), orange (4xx), red (5xx)</li>
<li><b>Stats</b> — Response time (ms), body size, redirect count</li>
</ul>
<h3>View Modes</h3>
<ul>
<li><b>Pretty</b> — Syntax-highlighted, formatted JSON/XML</li>
<li><b>Tree</b> — Collapsible JSON tree structure</li>
<li><b>Raw</b> — Unformatted response body</li>
<li><b>Preview</b> — HTML rendering for web responses</li>
</ul>
<h3>Response Tabs</h3>
<ul>
<li><b>Body</b> — Response content in selected view mode</li>
<li><b>Headers</b> — Complete response header table</li>
<li><b>Tests</b> — Assertion results from pre/post processors and test scripts</li>
</ul>
`},
  'environments': {title:'Environments', content:`
<h2>Environments & Variables</h2>
<p>Manage environment-specific variables with real-time substitution.</p>
<h3>Variable Syntax</h3>
<p>Use double curly braces: <code>{{variable_name}}</code></p>
<p>Variables are substituted in: URL, params, headers, body, and auth fields.</p>
<h3>Variable Highlighting</h3>
<ul>
<li><span style="color:#3dd68c">Green badge</span> — Variable resolved successfully</li>
<li><span style="color:#f47067">Red badge</span> — Unresolved variable (not found in environment)</li>
<li><span style="color:#f0883e">Orange badge</span> — Variable with a value</li>
</ul>
<h3>Managing Environments</h3>
<ol>
<li>Click <b>Envs</b> in the top nav</li>
<li>Click <b>+ New Environment</b></li>
<li>Add key-value pairs</li>
<li>Set as <b>Active</b> to use it</li>
<li>Use the environment selector dropdown in the top-right</li>
</ol>
<h3>Global Variables</h3>
<p>Global variables are always active across all environments. Set them via scripts:</p>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto">pm.globals.set('api_key', 'my-secret-key')</pre>
`},
  'collections': {title:'Collections', content:`
<h2>Collections & Organization</h2>
<h3>Creating Collections</h3>
<ol>
<li>Click <b>+</b> in the sidebar toolbar</li>
<li>Enter a collection name</li>
<li>Use the context menu to add folders or requests</li>
</ol>
<h3>Folders</h3>
<p>Create nested folders to organize requests hierarchically. Drag and drop requests to reorder or move between folders.</p>
<h3>Saving Requests</h3>
<ol>
<li>Build your request in the builder</li>
<li>Click <b>Save</b> or press <b>Ctrl+S</b></li>
<li>Select a collection and optional folder</li>
<li>Click <b>Save</b></li>
</ol>
<h3>Importing</h3>
<p>Supported formats:</p>
<ul>
<li><b>Postman Collections</b> (v2.1)</li>
<li><b>OpenAPI 3.0</b> specifications</li>
<li><b>Swagger 2.0</b> specifications</li>
<li><b>RequestLab Backup</b> (JSON)</li>
</ul>
<p>Click the <b>⬆</b> button in the sidebar to import.</p>
<h3>Multi-Tab Interface</h3>
<p>Open multiple requests in tabs. Each tab maintains its own state including params, headers, body, and response. Tabs persist across page reloads via localStorage.</p>
`},
  'scripting': {title:'Scripting', content:`
<h2>Scripting Engine</h2>
<p>Write pre-request and test scripts in <b>Python</b> or <b>JavaScript</b>.</p>
<h3>Postman-Compatible API</h3>
<table style="width:100%;border-collapse:collapse;font-size:12.5px">
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.environment.get(key)</code></td><td>Get environment variable</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.environment.set(key, val)</code></td><td>Set environment variable</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.globals.get(key)</code></td><td>Get global variable</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.globals.set(key, val)</code></td><td>Set global variable</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.request</code></td><td>Current request object</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.response</code></td><td>Response object (post-request only)</td></tr>
<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 0"><code>pm.test(name, fn)</code></td><td>Define a test assertion</td></tr>
<tr><td style="padding:5px 0"><code>pm.expect(name, val)</code></td><td>Assertion builder</td></tr>
</table>
<h3>Assertion Methods</h3>
<ul>
<li><code>.to_equal(expected)</code> — Equality check</li>
<li><code>.to_be_truthy()</code> — Truthiness check</li>
<li><code>.to_contain(value)</code> — Contains check</li>
</ul>
<h3>Python Example</h3>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto"># Pre-request: set dynamic values
pm.environment.set('timestamp', str(int(__import__('time').time())))

# Test: validate response
status = pm.response.get('status_code')
pm.test('Status is 200', lambda: pm.expect('Status', status == 200))
pm.test('Has users', lambda: pm.expect('Users', 'users' in pm.response.get('body')))</pre>
<h3>JavaScript Example</h3>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto">pm.test("Status is 200", function() {
    pm.expect(pm.response.status).to.equal(200);
});
pm.test("Has data", function() {
    const body = pm.response.json();
    pm.expect(body).to.have.property('data');
});</pre>
`},
  'teams': {title:'Team Collaboration', content:`
<h2>Team Collaboration</h2>
<h3>Creating Teams</h3>
<ol>
<li>Click the <b>Teams</b> tab in the sidebar</li>
<li>Click <b>+ New Team</b></li>
<li>Enter a team name</li>
</ol>
<h3>Inviting Members</h3>
<ol>
<li>Open team details by clicking on a team</li>
<li>Enter the member's email address</li>
<li>Select a role: <b>Viewer</b>, <b>Editor</b>, or <b>Admin</b></li>
<li>Click <b>Invite</b></li>
</ol>
<h3>Roles</h3>
<ul>
<li><b>Viewer</b> — Read-only access to shared collections</li>
<li><b>Editor</b> — Can modify requests and collections</li>
<li><b>Admin</b> — Full control including member management</li>
</ul>
<h3>Sharing Collections</h3>
<ol>
<li>Open team details</li>
<li>Select a collection from the dropdown</li>
<li>Click <b>Share</b></li>
</ol>
<p>Shared collections are accessible to all team members based on their role.</p>
`},
  'cicd': {title:'CI/CD & Test Suites', content:`
<h2>CI/CD Integration & Test Suites</h2>
<h3>Test Suites</h3>
<p>Group collections into test suites for organized, repeatable testing.</p>
<ol>
<li>Navigate to <b>Test Suites</b> in the top nav</li>
<li>Click <b>New Test Suite</b></li>
<li>Name the suite and select collections to include</li>
<li>Click <b>Create</b></li>
</ol>
<h3>Running Test Suites</h3>
<p>Click <b>Run</b> on any test suite to execute all requests in the included collections. Results show pass/fail status with assertion details.</p>
<h3>CI Tokens</h3>
<p>Generate API tokens for automated CI/CD pipelines:</p>
<ol>
<li>Go to <b>Settings</b></li>
<li>Navigate to <b>CI Tokens</b> section</li>
<li>Click <b>Generate Token</b></li>
<li>Use the token in your CI/CD pipeline to trigger test runs</li>
</ol>
<h3>CI/CD Integration</h3>
<pre style="background:var(--bg1);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto"># Example: Run test suite via API
curl -X POST http://localhost:5000/api/ci/run-suite \\
  -H "Authorization: Bearer YOUR_CI_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"suite_id": 1}'</pre>
`},
  'api-docs': {title:'API Documentation', content:`
<h2>API Documentation Generator</h2>
<p>Generate professional documentation from your saved requests.</p>
<h3>Creating Documentation</h3>
<ol>
<li>Navigate to <b>Docs</b> in the top nav</li>
<li>Click <b>New Documentation</b></li>
<li>Select a collection to document</li>
<li>Add custom descriptions and details</li>
<li>Click <b>Generate</b></li>
</ol>
<h3>Export Formats</h3>
<ul>
<li><b>OpenAPI 3.0</b> — Standard YAML specification file</li>
<li><b>Markdown</b> — README-style documentation</li>
<li><b>HTML</b> — Standalone styled HTML page</li>
</ul>
<h3>Sharing</h3>
<p>Generated documentation can be shared via link or downloaded for external hosting.</p>
`},
  'code-gen': {title:'Code Generation', content:`
<h2>Code Generation</h2>
<p>Generate ready-to-use code snippets for any request.</p>
<h3>How to Use</h3>
<ol>
<li>Build your request in the builder</li>
<li>Click the <b>Code</b> button (or right-click)</li>
<li>Select a target language</li>
<li>Copy the generated code</li>
</ol>
<h3>Supported Languages</h3>
<ul>
<li><b>cURL</b> — Command-line HTTP</li>
<li><b>Python</b> — requests library</li>
<li><b>JavaScript</b> — fetch API</li>
<li><b>Node.js</b> — axios</li>
<li><b>Go</b> — net/http</li>
<li><b>Java</b> — HttpClient</li>
<li><b>C#</b> — HttpClient</li>
<li><b>PHP</b> — cURL</li>
<li><b>Ruby</b> — net/http</li>
<li><b>Rust</b> — reqwest</li>
</ul>
`},
  'monitor': {title:'API Monitor', content:`
<h2>API Monitor</h2>
<p>Track API uptime, performance, and reliability.</p>
<h3>Creating a Monitor</h3>
<ol>
<li>Navigate to <b>Monitor</b> in the top nav</li>
<li>Click <b>New Monitor</b></li>
<li>Configure: name, URL, HTTP method, expected status</li>
<li>Set check interval (in seconds)</li>
<li>Optionally specify expected body content</li>
<li>Click <b>Save Monitor</b></li>
</ol>
<h3>Monitor Dashboard</h3>
<p>Each monitor displays:</p>
<ul>
<li><b>Status</b> — Last check result (up/down)</li>
<li><b>Uptime %</b> — Percentage of successful checks</li>
<li><b>Last Check</b> — When the monitor last ran</li>
<li><b>Duration</b> — Response time of last check</li>
<li><b>Total Checks / Failures</b> — Lifetime statistics</li>
</ul>
<h3>Running Checks</h3>
<p>Click <b>Run Now</b> to manually trigger a check. View detailed logs by clicking <b>Logs</b>.</p>
<h3>Check Logs</h3>
<p>Each log entry includes: status, status code, duration, response body snippet, and any errors.</p>
`},
  'mock-server': {title:'Mock Server', content:`
<h2>Mock Server</h2>
<p>Create mock API endpoints for testing without a backend.</p>
<h3>Creating Mocks</h3>
<ol>
<li>Click <b>Mocks</b> in the top nav</li>
<li>Click <b>+ New Mock</b></li>
<li>Configure: path, method, status code, response body</li>
<li>Optionally add response delay</li>
<li>Save and enable the mock</li>
</ol>
<h3>Testing Mocks</h3>
<p>Base URL: <code>http://localhost:5000/mock/</code></p>
<p>Use the 🧪 button on any mock card to instantly test it.</p>
<h3>Features</h3>
<ul>
<li>Static JSON/text responses</li>
<li>Configurable HTTP methods</li>
<li>Custom status codes</li>
<li>Response delay simulation</li>
<li>Enable/disable toggles</li>
</ul>
`},
  'runner': {title:'Collection Runner', content:`
<h2>Collection Runner</h2>
<p>Automate API testing by running entire collections sequentially.</p>
<h3>Running Collections</h3>
<ol>
<li>Click <b>Runner</b> in the top nav</li>
<li>Select a collection from the dropdown</li>
<li>Configure iterations (number of times to run)</li>
<li>Set delay between requests (ms)</li>
<li>Click <b>Run Collection</b></li>
</ol>
<h3>Results</h3>
<p>View detailed results including:</p>
<ul>
<li>Pass/fail status per request</li>
<li>Assertion results</li>
<li>Execution time per request</li>
<li>Total run statistics</li>
</ul>
`},
  'backup': {title:'Backup & Restore', content:`
<h2>Backup & Restore</h2>
<p>Protect your data with full workspace backup and restore.</p>
<h3>Exporting Backup</h3>
<ol>
<li>Go to <b>Settings</b></li>
<li>Click <b>Export Backup</b></li>
<li>A JSON file will download containing all your data</li>
</ol>
<p>Backup includes: users, collections, folders, requests, environments, mocks, history, teams, monitors, test suites, API docs, and CI tokens.</p>
<h3>Restoring from Backup</h3>
<ol>
<li>Go to <b>Settings</b></li>
<li>Click <b>Import Backup</b> and select your JSON backup file</li>
<li>Data will be restored from the backup</li>
</ol>
<p><b>Note:</b> Importing a backup will merge/overwrite existing data. Use with caution.</p>
`},
  'misc': {title:'Tips & Troubleshooting', content:`
<h2>Tips & Troubleshooting</h2>
<h3>Tips</h3>
<ul>
<li>Use <code>{{variable}}</code> syntax anywhere for dynamic values</li>
<li>Right-click on requests in the sidebar for context menu options</li>
<li>Drag requests to reorder or move between folders</li>
<li>Use the workspace state persistence — your tabs survive page reloads</li>
<li>Toggle dark/light theme with the ☀️/🌙 button</li>
<li>Use Pre-Processor scripts to add dynamic headers or timestamps</li>
<li>Use Post-Processor scripts to validate responses and set variables for the next request</li>
</ul>
<h3>Themes</h3>
<p>Toggle between dark and light mode with the theme button in the top-right. Your preference is saved automatically.</p>
<h3>Responsive Design</h3>
<ul>
<li><b>Desktop (1024px+)</b> — Full sidebar, multi-column layout</li>
<li><b>Tablet (768–1024px)</b> — Compact sidebar, optimized spacing</li>
<li><b>Mobile Landscape (480–768px)</b> — Hidden sidebar, wrapped URL bar</li>
<li><b>Mobile Portrait (&lt;480px)</b> — Ultra-compact, touch-friendly</li>
</ul>
<h3>Data Storage</h3>
<p>All data is stored in <code>RequestLab.db</code> (SQLite) in the project root. To migrate, simply copy this file or use the backup/restore feature.</p>
`}
};

function openHelpModal(){
  const nav=document.getElementById('help-nav');
  const content=document.getElementById('help-content');
  const keys=Object.keys(HELP_SECTIONS);
  nav.innerHTML=keys.map(k=>`<div class="help-nav-item" data-key="${k}" onclick="showHelpSection('${k}')" style="padding:8px 14px;cursor:pointer;font-size:12px;color:var(--txt2);border-left:3px solid transparent;transition:all .15s">${HELP_SECTIONS[k].title}</div>`).join('');
  showHelpSection(keys[0]);
  openModal('help-modal');
}

function showHelpSection(key){
  const content=document.getElementById('help-content');
  content.innerHTML=HELP_SECTIONS[key].content;
  document.querySelectorAll('.help-nav-item').forEach(el=>{
    const active=el.dataset.key===key;
    el.style.borderLeftColor=active?'var(--acc)':'transparent';
    el.style.color=active?'var(--acc)':'var(--txt2)';
    el.style.background=active?'var(--bg2)':'transparent';
    el.style.fontWeight=active?'600':'400';
  });
}

// ══════════════════════════════════════════════════════════
//  HISTORY
// ══════════════════════════════════════════════════════════
async function loadHistory(){
  const res=await fetch('/api/history?limit=80');
  S.history=await res.json(); renderHistory();
}

function renderHistory(){
  const list=document.getElementById('history-list');
  if(!S.history.length){list.innerHTML='<div style="color:var(--txt3);font-size:11px;padding:16px;text-align:center;font-family:var(--mono)">No history yet</div>';return;}
  list.innerHTML=S.history.map(h=>{
    const sc=h.status_code;
    const cls=sc>=500?'s-5xx':sc>=400?'s-4xx':sc>=300?'s-3xx':sc>=200?'s-2xx':'s-err';
    return `<div class="hist-item" onclick="loadHistoryItem(${h.id})"><span class="req-method m-${h.method}" style="min-width:40px">${h.method}</span><span class="hist-url">${esc(h.url)}</span><span class="status-badge ${cls}" style="font-size:9.5px;padding:2px 6px">${h.status_code||'ERR'}</span></div>`;
  }).join('');
}

async function loadHistoryItem(id){
  const res=await fetch('/api/history/'+id);
  const data=await res.json(); const req=data.request_data||{};
  newTab({method:req.method||'GET',url:req.url||'',name:req.url||'From History'});
  switchView('builder');
}

async function clearHistory(){
  if(!confirm('Clear all history?')) return;
  await fetch('/api/history',{method:'DELETE'});
  await loadHistory(); toast('History cleared','info');
}

// ══════════════════════════════════════════════════════════
//  ENVIRONMENTS
// ══════════════════════════════════════════════════════════
async function loadEnvironments(){
  try {
    const res=await fetch('/api/environments');
    S.environments=await res.json();
    renderEnvironmentsView(); renderEnvSelector();
  } catch(e) { console.error('loadEnvironments error:', e); }
}

function renderEnvSelector(){
  const sel=document.getElementById('env-selector');
  sel.innerHTML='<option value="">No Environment</option>'+
    S.environments.map(e=>`<option value="${e.id}" ${e.active?'selected':''}>${esc(e.name)}</option>`).join('');
}

function selectEnv(id){ if(id) activateEnv(parseInt(id)); }

function renderEnvironmentsView(){
  try {
  const panel=document.getElementById('envs-panel');
  if(!panel) { console.error('envs-panel not found'); return; }
  
  let gVars = {};
  try {
    if (S.globalVars && typeof S.globalVars === 'object') gVars = S.globalVars;
    else if (typeof S.globalVars === 'string') { 
      const parsed = JSON.parse(S.globalVars); 
      if(parsed && typeof parsed === 'object') gVars = parsed; 
    }
  } catch(e) { gVars = {}; }
  
  const gEntries = Object.entries(gVars);
  let gHtml = `<div class="env-card" id="env-card-global">
      <div class="env-card-header" onclick="toggleCard('global')" style="cursor:pointer">
        <span style="font-weight:600;color:var(--acc)">Global Variables</span>
        <span class="env-active-badge">Always Active</span>
        <span class="collapse-icon" id="collapse-icon-global" style="margin-left:auto;font-size:12px">▼</span>
      </div>
      <div class="env-card-content" id="env-content-global">
      <div class="kv-wrap" style="margin-bottom:10px"><table class="kv-table">
        <thead><tr><th>Variable</th><th>Value</th><th style="width:36px"></th></tr></thead>
        <tbody id="env-vars-global">
          ${gEntries.length ? gEntries.map(([k,v])=>`<tr><td><input class="kv-input" value="${esc(k)}" placeholder="variable_name"></td><td><input class="kv-input" value="${esc(String(v))}" placeholder="value"></td><td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td></tr>`).join('') : '<tr><td colspan="3" style="text-align:center;padding:20px;color:var(--txt3);font-size:12px">No global variables yet. Click "+ Add Variable" to get started.</td></tr>'}
        </tbody>
      </table></div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="add-row-btn" style="margin-top:0" onclick="addEnvVar('global')">+ Add Variable</button>
        <button class="env-save-btn" onclick="saveGlobals()">Save Globals</button>
      </div>
      </div>
    </div>`;

  let html = gHtml;
  if(S.environments && S.environments.length){
    html += S.environments.map(env=>{
      let vars={}; 
      try {
        const parsed = typeof env.vars==='string'?JSON.parse(env.vars):env.vars;
        if(parsed && typeof parsed === 'object') vars = parsed;
      } catch(e){}
      const entries=Object.entries(vars);
      return `<div class="env-card" id="env-card-${env.id}">
      <div class="env-card-header" onclick="toggleCard(${env.id})" style="cursor:pointer">
        <span class="collapse-icon" id="collapse-icon-${env.id}" style="margin-left:auto;font-size:12px">▼</span>
        <input class="env-name-input" value="${esc(env.name)}" id="en-${env.id}" placeholder="Environment name">
        ${env.active?'<span class="env-active-badge">● Active</span>':`<button class="activate-btn" onclick="activateEnv(${env.id})">Set Active</button>`}
        <button class="icon-btn" onclick="deleteEnv(${env.id})" style="margin-left:auto" title="Delete">🗑</button>
      </div>
      <div class="env-card-content" id="env-content-${env.id}">
      <div class="kv-wrap" style="margin-bottom:10px"><table class="kv-table">
        <thead><tr><th>Variable</th><th>Initial Value</th><th style="width:36px"></th></tr></thead>
        <tbody id="env-vars-${env.id}">
          ${entries.length ? entries.map(([k,v])=>`<tr><td><input class="kv-input" value="${esc(k)}" placeholder="variable_name"></td><td><input class="kv-input" value="${esc(String(v))}" placeholder="value"></td><td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td></tr>`).join('') : '<tr><td colspan="3" style="text-align:center;padding:20px;color:var(--txt3);font-size:12px">No variables yet. Click "+ Add Variable" to get started.</td></tr>'}
        </tbody>
      </table></div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="add-row-btn" style="margin-top:0" onclick="addEnvVar(${env.id})">+ Add Variable</button>
        <button class="env-save-btn" onclick="saveEnv(${env.id})">Save Changes</button>
      </div>
      </div>
    </div>`;
    }).join('');
  } else {
    html += '<div style="text-align:center;padding:60px 20px;color:var(--txt3)"><p style="font-size:14px;margin:0">No environments created yet</p><p style="font-size:12px;margin-top:8px">Click "+ New Environment" to create your first environment</p></div>';
  }
  panel.innerHTML = html;
  } catch(e) { console.error('renderEnvironmentsView error:', e); console.error(e.stack); }
}

function addEnvVar(id){
  const tbody=document.getElementById('env-vars-'+id);
  const tr=document.createElement('tr');
  tr.innerHTML=`<td><input class="kv-input" placeholder="variable_name"></td><td><input class="kv-input" placeholder="value"></td><td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td>`;
  tbody.appendChild(tr);
}

async function saveEnv(id){
  const nameEl=document.getElementById('en-'+id);
  const name=(nameEl?nameEl.value.trim():'')||'Environment';
  const rows=[...document.querySelectorAll('#env-vars-'+id+' tr')];
  const vars={};
  rows.forEach(tr=>{ const inputs=tr.querySelectorAll('input'); const key=inputs[0]?.value.trim(); if(key) vars[key]=inputs[1]?.value||''; });
  const res=await fetch('/api/environments/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,vars})});
  if(!res.ok){toast('Failed to save environment','error');return;}
  const envIdx=S.environments.findIndex(e=>e.id===id);
  if(envIdx>=0){S.environments[envIdx].name=name;S.environments[envIdx].vars=vars;}
  renderEnvSelector(); toast('Environment saved','success');
}

async function saveGlobals(){
  const rows=[...document.querySelectorAll('#env-vars-global tr')];
  const vars={};
  rows.forEach(tr=>{ const inputs=tr.querySelectorAll('input'); const key=inputs[0]?.value.trim(); if(key) vars[key]=inputs[1]?.value||''; });
  const res=await fetch('/api/globals',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({vars})});
  if(!res.ok){toast('Failed to save global variables','error');return;}
  S.globalVars=vars;
  toast('Global variables saved','success');
}

async function createEnvironment(){
  await fetch('/api/environments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'New Environment',vars:{}})});
  await loadEnvironments();
}

async function activateEnv(id){
  await fetch('/api/environments/'+id+'/activate',{method:'POST'});
  await loadEnvironments(); toast('Environment activated','success');
}

async function deleteEnv(id){
  if(!confirm('Delete this environment?')) return;
  await fetch('/api/environments/'+id,{method:'DELETE'});
  await loadEnvironments(); toast('Environment deleted','info');
}

function toggleCard(id){
  const content = document.getElementById(`env-content-${id}`);
  const icon = document.getElementById(`collapse-icon-${id}`);
  if(content && icon){
    if(content.style.display === 'none'){
      content.style.display = 'block';
      icon.textContent = '▼';
    } else {
      content.style.display = 'none';
      icon.textContent = '▶';
    }
  }
}

function toggleMockCard(id){
  const content = document.getElementById(`mock-content-${id}`);
  const icon = document.getElementById(`collapse-icon-mock-${id}`);
  if(content && icon){
    if(content.style.display === 'none'){
      content.style.display = 'block';
      icon.textContent = '▼';
    } else {
      content.style.display = 'none';
      icon.textContent = '▶';
    }
  }
}

// ══════════════════════════════════════════════════════════
//  MOCK SERVER
// ══════════════════════════════════════════════════════════
async function loadMocks(){
  try {
    const res = await fetch('/api/mocks');
    S.mocks = await res.json();
    renderMocksView();
  } catch(e) { console.error('loadMocks error:', e); }
}

function renderMocksView(){
  const panel = document.getElementById('mocks-panel');
  if(!panel) return;
  
  if(!S.mocks || S.mocks.length === 0){
    panel.innerHTML = '<div style="text-align:center;padding:60px 20px;color:var(--txt3)"><p style="font-size:14px;margin:0">No mock endpoints created yet</p><p style="font-size:12px;margin-top:8px">Click "+ New Mock" to create your first mock endpoint</p></div>';
    return;
  }
  
  panel.innerHTML = S.mocks.map(mock => `
    <div class="env-card" id="mock-card-${mock.id}">
      <div class="env-card-header" onclick="toggleMockCard(${mock.id})" style="cursor:pointer">
        <input class="env-name-input" value="${esc(mock.path)}" id="mock-path-${mock.id}" placeholder="/path" onclick="event.stopPropagation()">
        <select id="mock-method-${mock.id}" class="method-select" style="width:80px" onclick="event.stopPropagation()">
          <option value="GET" ${mock.method==='GET'?'selected':''}>GET</option>
          <option value="POST" ${mock.method==='POST'?'selected':''}>POST</option>
          <option value="PUT" ${mock.method==='PUT'?'selected':''}>PUT</option>
          <option value="PATCH" ${mock.method==='PATCH'?'selected':''}>PATCH</option>
          <option value="DELETE" ${mock.method==='DELETE'?'selected':''}>DELETE</option>
        </select>
        <span style="font-size:10px;color:var(--txt3);margin-right:8px">${mock.enabled ? '✓' : '✗'}</span>
        <button class="icon-btn" onclick="event.stopPropagation();toggleMock(${mock.id})" title="Toggle">${mock.enabled?'⏸':'▶'}</button>
        <button class="icon-btn" onclick="event.stopPropagation();testMock(${mock.id})" title="Test">🧪</button>
        <button class="icon-btn" onclick="event.stopPropagation();deleteMock(${mock.id})" style="margin-left:auto" title="Delete">🗑</button>
        <span class="collapse-icon" id="collapse-icon-mock-${mock.id}" style="font-size:12px">▼</span>
      </div>
      <div class="env-card-content" id="mock-content-${mock.id}">
      <textarea id="mock-body-${mock.id}" class="code-editor" rows="5" style="width:100%;margin-bottom:10px">${esc(mock.response_body)}</textarea>
      <div style="display:flex;gap:12px;margin-bottom:10px">
        <input id="mock-ctype-${mock.id}" class="kv-input" value="${esc(mock.content_type)}" placeholder="Content-Type" style="flex:1">
        <input id="mock-status-${mock.id}" type="number" class="kv-input" value="${mock.status_code}" style="width:100px">
        <input id="mock-delay-${mock.id}" type="number" class="kv-input" value="${mock.delay_ms}" min="0" placeholder="Delay ms" style="width:100px">
      </div>
      <button class="env-save-btn" onclick="saveMock(${mock.id})">Save Changes</button>
      </div>
    </div>
  `).join('');
}

async function createMockEndpoint(){
  await fetch('/api/mocks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:'/mock-endpoint',method:'GET',response_body:'{}',status_code:200,delay_ms:0})});
  await loadMocks();
  toast('Mock endpoint created','success');
}

async function saveMock(id){
  const path = document.getElementById(`mock-path-${id}`).value;
  const method = document.getElementById(`mock-method-${id}`).value;
  const response_body = document.getElementById(`mock-body-${id}`).value;
  const content_type = document.getElementById(`mock-ctype-${id}`).value;
  const status_code = parseInt(document.getElementById(`mock-status-${id}`).value);
  const delay_ms = parseInt(document.getElementById(`mock-delay-${id}`).value);
  await fetch(`/api/mocks/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,method,response_body,content_type,status_code,delay_ms})});
  await loadMocks();
  toast('Mock saved','success');
}

async function toggleMock(id){
  const mock = S.mocks.find(m => m.id === id);
  await fetch(`/api/mocks/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...mock, enabled: !mock.enabled})});
  await loadMocks();
}

function testMock(id){
  const mock = S.mocks.find(m => m.id === id);
  if(mock) window.open(`/mock/${mock.path.replace(/^\//, '')}`, '_blank');
}

async function deleteMock(id){
  if(!confirm('Delete this mock endpoint?')) return;
  await fetch(`/api/mocks/${id}`,{method:'DELETE'});
  await loadMocks();
  toast('Mock deleted','info');
}

// ══════════════════════════════════════════════════════════
//  COLLECTION RUNNER
// ══════════════════════════════════════════════════════════
async function loadRunnerCollections(){
  try {
    const res = await fetch('/api/collections');
    const collections = await res.json();
    const select = document.getElementById('runner-collection');
    select.innerHTML = '<option value="">Select a collection...</option>' + collections.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  } catch(e) { console.error('loadRunnerCollections error:', e); }
}

async function runCollection(){
  const collectionId = document.getElementById('runner-collection').value;
  const iterations = parseInt(document.getElementById('runner-iterations').value) || 1;
  const delay = parseInt(document.getElementById('runner-delay').value) || 0;
  if(!collectionId){ toast('Please select a collection','error'); return; }
  toast('Running collection...','info');
  const resultsDiv = document.getElementById('runner-results');
  resultsDiv.style.display = 'block';
  resultsDiv.innerHTML = '<div style="text-align:center;padding:40px">Running...</div>';
  try {
    const res = await fetch('/api/runner/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({collection_id: parseInt(collectionId), iterations, delay_ms: delay})});
    const data = await res.json();
    renderRunnerResults(data);
  } catch(e) { resultsDiv.innerHTML = `<div style="text-align:center;padding:40px;color:var(--red)">Error: ${e.message}</div>`; }
}

function renderRunnerResults(data){
  const s = data.summary;
  const html = `
    <div style="display:flex;gap:12px;margin-bottom:20px">
      <div style="flex:1;padding:16px;background:var(--bg2);border-radius:8px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--acc)">${s.total_requests}</div><div style="font-size:11px;color:var(--txt3)">Total</div></div>
      <div style="flex:1;padding:16px;background:var(--bg2);border-radius:8px;text-align:center"><div style="font-size:24px;font-weight:700;color:#3dd68c">${s.total_passed}</div><div style="font-size:11px;color:var(--txt3)">Passed</div></div>
      <div style="flex:1;padding:16px;background:var(--bg2);border-radius:8px;text-align:center"><div style="font-size:24px;font-weight:700;color:#f47067">${s.total_failed}</div><div style="font-size:11px;color:var(--txt3)">Failed</div></div>
    </div>
    <table class="kv-table"><thead><tr><th>#</th><th>Request</th><th>Status</th><th>Duration</th><th>Tests</th></tr></thead><tbody>
    ${data.results.map((r, i) => {
      const tp = (r.test_results||[]).filter(t=>t.passed).length;
      const tf = (r.test_results||[]).filter(t=>!t.passed).length;
      return `<tr><td>${i+1}</td><td>${esc(r.request_name)}</td><td style="color:${r.status_code<400?'#3dd68c':'#f47067'}">${r.status_code||'Err'}</td><td>${r.duration_ms?r.duration_ms+'ms':'-'}</td><td>${tp}✓ ${tf}✗</td></tr>`;
    }).join('')}
    </tbody></table>
  `;
  document.getElementById('runner-results').innerHTML = html;
  toast('Collection run completed','success');
}

// ══════════════════════════════════════════════════════════
//  REALTIME PROTOCOLS (WS, SOCKET.IO, MQTT)
// ══════════════════════════════════════════════════════════
function updateRealtimeStatus(text, connected) {
  const dot = document.getElementById('realtime-status-dot');
  const txt = document.getElementById('realtime-status-text');
  const btn = document.getElementById('realtime-connect-btn');
  txt.textContent = text;
  if (connected) {
    dot.style.background = 'var(--acc)';
    dot.style.boxShadow = '0 0 8px var(--acc-glow)';
    btn.textContent = 'Disconnect';
    btn.style.background = 'var(--red)';
    btn.style.borderColor = 'var(--red)';
  } else {
    dot.style.background = 'var(--txt3)';
    dot.style.boxShadow = 'none';
    btn.textContent = 'Connect';
    btn.style.background = '';
    btn.style.borderColor = '';
  }
}

function appendRealtimeLog(msg, type) {
  const log = document.getElementById('realtime-log');
  const div = document.createElement('div');
  div.style.padding = '6px 10px';
  div.style.borderRadius = '4px';
  div.style.wordBreak = 'break-all';
  if (type === 'sys') {
    div.style.background = 'var(--bg2)';
    div.style.color = 'var(--txt3)';
    div.textContent = 'ℹ️ ' + msg;
  } else if (type === 'err') {
    div.style.background = 'rgba(244,112,103,0.1)';
    div.style.color = 'var(--red)';
    div.textContent = '❌ ' + msg;
  } else if (type === 'tx') {
    div.style.background = 'rgba(0,212,255,0.1)';
    div.style.color = 'var(--acc)';
    div.textContent = '⬆ ' + msg;
  } else {
    div.style.background = 'var(--bg2)';
    div.style.color = 'var(--txt)';
    div.textContent = '⬇ ' + msg;
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function toggleRealtimeConnection() {
  const t = currentTab(); if (!t) return;
  const protocol = document.getElementById('protocol-select').value;
  
  if (t.realtimeClient) {
    if (t.protocol === 'ws') t.realtimeClient.close();
    else if (t.protocol === 'socketio') t.realtimeClient.disconnect();
    else if (t.protocol === 'mqtt') t.realtimeClient.end();
    
    t.realtimeClient = null;
    updateRealtimeStatus('Disconnected', false);
    return;
  }
  
  const rawUrl = document.getElementById('url-input').value.trim();
  if (!rawUrl) { toast('Please enter a URL', 'error'); return; }
  const url = substituteVars(rawUrl);
  
  updateRealtimeStatus('Connecting...', false);
  document.getElementById('realtime-log').innerHTML = '';
  t.protocol = protocol;
  
  try {
    if (protocol === 'ws') {
      const ws = new WebSocket(url);
      ws.onopen = () => { updateRealtimeStatus('Connected', true); appendRealtimeLog('Connected to ' + url, 'sys'); };
      ws.onmessage = (e) => { appendRealtimeLog(e.data, 'rx'); };
      ws.onclose = () => { t.realtimeClient = null; updateRealtimeStatus('Disconnected', false); appendRealtimeLog('Disconnected', 'sys'); };
      ws.onerror = (e) => { appendRealtimeLog('WebSocket Error', 'err'); };
      t.realtimeClient = ws;
    } else if (protocol === 'socketio') {
      if (typeof io === 'undefined') { toast('Socket.io library not loaded', 'error'); updateRealtimeStatus('Disconnected', false); return; }
      const socket = io(url);
      socket.on('connect', () => { updateRealtimeStatus('Connected', true); appendRealtimeLog('Connected to ' + url, 'sys'); });
      socket.on('disconnect', () => { t.realtimeClient = null; updateRealtimeStatus('Disconnected', false); appendRealtimeLog('Disconnected', 'sys'); });
      socket.on('connect_error', (e) => { appendRealtimeLog('Connection Error: ' + e.message, 'err'); });
      socket.onAny((event, ...args) => { appendRealtimeLog(`[${event}] ` + JSON.stringify(args), 'rx'); });
      t.realtimeClient = socket;
    } else if (protocol === 'mqtt') {
      if (typeof mqtt === 'undefined') { toast('MQTT library not loaded', 'error'); updateRealtimeStatus('Disconnected', false); return; }
      const client = mqtt.connect(url);
      client.on('connect', () => { 
        updateRealtimeStatus('Connected', true); 
        appendRealtimeLog('Connected to ' + url, 'sys'); 
        const topic = document.getElementById('mqtt-topic').value || '#';
        client.subscribe(topic);
        appendRealtimeLog('Subscribed to ' + topic, 'sys');
      });
      client.on('message', (topic, message) => { appendRealtimeLog(`[${topic}] ` + message.toString(), 'rx'); });
      client.on('close', () => { t.realtimeClient = null; updateRealtimeStatus('Disconnected', false); appendRealtimeLog('Disconnected', 'sys'); });
      client.on('error', (e) => { appendRealtimeLog('MQTT Error: ' + e.message, 'err'); });
      t.realtimeClient = client;
    }
  } catch (e) {
    updateRealtimeStatus('Disconnected', false);
    appendRealtimeLog('Error: ' + e.message, 'err');
  }
}

function sendRealtimeMessage() {
  const t = currentTab(); if (!t || !t.realtimeClient) { toast('Not connected', 'error'); return; }
  const input = document.getElementById('realtime-msg-input');
  const msg = input.value;
  if (!msg) return;
  
  const protocol = t.protocol;
  try {
    if (protocol === 'ws') {
      t.realtimeClient.send(msg);
      appendRealtimeLog(msg, 'tx');
      input.value = '';
    } else if (protocol === 'socketio') {
      const ev = document.getElementById('sio-event').value || 'message';
      let data = msg; try { data = JSON.parse(msg); } catch(e){}
      t.realtimeClient.emit(ev, data);
      appendRealtimeLog(`[${ev}] ` + msg, 'tx');
      input.value = '';
    } else if (protocol === 'mqtt') {
      const topic = document.getElementById('mqtt-topic').value || 'test';
      t.realtimeClient.publish(topic, msg);
      appendRealtimeLog(`[${topic}] ` + msg, 'tx');
      input.value = '';
    }
  } catch(e) {
    appendRealtimeLog('Send Error: ' + e.message, 'err');
  }
}

// ══════════════════════════════════════════════════════════
//  SIDEBAR TOGGLE & RESIZE + RESPONSE RESIZE
// ══════════════════════════════════════════════════════════
function toggleSidebar(){
  const app=document.getElementById('app');
  const collapsed=app.classList.toggle('sidebar-collapsed');
  localStorage.setItem('requestlab_sidebar_collapsed', collapsed?'1':'0');
}

function setupResizeHandle(){
  if(localStorage.getItem('requestlab_sidebar_collapsed')==='1') document.getElementById('app').classList.add('sidebar-collapsed');
  const sbW=localStorage.getItem('requestlab_sidebar_width');
  if(sbW) { document.getElementById('app').style.gridTemplateColumns=sbW+'px 1fr'; document.getElementById('sidebar-drag').style.left=(sbW-1)+'px'; }

  // Sidebar drag
  const sbDrag=document.getElementById('sidebar-drag');
  let sbDragging=false, startX, startW;
  sbDrag.addEventListener('mousedown',e=>{sbDragging=true;startX=e.clientX;startW=document.getElementById('sidebar').offsetWidth;sbDrag.classList.add('dragging');document.body.style.userSelect='none';});
  document.addEventListener('mousemove',e=>{if(!sbDragging)return; const w=Math.max(200,Math.min(600,startW+(e.clientX-startX))); document.getElementById('app').style.gridTemplateColumns=w+'px 1fr'; sbDrag.style.left=(w-1)+'px';});
  document.addEventListener('mouseup',()=>{if(sbDragging){sbDragging=false;sbDrag.classList.remove('dragging');document.body.style.userSelect=''; localStorage.setItem('requestlab_sidebar_width',document.getElementById('sidebar').offsetWidth);}});

  // Response drag
  const handle=document.getElementById('resize-handle');
  const resp=document.getElementById('response-panel');
  let dragging=false,startY,startH;
  handle.addEventListener('mousedown',e=>{dragging=true;startY=e.clientY;startH=resp.offsetHeight;handle.classList.add('dragging');document.body.style.userSelect='none';});
  document.addEventListener('mousemove',e=>{ if(!dragging) return; const delta=startY-e.clientY; const newH=Math.max(120,Math.min(window.innerHeight*.8,startH+delta)); resp.style.maxHeight=newH+'px'; });
  document.addEventListener('mouseup',()=>{dragging=false;handle.classList.remove('dragging');document.body.style.userSelect='';});
}

document.querySelectorAll('.modal-overlay').forEach(o=>o.addEventListener('click',e=>{if(e.target===o)o.classList.remove('open');}));

// ══════════════════════════════════════════════════════════
//  TEAM COLLABORATION
// ══════════════════════════════════════════════════════════
let _currentTeamId = null;

async function loadTeams() {
  try {
    const res = await fetch('/api/teams');
    const teams = await res.json();
    const list = document.getElementById('teams-list');
    if(!teams.length) { list.innerHTML = '<p style="color:var(--txt3);font-size:11px;padding:8px">No teams yet. Create one!</p>'; return; }
    list.innerHTML = teams.map(t => `
      <div class="coll-group" style="cursor:pointer" onclick="openTeamDetails(${t.id})">
        <div class="coll-header">
          <span class="coll-icon">\u{1F465}</span>
          <span class="coll-name">${esc(t.name)}</span>
          <span class="coll-count">${t.member_count}</span>
          <span style="font-size:10px;color:var(--txt3);background:var(--bg3);padding:1px 6px;border-radius:10px">${t.role}</span>
        </div>
      </div>
    `).join('');
  } catch(e) { console.error(e); }
}

function showCreateTeamModal() { document.getElementById('new-team-name').value=''; openModal('create-team-modal'); }

async function createTeam() {
  const name = document.getElementById('new-team-name').value.trim();
  if(!name) { toast('Enter a team name','error'); return; }
  try {
    const res = await fetch('/api/teams', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    closeModal('create-team-modal');
    toast('Team created!');
    loadTeams();
  } catch(e) { toast('Error creating team','error'); }
}

async function openTeamDetails(tid) {
  _currentTeamId = tid;
  try {
    const res = await fetch(`/api/teams/${tid}`);
    const data = await res.json();
    document.getElementById('team-details-name').textContent = data.team.name;
    const ml = document.getElementById('team-members-list');
    ml.innerHTML = data.members.map(m => `
      <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="width:28px;height:28px;border-radius:50%;background:${m.avatar_color||'#00d4ff'};display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#000">${(m.username||'?')[0].toUpperCase()}</div>
        <span style="flex:1;font-size:12px">${esc(m.username)}</span>
        <span style="font-size:10px;color:var(--txt3)">${m.role}</span>
        ${data.team.owner_id !== m.id ? `<button class="icon-btn" style="font-size:10px;padding:2px 6px" onclick="removeMember(${tid},${m.id})">&times;</button>` : ''}
      </div>
    `).join('');
    const sc = document.getElementById('team-shared-collections');
    sc.innerHTML = data.shared_collections.length ? data.shared_collections.map(c => `
      <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="flex:1;font-size:12px">${esc(c.name)}</span>
        <span style="font-size:10px;color:var(--txt3)">by ${esc(c.shared_by_name||'unknown')}</span>
        <button class="icon-btn" style="font-size:10px;padding:2px 6px" onclick="unshareCollection(${tid},${c.collection_id})">&times;</button>
      </div>
    `).join('') : '<p style="color:var(--txt3);font-size:11px">No shared collections</p>';
    // Load collections for sharing
    const colRes = await fetch('/api/collections');
    const cols = await colRes.json();
    const sel = document.getElementById('share-collection-select');
    sel.innerHTML = '<option value="">Select collection...</option>' + cols.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    openModal('team-details-modal');
  } catch(e) { console.error(e); toast('Error loading team','error'); }
}

async function inviteMember() {
  const email = document.getElementById('invite-email').value.trim();
  const role = document.getElementById('invite-role').value;
  if(!email) { toast('Enter an email','error'); return; }
  try {
    const res = await fetch(`/api/teams/${_currentTeamId}/invite`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email, role})});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    document.getElementById('invite-email').value = '';
    toast('Member added!');
    openTeamDetails(_currentTeamId);
  } catch(e) { toast('Error','error'); }
}

async function removeMember(tid, uid) {
  try {
    const res = await fetch(`/api/teams/${tid}/members/${uid}`, {method:'DELETE'});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    toast('Member removed');
    openTeamDetails(tid);
  } catch(e) { toast('Error','error'); }
}

async function shareCollection() {
  const cid = document.getElementById('share-collection-select').value;
  if(!cid) { toast('Select a collection','error'); return; }
  try {
    const res = await fetch(`/api/teams/${_currentTeamId}/collections`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({collection_id:parseInt(cid)})});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    toast('Collection shared!');
    openTeamDetails(_currentTeamId);
  } catch(e) { toast('Error','error'); }
}

async function unshareCollection(tid, cid) {
  try {
    await fetch(`/api/teams/${tid}/collections/${cid}`, {method:'DELETE'});
    toast('Unshared');
    openTeamDetails(tid);
  } catch(e) { toast('Error','error'); }
}

// ══════════════════════════════════════════════════════════
//  TEST SUITES & CI/CD
// ══════════════════════════════════════════════════════════
let _allCollections = [];

async function loadTestSuites() {
  try {
    const [suitesRes, colsRes] = await Promise.all([fetch('/api/test-suites'), fetch('/api/collections')]);
    const suites = await suitesRes.json();
    _allCollections = await colsRes.json();
    const list = document.getElementById('suites-list');
    if(!suites.length) { list.innerHTML = '<p style="color:var(--txt3);font-size:12px">No test suites yet. Create one to group collections for automated testing.</p>'; return; }
    list.innerHTML = suites.map(s => {
      const colNames = (s.collection_ids||[]).map(id => { const c = _allCollections.find(x=>x.id===id); return c?c.name:'Unknown'; }).join(', ');
      const statusColor = s.status==='passed'?'var(--green)':s.status==='failed'?'var(--red)':s.status==='running'?'var(--yellow)':'var(--txt3)';
      return `
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <div>
              <span style="font-size:13px;font-weight:700">${esc(s.name)}</span>
              <span style="font-size:10px;color:${statusColor};margin-left:8px;text-transform:uppercase">${s.status}</span>
            </div>
            <div style="display:flex;gap:6px">
              <button class="icon-btn accent" style="font-size:10px;padding:4px 10px" onclick="runTestSuite(${s.id})">Run</button>
              <button class="icon-btn" style="font-size:10px;padding:4px 10px" onclick="viewSuiteResults(${s.id})">History</button>
              <button class="icon-btn danger" style="font-size:10px;padding:4px 10px" onclick="deleteTestSuite(${s.id})">&times;</button>
            </div>
          </div>
          <div style="font-size:11px;color:var(--txt3)">Collections: ${esc(colNames) || 'None'}</div>
          ${s.last_run ? `<div style="font-size:10px;color:var(--txt3);margin-top:4px">Last run: ${s.last_run}</div>` : ''}
        </div>
      `;
    }).join('');
  } catch(e) { console.error(e); }
}

function showCreateSuiteModal() {
  document.getElementById('new-suite-name').value = '';
  const picker = document.getElementById('suite-collections-picker');
  if(!_allCollections.length) { toast('Load collections first','error'); }
  picker.innerHTML = _allCollections.map(c => `
    <label style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:12px;cursor:pointer">
      <input type="checkbox" value="${c.id}" class="suite-col-cb"> ${esc(c.name)}
    </label>
  `).join('');
  openModal('create-suite-modal');
}

async function createTestSuite() {
  const name = document.getElementById('new-suite-name').value.trim();
  if(!name) { toast('Enter a suite name','error'); return; }
  const colIds = [...document.querySelectorAll('.suite-col-cb:checked')].map(cb => parseInt(cb.value));
  if(!colIds.length) { toast('Select at least one collection','error'); return; }
  try {
    const res = await fetch('/api/test-suites', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, collection_ids:colIds})});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    closeModal('create-suite-modal');
    toast('Suite created!');
    loadTestSuites();
  } catch(e) { toast('Error','error'); }
}

async function runTestSuite(sid) {
  toast('Running suite...');
  try {
    const res = await fetch(`/api/test-suites/${sid}/run`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({triggered_by:'manual'})});
    const data = await res.json();
    if(!res.ok) { toast(data.error||'Failed','error'); return; }
    const s = data.summary;
    const msg = `Suite done: ${s.passed} passed, ${s.failed} failed (${s.duration_ms}ms)`;
    toast(msg, s.failed > 0 ? 'error' : 'success');
    loadTestSuites();
  } catch(e) { toast('Error running suite','error'); }
}

async function viewSuiteResults(sid) {
  try {
    const res = await fetch(`/api/test-results?suite_id=${sid}&limit=20`);
    const results = await res.json();
    const panel = document.getElementById('suite-results-panel');
    const list = document.getElementById('suite-results-list');
    panel.style.display = 'block';
    if(!results.length) { list.innerHTML = '<p style="color:var(--txt3);font-size:11px">No runs yet</p>'; return; }
    list.innerHTML = results.map(r => {
      const statusColor = r.status==='passed'?'var(--green)':'var(--red)';
      return `
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:10px;font-size:11px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:${statusColor};font-weight:700;text-transform:uppercase">${r.status}</span>
            <span style="color:var(--txt3)">${r.triggered_by} | ${r.started_at}</span>
          </div>
          <div style="color:var(--txt2);margin-top:4px">${r.passed} passed, ${r.failed} failed | ${Math.round(r.duration_ms||0)}ms</div>
          <div style="margin-top:6px;display:flex;gap:6px">
            <a href="/api/ci/run/${r.id}/junit" target="_blank" style="color:var(--acc);font-size:10px">JUnit XML</a>
            <a href="/api/ci/run/${r.id}/json" target="_blank" style="color:var(--acc);font-size:10px">JSON</a>
          </div>
        </div>
      `;
    }).join('');
  } catch(e) { toast('Error loading results','error'); }
}

async function deleteTestSuite(sid) {
  if(!confirm('Delete this test suite?')) return;
  try {
    await fetch(`/api/test-suites/${sid}`, {method:'DELETE'});
    toast('Deleted');
    loadTestSuites();
  } catch(e) { toast('Error','error'); }
}

// CI Tokens
async function loadCITokens() {
  try {
    const res = await fetch('/api/ci/tokens');
    const tokens = await res.json();
    const list = document.getElementById('ci-tokens-list');
    if(!tokens.length) { list.innerHTML = '<p style="color:var(--txt3);font-size:11px">No CI tokens yet.</p>'; return; }
    list.innerHTML = tokens.map(t => `
      <div style="display:flex;align-items:center;gap:8px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:8px 12px;font-size:11px">
        <span style="font-weight:600">${esc(t.name)}</span>
        <span style="flex:1;color:var(--txt3)">Created: ${t.created}</span>
        ${t.last_used ? `<span style="color:var(--txt3)">Last used: ${t.last_used}</span>` : ''}
        <button class="icon-btn danger" style="font-size:10px;padding:2px 8px" onclick="deleteCIToken(${t.id})">&times;</button>
      </div>
    `).join('');
  } catch(e) { console.error(e); }
}

async function createCIToken() {
  const name = document.getElementById('ci-token-name').value.trim();
  if(!name) { toast('Enter a token name','error'); return; }
  try {
    const res = await fetch('/api/ci/tokens', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    const data = await res.json();
    if(!res.ok) { toast(data.error||'Failed','error'); return; }
    document.getElementById('ci-token-name').value = '';
    prompt('Copy your CI token (it won\'t be shown again):', data.token);
    loadCITokens();
  } catch(e) { toast('Error','error'); }
}

async function deleteCIToken(id) {
  if(!confirm('Revoke this token?')) return;
  try {
    await fetch(`/api/ci/tokens/${id}`, {method:'DELETE'});
    toast('Token revoked');
    loadCITokens();
  } catch(e) { toast('Error','error'); }
}

// ══════════════════════════════════════════════════════════
//  API DOCUMENTATION
// ══════════════════════════════════════════════════════════
let _docsCollectionId = null;

async function loadDocsCollections() {
  try {
    const res = await fetch('/api/collections');
    const cols = await res.json();
    const sel = document.getElementById('docs-collection-select');
    sel.innerHTML = '<option value="">Select collection...</option>' + cols.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  } catch(e) { console.error(e); }
}

async function loadDocsForCollection() {
  const cid = document.getElementById('docs-collection-select').value;
  if(!cid) { document.getElementById('docs-content-area').innerHTML = '<p style="color:var(--txt3);font-size:12px">Select a collection to generate API documentation.</p>'; return; }
  _docsCollectionId = parseInt(cid);
  try {
    const res = await fetch('/api/docs/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({collection_id:_docsCollectionId})});
    const spec = await res.json();
    renderDocs(spec);
  } catch(e) { toast('Error generating docs','error'); }
}

function renderDocs(spec) {
  const area = document.getElementById('docs-content-area');
  if(!spec.paths || !Object.keys(spec.paths).length) {
    area.innerHTML = '<p style="color:var(--txt3);font-size:12px">No endpoints found in this collection.</p>';
    return;
  }
  let html = `<h3 style="font-size:14px;font-weight:700;margin-bottom:4px">${esc(spec.info.title)}</h3>`;
  html += `<p style="font-size:12px;color:var(--txt3);margin-bottom:16px">${esc(spec.info.description||'')} &middot; v${spec.info.version}</p>`;
  for(const [path, methods] of Object.entries(spec.paths)) {
    for(const [method, op] of Object.entries(methods)) {
      const mColor = {get:'var(--green)',post:'var(--blue)',put:'var(--yellow)',delete:'var(--red)',patch:'var(--purple)'}[method]||'var(--txt2)';
      html += `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px;margin-bottom:10px">`;
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">`;
      html += `<span style="background:${mColor};color:#000;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase">${method}</span>`;
      html += `<code style="font-size:12px;color:var(--txt)">${esc(path)}</code></div>`;
      html += `<p style="font-size:12px;color:var(--txt2);margin-bottom:8px">${esc(op.summary||'')}</p>`;
      if(op.parameters && op.parameters.length) {
        html += '<table style="width:100%;font-size:11px;border-collapse:collapse;margin-bottom:8px"><tr style="background:var(--bg3)"><th style="padding:4px 8px;text-align:left">Name</th><th style="padding:4px 8px;text-align:left">In</th><th style="padding:4px 8px;text-align:left">Example</th></tr>';
        for(const p of op.parameters) {
          html += `<tr style="border-bottom:1px solid var(--border)"><td style="padding:4px 8px">${esc(p.name)}</td><td style="padding:4px 8px;color:var(--txt3)">${esc(p.in)}</td><td style="padding:4px 8px;font-family:var(--mono);color:var(--txt3)">${esc(String(p.example||''))}</td></tr>`;
        }
        html += '</table>';
      }
      if(op.requestBody) {
        html += '<details style="margin-top:6px"><summary style="font-size:11px;cursor:pointer;color:var(--acc)">Request Body</summary>';
        for(const [ct, ctData] of Object.entries(op.requestBody.content||{})) {
          html += `<p style="font-size:10px;color:var(--txt3);margin:4px 0">Content-Type: ${ct}</p>`;
          if(ctData.schema) html += `<pre style="background:var(--bg0);padding:8px;border-radius:var(--radius);font-size:11px;overflow:auto;font-family:var(--mono);color:var(--txt2)">${esc(JSON.stringify(ctData.schema,null,2))}</pre>`;
        }
        html += '</details>';
      }
      html += '</div>';
    }
  }
  area.innerHTML = html;
}

async function exportDocs(format) {
  if(!_docsCollectionId) { toast('Select a collection first','error'); return; }
  try {
    const res = await fetch(`/api/docs/${_docsCollectionId}/${format}`);
    if(!res.ok) { toast('Export failed','error'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const ext = format === 'openapi' ? 'json' : format === 'markdown' ? 'md' : 'html';
    a.download = `api_docs.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
    toast(`Exported as ${format.toUpperCase()}`);
  } catch(e) { toast('Error exporting','error'); }
}

// ══════════════════════════════════════════════════════════
//  CODE GENERATION
// ══════════════════════════════════════════════════════════
function openCodeGen() {
  const t = currentTab();
  if(!t) { toast('No active request','error'); return; }
  generateCode();
  openModal('code-gen-modal');
}

async function generateCode() {
  const t = currentTab();
  if(!t) return;
  const target = document.getElementById('code-target').value;
  const reqData = {
    method: t.method || 'GET',
    url: t.url || '',
    headers: (t.headers||[]).filter(h=>h.key),
    params: (t.params||[]).filter(p=>p.key),
    body_type: t.bodyType || 'none',
    body_content: t.body || '',
    auth_type: t.authType || 'none',
    auth_data: t.authData || {}
  };
  try {
    const res = await fetch('/api/code/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({target, request:reqData})});
    const data = await res.json();
    document.getElementById('code-output').textContent = data.code || '// No code generated';
  } catch(e) {
    document.getElementById('code-output').textContent = '// Error generating code';
  }
}

function copyGeneratedCode() {
  const code = document.getElementById('code-output').textContent;
  navigator.clipboard.writeText(code).then(() => toast('Copied!')).catch(() => toast('Copy failed','error'));
}

// ══════════════════════════════════════════════════════════
//  BACKUP & RESTORE
// ══════════════════════════════════════════════════════════
async function downloadBackup() {
  try {
    const res = await fetch('/api/backup/download');
    if(!res.ok) { toast('Export failed','error'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const cd = res.headers.get('Content-Disposition');
    a.download = cd ? cd.split('filename=')[1] : 'requestlab_backup.json';
    a.click();
    URL.revokeObjectURL(url);
    toast('Backup downloaded!');
  } catch(e) { toast('Error downloading backup','error'); }
}

async function uploadBackup(file) {
  if(!file) return;
  const mode = document.getElementById('restore-mode').value;
  if(mode === 'replace' && !confirm('This will REPLACE all your data. Continue?')) return;
  const status = document.getElementById('backup-status');
  status.textContent = 'Uploading...';
  try {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('mode', mode);
    const res = await fetch('/api/backup/upload', {method:'POST', body:formData});
    const data = await res.json();
    if(!res.ok) { status.textContent = 'Error: ' + (data.error||'Unknown'); toast(data.error||'Failed','error'); return; }
    const imp = data.imported;
    status.textContent = `Restored: ${imp.collections} collections, ${imp.environments} environments, ${imp.mock_endpoints} mocks`;
    toast('Backup restored!');
    loadCollections();
  } catch(e) { status.textContent = 'Error uploading backup'; toast('Error','error'); }
}

// ══════════════════════════════════════════════════════════
//  API MONITOR
// ══════════════════════════════════════════════════════════
async function loadMonitors() {
  try {
    const res = await fetch('/api/monitors');
    const monitors = await res.json();
    const list = document.getElementById('monitors-list');
    if(!monitors.length) { list.innerHTML = '<p style="color:var(--txt3);font-size:12px">No monitors yet. Create one to start tracking API uptime.</p>'; return; }
    list.innerHTML = monitors.map(m => {
      const statusColor = m.last_status==='success'?'var(--green)':m.last_status==='failure'?'var(--red)':'var(--txt3)';
      const statusIcon = m.last_status==='success'?'\u25CF':m.last_status==='failure'?'\u25CF':'\u25CB';
      const uptimeColor = m.uptime_pct >= 99 ? 'var(--green)' : m.uptime_pct >= 95 ? 'var(--yellow)' : 'var(--red)';
      return `
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:${statusColor};font-size:14px">${statusIcon}</span>
              <span style="font-size:13px;font-weight:700">${esc(m.name)}</span>
              <span style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--bg3);color:var(--txt3)">${m.method} ${esc(m.url)}</span>
            </div>
            <div style="display:flex;gap:6px">
              <button class="icon-btn accent" style="font-size:10px;padding:4px 10px" onclick="runMonitor(${m.id})">Run Now</button>
              <button class="icon-btn" style="font-size:10px;padding:4px 10px" onclick="viewMonitorLogs(${m.id},'${esc(m.name)}')">Logs</button>
              <button class="icon-btn" style="font-size:10px;padding:4px 10px" onclick="editMonitor(${m.id})">Edit</button>
              <button class="icon-btn danger" style="font-size:10px;padding:4px 10px" onclick="deleteMonitor(${m.id})">&times;</button>
            </div>
          </div>
          <div style="display:flex;gap:16px;font-size:11px;color:var(--txt3)">
            <span>Uptime: <span style="color:${uptimeColor};font-weight:600">${m.uptime_pct}%</span></span>
            <span>Checks: ${m.total_checks}</span>
            <span>Failures: <span style="color:${m.total_failures>0?'var(--red)':'inherit'}">${m.total_failures}</span></span>
            ${m.last_duration_ms ? `<span>Last: ${Math.round(m.last_duration_ms)}ms</span>` : ''}
            ${m.last_check ? `<span>Checked: ${m.last_check}</span>` : '<span>Never checked</span>'}
          </div>
        </div>
      `;
    }).join('');
  } catch(e) { console.error(e); }
}

function showCreateMonitorModal() {
  document.getElementById('mon-name').value = '';
  document.getElementById('mon-url').value = '';
  document.getElementById('mon-method').value = 'GET';
  document.getElementById('mon-expected-status').value = '200';
  document.getElementById('mon-interval').value = '300';
  document.getElementById('mon-expected-body').value = '';
  document.getElementById('mon-edit-id').value = '';
  document.getElementById('monitor-modal-title').textContent = 'New Monitor';
  openModal('create-monitor-modal');
}

async function editMonitor(id) {
  try {
    const res = await fetch('/api/monitors');
    const monitors = await res.json();
    const m = monitors.find(x => x.id === id);
    if(!m) { toast('Monitor not found','error'); return; }
    document.getElementById('mon-name').value = m.name;
    document.getElementById('mon-url').value = m.url;
    document.getElementById('mon-method').value = m.method;
    document.getElementById('mon-expected-status').value = m.expected_status;
    document.getElementById('mon-interval').value = m.interval_seconds;
    document.getElementById('mon-expected-body').value = m.expected_body || '';
    document.getElementById('mon-edit-id').value = id;
    document.getElementById('monitor-modal-title').textContent = 'Edit Monitor';
    openModal('create-monitor-modal');
  } catch(e) { toast('Error','error'); }
}

async function saveMonitor() {
  const id = document.getElementById('mon-edit-id').value;
  const data = {
    name: document.getElementById('mon-name').value.trim(),
    url: document.getElementById('mon-url').value.trim(),
    method: document.getElementById('mon-method').value,
    expected_status: parseInt(document.getElementById('mon-expected-status').value) || 200,
    interval_seconds: parseInt(document.getElementById('mon-interval').value) || 300,
    expected_body: document.getElementById('mon-expected-body').value.trim()
  };
  if(!data.name || !data.url) { toast('Name and URL required','error'); return; }
  try {
    const method = id ? 'PUT' : 'POST';
    const url = id ? `/api/monitors/${id}` : '/api/monitors';
    const res = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    if(!res.ok) { const e = await res.json(); toast(e.error||'Failed','error'); return; }
    closeModal('create-monitor-modal');
    toast(id ? 'Monitor updated!' : 'Monitor created!');
    loadMonitors();
  } catch(e) { toast('Error','error'); }
}

async function runMonitor(id) {
  toast('Running check...');
  try {
    const res = await fetch(`/api/monitors/${id}/run`, {method:'POST'});
    const data = await res.json();
    if(data.status === 'success') toast(`Check passed (${data.duration_ms}ms)`);
    else toast(`Check failed: ${data.error||'Unknown'}`, 'error');
    loadMonitors();
  } catch(e) { toast('Error','error'); }
}

async function deleteMonitor(id) {
  if(!confirm('Delete this monitor?')) return;
  try {
    await fetch(`/api/monitors/${id}`, {method:'DELETE'});
    toast('Deleted');
    loadMonitors();
  } catch(e) { toast('Error','error'); }
}

async function viewMonitorLogs(id, name) {
  document.getElementById('monitor-logs-title').textContent = `Logs: ${name}`;
  try {
    const res = await fetch(`/api/monitors/${id}/logs?limit=50`);
    const logs = await res.json();
    const content = document.getElementById('monitor-logs-content');
    if(!logs.length) { content.innerHTML = '<p style="color:var(--txt3);font-size:12px">No logs yet. Run the monitor first.</p>'; openModal('monitor-logs-modal'); return; }
    content.innerHTML = logs.map(l => {
      const sc = l.status === 'success' ? 'var(--green)' : 'var(--red)';
      return `
        <div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);font-size:11px">
          <span style="color:${sc};font-weight:700;text-transform:uppercase;width:60px">${l.status}</span>
          <span style="color:var(--txt2)">${l.status_code || '-'}</span>
          <span style="color:var(--txt3)">${Math.round(l.duration_ms||0)}ms</span>
          <span style="flex:1;color:var(--txt3)">${l.checked_at}</span>
          ${l.error ? `<span style="color:var(--red);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(l.error)}">${esc(l.error)}</span>` : ''}
        </div>
      `;
    }).join('');
    openModal('monitor-logs-modal');
  } catch(e) { toast('Error loading logs','error'); }
}

</script>
</body>
</html>"""

ERROR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{CODE}} - RequestLab</title>
<link rel="icon" type="image/png" href="/media/logo.png">
<style>
  :root { --bg: #080c10; --acc: #00d4ff; --acc-glow: rgba(0, 212, 255, 0.4); --txt: #cdd9e5; }
  body { margin: 0; background: var(--bg); color: var(--txt); font-family: 'Segoe UI', system-ui, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; overflow: hidden; }
  .blob { position: absolute; border-radius: 50%; filter: blur(80px); z-index: 0; opacity: 0.4; animation: float 10s infinite alternate ease-in-out; }
  .blob.one { top: -10%; left: -10%; width: 400px; height: 400px; background: #00d4ff; }
  .blob.two { bottom: -10%; right: -10%; width: 400px; height: 400px; background: #d2a8ff; animation-delay: -5s; }
  @keyframes float { from { transform: translate(0,0); } to { transform: translate(40px, 60px); } }
  .card { position: relative; z-index: 10; background: rgba(13,17,23,0.7); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.1); border-radius: 24px; padding: 60px; text-align: center; box-shadow: 0 40px 100px rgba(0,0,0,0.5); max-width: 440px; width: 90%; }
  .code { font-size: 120px; font-weight: 800; margin: 0; line-height: 1; background: linear-gradient(135deg, var(--acc), #d2a8ff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; filter: drop-shadow(0 0 15px var(--acc-glow)); animation: glitch 3s infinite; }
  @keyframes glitch { 0% { opacity: 1; } 95% { opacity: 1; transform: none; } 96% { opacity: 0.8; transform: skewX(-5deg); } 97% { opacity: 1; transform: skewX(5deg); } 98% { opacity: 0.9; transform: none; } 100% { opacity: 1; } }
  h1 { font-size: 28px; margin: 24px 0 12px; color: #fff; font-weight: 700; letter-spacing: -0.5px; }
  p { font-size: 16px; color: #8b9eb5; margin-bottom: 36px; line-height: 1.6; }
  .btn { display: inline-block; padding: 14px 36px; background: var(--acc); color: #000; text-decoration: none; font-weight: 700; border-radius: 30px; transition: all 0.2s; box-shadow: 0 4px 20px var(--acc-glow); }
  .btn:hover { transform: translateY(-2px); box-shadow: 0 8px 30px var(--acc-glow); background: #79c0ff; }
  .logo { width: 56px; height: 56px; margin-bottom: 24px; filter: drop-shadow(0 0 12px var(--acc-glow)); }
</style>
</head>
<body>
  <div class="blob one"></div><div class="blob two"></div>
  <div class="card">
    <div class="code">{{CODE}}</div>
    <h1>{{TITLE}}</h1>
    <p>{{MSG}}</p>
    <a href="/" class="btn">Back to Earth</a>
  </div>
</body>
</html>"""

@app.route("/")
def index():
    if get_current_user_id():
        return redirect("/app")
    return Response(LANDING_HTML, mimetype="text/html")

@app.errorhandler(404)
def page_not_found(e):
    return render_error(404, "Lost in Space", "The page you're looking for has drifted into a black hole.")

@app.errorhandler(403)
def access_forbidden(e):
    return render_error(403, "Access Denied", "You don't have the clearance to enter this sector.")

def render_error(code, title, msg):
    html = ERROR_HTML.replace("{{CODE}}", str(code)).replace("{{TITLE}}", title).replace("{{MSG}}", msg)
    return Response(html, mimetype="text/html"), code

@app.route("/app")
def app_ui():
    return Response(HTML, mimetype="text/html")

if __name__ == "__main__":
    print("\n+--------------------------------------+")
    print("|     RequestLab is running!            |")
    print("|  Landing Page: http://localhost:5000 |")
    print("|  App UI: http://localhost:5000/app   |")
    print("+--------------------------------------+\n")
    app.run(debug=True, port=5000)