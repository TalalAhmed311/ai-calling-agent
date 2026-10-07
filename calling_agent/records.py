"""Per-call artifacts: timestamped transcript + call recording on disk.

One JSON file per call under data/calls/, written at hangup and updated again
when Telnyx delivers the recording. Audio lands in data/recordings/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from calling_agent import config
from calling_agent.log import fail, log
from calling_agent.session import CallSession


def _safe(ccid: str) -> str:
    return ccid.replace(":", "_").replace("/", "_")


def record_path(ccid: str) -> Path:
    return config.CALLS_DIR / f"{_safe(ccid)}.json"


def save(ccid: str, session: CallSession | None = None, **extra: Any) -> Path:
    """Merge fields into the call's JSON artifact, creating it if needed."""
    path = record_path(ccid)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    if session is not None:
        outcome = session.outcome or {}
        data.update(
            {
                "call_control_id": ccid,
                "lead": session.lead,
                "disposition": outcome.get("disposition"),
                "summary": outcome.get("call_summary"),
                "slots": outcome.get("slots") or {},
                "final_node": session.node,
                "amd_result": session.amd_result,
                "transcript": list(session.transcript),
            }
        )
    data.update(extra)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    config.CALLS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


async def download_recording(ccid: str, urls: dict[str, str]) -> Path | None:
    """Fetch the recording before Telnyx's pre-signed URL expires (~10 min)."""
    for fmt in (config.CALL_RECORDING_FORMAT, "mp3", "wav"):
        url = (urls or {}).get(fmt)
        if not url:
            continue
        try:
            # Fresh client: the shared one carries a Deepgram auth header that
            # would conflict with S3 query-string auth.
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
        except Exception as e:
            fail("recording_download_failed", f"{fmt}: {e}", ccid=ccid)
            continue

        config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.RECORDINGS_DIR / f"{_safe(ccid)}.{fmt}"
        path.write_bytes(r.content)
        log(f"[{ccid}] recording saved: {path} ({len(r.content)}B)")
        return path

    fail(
        "recording_download_failed",
        f"no usable recording url in {list((urls or {}).keys())}",
        ccid=ccid,
    )
    return None
