import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from db import get_or_create_conversation, get_setting, init_db, add_message

logging.basicConfig(filename="logs/app.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    secret = get_setting("facebook_app_secret", "") or ""
    if not secret or not signature_header:
        return False
    try:
        algo, provided = signature_header.split("=", 1)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def parse_events(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for entry in payload.get("entry", []):
        page_id = entry.get("id")
        for messaging in entry.get("messaging", []):
            events.append({"page_id": page_id, **messaging})
    return events


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/webhook")
def verify_webhook(hub_mode: str = "", hub_verify_token: str = "", hub_challenge: str = ""):
    verify_token = get_setting("facebook_verify_token", "")
    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, x_hub_signature_256: str = Header(default="")):
    raw_body = await request.body()
    if not verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        logging.exception("Invalid webhook JSON payload")
        return {"status": "ignored", "reason": "invalid_json"}

    try:
        for event in parse_events(payload):
            sender = (event.get("sender") or {}).get("id")
            if not sender:
                continue
            msg = event.get("message", {})
            if msg.get("is_echo"):
                continue
            message_id = msg.get("mid") or event.get("delivery", {}).get("mids", [None])[0]
            text = msg.get("text") or "[non-text message]"
            attachments = msg.get("attachments", [])
            timestamp = str(event.get("timestamp") or "")
            convo_id = get_or_create_conversation("facebook", sender, "GENERAL")
            add_message(
                conversation_id=convo_id,
                source="facebook",
                external_message_id=message_id,
                payload=event,
                timestamp=timestamp,
                direction="inbound",
                text=text,
                attachments=attachments,
            )
        return {"status": "ok"}
    except Exception:
        logging.exception("Webhook processing failed")
        return {"status": "ignored", "reason": "processing_error"}

