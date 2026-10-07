"""Process-level runtime singletons (clip cache, helpers)."""

from __future__ import annotations

import json
from typing import Any

from calling_agent import config
from calling_agent.cache import AudioCache
from calling_agent.graph import build_clip_texts
from calling_agent.session import CallSession

CLIP_TEXTS = build_clip_texts(
    agent_name=config.AGENT_NAME,
    company_name=config.COMPANY_NAME,
    callback_number=config.CALLBACK_NUMBER,
    service_summary=config.SERVICE_SUMMARY,
)

AUDIO_CACHE = AudioCache(config.CACHE_DIR)


def record_outcome(
    session: CallSession,
    disposition: str,
    summary: str,
    slots: dict | None = None,
) -> None:
    slots = slots or {}
    session.outcome = {
        "disposition": disposition,
        "call_summary": summary,
        "slots": slots,
        "node": session.node,
        "transcript": list(session.transcript),
    }
    print(f"LEAD UPDATE {json.dumps(session.outcome, indent=2)}", flush=True)
