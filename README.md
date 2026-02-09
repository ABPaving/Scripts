# Internal Lead Intake Platform (Local-First)

Python + Streamlit + SQLite platform for internal lead intake and CRM workflows.

## Features Implemented

- Internal login with SQLite users table and bcrypt password hashing.
- Auto admin bootstrap from `APP_ADMIN_USER` and `APP_ADMIN_PASS`.
- Facebook webhook endpoint (`GET/POST /webhook`) with signature verification (`X-Hub-Signature-256`).
- Gmail ingestion worker using Gmail API polling.
- Configurable email categorization rules stored in SQLite settings.
- Unified inbox for Facebook + Email conversations.
- Extraction workflow: **Extract Details → Create/Update Contact + Job** with editable review form.
- CRM tables: Contacts, Conversations, Messages, Jobs.
- Job command center tabs:
  - Details
  - Follow-Ups
  - Address / Maps
  - Measurements (folder scan)
  - Pricing (manual notes)
  - Scope
  - Proposal PDF generation
- Local storage folders auto-created per job:
  - `data/uploads/<job_id>/screenshots/`
  - `data/uploads/<job_id>/attachments/`
  - `data/uploads/<job_id>/proposals/`
- Logging to `logs/app.log`.

## Project Layout

- `app.py` – Streamlit UI
- `webhook_server.py` – FastAPI Facebook webhook server
- `gmail_ingest.py` – Gmail polling worker
- `db.py` – shared SQLite schema + data access
- `data/workflow.db` – SQLite database (auto-created)

## Requirements

- Python 3.10+
- Meta app/page webhook setup for Messenger
- Google Cloud OAuth credentials for Gmail API

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment Variables

Set these before first app run:

```bash
export APP_ADMIN_USER=admin
export APP_ADMIN_PASS='change-me'
```

## Gmail OAuth Setup

1. In Google Cloud Console, enable Gmail API.
2. Create OAuth client credentials (Desktop app recommended for local workflow).
3. Save client secret JSON to:

```text
credentials/google_oauth_client_secret.json
```

4. Start worker once to trigger OAuth browser flow:

```bash
python gmail_ingest.py
```

5. On success, token is stored at:

```text
data/tokens/gmail_token.json
```

## Run Instructions

Run each service in its own terminal.

### 1) Start webhook server

```bash
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

### 2) Start Gmail ingestion worker

```bash
python gmail_ingest.py
```

### 3) Start Streamlit app

```bash
streamlit run app.py
```

## Facebook Webhook Setup Notes

- In Streamlit **Settings**, set:
  - Facebook Verify Token
  - Facebook App Secret
  - Webhook URL
- Meta requires a publicly reachable HTTPS endpoint.
  - Use `ngrok` or `cloudflared` to tunnel local port 8000.

## Statuses (Exact)

- Lead In
- Gather Job Details
- Needs Follow-Up
- Address Searched
- Pricing In Progress
- Added to CRM
- Measured
- Screenshots Saved
- Scope Drafted
- Proposal Generated

## Reliability Notes

- Defensive handling for malformed webhook payloads and malformed emails.
- Message deduplication via external message ID and payload hash.
- Folder scan ignores hidden/temp files and handles empty folders.

