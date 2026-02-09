import json
import logging
import re
from pathlib import Path
from typing import Dict, List

import streamlit as st
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from db import (
    UPLOADS_DIR,
    archive_conversation,
    get_conversation_messages,
    get_job,
    get_setting,
    init_db,
    link_conversation,
    list_jobs,
    mark_conversation_reviewed,
    query_inbox,
    set_setting,
    upsert_contact_and_create_job,
    update_job,
    verify_user,
)

LOG_FILE = Path("logs/app.log")
STATUSES = [
    "Lead In",
    "Gather Job Details",
    "Needs Follow-Up",
    "Address Searched",
    "Pricing In Progress",
    "Added to CRM",
    "Measured",
    "Screenshots Saved",
    "Scope Drafted",
    "Proposal Generated",
]


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def extract_details(text: str) -> Dict[str, str]:
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    phone = re.search(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    name = re.search(r"(?:my name is|i am|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text, flags=re.I)
    address = re.search(r"\d+\s+[A-Za-z0-9\s]+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Blvd)", text, flags=re.I)

    return {
        "first_name": (name.group(1).split()[0] if name else ""),
        "last_name": (name.group(1).split()[1] if name and len(name.group(1).split()) > 1 else ""),
        "phone": phone.group(0) if phone else "",
        "email": email.group(0) if email else "",
        "address_line1": address.group(0) if address else "",
        "project_details": text[:500],
        "job_address": address.group(0) if address else "",
        "company": "A&B",
        "status": "Lead In",
        "scope_text": "",
        "pricing_notes": "",
    }


def ensure_auth() -> None:
    if "authed" not in st.session_state:
        st.session_state.authed = False
    if st.session_state.authed:
        return

    st.title("Internal Lead Intake Login")
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        if verify_user(username, password):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Invalid credentials")
    st.stop()


def render_inbox() -> None:
    st.header("Unified Inbox")
    cols = st.columns(4)
    source = cols[0].selectbox("Source", ["All", "Facebook", "Email"])
    category = cols[1].selectbox("Category", ["All", "WEBFLOW_FORMS", "WEBFLOW_PERMISSIONS", "COLD_BID", "GENERAL"])
    review_state = cols[2].selectbox("State", ["All", "New", "Reviewed"])
    keyword = cols[3].text_input("Keyword")

    rows = query_inbox(
        {
            "source": source,
            "category": category,
            "review_state": review_state,
            "keyword": keyword,
        }
    )

    for row in rows:
        with st.expander(f"#{row['conversation_id']} [{row['source']}] {row['external_thread_key']}"):
            st.write(f"Category: {row['category']} | Reviewed: {bool(row['reviewed'])}")
            st.write(row["latest_text"] or "")
            messages = get_conversation_messages(row["conversation_id"])
            full_text = "\n".join([m["text"] or "" for m in messages])
            st.text_area("Conversation", full_text, height=120, key=f"conversation_{row['conversation_id']}")

            if st.button("Extract Details → Create/Update Contact + Job", key=f"extract_{row['conversation_id']}"):
                st.session_state[f"extract_{row['conversation_id']}"] = extract_details(full_text)

            extract_key = f"extract_{row['conversation_id']}"
            if extract_key in st.session_state:
                data = st.session_state[extract_key]
                st.subheader("Review Extracted Fields")
                for field in [
                    "first_name",
                    "last_name",
                    "phone",
                    "email",
                    "address_line1",
                    "job_address",
                    "project_details",
                    "company",
                    "status",
                    "pricing_notes",
                    "scope_text",
                ]:
                    if field in ["project_details", "pricing_notes", "scope_text"]:
                        data[field] = st.text_area(field, value=data.get(field, ""), key=f"{field}_{row['conversation_id']}")
                    elif field == "status":
                        data[field] = st.selectbox(field, STATUSES, index=STATUSES.index(data.get(field, "Lead In")), key=f"status_{row['conversation_id']}")
                    elif field == "company":
                        data[field] = st.selectbox(field, ["A&B", "Atlas", "United"], key=f"company_{row['conversation_id']}")
                    else:
                        data[field] = st.text_input(field, value=data.get(field, ""), key=f"{field}_{row['conversation_id']}")

                if st.button("Confirm Save to CRM", key=f"save_{row['conversation_id']}"):
                    contact_id, job_id = upsert_contact_and_create_job(data)
                    link_conversation(row["conversation_id"], contact_id, job_id)
                    Path(UPLOADS_DIR / str(job_id) / "screenshots").mkdir(parents=True, exist_ok=True)
                    Path(UPLOADS_DIR / str(job_id) / "attachments").mkdir(parents=True, exist_ok=True)
                    Path(UPLOADS_DIR / str(job_id) / "proposals").mkdir(parents=True, exist_ok=True)
                    st.success(f"Saved: contact #{contact_id}, job #{job_id}")

            c1, c2 = st.columns(2)
            if c1.button("Mark Reviewed", key=f"review_{row['conversation_id']}"):
                mark_conversation_reviewed(row["conversation_id"], True)
                st.rerun()
            if c2.button("Archive / Ignore", key=f"archive_{row['conversation_id']}"):
                archive_conversation(row["conversation_id"])
                st.rerun()


def scan_folder(path: Path) -> Dict[str, List[Path]]:
    images, pdfs = [], []
    if not path.exists():
        return {"images": images, "pdfs": pdfs}
    for entry in path.iterdir():
        if entry.name.startswith(".") or entry.name.lower() in {"thumbs.db", ".ds_store"}:
            continue
        if entry.is_file():
            suffix = entry.suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                images.append(entry)
            elif suffix == ".pdf":
                pdfs.append(entry)
    images.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    pdfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {"images": images, "pdfs": pdfs}


def generate_pdf(job_id: int, job: Dict[str, str]) -> Path:
    out_dir = UPLOADS_DIR / str(job_id) / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"proposal_{job_id}.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    text = c.beginText(50, 750)
    lines = [
        f"Company: {job.get('company', '')}",
        f"Contact: {job.get('first_name', '')} {job.get('last_name', '')}",
        f"Email: {job.get('email', '')}",
        f"Phone: {job.get('phone', '')}",
        f"Address: {job.get('address', '')}",
        "",
        "Scope:",
        job.get("scope_text", ""),
        "",
        "Pricing Notes:",
        job.get("pricing_notes", ""),
        "",
        "Basic Terms: Payment due upon completion unless otherwise specified.",
    ]
    for line in lines:
        text.textLine(line)
    c.drawText(text)
    c.save()
    return pdf_path


def render_jobs() -> None:
    st.header("Job Command Center")
    jobs = list_jobs()
    if not jobs:
        st.info("No jobs yet")
        return
    labels = [f"#{j['job_id']} - {j['first_name'] or ''} {j['last_name'] or ''} ({j['status']})" for j in jobs]
    selected = st.selectbox("Select Job", range(len(jobs)), format_func=lambda i: labels[i])
    job_id = jobs[selected]["job_id"]
    job = get_job(job_id)
    if not job:
        return
    tabs = st.tabs(["Details", "Follow-Ups", "Address / Maps", "Measurements", "Pricing", "Scope", "Proposal"])

    with tabs[0]:
        new_status = st.selectbox("Status", STATUSES, index=STATUSES.index(job["status"] if job["status"] in STATUSES else "Lead In"))
        addr = st.text_input("Address", value=job["address"] or "")
        if st.button("Save Details"):
            update_job(job_id, {"status": new_status, "address": addr})
            st.success("Saved")

    with tabs[1]:
        st.write("Suggested missing-info follow-ups:")
        if not job["phone"]:
            st.write("- Please share the best contact phone number.")
        if not job["address"]:
            st.write("- Please confirm the full project address.")
        if not job["project_details"]:
            st.write("- Can you provide project scope details and timeline?")

    with tabs[2]:
        st.write("Manual address/maps workflow placeholder")
        st.write(job["address"] or "No address yet")

    with tabs[3]:
        path = UPLOADS_DIR / str(job_id) / "screenshots"
        st.code(str(path.resolve()))
        if st.button("Scan / Refresh Folder"):
            files = scan_folder(path)
            if not files["images"] and not files["pdfs"]:
                st.info("No files found")
            for img in files["images"]:
                st.image(str(img), caption=img.name)
            for pdf in files["pdfs"]:
                with open(pdf, "rb") as fh:
                    st.download_button(f"Download {pdf.name}", fh.read(), file_name=pdf.name, mime="application/pdf")

    with tabs[4]:
        notes = st.text_area("Pricing Notes", value=job["pricing_notes"] or "", height=180)
        if st.button("Save Pricing"):
            update_job(job_id, {"pricing_notes": notes, "status": "Pricing In Progress"})
            st.success("Pricing saved")

    with tabs[5]:
        scope = st.text_area("Scope Text", value=job["scope_text"] or "", height=220)
        if st.button("Save Scope"):
            update_job(job_id, {"scope_text": scope, "status": "Scope Drafted"})
            st.success("Scope saved")

    with tabs[6]:
        company = st.selectbox("Company", ["A&B", "Atlas", "United"], index=["A&B", "Atlas", "United"].index(job["company"] if job["company"] in ["A&B", "Atlas", "United"] else "A&B"))
        if st.button("Generate Proposal PDF"):
            update_job(job_id, {"company": company, "status": "Proposal Generated"})
            refreshed = get_job(job_id)
            pdf = generate_pdf(job_id, dict(refreshed))
            with open(pdf, "rb") as fh:
                st.download_button("Download Proposal", fh.read(), file_name=pdf.name, mime="application/pdf")


def render_settings() -> None:
    st.header("Settings")
    fb_verify = st.text_input("Facebook Verify Token", value=get_setting("facebook_verify_token", ""))
    fb_secret = st.text_input("Facebook App Secret", value=get_setting("facebook_app_secret", ""), type="password")
    gmail_interval = st.number_input("Gmail Polling Interval Minutes", value=int(get_setting("gmail_poll_interval_minutes", "5")), min_value=1)
    webhook_url = st.text_input("Webhook URL", value=get_setting("webhook_url", "http://localhost:8000/webhook"))
    rules_text = st.text_area("Email Categorization Rules (JSON)", value=get_setting("category_rules", "{}"), height=220)
    st.info("Meta webhooks require a public HTTPS URL. Use ngrok or cloudflared tunnel for local development.")
    if st.button("Save Settings"):
        set_setting("facebook_verify_token", fb_verify)
        set_setting("facebook_app_secret", fb_secret)
        set_setting("gmail_poll_interval_minutes", str(gmail_interval))
        set_setting("webhook_url", webhook_url)
        json.loads(rules_text)
        set_setting("category_rules", rules_text)
        st.success("Saved settings")


def main() -> None:
    setup_logging()
    init_db()
    ensure_auth()
    st.sidebar.title("Lead Intake Platform")
    page = st.sidebar.radio("Go to", ["Inbox", "Jobs", "Settings"])
    if st.sidebar.button("Logout"):
        st.session_state.authed = False
        st.rerun()

    if page == "Inbox":
        render_inbox()
    elif page == "Jobs":
        render_jobs()
    else:
        render_settings()


if __name__ == "__main__":
    main()
