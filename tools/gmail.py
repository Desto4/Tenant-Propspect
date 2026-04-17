"""Gmail tools — send emails and create drafts via SMTP/IMAP App Password."""
import email as email_lib
import email.mime.text
import imaplib
import json
import os
import smtplib
from datetime import datetime

from core.state import get_outreach

_GMAIL_APP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".gmail_app.json")
_GMAIL_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".gmail_token.json")
_GMAIL_CLIENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".gmail_client.json")
_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
GMAIL_REDIRECT_URI = "http://localhost:8504/api/gmail/callback"

try:
    from google_auth_oauthlib.flow import Flow as GoogleFlow
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build as google_build
    _GMAIL_AVAILABLE = True
except ImportError:
    _GMAIL_AVAILABLE = False


def load_gmail_creds():
    """Load Gmail OAuth credentials from disk, refresh if expired."""
    if not os.path.exists(_GMAIL_TOKEN_FILE):
        return None
    try:
        with open(_GMAIL_TOKEN_FILE) as f:
            data = json.load(f)
        creds = GoogleCredentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=_GMAIL_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            save_gmail_creds(creds, data.get("client_id"), data.get("client_secret"))
        return creds
    except Exception:
        return None


def save_gmail_creds(creds, client_id, client_secret):
    with open(_GMAIL_TOKEN_FILE, "w") as f:
        json.dump({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
        }, f)


def get_gmail_creds():
    """Return (address, app_password) — checks session, .env, then persistent file."""
    import flask
    gmail_address, gmail_password = "", ""
    try:
        gmail_address  = flask.session.get("gmail_address", "")
        gmail_password = flask.session.get("gmail_app_password", "")
    except RuntimeError:
        pass
    if not gmail_address:
        gmail_address  = os.getenv("GMAIL_ADDRESS", "")
    if not gmail_password:
        gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    if (not gmail_address or not gmail_password) and os.path.exists(_GMAIL_APP_FILE):
        try:
            with open(_GMAIL_APP_FILE) as f:
                data = json.load(f)
            gmail_address  = gmail_address  or data.get("gmail_address", "")
            gmail_password = gmail_password or data.get("gmail_app_password", "")
        except Exception:
            pass
    return gmail_address, gmail_password


def send_gmail_email(to=None, subject=None, body=None, send_all_drafts=False) -> dict:
    """Send email(s) via Gmail SMTP using an App Password."""
    gmail_address, gmail_password = get_gmail_creds()
    if not gmail_address or not gmail_password:
        return {"error": "Gmail not configured. Add your Gmail address and App Password in Settings."}

    def _send_one(to_addr, subj, text_body):
        msg = email_lib.mime.text.MIMEText(text_body, "plain")
        msg["From"]    = gmail_address
        msg["To"]      = to_addr
        msg["Subject"] = subj
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, gmail_password)
            server.send_message(msg)

    if send_all_drafts:
        outreach = get_outreach()
        if not outreach:
            return {"error": "No outreach drafts found. Write outreach emails first."}
        sent, failed = [], []
        for draft in outreach:
            try:
                _send_one(
                    draft.get("email", ""),
                    draft.get("subject_line", "No subject"),
                    draft.get("email_body", ""),
                )
                sent.append(draft.get("email", ""))
            except Exception as ex:
                failed.append({"email": draft.get("email", ""), "error": str(ex)})
        return {
            "success": True,
            "sent_count": len(sent), "failed_count": len(failed),
            "sent": sent, "failed": failed,
        }
    else:
        if not to or not subject or not body:
            return {"error": "Missing required fields: to, subject, body"}
        try:
            _send_one(to, subject, body)
            return {"success": True, "to": to}
        except Exception as e:
            return {"error": f"Gmail send failed: {str(e)}"}


def create_gmail_drafts(draft_all=True, to=None, subject=None, body=None) -> dict:
    """Save outreach emails as IMAP APPEND drafts in Gmail Drafts folder."""
    gmail_address, gmail_password = get_gmail_creds()
    if not gmail_address or not gmail_password:
        return {"error": "Gmail not configured. Add your Gmail address and App Password in Settings."}

    def _append_draft(to_addr, subj, text_body):
        msg = email_lib.mime.text.MIMEText(text_body, "plain")
        msg["From"]    = gmail_address
        msg["To"]      = to_addr
        msg["Subject"] = subj
        raw  = msg.as_bytes()
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(gmail_address, gmail_password)
        imap.append(
            "[Gmail]/Drafts", "\\Draft",
            imaplib.Time2Internaldate(datetime.now().timestamp()), raw,
        )
        imap.logout()

    if draft_all:
        outreach = get_outreach()
        if not outreach:
            return {"error": "No outreach drafts found. Write outreach emails first, then create Gmail drafts."}
        created, failed = [], []
        for draft in outreach:
            try:
                _append_draft(
                    draft.get("email", ""),
                    draft.get("subject_line", "No subject"),
                    draft.get("email_body", ""),
                )
                created.append(draft.get("email", ""))
            except Exception as ex:
                failed.append({"email": draft.get("email", ""), "error": str(ex)})
        return {
            "success": True,
            "created_count": len(created), "failed_count": len(failed),
            "message": (
                f"Saved {len(created)} email(s) to your Gmail Drafts folder. "
                "Open Gmail to review and send."
            ),
        }
    else:
        if not to or not subject or not body:
            return {"error": "Missing required fields: to, subject, body"}
        try:
            _append_draft(to, subject, body)
            return {"success": True, "to": to, "message": "Draft saved to your Gmail Drafts folder."}
        except Exception as e:
            return {"error": f"Gmail draft creation failed: {str(e)}"}
