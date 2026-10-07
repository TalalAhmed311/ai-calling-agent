"""Timestamped stdout logging + a failures-only file.

The full agent log is noisy (webhooks, audio levels, clip playback). Failures
that actually break a call — Telnyx dial/stream, Deepgram Listen, recording,
hangups that never connected — go to logs/failures.log as well.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from calling_agent import config

_FAILURE_FILE: Path | None = None

# Hangup causes that mean the callee never talked to the agent.
_FAILED_HANGUPS = frozenset({
    "timeout",
    "user_busy",
    "call_rejected",
    "unallocated",
    "no_route_destination",
    "network_out_of_order",
    "destination_out_of_order",
    "recovery_on_timer_expire",
    "not_found",
    "forbidden",
    "unavailable",
    "congestion",
    "no_user_response",
    "no_answer",
})


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts}Z {msg}", flush=True)


def _failures_path() -> Path:
    global _FAILURE_FILE
    if _FAILURE_FILE is None:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _FAILURE_FILE = config.FAILURES_LOG
        _FAILURE_FILE.touch(exist_ok=True)
    return _FAILURE_FILE


def fail(
    kind: str,
    message: str,
    *,
    ccid: str | None = None,
    **extra: Any,
) -> None:
    """Record an operational failure. Always also prints to stdout."""
    kind = kind.upper().replace(" ", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    who = ccid or "-"
    line = f"{stamp}  {kind:<28}  ccid={who}  {message}"
    extras = {k: v for k, v in extra.items() if v is not None}
    if extras:
        try:
            line += "  " + json.dumps(extras, default=str)
        except TypeError:
            line += f"  extra={extras!r}"
    path = _failures_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    log(f"[FAIL {kind}] [{who}] {message}")


def fail_hangup(ccid: str | None, payload: dict[str, Any]) -> None:
    """Log a hangup only when the call never completed normally."""
    cause = str(payload.get("hangup_cause") or "").lower()
    source = str(payload.get("hangup_source") or "")
    sip = str(payload.get("sip_hangup_cause") or "")
    err = payload.get("telnyx_error")
    if err:
        fail(
            "telnyx_error",
            f"Telnyx error on hangup: {err}",
            ccid=ccid,
            hangup_cause=cause,
            hangup_source=source,
            sip=sip,
            to=payload.get("to"),
        )
        return
    if cause in _FAILED_HANGUPS:
        fail(
            "call_not_connected" if cause in ("timeout", "no_answer", "no_user_response")
            else "hangup_failed",
            f"call ended without a completed conversation: {cause}",
            ccid=ccid,
            hangup_cause=cause,
            hangup_source=source,
            sip=sip,
            to=payload.get("to"),
        )
