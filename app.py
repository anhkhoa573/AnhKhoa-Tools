from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
import os
import sqlite3
import hashlib
import base64
import secrets
import re
from datetime import datetime, timedelta
from functools import wraps
import requests

app = Flask(__name__)
app.secret_key = "anhkhoa-tools-secret-2026"
app.permanent_session_lifetime = timedelta(days=7)

DB_PATH = "tools.db"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_FALLBACKS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

MODE_CONFIG = {
    "basic": {"system": "Ban la tro ly AI than thien. Tra loi ngan gon.", "max_tokens": 1000},
    "pro": {"system": "Ban la chuyen gia AI. Tra loi chi tiet.", "max_tokens": 2000},
    "max": {"system": "Ban la AI thong minh nhat. Suy luan sau.", "max_tokens": 4000}
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS scripts (id INTEGER PRIMARY KEY AUTOINCREMENT, script_id TEXT UNIQUE NOT NULL, code_encrypted TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def deco(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return deco


# ============ AI CHAT ============
def call_groq(messages, system, max_tokens, temperature):
    last_error = "Khong the ket noi AI."
    if not GROQ_API_KEY:
        return False, "Chua cau hinh GROQ_API_KEY."
    for model in MODEL_FALLBACKS:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            headers = {"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "application/json"}
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=60)
            data = r.json()
            if r.status_code == 200 and "choices" in data:
                return True, data["choices"][0]["message"]["content"]
            last_error = data.get("error", {}).get("message", "Loi API")
        except Exception as e:
            last_error = str(e)
    return False, last_error


# ============ LUA ENCODER ============
def encrypt_script(code):
    return base64.b64encode(code.encode("utf-8")).decode("utf-8")


def build_lua_loader(encoded):
    chunk_size = 200
    chunks = [encoded[i:i+chunk_size] for i in range(0, len(encoded), chunk_size)]
    parts = []
    parts.append("local b='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'")
    parts.append("local function d(s)")
    parts.append("s=string.gsub(s,'[^'..b..'=]','')")
    parts.append("return (s:gsub('.',function(x)")
    parts.append("if x=='=' then return '' end")
    parts.append("local r,f='',(b:find(x,1,true)-1)")
    parts.append("for i=6,1,-1 do r=r..(f%2^i-f%2^(i-1)>0 and '1' or '0') end")
    parts.append("return r")
    parts.append("end):gsub('%d%d%d?%d?%d?%d?%d?%d?',function(x)")
    parts.append("if #x~=8 then return '' end")
    parts.append("local c=0")
    parts.append("for i=1,8 do c=c+(x:sub(i,i)=='1' and 2^(8-i) or 0) end")
    parts.append("return string.char(c)")
    parts.append("end))")
    parts.append("end")
    parts.append("local t={")
    for chunk in chunks:
        parts.append("'" + chunk + "',")
    parts.append("}")
    parts.append("local s=table.concat(t)")
    parts.append("local code=d(s)")
    parts.append("local fn=loadstring or load")
    parts.append("fn(code)()")
    return chr(10).join(parts)


# ============ DUMP FILTER ============
def filter_dump_content(content):
    """Loc offset tu file dump il2cpp - tra ve chuoi da format."""
    results = []
    current_class = "Unknown"

    class_pattern = re.compile(r"(?:public|internal|private)?\s*(?:class|struct)\s+(\w+)")
    field_pattern = re.compile(
        r"(?:public|internal|private|protected)?\s*(?:static\s+)?"
        r"([\w<>\[\],\.]+)\s+(\w+)\s*;\s*//\s*(0x[0-9A-Fa-f]+)"
    )

    for line in content.split("\n"):
        line_stripped = line.strip()
        cm = class_pattern.search(line_stripped)
        if cm:
            current_class = cm.group(1)
            continue
        fm = field_pattern.search(line_stripped)
        if fm:
            field_type = fm.group(1).strip()
            field_name = fm.group(2).strip()
            offset = fm.group(3).strip()
            results.append((current_class, field_type, field_name, offset))

    results.sort(key=lambda x: (x[0], int(x[3], 16)))

    output = []
    output.append("=" * 70)
    output.append("OFFSET FILTER - ANHKHOA SYSTEM")
    output.append("=" * 70)
    output.append("")

    current_class = None
    for cls, ftype, fname, offset in results:
        if cls != current_class:
            output.append("")
            output.append("=" * 70)
            output.append("CLASS: " + cls)
            output.append("=" * 70)
            output.append("{:<12} {:<25} {:<30}".format("Offset", "Type", "Name"))
            output.append("-" * 70)
            current_class = cls
        output.append("{:<12} {:<25} {:<30}".format(offset, ftype, fname))

    output.append("")
    output.append("=" * 70)
    output.append("Tong so offset: " + str(len(results)))
    output.append("=" * 70)

    return "\n".join(output), len(results)


# ============ ROUTES ============
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        d = request.get_json()
        u = d.get("username", "").strip()
        p = d.get("password", "")
        action = d.get("action", "login")
        if not u or not p:
            return jsonify({"ok": False, "error": "Nhap day du thong tin!"})
        init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        if action == "register":
            if len(u) < 3 or len(u) > 20:
                conn.close()
                return jsonify({"ok": False, "error": "Ten 3-20 ky tu!"})
            if len(p) < 6:
                conn.close()
                return jsonify({"ok": False, "error": "Mat khau it nhat 6 ky tu!"})
            try:
                conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                             (u, hash_pw(p), datetime.utcnow().isoformat()))
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE username = ?", (u,)).fetchone()
                session["user_id"] = row["id"]
                session["username"] = row["username"]
                session.permanent = True
                conn.close()
                return jsonify({"ok": True, "message": "Dang ky thanh cong!"})
            except sqlite3.IntegrityError:
                conn.close()
                return jsonify({"ok": False, "error": "Ten da ton tai!"})
        row = conn.execute("SELECT * FROM users WHERE username = ? AND password_hash = ?",
                           (u, hash_pw(p))).fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error": "Sai ten hoac mat khau!"})
        session["user_id"] = row["id"]
        session["username"] = row["username"]
        session.permanent = True
        return jsonify({"ok": True, "message": "Dang nhap thanh cong!"})
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username"))


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    try:
        d = request.get_json()
        msg = d.get("message", "").strip()
        mode = d.get("mode", "basic")
        if not msg:
            return jsonify({"ok": False, "error": "Nhap tin nhan!"})
        if mode not in MODE_CONFIG:
            mode = "basic"
        uid = session["user_id"]
        cfg = MODE_CONFIG[mode]
        init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO chats (user_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                     (uid, msg, datetime.utcnow().isoformat()))
        conn.commit()
        rows = conn.execute("SELECT role, content FROM chats WHERE user_id = ? ORDER BY id DESC LIMIT 6",
                            (uid,)).fetchall()
        conn.close()
        history = list(reversed(rows))
        messages = []
        for role, content in history:
            messages.append({"role": role, "content": content})
        temperature = 0.9 if mode == "max" else 0.7
        ok, result = call_groq(messages, cfg["system"], cfg["max_tokens"], temperature)
        if not ok:
            return jsonify({"ok": False, "error": "Loi AI: " + result[:200]})
        reply = result
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO chats (user_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                     (uid, reply, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "reply": reply, "mode": mode})
    except Exception as e:
        return jsonify({"ok": False, "error": "Loi he thong: " + str(e)[:200]})


@app.route("/api/history")
@login_required
def api_history():
    uid = session["user_id"]
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT role, content, created_at FROM chats WHERE user_id = ? ORDER BY id ASC LIMIT 30",
                        (uid,)).fetchall()
    conn.close()
    return jsonify({"ok": True, "history": [dict(r) for r in rows]})


@app.route("/api/clear", methods=["POST"])
@login_required
def api_clear():
    uid = session["user_id"]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chats WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/encode", methods=["POST"])
@login_required
def api_encode():
    try:
        data = request.get_json()
        code = data.get("code", "").strip()
        if not code:
            return jsonify({"ok": False, "error": "Nhap code Lua!"})
        encoded = encrypt_script(code)
        loader = build_lua_loader(encoded)
        init_db()
        script_id = secrets.token_urlsafe(16)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO scripts (script_id, code_encrypted, created_at) VALUES (?, ?, ?)",
                     (script_id, encoded, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        host = request.host_url.rstrip("/")
        raw_url = host + "/raw/" + script_id
        return jsonify({
            "ok": True,
            "loader": loader,
            "raw_url": raw_url,
            "loadstring_url": 'loadstring(game:HttpGet("' + raw_url + '"))()',
            "original_size": len(code),
            "encoded_size": len(loader)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "Loi: " + str(e)})


@app.route("/raw/<script_id>")
def raw_script(script_id):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT code_encrypted FROM scripts WHERE script_id = ?", (script_id,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    loader = build_lua_loader(row["code_encrypted"])
    ua = request.headers.get("User-Agent", "").lower()
    keywords = ["roblox", "delta", "executor", "synapse", "krnl", "fluxus",
                "evon", "codex", "wave", "solara", "xeno", "hydrogen", "argon",
                "rayfield", "httpget", "http_request", "swift", "trigon", "valyse"]
    if not any(kw in ua for kw in keywords):
        return render_template("protected.html"), 200
    return Response(loader, mimetype="text/plain")


@app.route("/api/filter-dump", methods=["POST"])
@login_required
def api_filter_dump():
    try:
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "Chua chon file!"})
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"ok": False, "error": "File trong!"})
        content = file.read().decode("utf-8", errors="ignore")
        result, count = filter_dump_content(content)
        return jsonify({
            "ok": True,
            "result": result,
            "count": count,
            "filename": "Offset&anhkhoa.txt"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "Loi: " + str(e)})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
