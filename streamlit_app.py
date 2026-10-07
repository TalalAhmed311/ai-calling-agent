"""Streamlit campaign UI: CSV upload → pick phone column → queued dials."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

AGENT_URL = os.environ.get("AGENT_URL", "http://127.0.0.1:8081").rstrip("/")
SECRET = os.environ.get("ENDPOINT_SECRET", "").strip()
FAILURES_LOG = ROOT / "logs" / "failures.log"
CALLS_DIR = ROOT / "data" / "calls"
RECORDINGS_DIR = ROOT / "data" / "recordings"

PHONE_HINTS = (
    "phone",
    "mobile",
    "cell",
    "msisdn",
    "to",
    "number",
    "phone_number",
    "phonenumber",
    "tel",
    "contact",
)

st.set_page_config(page_title="CraftPay outbound dialer", layout="wide")


def api(method: str, path: str, json: dict | None = None) -> dict:
    r = httpx.request(
        method,
        f"{AGENT_URL}{path}",
        headers={"x-endpoint-secret": SECRET, "Content-Type": "application/json"},
        json=json,
        timeout=30.0,
    )
    if r.is_error:
        raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def guess_phone_column(columns: list[str]) -> str | None:
    lower = {c.lower().strip().replace(" ", "_"): c for c in columns}
    for hint in PHONE_HINTS:
        if hint in lower:
            return lower[hint]
    for key, orig in lower.items():
        if "phone" in key or "mobile" in key:
            return orig
    return columns[0] if columns else None


def guess_name_column(columns: list[str]) -> str | None:
    lower = {c.lower().strip().replace(" ", "_"): c for c in columns}
    for hint in ("name", "lead_name", "contact_name", "full_name", "owner"):
        if hint in lower:
            return lower[hint]
    return None


def save_campaign(
    name: str,
    phone_column: str,
    leads: list[dict],
    filename: str = "",
) -> dict:
    camp = api(
        "POST",
        "/campaigns",
        {
            "name": name,
            "filename": filename,
            "phone_column": phone_column,
            "leads": leads,
        },
    )
    st.session_state.campaign_id = camp["id"]
    return camp


def call_record_path(ccid: str | None) -> Path | None:
    if not ccid:
        return None
    return CALLS_DIR / f"{ccid.replace(':', '_')}.json"


def load_call_record(ccid: str | None) -> dict | None:
    path = call_record_path(ccid)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def recording_bytes(lead: dict) -> tuple[bytes | None, str]:
    """Return (audio bytes, filename) for a lead, or (None, '')."""
    raw = (lead.get("recording") or "").strip()
    path = Path(raw) if raw else None
    if path is None or not path.is_file():
        ccid = lead.get("call_control_id") or ""
        if ccid:
            alt = RECORDINGS_DIR / f"{ccid.replace(':', '_')}.mp3"
            if alt.is_file():
                path = alt
    if path is None or not path.is_file():
        return None, ""
    return path.read_bytes(), path.name


def transcript_export(lead: dict, record: dict | None) -> tuple[str, str]:
    """Build a downloadable transcript text + suggested filename."""
    ccid = lead.get("call_control_id") or "call"
    safe = ccid.replace(":", "_")
    lines: list[str] = [
        f"Call: {ccid}",
        f"To: {lead.get('phone') or (record or {}).get('to') or ''}",
        f"Name: {lead.get('name') or ''}",
        f"Disposition: {lead.get('disposition') or (record or {}).get('disposition') or ''}",
        f"Hangup: {lead.get('hangup_cause') or (record or {}).get('hangup_cause') or ''}",
        "",
        "--- transcript ---",
    ]
    turns = (record or {}).get("transcript") or []
    if not turns:
        lines.append("(no transcript turns saved)")
    else:
        for turn in turns:
            role = (turn.get("role") or "?").upper()
            content = turn.get("content") or ""
            at = turn.get("at")
            prefix = f"[{at}s] " if at is not None else ""
            lines.append(f"{prefix}{role}: {content}")
    return "\n".join(lines) + "\n", f"{safe}_transcript.txt"


if "unlocked" not in st.session_state:
    st.session_state.unlocked = False

if not st.session_state.unlocked:
    st.title("Outbound dialer")
    pw = st.text_input("Endpoint secret", type="password")
    if st.button("Unlock") and pw:
        if pw == SECRET:
            st.session_state.unlocked = True
            st.rerun()
        else:
            st.error("Wrong secret")
    st.stop()

st.title("Outbound dialer")
st.caption(
    f"Agent `{AGENT_URL}` · max 2 concurrent Telnyx calls · leads stored in `data/leads.db`"
)

tab_upload, tab_campaigns, tab_failures = st.tabs(
    ["Add leads", "Campaigns", "Failures"]
)

with tab_upload:
    mode = st.radio(
        "How do you want to add numbers?",
        ["Upload CSV", "Single number"],
        horizontal=True,
    )

    if mode == "Upload CSV":
        st.subheader("CSV format")
        st.markdown(
            """
