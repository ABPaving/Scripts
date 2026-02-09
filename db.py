import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import bcrypt

DB_PATH = Path("data/workflow.db")
LOG_DIR = Path("logs")
UPLOADS_DIR = Path("data/uploads")
TOKENS_DIR = Path("data/tokens")


def ensure_dirs() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def init_db() -> None:
    ensure_dirs()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contacts (
                contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                email TEXT,
                address_line1 TEXT,
                address_line2 TEXT,
                city TEXT,
                state TEXT,
                postal_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER,
                company TEXT,
                address TEXT,
                status TEXT,
                project_details TEXT,
                pricing_notes TEXT,
                scope_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(contact_id) REFERENCES contacts(contact_id)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_thread_key TEXT NOT NULL,
                category TEXT,
                reviewed INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                contact_id INTEGER,
                job_id INTEGER,
                last_message_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source, external_thread_key),
                FOREIGN KEY(contact_id) REFERENCES contacts(contact_id),
                FOREIGN KEY(job_id) REFERENCES jobs(job_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_message_id TEXT,
                payload_hash TEXT,
                timestamp TEXT,
                direction TEXT,
                text TEXT,
                attachments_json TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source, external_message_id),
                UNIQUE(source, payload_hash),
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            );

            CREATE TABLE IF NOT EXISTS email_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                thread_id TEXT,
                sender TEXT,
                recipient TEXT,
                subject TEXT,
                timestamp TEXT,
                snippet TEXT,
                plain_text_body TEXT,
                category TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);
            CREATE INDEX IF NOT EXISTS idx_email_messages_category ON email_messages(category);
            """
        )

    ensure_admin_user()
    seed_default_settings()


def ensure_admin_user() -> None:
    admin_user = os.getenv("APP_ADMIN_USER")
    admin_pass = os.getenv("APP_ADMIN_PASS")
    if not admin_user or not admin_pass:
        return

    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE username = ?", (admin_user,)).fetchone()
        if row:
            return
        password_hash = bcrypt.hashpw(admin_pass.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (admin_user, password_hash, now_iso()),
        )


def seed_default_settings() -> None:
    defaults = {
        "facebook_verify_token": "",
        "facebook_app_secret": "",
        "gmail_poll_interval_minutes": "5",
        "webhook_url": "http://localhost:8000/webhook",
        "category_rules": json.dumps(
            {
                "WEBFLOW_FORMS": {
                    "senders": [
                        "no-reply@webflow.com",
                        "no-reply@webforms.io",
                        "no-reply-forms@webflow.com",
                    ],
                    "keywords": ["form submission"],
                },
                "WEBFLOW_PERMISSIONS": {
                    "keywords": [
                        "request edit access",
                        "ask to edit",
                        "site access",
                        "role",
                        "invited",
                    ]
                },
                "COLD_BID": {
                    "keywords": [
                        "itb",
                        "rfp",
                        "rfq",
                        "invitation to bid",
                        "scope of work",
                        "pricing request",
                        "paving",
                        "sealcoating",
                        "milling",
                        "striping",
                    ]
                },
            }
        ),
    }
    with get_conn() as conn:
        for key, value in defaults.items():
            row = conn.execute("SELECT key FROM settings WHERE key = ?", (key,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now_iso()),
                )


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )


def verify_user(username: str, password: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))


def get_or_create_conversation(source: str, external_thread_key: str, category: str = "GENERAL") -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM conversations WHERE source = ? AND external_thread_key = ?",
            (source, external_thread_key),
        ).fetchone()
        if row:
            return row["conversation_id"]
        cur = conn.execute(
            """
            INSERT INTO conversations(source, external_thread_key, category, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, external_thread_key, category, now_iso(), now_iso()),
        )
        return cur.lastrowid


