"""Outbound dialer: at most MAX_CONCURRENT_CALLS live Telnyx legs."""

from __future__ import annotations

import asyncio
from typing import Any

from calling_agent import config, db
from calling_agent.log import fail, log
from calling_agent.session import SESSIONS, CallSession
from calling_agent.telnyx import telnyx_post

_wakeup = asyncio.Event()
_task: asyncio.Task | None = None


def kick() -> None:
    _wakeup.set()


async def place_call(lead: dict[str, Any]) -> str:
    """Dial one number and register a CallSession. Returns call_control_id."""
    to = lead.get("to") or lead.get("phone")
    if not to:
        raise ValueError("missing phone")
    result = await telnyx_post(
        "/calls",
        {
            "connection_id": config.TELNYX_CONNECTION_ID,
            "to": to,
            "from": config.TELNYX_FROM_NUMBER,
            "answering_machine_detection": "premium",
            "timeout_secs": config.RING_TIMEOUT_SECS,
            "webhook_url": f"https://{config.PUBLIC_HOST}/telnyx/events",
            "webhook_url_method": "POST",
        },
    )
    ccid = result["data"]["call_control_id"]
    session = CallSession(lead)
    SESSIONS[ccid] = session
    log(f"[{ccid}] dialing {to}")
    return ccid


async def _loop() -> None:
    log(f"[dialer] started cap={config.MAX_CONCURRENT_CALLS}")
    while True:
        try:
            if not db.running_campaigns():
                _wakeup.clear()
                await _wakeup.wait()
                continue

            live = db.count_dialing()
            if live >= config.MAX_CONCURRENT_CALLS:
                await asyncio.sleep(1.0)
                db.mark_campaigns_done()
                continue

            nxt = db.claim_next_lead()
            if nxt is None:
                # Either nothing left, or all pending leads are outside their
                # local 10–2 window — wait and retry without burning the CPU.
                db.mark_campaigns_done()
                await asyncio.sleep(5.0)
                continue

            lead_payload = {
                "to": nxt["phone"],
                "lead_id": nxt["id"],
                "campaign_id": nxt["campaign_id"],
                "lead_name": nxt.get("name") or "there",
                **(nxt.get("fields") or {}),
            }
            try:
                ccid = await place_call(lead_payload)
                db.update_lead(nxt["id"], call_control_id=ccid, status="dialing")
            except Exception as e:
                fail(
                    "dial_failed",
                    f"campaign lead {nxt['id']} {nxt['phone']}: {e}",
                    to=nxt["phone"],
                )
                db.update_lead(nxt["id"], status="failed", error=str(e)[:500])

            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fail("dialer_loop", str(e))
            await asyncio.sleep(1.0)


def start() -> None:
    global _task
    db.init()
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
