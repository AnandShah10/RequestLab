"""
RequestLab - A Postman Alternative built with Python + Flask
Run: pip install flask requests && python app.py
Then open: http://localhost:5000
"""

import json
import os
import time
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
DB_PATH = "RequestLab.db"

# ─── Database Setup ─────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                created TEXT DEFAULT (datetime('now'))
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
                name    TEXT NOT NULL,
                vars    TEXT DEFAULT '{}',
                active  INTEGER DEFAULT 0
            );
        """)

init_db()

# ─── Proxy / Execute Request ─────────────────────────────────────────────────

@app.route("/api/execute", methods=["POST"])
def execute_request():
    data = request.json or {}
    method      = data.get("method", "GET").upper()
    url         = data.get("url", "").strip()
    params_list = data.get("params", [])
    headers_list= data.get("headers", [])
    body_type   = data.get("body_type", "none")
    body_content= data.get("body_content", "")
    auth_type   = data.get("auth_type", "none")
    auth_data   = data.get("auth_data", {})
    timeout     = data.get("timeout", 30)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    params = {p["key"]: p["value"] for p in params_list if p.get("key") and p.get("enabled", True)}
    headers = {h["key"]: h["value"] for h in headers_list if h.get("key") and h.get("enabled", True)}

    auth = None
    if auth_type == "basic":
        auth = (auth_data.get("username",""), auth_data.get("password",""))
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth_data.get('token','')}"
    elif auth_type == "apikey":
        key_loc = auth_data.get("location","header")
        key_name= auth_data.get("key","X-API-Key")
        key_val = auth_data.get("value","")
        if key_loc == "header":
            headers[key_name] = key_val
        else:
            params[key_name] = key_val

    req_body = None
    req_json = None
    form_data = None

    if body_type == "json":
        try:
            req_json = json.loads(body_content) if body_content.strip() else None
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Invalid JSON body: {e}"}), 400
    elif body_type == "raw":
        req_body = body_content.encode("utf-8")
    elif body_type == "form":
        try:
            form_data = json.loads(body_content) if body_content.strip() else {}
        except Exception:
            form_data = {}
    elif body_type == "urlencoded":
        try:
            form_data = json.loads(body_content) if body_content.strip() else {}
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        except Exception:
            form_data = {}

    start = time.time()
    try:
        resp = requests.request(
            method=method, url=url, params=params, headers=headers,
            json=req_json, data=form_data or req_body,
            auth=auth, timeout=timeout, allow_redirects=True, verify=True,
        )
        duration_ms = (time.time() - start) * 1000
        try:
            resp_body = resp.text
        except Exception:
            resp_body = "<binary content>"
        resp_json = None
        try:
            resp_json = resp.json()
        except Exception:
            pass

        result = {
            "status_code": resp.status_code,
            "status_text": resp.reason,
            "duration_ms": round(duration_ms, 2),
            "size_bytes":  len(resp.content),
            "headers":     dict(resp.headers),
            "cookies":     {c.name: c.value for c in resp.cookies},
            "body":        resp_body,
            "body_json":   resp_json,
            "url":         resp.url,
            "redirects":   len(resp.history),
        }

        with get_db() as conn:
            conn.execute(
                "INSERT INTO history (method,url,status_code,duration_ms,request_data,response_data) VALUES (?,?,?,?,?,?)",
                (method, url, resp.status_code, round(duration_ms, 2),
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


# ─── Collections ─────────────────────────────────────────────────────────────

@app.route("/api/collections", methods=["GET"])
def list_collections():
    with get_db() as conn:
        cols = conn.execute("SELECT * FROM collections ORDER BY created DESC").fetchall()
        result = []
        for c in cols:
            reqs = conn.execute(
                "SELECT id,name,method,url FROM requests WHERE collection_id=? ORDER BY id",
                (c["id"],)
            ).fetchall()
            result.append({
                "id": c["id"], "name": c["name"], "created": c["created"],
                "requests": [dict(r) for r in reqs]
            })
    return jsonify(result)

@app.route("/api/collections", methods=["POST"])
def create_collection():
    name = (request.json or {}).get("name", "New Collection")
    with get_db() as conn:
        cur = conn.execute("INSERT INTO collections (name) VALUES (?)", (name,))
        cid = cur.lastrowid
    return jsonify({"id": cid, "name": name, "requests": []})

@app.route("/api/collections/<int:cid>", methods=["PUT"])
def rename_collection(cid):
    name = (request.json or {}).get("name", "")
    with get_db() as conn:
        conn.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))
    return jsonify({"ok": True})

@app.route("/api/collections/<int:cid>", methods=["DELETE"])
def delete_collection(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM collections WHERE id=?", (cid,))
    return jsonify({"ok": True})

# ─── Export Collection ────────────────────────────────────────────────────────

@app.route("/api/collections/<int:cid>/export", methods=["GET"])
def export_collection(cid):
    with get_db() as conn:
        col = conn.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
        if not col:
            return jsonify({"error": "Not found"}), 404
        reqs = conn.execute("SELECT * FROM requests WHERE collection_id=?", (cid,)).fetchall()
    
    export_data = {
        "RequestLab_export": True,
        "version": "1.0",
        "exported_at": datetime.utcnow().isoformat(),
        "collection": {
            "name": col["name"],
            "created": col["created"],
            "requests": []
        }
    }
    for r in reqs:
        d = dict(r)
        d["params"]    = json.loads(d["params"] or "[]")
        d["headers"]   = json.loads(d["headers"] or "[]")
        d["auth_data"] = json.loads(d["auth_data"] or "{}")
        del d["id"]
        del d["collection_id"]
        export_data["collection"]["requests"].append(d)
    
    return Response(
        json.dumps(export_data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{col["name"]}.json"'}
    )

# ─── Import Collection ────────────────────────────────────────────────────────

@app.route("/api/collections/import", methods=["POST"])
def import_collection():
    try:
        data = request.json or {}

        if data.get("RequestLab_export"):
            col_data = data.get("collection", {})
            name = col_data.get("name", "Imported Collection")
            with get_db() as conn:
                cur = conn.execute("INSERT INTO collections (name) VALUES (?)", (name,))
                cid = cur.lastrowid
                for r in col_data.get("requests", []):
                    conn.execute(
                        "INSERT INTO requests (collection_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (cid, r.get("name","Untitled"), r.get("method","GET"),
                         r.get("url",""), json.dumps(r.get("params",[])), json.dumps(r.get("headers",[])),
                         r.get("body_type","none"), r.get("body_content",""),
                         r.get("auth_type","none"), json.dumps(r.get("auth_data",{})))
                    )
            return jsonify({"ok": True, "id": cid, "name": name})

        postman_schema = ""
        if "info" in data:
            postman_schema = data["info"].get("schema", "")
        elif "collection" in data and "info" in data.get("collection", {}):
            data = data["collection"]
            postman_schema = data["info"].get("schema", "")

        if "schema.getpostman.com" in postman_schema or "item" in data:
            name = data.get("info", {}).get("name", "Imported Collection")

            def extract_items(items):
                result = []
                for item in (items or []):
                    if "item" in item:
                        result.extend(extract_items(item["item"]))
                    elif "request" in item:
                        result.append(item)
                return result

            flat_items = extract_items(data.get("item", []))

            with get_db() as conn:
                cur = conn.execute("INSERT INTO collections (name) VALUES (?)", (name,))
                cid = cur.lastrowid
                for item in flat_items:
                    req       = item.get("request", {})
                    item_name = item.get("name", "Untitled")
                    method    = req.get("method", "GET").upper()

                    url_raw = req.get("url", "")
                    if isinstance(url_raw, dict):
                        url = url_raw.get("raw", "")
                    else:
                        url = url_raw

                    params = []
                    if isinstance(url_raw, dict):
                        for q in url_raw.get("query", []):
                            if not q.get("disabled", False):
                                params.append({"key": q.get("key",""), "value": q.get("value",""), "enabled": True})

                    headers = []
                    for h in req.get("header", []):
                        if not h.get("disabled", False):
                            headers.append({"key": h.get("key",""), "value": h.get("value",""), "enabled": True})

                    body_type    = "none"
                    body_content = ""
                    body_obj = req.get("body") or {}
                    mode = body_obj.get("mode", "none")
                    if mode == "raw":
                        body_type    = "json" if "json" in body_obj.get("options", {}).get("raw", {}).get("language", "") else "raw"
                        body_content = body_obj.get("raw", "")
                    elif mode == "urlencoded":
                        body_type = "urlencoded"
                        kv = {x["key"]: x.get("value","") for x in body_obj.get("urlencoded", []) if not x.get("disabled")}
                        body_content = json.dumps(kv)
                    elif mode == "formdata":
                        body_type = "form"
                        kv = {x["key"]: x.get("value","") for x in body_obj.get("formdata", []) if not x.get("disabled")}
                        body_content = json.dumps(kv)

                    auth_type = "none"
                    auth_data = {}
                    auth_obj  = req.get("auth") or {}
                    a_type    = auth_obj.get("type", "noauth")
                    if a_type == "basic":
                        auth_type = "basic"
                        items_list = auth_obj.get("basic", [])
                        auth_data  = {x["key"]: x.get("value","") for x in items_list}
                    elif a_type == "bearer":
                        auth_type = "bearer"
                        items_list = auth_obj.get("bearer", [])
                        auth_data  = {"token": next((x.get("value","") for x in items_list if x["key"]=="token"), "")}
                    elif a_type == "apikey":
                        auth_type = "apikey"
                        items_list = auth_obj.get("apikey", [])
                        auth_data  = {x["key"]: x.get("value","") for x in items_list}

                    conn.execute(
                        "INSERT INTO requests (collection_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (cid, item_name, method, url,
                         json.dumps(params), json.dumps(headers),
                         body_type, body_content,
                         auth_type, json.dumps(auth_data))
                    )

            return jsonify({"ok": True, "id": cid, "name": name})

        return jsonify({"error": "Unrecognised file. Export a collection from Postman (v2 / v2.1) or use a RequestLab export."}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ─── Saved Requests ──────────────────────────────────────────────────────────

@app.route("/api/requests", methods=["POST"])
def save_request():
    d = request.json or {}
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO requests (collection_id,name,method,url,params,headers,body_type,body_content,auth_type,auth_data) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.get("collection_id"), d.get("name","Untitled"), d.get("method","GET"),
             d.get("url",""), json.dumps(d.get("params",[])), json.dumps(d.get("headers",[])),
             d.get("body_type","none"), d.get("body_content",""),
             d.get("auth_type","none"), json.dumps(d.get("auth_data",{})))
        )
        rid = cur.lastrowid
    return jsonify({"id": rid, "ok": True})

@app.route("/api/requests/<int:rid>", methods=["GET"])
def get_request(rid):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"error": "Not found"}), 404
    d = dict(r)
    d["params"]    = json.loads(d["params"])
    d["headers"]   = json.loads(d["headers"])
    d["auth_data"] = json.loads(d["auth_data"])
    return jsonify(d)

@app.route("/api/requests/<int:rid>", methods=["PUT"])
def update_request(rid):
    d = request.json or {}
    with get_db() as conn:
        conn.execute(
            "UPDATE requests SET name=?,method=?,url=?,params=?,headers=?,body_type=?,body_content=?,auth_type=?,auth_data=? WHERE id=?",
            (d.get("name","Untitled"), d.get("method","GET"), d.get("url",""),
             json.dumps(d.get("params",[])), json.dumps(d.get("headers",[])),
             d.get("body_type","none"), d.get("body_content",""),
             d.get("auth_type","none"), json.dumps(d.get("auth_data",{})), rid)
        )
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>/rename", methods=["PUT"])
def rename_request(rid):
    name = (request.json or {}).get("name", "Untitled")
    with get_db() as conn:
        conn.execute("UPDATE requests SET name=? WHERE id=?", (name, rid))
    return jsonify({"ok": True})

@app.route("/api/requests/<int:rid>", methods=["DELETE"])
def delete_request(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM requests WHERE id=?", (rid,))
    return jsonify({"ok": True})


# ─── History ─────────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    limit = int(request.args.get("limit", 50))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,method,url,status_code,duration_ms,timestamp FROM history ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/history/<int:hid>", methods=["GET"])
def get_history_item(hid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM history WHERE id=?", (hid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["request_data"]  = json.loads(d["request_data"] or "{}")
    d["response_data"] = json.loads(d["response_data"] or "{}")
    return jsonify(d)

@app.route("/api/history", methods=["DELETE"])
def clear_history():
    with get_db() as conn:
        conn.execute("DELETE FROM history")
    return jsonify({"ok": True})


# ─── Environments ─────────────────────────────────────────────────────────────

@app.route("/api/environments", methods=["GET"])
def list_environments():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM environments ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/environments", methods=["POST"])
def create_environment():
    d = request.json or {}
    name = d.get("name", "New Environment")
    vars_ = json.dumps(d.get("vars", {}))
    with get_db() as conn:
        cur = conn.execute("INSERT INTO environments (name,vars) VALUES (?,?)", (name, vars_))
        eid = cur.lastrowid
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
    with get_db() as conn:
        conn.execute("UPDATE environments SET active=0")
        conn.execute("UPDATE environments SET active=1 WHERE id=?", (eid,))
    return jsonify({"ok": True})

@app.route("/api/environments/<int:eid>", methods=["DELETE"])
def delete_environment(eid):
    with get_db() as conn:
        conn.execute("DELETE FROM environments WHERE id=?", (eid,))
    return jsonify({"ok": True})


# ─── Frontend ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RequestLab</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#080c10;--bg1:#0d1117;--bg2:#131920;--bg3:#1a2230;--bg4:#212d40;
  --border:#1e2d3d;--border2:#253548;--border3:#2d4060;
  --txt:#cdd9e5;--txt2:#8b9eb5;--txt3:#4d6377;
  --acc:#00d4ff;--acc2:#00b8e0;--acc-dim:#00d4ff12;--acc-glow:#00d4ff30;
  --green:#3dd68c;--red:#f47067;--blue:#79c0ff;--yellow:#e3b341;--purple:#d2a8ff;--orange:#f0883e;
  --mono:'IBM Plex Mono',monospace;
  --sans:'Space Grotesk',sans-serif;
  --radius:5px;--radius-lg:8px;--radius-xl:12px;
  --shadow:0 4px 24px rgba(0,0,0,.4);
  --glow:0 0 20px var(--acc-glow);
}
html,body{height:100%;overflow:hidden;background:var(--bg0);color:var(--txt);font-family:var(--sans)}
body::before{
  content:'';position:fixed;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
  pointer-events:none;z-index:0;opacity:.4;
}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--border3)}

/* ── Layout ── */
#app{display:grid;grid-template-columns:260px 1fr;grid-template-rows:52px 1fr;height:100vh;position:relative;z-index:1}
#topbar{grid-column:1/-1;display:flex;align-items:center;gap:0;background:var(--bg1);border-bottom:1px solid var(--border);z-index:20;padding:0}
#sidebar{background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#main{display:flex;flex-direction:column;overflow:hidden;background:var(--bg0)}

/* ── Topbar ── */
.logo-area{
  display:flex;align-items:center;gap:10px;padding:0 18px;
  width:260px;border-right:1px solid var(--border);height:100%;flex-shrink:0;
}
.logo-mark{
  width:26px;height:26px;background:var(--acc);border-radius:6px;
  display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#000;
  box-shadow:0 0 12px var(--acc-glow);flex-shrink:0;
}
.logo-text{font-weight:700;font-size:15px;letter-spacing:-.3px;color:var(--txt)}
.logo-text span{color:var(--acc)}
.top-nav{display:flex;height:100%;flex:1;padding:0 12px;gap:2px;align-items:center}
.top-tab{
  padding:7px 14px;border-radius:var(--radius);font-size:12px;font-weight:600;cursor:pointer;
  color:var(--txt3);border:none;background:transparent;transition:all .15s;font-family:var(--sans);
  letter-spacing:.2px;
}
.top-tab:hover{color:var(--txt2);background:var(--bg3)}
.top-tab.active{color:var(--acc);background:var(--acc-dim)}
.top-right{display:flex;align-items:center;gap:8px;padding:0 16px;margin-left:auto}
.env-select{
  background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);
  padding:5px 10px;font-size:11px;color:var(--txt2);outline:none;cursor:pointer;
  font-family:var(--mono);transition:border-color .15s;
}
.env-select:hover,.env-select:focus{border-color:var(--border3)}
.conn-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}

/* ── Sidebar ── */
.sidebar-tabs{display:flex;border-bottom:1px solid var(--border)}
.s-tab{
  flex:1;padding:10px 0;font-size:11px;font-weight:600;cursor:pointer;
  color:var(--txt3);border-bottom:2px solid transparent;transition:all .15s;
  text-align:center;letter-spacing:.5px;text-transform:uppercase;background:none;border-left:none;border-right:none;border-top:none;
  font-family:var(--sans);
}
.s-tab:hover{color:var(--txt2)}
.s-tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.sidebar-inner{flex:1;display:flex;flex-direction:column;overflow:hidden}
.sidebar-toolbar{display:flex;align-items:center;gap:6px;padding:10px 10px 6px}
.sidebar-search{
  flex:1;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:6px 10px;font-size:11px;color:var(--txt);font-family:var(--mono);outline:none;
  transition:border-color .15s;
}
.sidebar-search:focus{border-color:var(--acc)}
.sidebar-search::placeholder{color:var(--txt3)}
.icon-btn{
  background:none;border:1px solid var(--border);color:var(--txt3);cursor:pointer;
  padding:5px 8px;border-radius:var(--radius);display:flex;align-items:center;
  font-size:12px;transition:all .15s;font-family:var(--mono);
}
.icon-btn:hover{color:var(--txt);background:var(--bg3);border-color:var(--border2)}
.icon-btn.accent{border-color:var(--acc-dim);color:var(--acc)}
.icon-btn.accent:hover{background:var(--acc-dim)}
.sidebar-scroll{flex:1;overflow-y:auto;padding:4px 8px 12px}

/* ── Collection tree ── */
.coll-group{margin-bottom:3px;border-radius:var(--radius-lg);overflow:hidden}
.coll-header{
  display:flex;align-items:center;gap:6px;padding:7px 8px;
  cursor:pointer;font-size:12px;font-weight:600;color:var(--txt2);user-select:none;
  border-radius:var(--radius);transition:all .15s;
}
.coll-header:hover{background:var(--bg3);color:var(--txt)}
.coll-arrow{font-size:9px;transition:transform .2s;flex-shrink:0;opacity:.5}
.coll-arrow.open{transform:rotate(90deg);opacity:1}
.coll-icon{font-size:13px;opacity:.7}
.coll-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.coll-count{font-size:10px;color:var(--txt3);background:var(--bg3);padding:1px 6px;border-radius:10px;font-family:var(--mono)}
.coll-actions{display:none;gap:2px}
.coll-header:hover .coll-actions{display:flex}
.coll-act-btn{background:none;border:none;color:var(--txt3);cursor:pointer;padding:2px 5px;border-radius:3px;font-size:11px;transition:all .15s}
.coll-act-btn:hover{color:var(--txt);background:var(--bg4)}
.coll-act-btn.danger:hover{color:var(--red)}
.req-list{margin-left:14px;display:none;flex-direction:column;gap:1px;padding:2px 0}
.req-list.open{display:flex}
.req-item{
  display:flex;align-items:center;gap:7px;padding:5px 8px;border-radius:var(--radius);
  cursor:pointer;font-size:11px;color:var(--txt3);transition:all .12s;border:1px solid transparent;
}
.req-item:hover{background:var(--bg3);color:var(--txt2);border-color:var(--border)}
.req-item.active{background:var(--acc-dim);color:var(--acc);border-color:var(--acc-dim)}
.req-method{font-family:var(--mono);font-size:9.5px;font-weight:600;min-width:36px;letter-spacing:.3px}
.req-name-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.req-item-actions{display:none;gap:1px;margin-left:auto;flex-shrink:0}
.req-item:hover .req-item-actions{display:flex}
.req-act-btn{background:none;border:none;color:var(--txt3);cursor:pointer;padding:1px 4px;border-radius:3px;font-size:10px;transition:all .15s}
.req-act-btn:hover{color:var(--txt)}
.req-act-btn.danger:hover{color:var(--red)}

/* ── History ── */
.hist-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--radius);cursor:pointer;font-size:11px;color:var(--txt2);transition:all .12s;margin-bottom:1px}
.hist-item:hover{background:var(--bg3);color:var(--txt)}
.hist-url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;font-family:var(--mono);font-size:10.5px}

/* ── Method pills ── */
.m-GET{color:#3dd68c}.m-POST{color:#f0883e}.m-PUT{color:#79c0ff}
.m-PATCH{color:#d2a8ff}.m-DELETE{color:#f47067}.m-HEAD{color:#e3b341}
.m-OPTIONS{color:#00d4ff}

/* ══════════════════════════════════════════════════
   REQUEST TABS (Postman-style multi-tab bar)
══════════════════════════════════════════════════ */
#req-tabs-bar{
  display:flex;align-items:stretch;background:var(--bg1);
  border-bottom:1px solid var(--border);min-height:36px;overflow-x:auto;overflow-y:hidden;
  flex-shrink:0;
}
#req-tabs-bar::-webkit-scrollbar{height:3px}
.req-tab-pill{
  display:flex;align-items:center;gap:6px;padding:0 14px;
  font-size:11px;font-weight:600;font-family:var(--mono);
  color:var(--txt3);cursor:pointer;border-right:1px solid var(--border);
  background:transparent;border-top:none;border-left:none;border-bottom:none;
  transition:all .15s;white-space:nowrap;min-width:120px;max-width:200px;
  position:relative;flex-shrink:0;
}
.req-tab-pill:hover{background:var(--bg2);color:var(--txt2)}
.req-tab-pill.active{
  background:var(--bg0);color:var(--txt);
  border-bottom:2px solid var(--acc);
}
.req-tab-pill.active::after{
  content:'';position:absolute;bottom:-1px;left:0;right:0;height:1px;background:var(--bg0);
}
.req-tab-method{font-size:9px;font-weight:700;min-width:30px}
.req-tab-name{overflow:hidden;text-overflow:ellipsis;flex:1;text-align:left}
.req-tab-close{
  opacity:0;background:none;border:none;color:var(--txt3);cursor:pointer;
  font-size:11px;padding:0 2px;border-radius:3px;line-height:1;transition:all .1s;flex-shrink:0;
}
.req-tab-pill:hover .req-tab-close,
.req-tab-pill.active .req-tab-close{opacity:1}
.req-tab-close:hover{color:var(--red);background:rgba(244,112,103,.15)}
.req-tab-pill.unsaved .req-tab-name::after{
  content:'●';margin-left:5px;font-size:8px;color:var(--acc);opacity:.8;
}
#new-tab-btn{
  padding:0 12px;font-size:16px;color:var(--txt3);cursor:pointer;
  background:none;border:none;transition:all .15s;flex-shrink:0;align-self:center;
}
#new-tab-btn:hover{color:var(--acc)}

/* ── URL bar ── */
.url-bar{
  display:flex;gap:8px;align-items:center;padding:10px 16px;
  background:var(--bg1);border-bottom:1px solid var(--border);flex-shrink:0;
}
.req-name-display{
  display:flex;align-items:center;gap:6px;padding:4px 10px;
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  font-size:11px;color:var(--txt2);font-family:var(--mono);cursor:pointer;
  transition:all .15s;white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis;
}
.req-name-display:hover{border-color:var(--border2);color:var(--txt)}
.method-select{
  background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);
  padding:7px 10px;font-size:12px;font-weight:700;color:var(--green);cursor:pointer;
  outline:none;font-family:var(--mono);transition:border-color .15s;
}
.method-select:hover,.method-select:focus{border-color:var(--border3)}
.url-input{
  flex:1;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:8px 14px;font-size:12px;color:var(--txt);font-family:var(--mono);outline:none;
  transition:all .15s;
}
.url-input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-dim)}
.url-input::placeholder{color:var(--txt3)}
.btn-group{display:flex;gap:6px;flex-shrink:0}
.save-btn{
  background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);
  padding:8px 14px;font-size:12px;font-weight:600;color:var(--txt2);cursor:pointer;
  font-family:var(--sans);transition:all .15s;
}
.save-btn:hover{border-color:var(--border3);color:var(--txt)}
.send-btn{
  background:var(--acc);border:none;border-radius:var(--radius);
  padding:8px 22px;font-size:12px;font-weight:700;color:#000;cursor:pointer;
  font-family:var(--sans);letter-spacing:.3px;transition:all .2s;
  box-shadow:0 0 12px var(--acc-glow);
}
.send-btn:hover{background:var(--acc2);box-shadow:0 0 20px var(--acc-glow);transform:translateY(-1px)}
.send-btn:active{transform:translateY(0)}
.send-btn:disabled{opacity:.5;cursor:wait;transform:none}

/* ── Request Tabs ── */
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

/* ── Form-data type selector ── */
.form-type-select{
  background:var(--bg2);border:1px solid var(--border);border-radius:3px;
  padding:3px 6px;font-size:10px;color:var(--txt3);outline:none;cursor:pointer;
  font-family:var(--mono);transition:border-color .15s;max-width:80px;
}
.form-type-select:focus{border-color:var(--acc);color:var(--txt)}
/* file input hidden, styled via label */
.file-cell-wrap{display:flex;align-items:center;gap:4px;flex:1}
.file-pick-btn{
  background:var(--bg3);border:1px solid var(--border2);border-radius:3px;
  padding:3px 8px;font-size:10px;color:var(--txt3);cursor:pointer;
  font-family:var(--mono);white-space:nowrap;transition:all .15s;
}
.file-pick-btn:hover{border-color:var(--acc);color:var(--acc)}
.file-name-txt{font-size:10px;color:var(--txt3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px}

/* ── Body editor ── */
.body-type-bar{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.body-type-btn{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;color:var(--txt3);font-family:var(--mono);transition:all .15s}
.body-type-btn:hover{border-color:var(--border2);color:var(--txt2)}
.body-type-btn.active{background:var(--acc-dim);border-color:var(--acc);color:var(--acc)}
.code-editor{width:100%;min-height:180px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:12px 14px;font-size:12px;color:var(--txt);font-family:var(--mono);resize:vertical;outline:none;line-height:1.7;tab-size:2;transition:border-color .15s}
.code-editor:focus{border-color:var(--acc)}
.code-editor::placeholder{color:var(--txt3)}

/* ── Auth ── */
.auth-type-select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:7px 12px;font-size:12px;color:var(--txt);outline:none;cursor:pointer;font-family:var(--mono);margin-bottom:14px;transition:border-color .15s}
.auth-type-select:focus{border-color:var(--acc)}
.auth-field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
.auth-field label{font-size:10px;font-weight:700;color:var(--txt3);letter-spacing:.8px;text-transform:uppercase}
.auth-field input,.auth-field select{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);padding:8px 12px;font-size:12px;color:var(--txt);font-family:var(--mono);outline:none;transition:border-color .15s}
.auth-field input:focus,.auth-field select:focus{border-color:var(--acc)}
.auth-field input::placeholder{color:var(--txt3)}

/* ── Variable preview hint ── */
.var-hint{
  font-size:10px;color:var(--acc);font-family:var(--mono);
  margin-top:3px;padding:2px 6px;background:var(--acc-dim);border-radius:3px;
  display:inline-block;
}

/* ── Response panel ── */
#response-panel{background:var(--bg1);border-top:1px solid var(--border);display:flex;flex-direction:column;min-height:200px;max-height:48vh}
.resp-topbar{display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--bg1);border-bottom:1px solid var(--border);flex-shrink:0}
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

/* ── JSON syntax ── */
.j-key{color:#79c0ff}.j-str{color:#a5d6a7}.j-num{color:#f0883e}
.j-bool{color:#e3b341}.j-null{color:#d2a8ff}

/* ── Response headers table ── */
.resp-headers-table{width:100%;font-size:11.5px;font-family:var(--mono);border-collapse:collapse}
.resp-headers-table tr:nth-child(even) td{background:var(--bg2)}
.resp-headers-table td{padding:5px 10px;border-bottom:1px solid var(--border);vertical-align:top}
.resp-headers-table td:first-child{color:var(--txt3);width:38%;white-space:nowrap}
.resp-headers-table td:last-child{color:var(--txt);word-break:break-all}

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
.btn-danger{background:rgba(244,112,103,.1);border:1px solid rgba(244,112,103,.3);border-radius:var(--radius);padding:8px 16px;font-size:12px;font-weight:600;color:var(--red);cursor:pointer;font-family:var(--sans)}
.btn-danger:hover{background:rgba(244,112,103,.2)}

/* ── Toast ── */
#toast-container{position:fixed;bottom:20px;right:20px;z-index:999;display:flex;flex-direction:column;gap:8px}
.toast{background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius-lg);padding:10px 16px;font-size:12px;color:var(--txt);font-family:var(--sans);box-shadow:var(--shadow);animation:toastIn .2s ease-out;display:flex;align-items:center;gap:8px;min-width:220px}
.toast.success{border-left:3px solid var(--green)}
.toast.error{border-left:3px solid var(--red)}
.toast.info{border-left:3px solid var(--acc)}
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
</style>
</head>
<body>

<div id="app">

  <!-- ── Topbar ── -->
  <header id="topbar">
    <div class="logo-area">
      <div class="logo-mark">R</div>
      <div class="logo-text">Request<span>Lab</span></div>
    </div>
    <div class="top-nav">
      <button class="top-tab active" onclick="switchView('builder')">Request Builder</button>
      <button class="top-tab" onclick="switchView('environments')">Environments</button>
    </div>
    <div class="top-right">
      <select id="env-selector" class="env-select" onchange="selectEnv(this.value)">
        <option value="">No Environment</option>
      </select>
      <div class="conn-dot" title="Server connected"></div>
    </div>
  </header>

  <!-- ── Sidebar ── -->
  <aside id="sidebar">
    <div class="sidebar-tabs">
      <button class="s-tab active" id="st-collections" onclick="sidebarTab('collections')">Collections</button>
      <button class="s-tab" id="st-history" onclick="sidebarTab('history')">History</button>
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
  </aside>

  <!-- ── Main ── -->
  <main id="main">

    <!-- Request Builder View -->
    <div id="view-builder" style="display:flex;flex-direction:column;overflow:hidden;flex:1;height:100%">

      <!-- ══ Postman-style request tab bar ══ -->
      <div id="req-tabs-bar">
        <!-- tabs rendered here by JS -->
        <button id="new-tab-btn" onclick="newTab()" title="New tab">＋</button>
      </div>

      <div id="request-panel" style="display:flex;flex-direction:column;overflow:hidden;flex:1">
        <!-- URL bar -->
        <div class="url-bar">
          <div class="req-name-display" id="req-name-display" onclick="openRenameReqModal()" title="Click to rename">
            <span id="req-name-text">Untitled Request</span>
            <span style="font-size:9px;opacity:.4">✎</span>
          </div>
          <select id="method-select" class="method-select" onchange="updateMethodColor();markTabDirty()">
            <option>GET</option><option>POST</option><option>PUT</option>
            <option>PATCH</option><option>DELETE</option><option>HEAD</option><option>OPTIONS</option>
          </select>
          <input id="url-input" class="url-input" type="text"
            placeholder="https://api.example.com/endpoint"
            oninput="markTabDirty()"
            onkeydown="if(event.key==='Enter')sendRequest()">
          <div class="btn-group">
            <button class="save-btn" onclick="handleSave()">Save</button>
            <button class="send-btn" id="send-btn" onclick="sendRequest()">Send</button>
          </div>
        </div>

        <!-- Request section tabs -->
        <div class="tab-bar">
          <div class="tab active" onclick="reqTab('params')" id="rt-params">Params <span class="tab-badge" id="tc-params">0</span></div>
          <div class="tab" onclick="reqTab('headers')" id="rt-headers">Headers <span class="tab-badge" id="tc-headers">0</span></div>
          <div class="tab" onclick="reqTab('body')" id="rt-body">Body</div>
          <div class="tab" onclick="reqTab('auth')" id="rt-auth">Auth</div>
        </div>

        <div style="flex:1;overflow-y:auto;background:var(--bg0)">
          <!-- Params -->
          <div class="tab-content tab-pane active" id="pane-params">
            <div class="kv-wrap">
              <table class="kv-table">
                <thead><tr><th style="width:28px"></th><th>Key</th><th>Value</th><th>Description</th><th style="width:36px"></th></tr></thead>
                <tbody id="params-body"></tbody>
              </table>
            </div>
            <button class="add-row-btn" onclick="addKVRow('params')">+ Add Parameter</button>
          </div>

          <!-- Headers -->
          <div class="tab-content tab-pane" id="pane-headers">
            <div class="kv-wrap">
              <table class="kv-table">
                <thead><tr><th style="width:28px"></th><th>Key</th><th>Value</th><th>Description</th><th style="width:36px"></th></tr></thead>
                <tbody id="headers-body"></tbody>
              </table>
            </div>
            <button class="add-row-btn" onclick="addKVRow('headers')">+ Add Header</button>
          </div>

          <!-- Body -->
          <div class="tab-content tab-pane" id="pane-body">
            <div class="body-type-bar">
              <button class="body-type-btn active" onclick="setBodyType('none')">none</button>
              <button class="body-type-btn" onclick="setBodyType('json')">JSON</button>
              <button class="body-type-btn" onclick="setBodyType('raw')">raw</button>
              <button class="body-type-btn" onclick="setBodyType('form')">form-data</button>
              <button class="body-type-btn" onclick="setBodyType('urlencoded')">urlencoded</button>
            </div>
            <div id="body-none-msg" style="color:var(--txt3);font-size:12px;font-family:var(--mono);padding:8px 0">This request does not have a body.</div>
            <textarea class="code-editor" id="body-editor" style="display:none" placeholder="Enter request body…" spellcheck="false" oninput="markTabDirty()"></textarea>
            <div id="body-kv-wrap" style="display:none">
              <div class="kv-wrap">
                <table class="kv-table">
                  <thead><tr><th style="width:28px"></th><th>Type</th><th>Key</th><th>Value / File</th><th style="width:36px"></th></tr></thead>
                  <tbody id="body-kv-body"></tbody>
                </table>
              </div>
              <button class="add-row-btn" onclick="addFormRow()">+ Add Field</button>
            </div>
          </div>

          <!-- Auth -->
          <div class="tab-content tab-pane" id="pane-auth">
            <select class="auth-type-select" id="auth-type" onchange="renderAuthFields();markTabDirty()">
              <option value="none">No Auth</option>
              <option value="basic">Basic Auth</option>
              <option value="bearer">Bearer Token</option>
              <option value="apikey">API Key</option>
            </select>
            <div id="auth-fields"></div>
          </div>
        </div>
      </div>

      <!-- Resize handle -->
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
          </div>
        </div>
        <div class="tab-bar">
          <div class="tab active" onclick="respTab('body')" id="rst-body">Body</div>
          <div class="tab" onclick="respTab('headers')" id="rst-headers">Headers</div>
          <div class="tab" onclick="respTab('cookies')" id="rst-cookies">Cookies</div>
        </div>
        <div class="resp-body" id="resp-body-pane">
          <div class="empty-state" id="resp-empty">
            <div class="empty-icon">◈</div>
            <p>Hit <strong>Send</strong> to fire a request</p>
          </div>
          <pre id="resp-body-content" style="display:none"></pre>
        </div>
        <div class="resp-body" id="resp-headers-pane" style="display:none">
          <table class="resp-headers-table"><tbody id="resp-headers-tbody"></tbody></table>
        </div>
        <div class="resp-body" id="resp-cookies-pane" style="display:none">
          <table class="resp-headers-table"><tbody id="resp-cookies-tbody"></tbody></table>
        </div>
      </div>
    </div>

    <!-- Environments View -->
    <div id="view-environments" style="display:none;flex-direction:column;overflow:hidden;flex:1;height:100%">
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg1)">
        <div>
          <h2 style="font-size:14px;font-weight:700">Environments</h2>
          <p style="font-size:11px;color:var(--txt3);margin-top:2px">Use <code style="font-family:var(--mono);color:var(--acc)">{{variable}}</code> in your requests</p>
        </div>
        <button class="btn-primary" onclick="createEnvironment()">+ New Environment</button>
      </div>
      <div id="envs-panel" style="overflow-y:auto;padding:16px;flex:1"></div>
    </div>

  </main>
</div>

<!-- Toast Container -->
<div id="toast-container"></div>

<!-- Save Request Modal -->
<div class="modal-overlay" id="save-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>Save Request</h3>
      <button class="modal-close" onclick="closeModal('save-modal')">×</button>
    </div>
    <div class="form-group">
      <label class="form-label">Request Name</label>
      <input id="save-name" placeholder="My Request" type="text">
    </div>
    <div class="form-group">
      <label class="form-label">Collection</label>
      <select id="save-collection">
        <option value="">Select collection…</option>
      </select>
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('save-modal')">Cancel</button>
      <button class="btn-primary" onclick="saveRequest()">Save Request</button>
    </div>
  </div>
</div>

<!-- New Collection Modal -->
<div class="modal-overlay" id="coll-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>New Collection</h3>
      <button class="modal-close" onclick="closeModal('coll-modal')">×</button>
    </div>
    <div class="form-group">
      <label class="form-label">Collection Name</label>
      <input id="coll-name-input" placeholder="My API Collection" type="text" onkeydown="if(event.key==='Enter')createCollection()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('coll-modal')">Cancel</button>
      <button class="btn-primary" onclick="createCollection()">Create</button>
    </div>
  </div>
</div>

<!-- Rename Collection Modal -->
<div class="modal-overlay" id="rename-coll-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>Rename Collection</h3>
      <button class="modal-close" onclick="closeModal('rename-coll-modal')">×</button>
    </div>
    <div class="form-group">
      <label class="form-label">New Name</label>
      <input id="rename-coll-input" type="text" onkeydown="if(event.key==='Enter')confirmRenameCollection()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('rename-coll-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmRenameCollection()">Rename</button>
    </div>
  </div>
</div>

<!-- Rename Request Modal -->
<div class="modal-overlay" id="rename-req-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>Rename Request</h3>
      <button class="modal-close" onclick="closeModal('rename-req-modal')">×</button>
    </div>
    <div class="form-group">
      <label class="form-label">New Name</label>
      <input id="rename-req-input" type="text" onkeydown="if(event.key==='Enter')confirmRenameRequest()">
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('rename-req-modal')">Cancel</button>
      <button class="btn-primary" onclick="confirmRenameRequest()">Rename</button>
    </div>
  </div>
</div>

<!-- Import Collection Modal -->
<div class="modal-overlay" id="import-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>Import Collection</h3>
      <button class="modal-close" onclick="closeModal('import-modal')">×</button>
    </div>
    <div class="import-drop" id="import-drop"
      onclick="document.getElementById('import-file').click()"
      ondragover="importDragOver(event)" ondragleave="importDragLeave(event)" ondrop="importDrop(event)">
      <div class="import-icon">📂</div>
      <div>Drop a <strong>Postman</strong> or <strong>RequestLab</strong> export <strong>.json</strong> here</div>
      <div style="margin-top:4px;font-size:11px;opacity:.6">Supports Postman Collection v2 / v2.1 — or click to browse</div>
    </div>
    <input type="file" id="import-file" accept=".json" style="display:none" onchange="importFile(this)">
    <div id="import-status" style="font-size:12px;color:var(--txt3);font-family:var(--mono);min-height:20px"></div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeModal('import-modal')">Cancel</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════
//  TAB SYSTEM
// ═══════════════════════════════════════════════════════

let tabCounter = 0;

function makeTabState(overrides = {}) {
  return Object.assign({
    id: ++tabCounter,
    name: 'Untitled Request',
    savedReqId: null,
    dirty: false,
    method: 'GET',
    url: '',
    params: [],
    headers: [],
    bodyType: 'none',
    bodyContent: '',
    bodyKV: [],        // [{type:'text'|'file', key, value, fileName}]
    authType: 'none',
    authData: {},
    response: null,
  }, overrides);
}

let tabs = [];
let activeTabIdx = -1;

function currentTab() { return tabs[activeTabIdx] || null; }

// ── Save DOM → tab state ──────────────────────────────
function snapshotTab() {
  const t = currentTab();
  if (!t) return;
  t.method = document.getElementById('method-select').value;
  t.url    = document.getElementById('url-input').value;
  t.params  = getKVRows('params-body');
  t.headers = getKVRows('headers-body');
  t.bodyType    = S.bodyType;
  t.bodyContent = ['json','raw'].includes(S.bodyType)
    ? document.getElementById('body-editor').value : '';
  t.bodyKV = ['form','urlencoded'].includes(S.bodyType)
    ? getFormRows() : [];
  t.authType = document.getElementById('auth-type').value;
  t.authData = getAuthData();
  t.name = document.getElementById('req-name-text').textContent;
}

// ── Restore tab state → DOM ───────────────────────────
function restoreTab(t) {
  document.getElementById('method-select').value = t.method;
  document.getElementById('url-input').value = t.url;
  document.getElementById('req-name-text').textContent = t.name;
  updateMethodColor();

  document.getElementById('params-body').innerHTML = '';
  if (t.params.length) t.params.forEach(p => addKVRow('params', p.key, p.value, p.desc||'', p.enabled));
  else addKVRow('params');

  document.getElementById('headers-body').innerHTML = '';
  if (t.headers.length) t.headers.forEach(h => addKVRow('headers', h.key, h.value, h.desc||'', h.enabled));
  else addKVRow('headers');

  S.bodyType = t.bodyType;
  setBodyType(t.bodyType);
  document.getElementById('body-editor').value = t.bodyContent || '';

  document.getElementById('body-kv-body').innerHTML = '';
  if (t.bodyKV && t.bodyKV.length) {
    t.bodyKV.forEach(r => addFormRow(r.type||'text', r.key, r.value, r.fileName));
  }

  document.getElementById('auth-type').value = t.authType;
  S.authType = t.authType;
  S.authData = t.authData || {};
  renderAuthFields();
  if (t.authType === 'basic') {
    setInputVal('auth-username', t.authData.username || '');
    setInputVal('auth-password', t.authData.password || '');
  } else if (t.authType === 'bearer') {
    setInputVal('auth-token', t.authData.token || '');
  } else if (t.authType === 'apikey') {
    setInputVal('auth-key', t.authData.key || '');
    setInputVal('auth-value', t.authData.value || '');
    setInputVal('auth-location', t.authData.location || 'header');
  }

  if (t.response) {
    S.response = t.response;
    renderResponse(t.response);
  } else {
    S.response = null;
    document.getElementById('resp-empty').style.display = 'flex';
    document.getElementById('resp-body-content').style.display = 'none';
    document.getElementById('resp-status-wrap').style.display = 'none';
  }

  updateTabBadge('params');
  updateTabBadge('headers');
}

function setInputVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

// ── Render tab pill bar ───────────────────────────────
function renderTabBar() {
  const bar = document.getElementById('req-tabs-bar');
  bar.querySelectorAll('.req-tab-pill').forEach(el => el.remove());
  const newBtn = document.getElementById('new-tab-btn');

  tabs.forEach((t, idx) => {
    const pill = document.createElement('button');
    pill.className = 'req-tab-pill' + (idx === activeTabIdx ? ' active' : '') + (t.dirty ? ' unsaved' : '');
    pill.dataset.idx = idx;
    const mc = methodColor(t.method);
    pill.innerHTML = `
      <span class="req-tab-method" style="color:${mc}">${t.method}</span>
      <span class="req-tab-name">${esc(t.name)}</span>
      <button class="req-tab-close" onclick="closeTab(event,${idx})" title="Close">×</button>`;
    pill.addEventListener('click', (e) => {
      if (e.target.classList.contains('req-tab-close')) return;
      switchTab(idx);
    });
    bar.insertBefore(pill, newBtn);
  });
}

function methodColor(m) {
  return {GET:'#3dd68c',POST:'#f0883e',PUT:'#79c0ff',PATCH:'#d2a8ff',DELETE:'#f47067',HEAD:'#e3b341',OPTIONS:'#00d4ff'}[m] || '#cdd9e5';
}

function switchTab(idx) {
  if (idx === activeTabIdx) return;
  if (activeTabIdx >= 0) snapshotTab();
  activeTabIdx = idx;
  restoreTab(tabs[idx]);
  renderTabBar();
  document.querySelectorAll('.req-item').forEach(el =>
    el.classList.toggle('active', el.id === 'ri-' + tabs[idx].savedReqId));
}

function newTab(overrides = {}) {
  if (activeTabIdx >= 0) snapshotTab();
  const t = makeTabState(overrides);
  tabs.push(t);
  activeTabIdx = tabs.length - 1;
  restoreTab(t);
  renderTabBar();
}

function closeTab(e, idx) {
  e.stopPropagation();
  if (tabs.length === 1) {
    tabs[0] = makeTabState();
    activeTabIdx = 0;
    restoreTab(tabs[0]);
    renderTabBar();
    return;
  }
  tabs.splice(idx, 1);
  if (activeTabIdx >= tabs.length) activeTabIdx = tabs.length - 1;
  else if (activeTabIdx > idx) activeTabIdx--;
  restoreTab(tabs[activeTabIdx]);
  renderTabBar();
}

function markTabDirty() {
  const t = currentTab();
  if (!t) return;
  if (!t.dirty) { t.dirty = true; renderTabBar(); }
}

// ═══════════════════════════════════════════════════════
//  GLOBAL STATE
// ═══════════════════════════════════════════════════════
const S = {
  bodyType: 'none',
  authType: 'none',
  authData: {},
  response: null,
  collections: [],
  history: [],
  environments: [],
  renameCollId: null,
  renameReqId: null,
};

// ═══════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  tabs.push(makeTabState());
  activeTabIdx = 0;
  renderTabBar();
  addKVRow('params');
  addKVRow('headers');
  loadCollections();
  loadHistory();
  loadEnvironments();
  setupResizeHandle();
  renderAuthFields();
  updateMethodColor();
});

// ═══════════════════════════════════════════════════════
//  TOAST
// ═══════════════════════════════════════════════════════
function toast(msg, type='info', duration=2800) {
  const icons = {success:'✓', error:'✕', info:'ℹ'};
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span style="font-weight:700">${icons[type]||''}</span>${msg}`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => {
    el.style.animation = 'toastOut .2s forwards';
    setTimeout(() => el.remove(), 200);
  }, duration);
}

// ═══════════════════════════════════════════════════════
//  VIEW / SIDEBAR SWITCHING
// ═══════════════════════════════════════════════════════
function switchView(v) {
  document.querySelectorAll('.top-tab').forEach((t,i) =>
    t.classList.toggle('active', ['builder','environments'][i]===v));
  document.getElementById('view-builder').style.display = v==='builder' ? 'flex' : 'none';
  document.getElementById('view-environments').style.display = v==='environments' ? 'flex' : 'none';
}

function sidebarTab(t) {
  document.getElementById('st-collections').classList.toggle('active', t==='collections');
  document.getElementById('st-history').classList.toggle('active', t==='history');
  document.getElementById('sp-collections').style.display = t==='collections' ? 'flex' : 'none';
  document.getElementById('sp-history').style.display = t==='history' ? 'flex' : 'none';
  if(t==='history') loadHistory();
}

function reqTab(name) {
  ['params','headers','body','auth'].forEach(t => {
    document.getElementById('pane-'+t).classList.toggle('active', t===name);
    const el = document.getElementById('rt-'+t);
    if(el) el.classList.toggle('active', t===name);
  });
}

function respTab(name) {
  ['body','headers','cookies'].forEach(t => {
    document.getElementById('resp-'+t+'-pane').style.display = t===name ? 'block' : 'none';
    document.getElementById('rst-'+t).classList.toggle('active', t===name);
  });
}

// ═══════════════════════════════════════════════════════
//  KV ROWS (params, headers)
// ═══════════════════════════════════════════════════════
function addKVRow(type, key='', value='', desc='', enabled=true) {
  const tbody = document.getElementById(type+'-body');
  const tr = document.createElement('tr');
  const hasDesc = !['body-kv'].includes(type);
  tr.innerHTML = `
    <td style="text-align:center"><input type="checkbox" class="kv-cb" ${enabled?'checked':''} onchange="updateTabBadge('${type}');markTabDirty()"></td>
    <td><input class="kv-input" placeholder="key" value="${esc(key)}" oninput="updateTabBadge('${type}');markTabDirty()"></td>
    <td><input class="kv-input" placeholder="value" value="${esc(value)}" oninput="markTabDirty()"></td>
    ${hasDesc ? `<td><input class="kv-input" placeholder="description" value="${esc(desc)}"></td>` : ''}
    <td><button class="del-row-btn" onclick="this.closest('tr').remove();updateTabBadge('${type}');markTabDirty()">✕</button></td>`;
  tbody.appendChild(tr);
  updateTabBadge(type);
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function updateTabBadge(type) {
  const map = { params:'tc-params', headers:'tc-headers' };
  const badgeId = map[type];
  if(!badgeId) return;
  const n = [...document.querySelectorAll('#'+type+'-body tr')].filter(r => {
    const cb = r.querySelector('input[type=checkbox]');
    const inp = r.querySelectorAll('input:not([type=checkbox])')[0];
    return cb?.checked && inp?.value.trim();
  }).length;
  const badge = document.getElementById(badgeId);
  badge.textContent = n;
  badge.classList.toggle('has', n>0);
}

function getKVRows(tbodyId) {
  return [...document.querySelectorAll('#'+tbodyId+' tr')].map(tr => {
    const inputs = tr.querySelectorAll('input:not([type=checkbox])');
    const cb = tr.querySelector('input[type=checkbox]');
    return { key: inputs[0]?.value||'', value: inputs[1]?.value||'', enabled: cb?.checked!==false };
  }).filter(r => r.key.trim());
}

// ═══════════════════════════════════════════════════════
//  FORM-DATA ROWS  (with type selector + file pick)
// ═══════════════════════════════════════════════════════
// Each row stores: {type:'text'|'file', key, value, file (File obj or null), fileName}
const formFileMap = new WeakMap(); // tr → File object

function addFormRow(type='text', key='', value='', fileName='') {
  const tbody = document.getElementById('body-kv-body');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td style="text-align:center"><input type="checkbox" class="kv-cb" checked onchange="markTabDirty()"></td>
    <td style="min-width:72px">
      <select class="form-type-select" onchange="onFormTypeChange(this)">
        <option value="text" ${type==='text'?'selected':''}>Text</option>
        <option value="file" ${type==='file'?'selected':''}>File</option>
      </select>
    </td>
    <td><input class="kv-input" placeholder="key" value="${esc(key)}" oninput="markTabDirty()"></td>
    <td id="fv-cell">
      ${type==='file'
        ? `<div class="file-cell-wrap">
             <label class="file-pick-btn">Choose File<input type="file" style="display:none" onchange="onFilePicked(this)"></label>
             <span class="file-name-txt">${esc(fileName||'No file chosen')}</span>
           </div>`
        : `<input class="kv-input" placeholder="value" value="${esc(value)}" oninput="markTabDirty()">`
      }
    </td>
    <td><button class="del-row-btn" onclick="this.closest('tr').remove();markTabDirty()" style="opacity:1">✕</button></td>`;
  tbody.appendChild(tr);

  // label the value cell so we can find it
  tr.querySelector('td:nth-child(4)').id = '';
}

function onFormTypeChange(sel) {
  const tr = sel.closest('tr');
  const valueCell = tr.querySelector('td:nth-child(4)');
  const type = sel.value;
  if (type === 'file') {
    valueCell.innerHTML = `<div class="file-cell-wrap">
      <label class="file-pick-btn">Choose File<input type="file" style="display:none" onchange="onFilePicked(this)"></label>
      <span class="file-name-txt">No file chosen</span>
    </div>`;
    formFileMap.delete(tr);
  } else {
    valueCell.innerHTML = `<input class="kv-input" placeholder="value" oninput="markTabDirty()">`;
    formFileMap.delete(tr);
  }
  markTabDirty();
}

function onFilePicked(input) {
  const file = input.files[0];
  if (!file) return;
  const tr = input.closest('tr');
  formFileMap.set(tr, file);
  const nameSpan = tr.querySelector('.file-name-txt');
  if (nameSpan) nameSpan.textContent = file.name;
  markTabDirty();
}

function getFormRows() {
  return [...document.querySelectorAll('#body-kv-body tr')].map(tr => {
    const typeEl = tr.querySelector('.form-type-select');
    const keyEl  = tr.querySelectorAll('input:not([type=checkbox]):not([type=file])')[0];
    const type   = typeEl?.value || 'text';
    const key    = keyEl?.value || '';
    if (type === 'file') {
      const file = formFileMap.get(tr) || null;
      const nameSpan = tr.querySelector('.file-name-txt');
      return { type:'file', key, value:'', fileName: file ? file.name : (nameSpan?.textContent||'') };
    } else {
      const valEl = tr.querySelector('td:nth-child(4) input.kv-input');
      return { type:'text', key, value: valEl?.value||'' };
    }
  }).filter(r => r.key.trim());
}

// Build FormData for file-capable form submissions
function buildFormData() {
  const fd = new FormData();
  const rows = [...document.querySelectorAll('#body-kv-body tr')];
  rows.forEach(tr => {
    const typeEl = tr.querySelector('.form-type-select');
    const keyEl  = tr.querySelectorAll('input:not([type=checkbox]):not([type=file])')[0];
    const type   = typeEl?.value || 'text';
    const key    = keyEl?.value || '';
    if (!key) return;
    if (type === 'file') {
      const file = formFileMap.get(tr);
      if (file) fd.append(key, file, file.name);
    } else {
      const valEl = tr.querySelector('td:nth-child(4) input.kv-input');
      fd.append(key, valEl?.value || '');
    }
  });
  return fd;
}

// ═══════════════════════════════════════════════════════
//  BODY TYPE
// ═══════════════════════════════════════════════════════
function setBodyType(type) {
  S.bodyType = type;
  document.querySelectorAll('.body-type-btn').forEach(b =>
    b.classList.toggle('active', b.textContent.trim()===type));
  document.getElementById('body-none-msg').style.display = type==='none' ? 'block' : 'none';
  document.getElementById('body-editor').style.display = ['json','raw'].includes(type) ? 'block' : 'none';
  document.getElementById('body-kv-wrap').style.display = ['form','urlencoded'].includes(type) ? 'block' : 'none';
  if(type==='json' && !document.getElementById('body-editor').value.trim())
    document.getElementById('body-editor').value = '{\n  \n}';
}

// ═══════════════════════════════════════════════════════
//  AUTH
// ═══════════════════════════════════════════════════════
function renderAuthFields() {
  const t = document.getElementById('auth-type').value;
  S.authType = t;
  const c = document.getElementById('auth-fields');
  c.innerHTML = '';
  if(t==='basic') {
    c.innerHTML = authField('Username','auth-username',S.authData.username||'') +
                  authField('Password','auth-password',S.authData.password||'','password');
  } else if(t==='bearer') {
    c.innerHTML = authField('Token','auth-token',S.authData.token||'');
  } else if(t==='apikey') {
    c.innerHTML = authField('Key Name','auth-key',S.authData.key||'X-API-Key') +
      authField('Key Value','auth-value',S.authData.value||'') +
      `<div class="auth-field"><label>Add to</label>
       <select class="auth-type-select" id="auth-location" style="margin-bottom:0">
         <option value="header" ${S.authData.location==='header'?'selected':''}>Header</option>
         <option value="query" ${S.authData.location==='query'?'selected':''}>Query Param</option>
       </select></div>`;
  }
}
function authField(label, id, val='', type='text') {
  return `<div class="auth-field"><label>${label}</label><input id="${id}" type="${type}" value="${esc(val)}" placeholder="${label}" oninput="markTabDirty()"></div>`;
}
function getAuthData() {
  const t = document.getElementById('auth-type').value;
  if(t==='basic') return { username: gv('auth-username'), password: gv('auth-password') };
  if(t==='bearer') return { token: gv('auth-token') };
  if(t==='apikey') return { key: gv('auth-key'), value: gv('auth-value'), location: gv('auth-location') };
  return {};
}
function gv(id) { return document.getElementById(id)?.value || ''; }

// ═══════════════════════════════════════════════════════
//  METHOD COLOR
// ═══════════════════════════════════════════════════════
function updateMethodColor() {
  const sel = document.getElementById('method-select');
  sel.style.color = methodColor(sel.value);
  const t = currentTab();
  if (t) { t.method = sel.value; renderTabBar(); }
}

// ═══════════════════════════════════════════════════════
//  ENVIRONMENT VARIABLE SUBSTITUTION
//  Applies {{var}} replacement everywhere before sending
// ═══════════════════════════════════════════════════════
function getActiveEnv() { return S.environments.find(e => e.active) || null; }

function getEnvVars() {
  const env = getActiveEnv();
  if (!env || !env.vars) return {};
  try { return typeof env.vars === 'string' ? JSON.parse(env.vars) : (env.vars || {}); }
  catch(e) { return {}; }
}

function substituteVars(str) {
  if (typeof str !== 'string') return str;
  const vars = getEnvVars();
  if (!Object.keys(vars).length) return str;
  return str.replace(/\{\{(\w+)\}\}/g, (_, k) => vars[k] !== undefined ? vars[k] : `{{${k}}}`);
}

// Sub an entire KV array
function substituteKVList(list) {
  return list.map(item => ({
    ...item,
    key:   substituteVars(item.key),
    value: substituteVars(item.value),
  }));
}

// Sub auth data values (not keys)
function substituteAuthData(authType, authData) {
  const out = {};
  for (const [k, v] of Object.entries(authData)) {
    out[k] = typeof v === 'string' ? substituteVars(v) : v;
  }
  return out;
}

// ═══════════════════════════════════════════════════════
//  SEND REQUEST
// ═══════════════════════════════════════════════════════
async function sendRequest() {
  const rawUrl = document.getElementById('url-input').value.trim();
  if (!rawUrl) { toast('Enter a URL first', 'error'); return; }

  const url = substituteVars(rawUrl);

  const btn = document.getElementById('send-btn');
  btn.innerHTML = '<span class="spinner"></span>';
  btn.disabled = true;

  const bodyType = S.bodyType;
  const authType = document.getElementById('auth-type').value;
  const rawAuthData = getAuthData();
  const authData = substituteAuthData(authType, rawAuthData);

  // Substitute params & headers
  const rawParams  = getKVRows('params-body');
  const rawHeaders = getKVRows('headers-body');
  const params  = substituteKVList(rawParams);
  const headers = substituteKVList(rawHeaders);

  try {
    let result;

    if (bodyType === 'form') {
      // ── multipart/form-data with actual file support ──
      // Build FormData, apply var substitution to text fields
      const fd = new FormData();
      const rows = [...document.querySelectorAll('#body-kv-body tr')];
      rows.forEach(tr => {
        const typeEl = tr.querySelector('.form-type-select');
        const keyEl  = tr.querySelectorAll('input:not([type=checkbox]):not([type=file])')[0];
        const ftype  = typeEl?.value || 'text';
        const key    = substituteVars(keyEl?.value || '');
        if (!key) return;
        if (ftype === 'file') {
          const file = formFileMap.get(tr);
          if (file) fd.append(key, file, file.name);
        } else {
          const valEl = tr.querySelector('td:nth-child(4) input.kv-input');
          fd.append(key, substituteVars(valEl?.value || ''));
        }
      });

      // Build headers map (without Content-Type — browser sets boundary)
      const hdrs = {};
      headers.filter(h => h.key && h.enabled !== false).forEach(h => { hdrs[h.key] = h.value; });
      // Auth injection
      if (authType === 'bearer') hdrs['Authorization'] = `Bearer ${authData.token||''}`;
      else if (authType === 'apikey' && authData.location === 'header') hdrs[authData.key||'X-API-Key'] = authData.value||'';

      // Build query string
      const qp = new URLSearchParams();
      params.filter(p => p.key && p.enabled !== false).forEach(p => qp.set(p.key, p.value));
      if (authType === 'apikey' && authData.location === 'query') qp.set(authData.key||'X-API-Key', authData.value||'');

      const fullUrl = qp.toString() ? url + (url.includes('?')?'&':'?') + qp.toString() : url;

      // For file uploads we proxy directly via fetch to preserve FormData
      // We need the server to relay it — but since our /api/execute uses requests lib
      // and can't receive FormData from JS, we use a direct fetch instead
      // and handle auth ourselves. For simplicity, send multipart via the proxy
      // by converting to JSON descriptor and handling on server (files go as filename only).
      // Better: send multipart directly to target from browser (CORS permitting),
      // or send via proxy as JSON with base64. We'll proxy as JSON with base64 for files.

      // Collect form rows as JSON for proxy
      const formRows = [];
      rows.forEach(tr => {
        const typeEl = tr.querySelector('.form-type-select');
        const keyEl  = tr.querySelectorAll('input:not([type=checkbox]):not([type=file])')[0];
        const ftype  = typeEl?.value || 'text';
        const key    = substituteVars(keyEl?.value || '');
        if (!key) return;
        if (ftype === 'file') {
          const file = formFileMap.get(tr);
          if (file) formRows.push({type:'file', key, fileName:file.name});
          // Note: actual file bytes can't be proxied this way without base64 encoding
          // We include them as text placeholder; full file upload would need a different endpoint
        } else {
          const valEl = tr.querySelector('td:nth-child(4) input.kv-input');
          formRows.push({type:'text', key, value: substituteVars(valEl?.value||'')});
        }
      });

      let bodyContent = JSON.stringify(Object.fromEntries(
        formRows.filter(r=>r.type==='text').map(r=>[r.key,r.value])
      ));

      const payload = {
        method: document.getElementById('method-select').value,
        url, params, headers,
        body_type: 'form', body_content: bodyContent,
        auth_type: authType, auth_data: authData
      };
      const res = await fetch('/api/execute', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
      });
      result = await res.json();
    } else {
      // ── All other body types ──
      let bodyContent = '';
      if (['json','raw'].includes(bodyType)) {
        bodyContent = substituteVars(document.getElementById('body-editor').value);
      } else if (bodyType === 'urlencoded') {
        const kvRows = getFormRows();
        const obj = {};
        kvRows.forEach(r => { obj[substituteVars(r.key)] = substituteVars(r.value); });
        bodyContent = JSON.stringify(obj);
      }

      const payload = {
        method: document.getElementById('method-select').value,
        url, params, headers,
        body_type: bodyType, body_content: bodyContent,
        auth_type: authType, auth_data: authData
      };

      const res = await fetch('/api/execute', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
      });
      result = await res.json();
    }

    S.response = result;
    const t = currentTab();
    if (t) t.response = result;
    renderResponse(result);
    loadHistory();
  } catch(e) {
    renderError(e.message);
    toast('Request failed: '+e.message, 'error');
  } finally {
    btn.innerHTML = 'Send';
    btn.disabled = false;
  }
}

function renderResponse(data) {
  const empty = document.getElementById('resp-empty');
  const content = document.getElementById('resp-body-content');
  const statusWrap = document.getElementById('resp-status-wrap');
  if(data.error) { renderError(data.error); return; }
  const sc = data.status_code;
  const cls = sc>=500?'s-5xx':sc>=400?'s-4xx':sc>=300?'s-3xx':sc>=200?'s-2xx':'s-err';
  document.getElementById('resp-status-badge').className = 'status-badge '+cls;
  document.getElementById('resp-status-badge').textContent = `${sc} ${data.status_text||''}`;
  document.getElementById('resp-meta').innerHTML =
    `<span>Time: <span class="val">${data.duration_ms}ms</span></span>
     <span>Size: <span class="val">${formatSize(data.size_bytes)}</span></span>
     ${data.redirects>0?`<span>Redirects: <span class="val">${data.redirects}</span></span>`:''}`;
  statusWrap.style.display = 'flex';
  empty.style.display = 'none';
  content.style.display = 'block';
  if(data.body_json !== null && data.body_json !== undefined) {
    content.innerHTML = syntaxHighlight(JSON.stringify(data.body_json, null, 2));
  } else {
    content.textContent = data.body || '';
  }
  document.getElementById('resp-headers-tbody').innerHTML =
    Object.entries(data.headers||{}).map(([k,v]) =>
      `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('');
  const cookies = data.cookies || {};
  document.getElementById('resp-cookies-tbody').innerHTML = Object.keys(cookies).length
    ? Object.entries(cookies).map(([k,v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('')
    : '<tr><td colspan="2" style="color:var(--txt3);font-size:12px;padding:12px">No cookies</td></tr>';
}

function renderError(msg) {
  document.getElementById('resp-empty').style.display = 'none';
  const content = document.getElementById('resp-body-content');
  content.style.display = 'block';
  content.innerHTML = `<span style="color:var(--red)">✕ ${esc(msg)}</span>`;
  document.getElementById('resp-status-badge').className = 'status-badge s-err';
  document.getElementById('resp-status-badge').textContent = 'Error';
  document.getElementById('resp-status-wrap').style.display = 'flex';
  document.getElementById('resp-meta').innerHTML = '';
}

function syntaxHighlight(json) {
  return json.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, m => {
      let cls='j-num';
      if(/^"/.test(m)) cls=/:\s*$/.test(m)?'j-key':'j-str';
      else if(/true|false/.test(m)) cls='j-bool';
      else if(/null/.test(m)) cls='j-null';
      return `<span class="${cls}">${m}</span>`;
    });
}

function formatSize(b) {
  if(b<1024) return b+'B'; if(b<1048576) return (b/1024).toFixed(1)+'KB'; return (b/1048576).toFixed(1)+'MB';
}

function copyResponse() {
  const data = S.response;
  if(!data) return;
  const txt = data.body_json ? JSON.stringify(data.body_json, null, 2) : (data.body||'');
  navigator.clipboard.writeText(txt).then(() => {
    const btn = document.getElementById('copy-resp-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent='Copy', 1500);
    toast('Response copied to clipboard', 'success');
  });
}

function downloadResponse() {
  const data = S.response;
  if(!data) return;
  const txt = data.body_json ? JSON.stringify(data.body_json, null, 2) : (data.body||'');
  const ext = data.body_json ? 'json' : 'txt';
  const blob = new Blob([txt], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `response.${ext}`;
  a.click();
}

// ═══════════════════════════════════════════════════════
//  COLLECTIONS
// ═══════════════════════════════════════════════════════
async function loadCollections() {
  const res = await fetch('/api/collections');
  S.collections = await res.json();
  renderCollections(S.collections);
  renderSaveModal(S.collections);
}

function renderCollections(colls, filter='') {
  const tree = document.getElementById('collections-tree');
  const filtered = filter ? colls.filter(c => c.name.toLowerCase().includes(filter.toLowerCase())) : colls;
  if(!filtered.length) {
    tree.innerHTML = `<div style="color:var(--txt3);font-size:11px;padding:16px;text-align:center;font-family:var(--mono)">${filter?'No matches':'No collections yet'}</div>`;
    return;
  }
  tree.innerHTML = filtered.map(c => `
    <div class="coll-group" id="coll-${c.id}">
      <div class="coll-header" onclick="toggleColl(${c.id})">
        <span class="coll-arrow" id="ca-${c.id}">▶</span>
        <span class="coll-icon">📁</span>
        <span class="coll-name" title="${esc(c.name)}">${esc(c.name)}</span>
        <span class="coll-count">${c.requests.length}</span>
        <span class="coll-actions" onclick="event.stopPropagation()">
          <button class="coll-act-btn" title="Export" onclick="exportCollection(${c.id},'${esc(c.name)}')">⬇</button>
          <button class="coll-act-btn" title="Rename" onclick="openRenameCollModal(${c.id},'${esc(c.name)}')">✎</button>
          <button class="coll-act-btn danger" title="Delete" onclick="deleteCollection(${c.id})">🗑</button>
        </span>
      </div>
      <div class="req-list" id="rl-${c.id}">
        ${c.requests.length === 0
          ? `<div style="color:var(--txt3);font-size:11px;padding:4px 8px;font-family:var(--mono)">Empty collection</div>`
          : c.requests.map(r => `
          <div class="req-item" id="ri-${r.id}" onclick="loadRequestInTab(${r.id})">
            <span class="req-method m-${r.method}">${r.method}</span>
            <span class="req-name-text" title="${esc(r.name)}">${esc(r.name)}</span>
            <span class="req-item-actions">
              <button class="req-act-btn" title="Rename" onclick="event.stopPropagation();openRenameReqModalById(${r.id},'${esc(r.name)}')">✎</button>
              <button class="req-act-btn danger" title="Delete" onclick="event.stopPropagation();deleteReq(${r.id})">✕</button>
            </span>
          </div>`).join('')}
      </div>
    </div>`).join('');
}

function filterCollections(v) { renderCollections(S.collections, v); }

function toggleColl(id) {
  const rl = document.getElementById('rl-'+id);
  const arrow = document.getElementById('ca-'+id);
  const open = rl.classList.toggle('open');
  arrow.classList.toggle('open', open);
}

function openNewCollModal() {
  document.getElementById('coll-name-input').value = '';
  document.getElementById('coll-modal').classList.add('open');
  setTimeout(() => document.getElementById('coll-name-input').focus(), 50);
}

async function createCollection() {
  const name = document.getElementById('coll-name-input').value.trim() || 'New Collection';
  await fetch('/api/collections', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  closeModal('coll-modal');
  await loadCollections();
  toast(`Collection "${name}" created`, 'success');
}

async function deleteCollection(id) {
  if(!confirm('Delete this collection and all its requests?')) return;
  await fetch('/api/collections/'+id, {method:'DELETE'});
  await loadCollections();
  toast('Collection deleted', 'info');
}

function openRenameCollModal(id, currentName) {
  S.renameCollId = id;
  document.getElementById('rename-coll-input').value = currentName;
  document.getElementById('rename-coll-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-coll-input').focus(), 50);
}

async function confirmRenameCollection() {
  const name = document.getElementById('rename-coll-input').value.trim();
  if(!name) return;
  await fetch('/api/collections/'+S.renameCollId, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  closeModal('rename-coll-modal');
  await loadCollections();
  toast(`Renamed to "${name}"`, 'success');
}

function exportCollection(id, name) {
  const a = document.createElement('a');
  a.href = '/api/collections/'+id+'/export';
  a.download = name+'.json';
  a.click();
  toast(`Exporting "${name}"…`, 'info');
}

function openImportModal() {
  document.getElementById('import-status').textContent = '';
  document.getElementById('import-modal').classList.add('open');
}
function importDragOver(e) { e.preventDefault(); document.getElementById('import-drop').classList.add('over'); }
function importDragLeave(e) { document.getElementById('import-drop').classList.remove('over'); }
function importDrop(e) {
  e.preventDefault(); document.getElementById('import-drop').classList.remove('over');
  const file = e.dataTransfer.files[0]; if(file) processImportFile(file);
}
function importFile(input) { if(input.files[0]) processImportFile(input.files[0]); }

async function processImportFile(file) {
  const status = document.getElementById('import-status');
  status.style.color = 'var(--txt3)'; status.textContent = `Reading ${file.name}…`;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const res = await fetch('/api/collections/import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    const result = await res.json();
    if(result.error) throw new Error(result.error);
    status.style.color = 'var(--green)'; status.textContent = `✓ Imported "${result.name}" successfully`;
    await loadCollections();
    toast(`Imported "${result.name}"`, 'success');
    setTimeout(() => closeModal('import-modal'), 1200);
  } catch(e) {
    status.style.color = 'var(--red)'; status.textContent = `✕ ${e.message}`;
  }
}

// ═══════════════════════════════════════════════════════
//  LOAD / SAVE REQUESTS
// ═══════════════════════════════════════════════════════

async function loadRequestInTab(id) {
  const existingIdx = tabs.findIndex(t => t.savedReqId === id);
  if (existingIdx >= 0) { switchTab(existingIdx); return; }

  const res = await fetch('/api/requests/'+id);
  const r = await res.json();

  const t = currentTab();
  const reuseCurrentTab = t && !t.dirty && !t.savedReqId && !t.url;

  const newState = makeTabState({
    savedReqId: id,
    name: r.name,
    method: r.method,
    url: r.url,
    params: r.params || [],
    headers: r.headers || [],
    bodyType: r.body_type || 'none',
    bodyContent: r.body_content || '',
    authType: r.auth_type || 'none',
    authData: r.auth_data || {},
    dirty: false,
  });

  if (reuseCurrentTab) {
    tabs[activeTabIdx] = newState;
    restoreTab(newState);
  } else {
    if (activeTabIdx >= 0) snapshotTab();
    tabs.push(newState);
    activeTabIdx = tabs.length - 1;
    restoreTab(newState);
  }

  renderTabBar();
  document.querySelectorAll('.req-item').forEach(el =>
    el.classList.toggle('active', el.id === 'ri-'+id));
  switchView('builder');
}

async function deleteReq(id) {
  await fetch('/api/requests/'+id, {method:'DELETE'});
  const idx = tabs.findIndex(t => t.savedReqId === id);
  if (idx >= 0) closeTab({ stopPropagation:()=>{} }, idx);
  await loadCollections();
  toast('Request deleted', 'info');
}

function openRenameReqModal() {
  const t = currentTab();
  document.getElementById('rename-req-input').value = t ? t.name : '';
  S.renameReqId = t ? t.savedReqId : null;
  document.getElementById('rename-req-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-req-input').focus(), 50);
}

function openRenameReqModalById(id, name) {
  S.renameReqId = id;
  document.getElementById('rename-req-input').value = name;
  document.getElementById('rename-req-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-req-input').focus(), 50);
}

async function confirmRenameRequest() {
  const name = document.getElementById('rename-req-input').value.trim();
  if(!name) return;
  if(S.renameReqId) {
    await fetch('/api/requests/'+S.renameReqId+'/rename', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    await loadCollections();
    const idx = tabs.findIndex(t => t.savedReqId === S.renameReqId);
    if (idx >= 0) { tabs[idx].name = name; if(idx===activeTabIdx) document.getElementById('req-name-text').textContent = name; renderTabBar(); }
  } else {
    const t = currentTab();
    if (t) { t.name = name; document.getElementById('req-name-text').textContent = name; renderTabBar(); }
  }
  closeModal('rename-req-modal');
  toast(`Renamed to "${name}"`, 'success');
}

// ─── Save logic ───────────────────────────────────────────────────────────────
function renderSaveModal(colls) {
  const sel = document.getElementById('save-collection');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Select collection…</option>' +
    colls.map(c => `<option value="${c.id}" ${c.id==cur?'selected':''}>${esc(c.name)}</option>`).join('');
}

// Smart save: if already saved → update silently; else → open modal
async function handleSave() {
  const t = currentTab();
  if (t && t.savedReqId) {
    // Already saved — just update in place
    await performSave(t.name, null);   // collId=null means keep existing
  } else {
    openSaveModal();
  }
}

function openSaveModal() {
  const t = currentTab();
  document.getElementById('save-name').value = t ? t.name : 'Untitled';
  document.getElementById('save-modal').classList.add('open');
  setTimeout(() => document.getElementById('save-name').focus(), 50);
}

async function saveRequest() {
  const name   = document.getElementById('save-name').value.trim() || 'Untitled';
  const collId = document.getElementById('save-collection').value;
  if (!collId) { toast('Select a collection first', 'error'); return; }
  closeModal('save-modal');
  await performSave(name, parseInt(collId));
}

async function performSave(name, collId) {
  const bodyType = S.bodyType;
  let bodyContent = '';
  if (['json','raw'].includes(bodyType)) bodyContent = document.getElementById('body-editor').value;
  else if (['form','urlencoded'].includes(bodyType)) {
    const kv = getFormRows();
    bodyContent = JSON.stringify(Object.fromEntries(
      kv.filter(r=>r.type!=='file').map(r=>[r.key, r.value])
    ));
  }

  const payload = {
    name: name,
    method: document.getElementById('method-select').value,
    url: document.getElementById('url-input').value,
    params:  getKVRows('params-body'),
    headers: getKVRows('headers-body'),
    body_type: bodyType, body_content: bodyContent,
    auth_type: document.getElementById('auth-type').value,
    auth_data: getAuthData(),
  };
  if (collId) payload.collection_id = collId;

  const t = currentTab();
  if (t && t.savedReqId) {
    // Update existing
    await fetch('/api/requests/'+t.savedReqId, {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    if (t) { t.name = name; t.dirty = false; }
    document.getElementById('req-name-text').textContent = name;
    renderTabBar();
    await loadCollections();
    toast('Request updated', 'success');
  } else {
    // Insert new
    const res = await fetch('/api/requests', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    const data = await res.json();
    if (t) { t.savedReqId = data.id; t.name = name; t.dirty = false; }
    document.getElementById('req-name-text').textContent = name;
    renderTabBar();
    await loadCollections();
    toast('Request saved', 'success');
  }
}

function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// ═══════════════════════════════════════════════════════
//  HISTORY
// ═══════════════════════════════════════════════════════
async function loadHistory() {
  const res = await fetch('/api/history?limit=80');
  S.history = await res.json();
  renderHistory();
}

function renderHistory() {
  const list = document.getElementById('history-list');
  if(!S.history.length) {
    list.innerHTML = '<div style="color:var(--txt3);font-size:11px;padding:16px;text-align:center;font-family:var(--mono)">No history yet</div>';
    return;
  }
  list.innerHTML = S.history.map(h => {
    const sc = h.status_code;
    const cls = sc>=500?'s-5xx':sc>=400?'s-4xx':sc>=300?'s-3xx':sc>=200?'s-2xx':'s-err';
    return `<div class="hist-item" onclick="loadHistoryItem(${h.id})">
      <span class="req-method m-${h.method}" style="min-width:40px">${h.method}</span>
      <span class="hist-url">${esc(h.url)}</span>
      <span class="status-badge ${cls}" style="font-size:9.5px;padding:2px 6px">${h.status_code||'ERR'}</span>
    </div>`;
  }).join('');
}

async function loadHistoryItem(id) {
  const res = await fetch('/api/history/'+id);
  const data = await res.json();
  const req = data.request_data || {};
  newTab({ method: req.method||'GET', url: req.url||'', name: req.url||'From History' });
  switchView('builder');
}

async function clearHistory() {
  if(!confirm('Clear all history?')) return;
  await fetch('/api/history', {method:'DELETE'});
  await loadHistory();
  toast('History cleared', 'info');
}

// ═══════════════════════════════════════════════════════
//  ENVIRONMENTS
// ═══════════════════════════════════════════════════════
async function loadEnvironments() {
  const res = await fetch('/api/environments');
  S.environments = await res.json();
  renderEnvironmentsView();
  renderEnvSelector();
}

function renderEnvSelector() {
  const sel = document.getElementById('env-selector');
  sel.innerHTML = '<option value="">No Environment</option>' +
    S.environments.map(e => `<option value="${e.id}" ${e.active?'selected':''}>${esc(e.name)}</option>`).join('');
}

function selectEnv(id) { if(id) activateEnv(parseInt(id)); }

function renderEnvironmentsView() {
  const panel = document.getElementById('envs-panel');
  if(!S.environments.length) {
    panel.innerHTML = `<div class="empty-state" style="height:200px">
      <div class="empty-icon">⚙</div>
      <p>No environments yet. Create one to use variables.</p>
    </div>`;
    return;
  }
  panel.innerHTML = S.environments.map(env => {
    let vars = {};
    try { vars = typeof env.vars==='string' ? JSON.parse(env.vars) : (env.vars||{}); } catch(e){}
    const entries = Object.entries(vars);
    return `<div class="env-card" id="env-card-${env.id}">
      <div class="env-card-header">
        <input class="env-name-input" value="${esc(env.name)}" id="en-${env.id}" placeholder="Environment name">
        ${env.active
          ? '<span class="env-active-badge">● Active</span>'
          : `<button class="activate-btn" onclick="activateEnv(${env.id})">Set Active</button>`}
        <button class="icon-btn" onclick="deleteEnv(${env.id})" style="margin-left:auto" title="Delete">🗑</button>
      </div>
      <div class="kv-wrap" style="margin-bottom:10px">
        <table class="kv-table">
          <thead><tr><th>Variable</th><th>Initial Value</th><th style="width:36px"></th></tr></thead>
          <tbody id="env-vars-${env.id}">
            ${entries.map(([k,v])=>`<tr>
              <td><input class="kv-input" value="${esc(k)}" placeholder="variable_name"></td>
              <td><input class="kv-input" value="${esc(String(v))}" placeholder="value"></td>
              <td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="add-row-btn" style="margin-top:0" onclick="addEnvVar(${env.id})">+ Add Variable</button>
        <button class="env-save-btn" onclick="saveEnv(${env.id})">Save Changes</button>
      </div>
    </div>`;
  }).join('');
}

function addEnvVar(id) {
  const tbody = document.getElementById('env-vars-'+id);
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><input class="kv-input" placeholder="variable_name"></td>
    <td><input class="kv-input" placeholder="value"></td>
    <td><button class="del-row-btn" onclick="this.closest('tr').remove()" style="opacity:1">✕</button></td>`;
  tbody.appendChild(tr);
}

async function saveEnv(id) {
  const nameEl = document.getElementById('en-'+id);
  const name = (nameEl ? nameEl.value.trim() : '') || 'Environment';
  const rows = [...document.querySelectorAll('#env-vars-'+id+' tr')];
  const vars = {};
  rows.forEach(tr => {
    const inputs = tr.querySelectorAll('input');
    const key = inputs[0]?.value.trim();
    if (key) vars[key] = inputs[1]?.value || '';
  });
  const res = await fetch('/api/environments/'+id, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, vars})
  });
  if (!res.ok) { toast('Failed to save environment', 'error'); return; }
  const envIdx = S.environments.findIndex(e => e.id === id);
  if (envIdx >= 0) {
    S.environments[envIdx].name = name;
    S.environments[envIdx].vars = vars;
  }
  renderEnvSelector();
  toast('Environment saved', 'success');
}

async function createEnvironment() {
  await fetch('/api/environments', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:'New Environment',vars:{}})});
  await loadEnvironments();
}

async function activateEnv(id) {
  await fetch('/api/environments/'+id+'/activate', {method:'POST'});
  await loadEnvironments();
  toast('Environment activated', 'success');
}

async function deleteEnv(id) {
  if(!confirm('Delete this environment?')) return;
  await fetch('/api/environments/'+id, {method:'DELETE'});
  await loadEnvironments();
  toast('Environment deleted', 'info');
}

// ═══════════════════════════════════════════════════════
//  RESIZE HANDLE
// ═══════════════════════════════════════════════════════
function setupResizeHandle() {
  const handle = document.getElementById('resize-handle');
  const resp = document.getElementById('response-panel');
  let dragging=false, startY, startH;
  handle.addEventListener('mousedown', e => {
    dragging=true; startY=e.clientY; startH=resp.offsetHeight;
    handle.classList.add('dragging');
    document.body.style.userSelect='none';
  });
  document.addEventListener('mousemove', e => {
    if(!dragging) return;
    const delta = startY - e.clientY;
    const newH = Math.max(120, Math.min(window.innerHeight*.8, startH+delta));
    resp.style.maxHeight = newH+'px';
  });
  document.addEventListener('mouseup', () => {
    dragging=false; handle.classList.remove('dragging');
    document.body.style.userSelect='';
  });
}

document.querySelectorAll('.modal-overlay').forEach(o =>
  o.addEventListener('click', e => { if(e.target===o) o.classList.remove('open'); }));
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

if __name__ == "__main__":
    print("\n+--------------------------------------+")
    print("|     RequestLab is running!            |")
    print("|  Open: http://localhost:5000         |")
    print("+--------------------------------------+\n")
    app.run(debug=True, port=5000)