def _payload_hash(payload: Dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def add_message(
    conversation_id: int,
    source: str,
    external_message_id: Optional[str],
    payload: Dict[str, Any],
    timestamp: Optional[str],
    direction: str,
    text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    payload_hash = _payload_hash(payload)
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO messages(
                    conversation_id, source, external_message_id, payload_hash, timestamp, direction,
                    text, attachments_json, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    source,
                    external_message_id,
                    payload_hash,
                    timestamp,
                    direction,
                    text,
                    json.dumps(attachments or []),
                    json.dumps(payload),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            "UPDATE conversations SET last_message_at = ?, updated_at = ? WHERE conversation_id = ?",
            (timestamp or now_iso(), now_iso(), conversation_id),
        )
        return True


def upsert_email_message(record: Dict[str, Any]) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO email_messages(
                    gmail_message_id, thread_id, sender, recipient, subject, timestamp,
                    snippet, plain_text_body, category, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["gmail_message_id"],
                    record.get("thread_id"),
                    record.get("from"),
                    record.get("to"),
                    record.get("subject"),
                    record.get("timestamp"),
                    record.get("snippet"),
                    record.get("plain_text_body"),
                    record.get("category", "GENERAL"),
                    json.dumps(record.get("raw_json", {})),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return False

        convo_id = get_or_create_conversation("email", record.get("thread_id") or record["gmail_message_id"], record.get("category", "GENERAL"))
        add_message(
            conversation_id=convo_id,
            source="email",
            external_message_id=record["gmail_message_id"],
            payload=record.get("raw_json", {}),
            timestamp=record.get("timestamp") or now_iso(),
            direction="inbound",
            text=record.get("plain_text_body") or record.get("snippet") or "",
            attachments=[],
        )
        return True


def query_inbox(filters: Dict[str, Any]) -> List[sqlite3.Row]:
    where = ["1=1"]
    params: List[Any] = []
    if filters.get("source") and filters["source"] != "All":
        where.append("c.source = ?")
        params.append(filters["source"].lower())
    if filters.get("category") and filters["category"] != "All":
        where.append("c.category = ?")
        params.append(filters["category"])
    if filters.get("review_state") == "New":
        where.append("c.reviewed = 0")
    elif filters.get("review_state") == "Reviewed":
        where.append("c.reviewed = 1")
    if filters.get("keyword"):
        where.append("m.text LIKE ?")
        params.append(f"%{filters['keyword']}%")

    sql = f"""
    SELECT c.*, MAX(m.timestamp) AS latest_message_time,
           (SELECT text FROM messages m2 WHERE m2.conversation_id = c.conversation_id ORDER BY m2.timestamp DESC LIMIT 1) AS latest_text
    FROM conversations c
    LEFT JOIN messages m ON m.conversation_id = c.conversation_id
    WHERE {' AND '.join(where)} AND c.archived = 0
    GROUP BY c.conversation_id
    ORDER BY COALESCE(latest_message_time, c.updated_at) DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return rows


def get_conversation_messages(conversation_id: int) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
            (conversation_id,),
        ).fetchall()


def mark_conversation_reviewed(conversation_id: int, reviewed: bool = True) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET reviewed = ?, updated_at = ? WHERE conversation_id = ?",
            (1 if reviewed else 0, now_iso(), conversation_id),
        )


def archive_conversation(conversation_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET archived = 1, updated_at = ? WHERE conversation_id = ?",
            (now_iso(), conversation_id),
        )


def upsert_contact_and_create_job(payload: Dict[str, Any]) -> Tuple[int, int]:
    with get_conn() as conn:
        contact = None
        if payload.get("phone"):
            contact = conn.execute("SELECT * FROM contacts WHERE phone = ?", (payload["phone"],)).fetchone()
        if not contact and payload.get("email"):
            contact = conn.execute("SELECT * FROM contacts WHERE email = ?", (payload["email"],)).fetchone()

        if contact:
            contact_id = contact["contact_id"]
            conn.execute(
                """
                UPDATE contacts SET
                    first_name = ?, last_name = ?, phone = ?, email = ?,
                    address_line1 = ?, city = ?, state = ?, postal_code = ?, updated_at = ?
                WHERE contact_id = ?
                """,
                (
                    payload.get("first_name"),
                    payload.get("last_name"),
                    payload.get("phone"),
                    payload.get("email"),
                    payload.get("address_line1"),
                    payload.get("city"),
                    payload.get("state"),
                    payload.get("postal_code"),
                    now_iso(),
                    contact_id,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO contacts(
                    first_name, last_name, phone, email, address_line1, city, state, postal_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("first_name"),
                    payload.get("last_name"),
                    payload.get("phone"),
                    payload.get("email"),
                    payload.get("address_line1"),
                    payload.get("city"),
                    payload.get("state"),
                    payload.get("postal_code"),
                    now_iso(),
                    now_iso(),
                ),
            )
            contact_id = cur.lastrowid

        job_cur = conn.execute(
            """
            INSERT INTO jobs(
                contact_id, company, address, status, project_details, pricing_notes, scope_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contact_id,
                payload.get("company"),
                payload.get("job_address"),
                payload.get("status", "Lead In"),
                payload.get("project_details"),
                payload.get("pricing_notes"),
                payload.get("scope_text"),
                now_iso(),
                now_iso(),
            ),
        )
        return contact_id, job_cur.lastrowid


def link_conversation(conversation_id: int, contact_id: int, job_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET contact_id = ?, job_id = ?, updated_at = ? WHERE conversation_id = ?",
            (contact_id, job_id, now_iso(), conversation_id),
        )


def list_jobs() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT j.*, c.first_name, c.last_name FROM jobs j LEFT JOIN contacts c ON c.contact_id = j.contact_id ORDER BY j.updated_at DESC"
        ).fetchall()


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT j.*, c.* FROM jobs j LEFT JOIN contacts c ON c.contact_id = j.contact_id WHERE j.job_id = ?",
            (job_id,),
        ).fetchone()


def update_job(job_id: int, fields: Dict[str, Any]) -> None:
    if not fields:
        return
    set_clause = ", ".join([f"{k} = ?" for k in fields.keys()] + ["updated_at = ?"])
    params = list(fields.values()) + [now_iso(), job_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", params)

