"""
Flask application — routes only.

All business logic lives in:
  scrapers.py  — Playwright-based scrapers (PlaywrightScraper, MapsScraper, SunbizScraper)
  tools.py     — tool functions, TOOLS list, TOOL_MAP, shared lead/outreach state
  agent.py     — BaseAgent, AnthropicAgent, OpenAICompatAgent, PerplexityAgent, run_agent()
"""

import os
import json
import csv
import io
from datetime import datetime

# Load .env early so keys are available before anything else
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass

from flask import Flask, request, session, Response, send_file, jsonify, render_template, redirect

import tools
import agent
from tools import LEAD_FIELDS

# Gmail OAuth — optional dependency
try:
    from google_auth_oauthlib.flow import Flow as GoogleFlow
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    _GMAIL_AVAILABLE = True
except ImportError:
    _GMAIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ---------------------------------------------------------------------------
# User profile helpers
# ---------------------------------------------------------------------------

_PROFILE_FILE = os.path.join(os.path.dirname(__file__), ".user_profile.json")

def _load_profile():
    defaults = {"full_name": "Gabe", "email": "", "role": "MMG Broker",
                "company": "MMG", "initials": "G"}
    if not os.path.exists(_PROFILE_FILE):
        return defaults
    try:
        with open(_PROFILE_FILE) as f:
            data = json.load(f)
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return defaults

