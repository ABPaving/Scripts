import base64
import json
import logging
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from db import TOKENS_DIR, get_setting, init_db, upsert_email_message

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = TOKENS_DIR / "gmail_token.json"
CREDENTIALS_PATH = Path("credentials/google_oauth_client_secret.json")

logging.basicConfig(filename="logs/app.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def get_gmail_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: Dict[str, Any]) -> str:
    body_data = payload.get("body", {}).get("data")
    if body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
    return ""


def _headers_map(headers: List[Dict[str, str]]) -> Dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in headers}


def categorize_email(sender: str, subject: str, body: str) -> str:
    try:
        rules = json.loads(get_setting("category_rules", "{}"))
    except Exception:
        rules = {}

    sender_l = (sender or "").lower()
    text = f"{subject}\n{body}".lower()

    wf = rules.get("WEBFLOW_FORMS", {})
    if sender_l in [s.lower() for s in wf.get("senders", [])]:
        return "WEBFLOW_FORMS"
    if any(k.lower() in text for k in wf.get("keywords", [])):
        return "WEBFLOW_FORMS"

    for k in rules.get("WEBFLOW_PERMISSIONS", {}).get("keywords", []):
        if k.lower() in text:
            return "WEBFLOW_PERMISSIONS"

    for k in rules.get("COLD_BID", {}).get("keywords", []):
        if k.lower() in text:
            return "COLD_BID"

    return "GENERAL"


def ingest_once() -> int:
    service = get_gmail_service()
    count = 0
    result = service.users().messages().list(userId="me", maxResults=30).execute()
    for m in result.get("messages", []):
        msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = _headers_map(msg.get("payload", {}).get("headers", []))
        body = _decode_body(msg.get("payload", {}))
        dt = headers.get("date", "")
        try:
            ts = parsedate_to_datetime(dt).isoformat() if dt else datetime.utcnow().isoformat()
        except Exception:
            ts = datetime.utcnow().isoformat()
        category = categorize_email(headers.get("from", ""), headers.get("subject", ""), body)
        rec = {
            "gmail_message_id": msg["id"],
            "thread_id": msg.get("threadId"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "timestamp": ts,
            "snippet": msg.get("snippet", ""),
            "plain_text_body": body,
            "category": category,
            "raw_json": msg,
        }
        if upsert_email_message(rec):
            count += 1
    return count


def main() -> None:
    init_db()
    while True:
        try:
            inserted = ingest_once()
            logging.info("gmail_ingest inserted=%s", inserted)
        except Exception:
            logging.exception("Gmail ingest cycle failed")
        interval = int(get_setting("gmail_poll_interval_minutes", "5") or 5)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