Use a **header row**. One lead per line. The phone column can be named
`phone`, `mobile`, `Phone Number`, etc. — you pick it after upload.

- Phone: E.164 is best (`+16503365939`). US 10-digit (`6503365939`) and
  formatted (`+1(650)336-5939`) also work.
- Extra columns (`name`, `company`, …) are stored with the lead.
- UTF-8 `.csv` only. Empty / unreadable phones are skipped, not dialed.
"""
        )
        st.caption("Example file")
        st.code(
            "phone,name,company\n"
            "+16503365939,Alex Rivera,CraftPay\n"
            "6503365940,Sam Lee,Acme\n"
            "+923113302963,Talal,Shop",
            language="csv",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"phone": "+16503365939", "name": "Alex Rivera", "company": "CraftPay"},
                    {"phone": "6503365940", "name": "Sam Lee", "company": "Acme"},
                    {"phone": "+923113302963", "name": "Talal", "company": "Shop"},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

        uploaded = st.file_uploader("CSV of leads", type=["csv"])
        if uploaded is not None:
            df = pd.read_csv(uploaded, dtype=str).fillna("")
            st.write(f"{len(df)} rows · columns: {', '.join(df.columns)}")
            phone_col = st.selectbox(
                "Phone column",
                list(df.columns),
                index=max(
                    0,
                    list(df.columns).index(
                        guess_phone_column(list(df.columns)) or df.columns[0]
                    ),
                ),
            )
            name_choices = ["(none)"] + list(df.columns)
            guessed = guess_name_column(list(df.columns))
            name_col = st.selectbox(
                "Name column (optional)",
                name_choices,
                index=name_choices.index(guessed) if guessed in name_choices else 0,
            )
            campaign_name = st.text_input(
                "Campaign name", value=Path(uploaded.name).stem
            )
            st.dataframe(df.head(20), use_container_width=True)

            if st.button("Save campaign", type="primary"):
                leads = []
                for rec in df.to_dict(orient="records"):
                    leads.append(
                        {
                            "phone": rec.get(phone_col, ""),
                            "name": rec.get(name_col, "") if name_col != "(none)" else "",
                            "fields": rec,
                        }
                    )
                try:
                    camp = save_campaign(
                        campaign_name, phone_col, leads, uploaded.name
                    )
                    st.success(
                        f"Saved campaign #{camp['id']}: {camp.get('inserted', 0)} numbers, "
                        f"{camp.get('skipped', 0)} skipped (bad phone). "
                        "Open the Campaigns tab and click Start calling."
                    )
                except Exception as e:
                    st.error(str(e))

    else:
        st.subheader("Single number")
        st.markdown(
            "Enter one number to save as a one-lead campaign. "
            "Then start it from the **Campaigns** tab. "
            "Use E.164 (`+16503365939`) when you can."
        )
        phone = st.text_input("Phone number", placeholder="+16503365939")
        lead_name = st.text_input("Name (optional)", placeholder="Alex Rivera")
        campaign_name = st.text_input(
            "Campaign name", value="single-call", key="single_campaign_name"
        )
        start_now = st.checkbox("Start calling as soon as it is saved", value=True)
        if st.button("Save number", type="primary"):
            if not phone.strip():
                st.error("Enter a phone number")
            else:
                try:
                    camp = save_campaign(
                        campaign_name.strip() or "single-call",
                        "phone",
                        [
                            {
                                "phone": phone.strip(),
                                "name": lead_name.strip(),
                                "fields": {"phone": phone.strip(), "name": lead_name.strip()},
                            }
                        ],
                        filename="single",
                    )
                    skipped = camp.get("skipped") or 0
                    if skipped and not camp.get("inserted"):
                        st.error("That phone number could not be parsed. Try +1… or +92…")
                    else:
                        msg = (
                            f"Saved campaign #{camp['id']} for {phone.strip()}."
                        )
                        if start_now and camp.get("inserted"):
                            api("POST", f"/campaigns/{camp['id']}/start")
                            msg += " Dialing now — watch the Campaigns tab."
                        else:
                            msg += " Open Campaigns and click Start calling."
                        st.success(msg)
                except Exception as e:
                    st.error(str(e))

with tab_campaigns:
    try:
        listing = api("GET", "/campaigns")
    except Exception as e:
        st.error(f"Agent not reachable: {e}")
        listing = {"campaigns": [], "max_concurrent": 2, "dialing": 0}

    cols = st.columns(3)
    cols[0].metric("Live calls", listing.get("dialing") or 0)
    cols[1].metric("Cap", listing.get("max_concurrent") or 2)
    cols[2].metric("Campaigns", len(listing.get("campaigns") or []))

    campaigns = listing.get("campaigns") or []
    if not campaigns:
        st.info("Add a CSV or a single number on the Add leads tab.")
    else:
        options = {
            f"#{c['id']} {c['name']} ({c['status']})": c["id"] for c in campaigns
        }
        default_id = st.session_state.get("campaign_id", campaigns[0]["id"])
        labels = list(options.keys())
        default_ix = 0
        for i, cid in enumerate(options.values()):
            if cid == default_id:
                default_ix = i
                break
        chosen = st.selectbox("Campaign", labels, index=default_ix)
        cid = options[chosen]
        st.session_state.campaign_id = cid

        camp = api("GET", f"/campaigns/{cid}")
        m = st.columns(5)
        m[0].metric("Status", camp["status"])
        m[1].metric("Pending", camp.get("pending") or 0)
        m[2].metric("Dialing", camp.get("dialing") or 0)
        m[3].metric("Completed", camp.get("completed") or 0)
        m[4].metric("Failed", camp.get("failed") or 0)

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("Start calling", type="primary"):
                api("POST", f"/campaigns/{cid}/start")
                st.rerun()
        with b2:
            if st.button("Pause"):
                api("POST", f"/campaigns/{cid}/pause")
                st.rerun()
        with b3:
            auto = st.checkbox(
                "Auto-refresh",
                value=False,
                help="Turn off while downloading audio/transcript.",
            )

        leads = camp.get("leads") or []
        if leads:
            table = pd.DataFrame(leads)
            show = [
                c
                for c in (
                    "id",
                    "phone",
                    "name",
                    "status",
                    "disposition",
                    "hangup_cause",
                    "error",
                    "attempts",
                    "call_control_id",
                    "recording",
                )
                if c in table.columns
            ]
            st.dataframe(table[show], use_container_width=True, hide_index=True)

            st.subheader("Download call files")
            st.caption(
                "Pick a lead to download its recording (MP3) and/or transcript. "
                "Audio older than 30 hours is auto-deleted unless the campaign "
                "has a kept lead (callback / interested / do-not-call)."
            )
            by_id = {int(L["id"]): L for L in leads}
            downloadable = [
                L
                for L in leads
                if L.get("call_control_id") or L.get("recording")
            ]
            if not downloadable:
                st.info("No calls with recordings or transcripts in this campaign yet.")
            else:
                labels = {
                    f"#{L['id']} {L.get('name') or L.get('phone')} "
                    f"({L.get('disposition') or L.get('status') or 'n/a'})": int(L["id"])
                    for L in downloadable
                }
                pick = st.selectbox("Lead", list(labels.keys()), key=f"dl_lead_{cid}")
                lead = by_id[labels[pick]]
                record = load_call_record(lead.get("call_control_id"))
                audio, audio_name = recording_bytes(lead)
                transcript_text, transcript_name = transcript_export(lead, record)

                d1, d2, d3 = st.columns(3)
                with d1:
                    if audio:
                        st.download_button(
                            "Download audio (MP3)",
                            data=audio,
                            file_name=audio_name or "recording.mp3",
                            mime="audio/mpeg",
                            key=f"dl_audio_{lead['id']}",
                        )
                    else:
                        st.warning("No audio file on disk for this lead.")
                with d2:
                    st.download_button(
                        "Download transcript",
                        data=transcript_text.encode("utf-8"),
                        file_name=transcript_name,
                        mime="text/plain",
                        key=f"dl_tx_{lead['id']}",
                    )
                with d3:
                    if record is not None:
                        st.download_button(
                            "Download full call JSON",
                            data=json.dumps(record, indent=2).encode("utf-8"),
                            file_name=f"{(lead.get('call_control_id') or 'call').replace(':', '_')}.json",
                            mime="application/json",
                            key=f"dl_json_{lead['id']}",
                        )
                    else:
                        st.caption("No call JSON saved.")

                with st.expander("Preview transcript", expanded=False):
                    st.code(transcript_text, language="text")

        if auto:
            import time as _time

            _time.sleep(3)
            st.rerun()

with tab_failures:
    if FAILURES_LOG.exists():
        text = FAILURES_LOG.read_text(encoding="utf-8")
        st.code(text[-8000:] or "(empty — no failures yet)", language="text")
    else:
        st.info("No failures log yet.")
    if st.button("Reload failures"):
        st.rerun()