def _save_profile(data):
    with open(_PROFILE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ---------------------------------------------------------------------------
# Gmail OAuth helpers
# ---------------------------------------------------------------------------

_GMAIL_TOKEN_FILE  = os.path.join(os.path.dirname(__file__), ".gmail_token.json")
_GMAIL_CLIENT_FILE = os.path.join(os.path.dirname(__file__), ".gmail_client.json")
_GMAIL_SCOPES      = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
_GMAIL_REDIRECT_URI = "http://localhost:8502/api/gmail/callback"


def _load_gmail_creds():
    if not os.path.exists(_GMAIL_TOKEN_FILE):
        return None
    try:
        with open(_GMAIL_TOKEN_FILE) as f:
            data = json.load(f)
        creds = GoogleCredentials(
            token=data.get("token"), refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=data.get("client_id"), client_secret=data.get("client_secret"),
            scopes=_GMAIL_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            _save_gmail_creds(creds, data.get("client_id"), data.get("client_secret"))
        return creds
    except Exception:
        return None


def _save_gmail_creds(creds, client_id, client_secret):
    with open(_GMAIL_TOKEN_FILE, "w") as f:
        json.dump({"token": creds.token, "refresh_token": creds.refresh_token,
                   "client_id": client_id, "client_secret": client_secret}, f)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    from tools import _get_gmail_creds
    _gmail_addr, _gmail_pw = _get_gmail_creds()
    return jsonify({
        "anthropic":        bool(session.get("anthropic_key")   or os.getenv("ANTHROPIC_API_KEY")),
        "apollo":           bool(session.get("apollo_key")      or os.getenv("APOLLO_API_KEY")),
        "hubspot":          bool(session.get("hubspot_token")   or os.getenv("HUBSPOT_TOKEN")),
        "gemini":           bool(session.get("gemini_key")      or os.getenv("GEMINI_API_KEY")),
        "perplexity":       bool(session.get("perplexity_key")  or os.getenv("PERPLEXITY_API_KEY")),
        "gmail":            bool(_gmail_addr and _gmail_pw),
        "model_provider":   session.get("model_provider",   "anthropic"),
        "claude_model":     session.get("claude_model",     "claude-sonnet-4-6"),
        "gemini_model":     session.get("gemini_model",     "gemini-3-flash-preview"),
        "perplexity_model": session.get("perplexity_model", "sonar-pro"),
    })


@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.get_json(force=True)
    _set = lambda k, sk: session.__setitem__(sk, data[k]) if data.get(k) else None

    _set("anthropic_key",    "anthropic_key")
    _set("apollo_key",       "apollo_key")
    _set("hubspot_token",    "hubspot_token")
    _set("gemini_key",       "gemini_key")
    _set("model_provider",   "model_provider")
    _set("claude_model",     "claude_model")
    _set("gemini_model",     "gemini_model")
    _set("perplexity_key",   "perplexity_key")
    _set("perplexity_model", "perplexity_model")

    if data.get("hunter_key"):
        tools._hunter_key = data["hunter_key"]
        session["hunter_key"] = data["hunter_key"]

    if data.get("gmail_address") or data.get("gmail_app_password"):
        _file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gmail_app.json")
        existing = {}
        if os.path.exists(_file):
            try:
                with open(_file) as f:
                    existing = json.load(f)
            except Exception:
                pass
        if data.get("gmail_address"):
            existing["gmail_address"] = data["gmail_address"]
        if data.get("gmail_app_password"):
            existing["gmail_app_password"] = data["gmail_app_password"]
        with open(_file, "w") as f:
            json.dump(existing, f)
        session["gmail_address"]      = existing.get("gmail_address", "")
        session["gmail_app_password"] = existing.get("gmail_app_password", "")

    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    data    = request.get_json(force=True)
    message = data.get("message", "")
    history = data.get("history", [])

    # Read session values BEFORE entering the streaming generator
    anthropic_key    = session.get("anthropic_key")    or os.getenv("ANTHROPIC_API_KEY", "")
    apollo_key       = session.get("apollo_key")       or os.getenv("APOLLO_API_KEY", "")
    hubspot_token    = session.get("hubspot_token")    or os.getenv("HUBSPOT_TOKEN", "")
    gemini_key       = session.get("gemini_key")       or os.getenv("GEMINI_API_KEY", "")
    model_provider   = session.get("model_provider",   "anthropic")
    claude_model     = session.get("claude_model",     "claude-sonnet-4-6")
    gemini_model     = session.get("gemini_model",     "gemini-2.0-flash")
    perplexity_key   = session.get("perplexity_key")   or os.getenv("PERPLEXITY_API_KEY", "")
    perplexity_model = session.get("perplexity_model", "sonar-pro")

    # Restore Hunter key into tools module so enrichment can use it
    tools._hunter_key = session.get("hunter_key") or os.getenv("HUNTER_API_KEY", "") or tools._hunter_key

    def stream():
        try:
            yield from agent.run_agent(
                message, history,
                anthropic_key=anthropic_key, apollo_key=apollo_key,
                hubspot_token=hubspot_token, claude_model=claude_model,
                gemini_key=gemini_key, model_provider=model_provider,
                gemini_model=gemini_model, perplexity_key=perplexity_key,
                perplexity_model=perplexity_model,
            )
        except Exception as e:
            app.logger.error("Unhandled stream error [%s]: %s", model_provider, e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/performance")
def get_performance():
    from collections import defaultdict
    records = list(agent._perf_store)

    agg = defaultdict(lambda: {"requests": 0, "successes": 0, "total_ms": 0,
                                "total_in": 0, "total_out": 0, "total_tools": 0,
                                "total_leads": 0, "total_cost": 0.0})
    for r in records:
        p = r["provider"]
        agg[p]["requests"]    += 1
        agg[p]["successes"]   += 1 if r["success"] else 0
        agg[p]["total_ms"]    += r["duration_ms"]
        agg[p]["total_in"]    += r["input_tokens"]
        agg[p]["total_out"]   += r["output_tokens"]
        agg[p]["total_tools"] += r["tool_calls"]
        agg[p]["total_leads"] += r["leads_found"]
        agg[p]["total_cost"]  += r["cost_usd"]

    summary = {}
    for p, d in agg.items():
        n = d["requests"]
        summary[p] = {
            "requests":       n,
            "success_rate":   round(d["successes"] / n * 100, 1) if n else 0,
            "avg_ms":         round(d["total_ms"] / n) if n else 0,
            "total_tokens":   d["total_in"] + d["total_out"],
            "avg_tokens":     round((d["total_in"] + d["total_out"]) / n) if n else 0,
            "total_leads":    d["total_leads"],
            "avg_leads":      round(d["total_leads"] / n, 1) if n else 0,
            "total_cost_usd": round(d["total_cost"], 4),
            "avg_cost_usd":   round(d["total_cost"] / n, 4) if n else 0,
        }

    recent = sorted(records, key=lambda r: r["ts"], reverse=True)[:20]
    return jsonify({"summary": summary, "recent": recent})


@app.route("/api/download/leads")
def download_leads():
    path = os.path.join(os.path.dirname(__file__), "leads.csv")
    if os.path.exists(path):
        return send_file(path, mimetype="text/csv", as_attachment=True, download_name="leads.csv")
    if not tools._leads_store:
        return jsonify({"error": "No leads available"}), 404
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=LEAD_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(tools._leads_store)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.route("/api/save_outreach", methods=["POST"])
def save_outreach_edits():
    data = request.get_json(force=True)
    drafts = data.get("drafts", [])
    tools._outreach_store = drafts
    try:
        path = os.path.join(os.path.dirname(__file__), "outreach_drafts.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "email", "subject_line", "email_body"],
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(drafts)
        return jsonify({"success": True, "count": len(drafts)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/outreach")
def download_outreach():
    path = os.path.join(os.path.dirname(__file__), "outreach_drafts.csv")
    if os.path.exists(path):
        return send_file(path, mimetype="text/csv", as_attachment=True,
                         download_name="outreach_drafts.csv")
    if not tools._outreach_store:
        return jsonify({"error": "No outreach drafts available"}), 404
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["name", "email", "subject_line", "email_body"],
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(tools._outreach_store)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=outreach_drafts.csv"})


@app.route("/api/files")
def list_files():
    base = os.path.dirname(__file__)
    ext_type   = {".csv": "csv", ".pdf": "pdf", ".eml": "email", ".html": "email"}
    task_labels = {"leads.csv": "Prospecting search", "outreach_drafts.csv": "Outreach drafts"}
    files = []
    for fname in os.listdir(base):
        _, ext = os.path.splitext(fname.lower())
        ftype = ext_type.get(ext)
        if not ftype:
            continue
        fpath = os.path.join(base, fname)
        try:
            stat = os.stat(fpath)
        except OSError:
            continue
        sb = stat.st_size
        size_str = f"{sb} B" if sb < 1024 else (f"{sb/1024:.1f} KB" if sb < 1024**2 else f"{sb/1024**2:.1f} MB")
        files.append({
            "name": fname, "type": ftype,
            "size": size_str,
            "date": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
            "task": task_labels.get(fname, ""),
        })
    files.sort(key=lambda f: os.path.getmtime(os.path.join(base, f["name"])), reverse=True)
    return jsonify({"files": files})


@app.route("/api/download/file")
def download_file():
    name = request.args.get("name", "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(os.path.dirname(__file__), name)
    if not os.path.exists(fpath):
        return jsonify({"error": "File not found"}), 404
    return send_file(fpath, as_attachment=True, download_name=name)


@app.route("/api/clear_leads", methods=["POST"])
def clear_leads():
    tools._leads_store    = []
    tools._outreach_store = []
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Gmail OAuth endpoints
# ---------------------------------------------------------------------------

@app.route("/api/gmail/auth")
def gmail_auth():
    if not _GMAIL_AVAILABLE:
        return jsonify({"error": "Gmail libraries not installed"}), 500
    if not os.path.exists(_GMAIL_CLIENT_FILE):
        return jsonify({"error": "Gmail OAuth credentials not configured."}), 400
    with open(_GMAIL_CLIENT_FILE) as f:
        cd = json.load(f)
    client_id, client_secret = cd.get("client_id", ""), cd.get("client_secret", "")
    if not client_id or not client_secret:
        return jsonify({"error": "Gmail Client ID or Secret is empty"}), 400

    flow = GoogleFlow.from_client_config(
        {"web": {"client_id": client_id, "client_secret": client_secret,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token",
                 "redirect_uris": [_GMAIL_REDIRECT_URI]}},
        scopes=_GMAIL_SCOPES,
    )
    flow.redirect_uri = _GMAIL_REDIRECT_URI
    auth_url, state = flow.authorization_url(access_type="offline",
                                              include_granted_scopes="true", prompt="consent")
    session["gmail_oauth_state"] = state
    return redirect(auth_url)


@app.route("/api/gmail/callback")
def gmail_callback():
    if not _GMAIL_AVAILABLE:
        return "<p>Gmail libraries not installed.</p>", 500
    if not os.path.exists(_GMAIL_CLIENT_FILE):
        return "<p>Gmail client credentials missing.</p>", 400
    with open(_GMAIL_CLIENT_FILE) as f:
        cd = json.load(f)
    client_id, client_secret = cd.get("client_id", ""), cd.get("client_secret", "")

    flow = GoogleFlow.from_client_config(
        {"web": {"client_id": client_id, "client_secret": client_secret,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token",
                 "redirect_uris": [_GMAIL_REDIRECT_URI]}},
        scopes=_GMAIL_SCOPES, state=session.get("gmail_oauth_state"),
    )
    flow.redirect_uri = _GMAIL_REDIRECT_URI
    try:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(authorization_response=request.url)
        _save_gmail_creds(flow.credentials, client_id, client_secret)
        return """
        <html><body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f0fdf4">
        <div style="text-align:center;background:white;padding:2rem 3rem;border-radius:1rem;box-shadow:0 4px 24px rgba(0,0,0,.08)">
          <div style="font-size:3rem;margin-bottom:1rem">&#x2705;</div>
          <h2 style="color:#111827;margin:0 0 .5rem">Gmail Connected!</h2>
          <p style="color:#6b7280;margin:0 0 1.5rem">Your Gmail account has been authorized successfully.</p>
          <script>setTimeout(()=>window.close(),2000);</script>
          <p style="color:#9ca3af;font-size:.8rem">This window will close automatically…</p>
        </div></body></html>
        """
    except Exception as e:
        return f"<p>OAuth error: {e}</p>", 400


@app.route("/api/gmail/disconnect", methods=["POST"])
def gmail_disconnect():
    if os.path.exists(_GMAIL_TOKEN_FILE):
        os.remove(_GMAIL_TOKEN_FILE)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Profile endpoint
# ---------------------------------------------------------------------------

@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    if request.method == "GET":
        p = _load_profile()
        return jsonify({k: v for k, v in p.items() if k != "password_hash"})
    data = request.get_json(silent=True) or {}
    profile = _load_profile()
    for field in ["full_name", "email", "role", "company"]:
        if field in data:
            profile[field] = data[field]
    name  = profile.get("full_name", "")
    parts = name.split()
    profile["initials"] = (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper() if parts else "G"
    _save_profile(profile)
    return jsonify({"ok": True, "initials": profile["initials"]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    port  = int(os.environ.get("PORT", 8502))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None
    app.run(port=port, debug=debug, use_reloader=debug)
