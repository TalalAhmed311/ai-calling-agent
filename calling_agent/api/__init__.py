"""HTTP routes: health, dial, Telnyx webhooks."""

from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, HTTPException, Request, Response

from calling_agent import config, conversation, db, dialer, records, runtime
from calling_agent.amd import classify_amd
from calling_agent.log import fail, fail_hangup, log
from calling_agent.records import save as save_call_record
from calling_agent.session import SESSIONS, CallSession
from calling_agent.telnyx import (
    telnyx_post,
    telnyx_transcription_start,
    verify_telnyx_signature,
)
from calling_agent.tts import close_session_stt

# Hangups that mean the callee never had a real conversation.
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

_CLIP_ID = re.compile(r"^[a-z0-9_]+$")

router = APIRouter()


async def _start_transcription(ccid: str) -> None:
    try:
        await telnyx_transcription_start(ccid)
        log(f"[{ccid}] transcription started")
        return
    except Exception as e:
        fail("transcription_start_failed", str(e), ccid=ccid)
    try:
        await telnyx_transcription_start(ccid)
        log(f"[{ccid}] transcription started (Deepgram fallback)")
    except Exception as e:
        fail("transcription_fallback_failed", str(e), ccid=ccid)


async def _start_media_stream(ccid: str) -> None:
    try:
        await telnyx_post(
            f"/calls/{ccid}/actions/streaming_start",
            {
                "stream_url": f"wss://{config.PUBLIC_HOST}/telnyx/media",
                "stream_track": "inbound_track",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": config.TELNYX_BIDIRECTIONAL_CODEC,
            },
        )
        log(f"[{ccid}] streaming_start ok")
    except Exception as e:
        fail("streaming_start_failed", str(e), ccid=ccid)


async def _start_recording(ccid: str) -> None:
    try:
        await telnyx_post(
            f"/calls/{ccid}/actions/record_start",
            {
                "format": config.CALL_RECORDING_FORMAT,
                "channels": config.CALL_RECORDING_CHANNELS,
            },
        )
        log(
            f"[{ccid}] recording started "
            f"({config.CALL_RECORDING_FORMAT}/{config.CALL_RECORDING_CHANNELS})"
        )
    except Exception as e:
        fail("record_start_failed", str(e), ccid=ccid)


@router.get("/health")
async def health():
    return {
        "ok": True,
        "mode": "telnyx-playback",
        "clips": len(runtime.CLIP_TEXTS),
        "wavs": len(runtime.AUDIO_CACHE.wavs),
        "cache_dir": str(config.CACHE_DIR),
        "media_stream": config.MEDIA_STREAM,
    }


@router.get("/audio/{filename}")
async def clip_wav(filename: str):
    clip_id = filename.removesuffix(".wav")
    if not _CLIP_ID.match(clip_id):
        raise HTTPException(status_code=404, detail="unknown clip")
    wav = runtime.AUDIO_CACHE.get_wav(clip_id)
    if not wav:
        raise HTTPException(status_code=404, detail="clip not baked")
    return Response(content=wav, media_type="audio/wav")


@router.get("/calls")
async def list_calls(request: Request, limit: int = 20):
    if request.headers.get("x-endpoint-secret") != config.ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    if not config.CALLS_DIR.exists():
        return {"calls": []}
    files = sorted(
        config.CALLS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({
            "call_control_id": d.get("call_control_id"),
            "to": d.get("to"),
            "disposition": d.get("disposition"),
            "slots": d.get("slots"),
            "turns": len(d.get("transcript") or []),
            "recording": (d.get("recording") or {}).get("file"),
            "updated_at": d.get("updated_at"),
        })
    return {"calls": out}


@router.get("/calls/{ccid}")
async def get_call(ccid: str, request: Request):
    if request.headers.get("x-endpoint-secret") != config.ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    path = records.record_path(ccid)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no record for that call")
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/call")
async def start_call(request: Request):
    if request.headers.get("x-endpoint-secret") != config.ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    lead = await request.json()
    to = lead.get("to")
    if not to:
        raise HTTPException(status_code=400, detail="missing 'to'")

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
    SESSIONS[ccid] = CallSession(lead)
    log(f"[{ccid}] dialing {to}")
    return {"call_control_id": ccid}


@router.post("/telnyx/events")
async def telnyx_webhook(request: Request):
    raw = await request.body()
    try:
        verify_telnyx_signature(
            raw,
            request.headers.get("telnyx-signature-ed25519"),
            request.headers.get("telnyx-timestamp"),
        )
    except HTTPException as exc:
        log(f"webhook signature rejected: {exc.detail}")
        raise

    body = json.loads(raw)
    data = body.get("data", {})
    event = data.get("event_type")
    payload = data.get("payload", {})
    ccid = payload.get("call_control_id")
    session = SESSIONS.get(ccid)

    log(f"[{ccid}] webhook {event}")

    if event == "call.answered":
        log(f"[{ccid}] call answered — starting Telnyx playback + transcription")
        if session:
            session.connected_at = time.monotonic()
            session.answered_at = time.monotonic()
            # Claim speech ownership before media WS connects (avoids dual play).
            session.conversation_active = True
            asyncio.create_task(conversation.start(session, ccid))
        else:
            fail("answered_no_session", "answered webhook with no CallSession", ccid=ccid)

        if config.CALL_RECORDING:
            asyncio.create_task(_start_recording(ccid))

        # MEDIA_STREAM: inbound RTP → Deepgram Listen (nova-2-phonecall).
        # Otherwise: Telnyx-hosted transcription (Deepgram nova-2/nova-3).
        if config.MEDIA_STREAM:
            log(
                f"[{ccid}] STT via media stream + Deepgram "
                f"{config.DEEPGRAM_LISTEN_MODEL}"
            )
            # Open Listen when the media WS starts (not on answer). Preconnecting
            # left the socket idle through the greeting; Deepgram closed it
            # (~10s) before Telnyx sent the first inbound frame.
            asyncio.create_task(_start_media_stream(ccid))
        else:
            asyncio.create_task(_start_transcription(ccid))

    elif event == "streaming.failed":
        fail("streaming_failed", json.dumps(payload), ccid=ccid)

    elif event in ("call.playback.ended", "call.speak.ended"):
        if session:
            conversation.mark_playback_finished(session, payload.get("command_id"))
            log(f"[{ccid}] {event} status={payload.get('status')}")

    elif event == "call.transcription":
        data = payload.get("transcription_data") or payload
        text = (data.get("transcript") or payload.get("transcript") or "").strip()
        is_final = data.get("is_final", True)
        log(f"[{ccid}] transcription final={is_final} text={text!r}")
        if session and text and is_final:
            asyncio.create_task(conversation.handle_user_text(session, ccid, text))

    elif event in (
        "call.machine.detection.ended",
        "call.machine.premium.detection.ended",
    ):
        log(f"[{ccid}] AMD payload: {json.dumps(payload)}")
        raw_result = str(payload.get("result", ""))
        path = classify_amd(raw_result)
        if session:
            session.amd_result = path
            session.amd_known.set()
            if path == "machine":
                session.abort_to_voicemail = True
                await close_session_stt(session)
        log(f"[{ccid}] AMD raw={raw_result!r} -> {path}")

    elif event == "call.machine.premium.greeting.ended":
        if session:
            session.greeting_ended.set()

    elif event == "call.recording.saved":
        urls = {
            "mp3": payload.get("recording_urls", {}).get("mp3")
            or payload.get("public_recording_urls", {}).get("mp3"),
            "wav": payload.get("recording_urls", {}).get("wav")
            or payload.get("public_recording_urls", {}).get("wav"),
        }
        path = await records.download_recording(ccid, urls)
        if path:
            save_call_record(ccid, recording={"file": str(path), "urls": urls})
            if session and session.lead.get("lead_id"):
                db.update_lead(int(session.lead["lead_id"]), recording=str(path))
            log(f"[{ccid}] recording saved: {path}")

    elif event == "call.hangup":
        log(f"[{ccid}] hangup payload: {json.dumps(payload)}")
        cause = str(payload.get("hangup_cause") or "").lower()
        fail_hangup(ccid, payload)

        disposition = None
        if session:
            session.ended = True
            session.playback_done.set()
            if session.engine_task and not session.engine_task.done():
                session.engine_task.cancel()
            await close_session_stt(session)
            outcome = session.outcome or {}
            disposition = outcome.get("disposition")
            # Spoke but hung up before a terminal disposition — don't bury as bare failed.
            if not disposition:
                had_user = any(
                    t.get("role") == "user" and (t.get("content") or "").strip()
                    for t in (session.transcript or [])
                )
                if had_user:
                    runtime.record_outcome(
                        session, "no_decision", "callee hung up mid-conversation"
                    )
                    disposition = "no_decision"
            try:
                save_call_record(
                    ccid,
                    session,
                    hangup_cause=cause,
                    hangup_source=payload.get("hangup_source"),
                    sip_hangup_cause=payload.get("sip_hangup_cause"),
                    to=payload.get("to"),
                )
            except Exception as e:
                log(f"[{ccid}] save_call_record failed: {e}")
            SESSIONS.pop(ccid, None)

        if cause in _FAILED_HANGUPS or not disposition:
            status = "failed"
        else:
            status = "completed"
        db.update_lead_by_ccid(
            ccid,
            status=status,
            hangup_cause=cause or None,
            disposition=disposition,
            error=(None if status == "completed" else (cause or "hangup")),
        )
        dialer.kick()
        log(f"[{ccid}] lead marked {status} (cause={cause or 'n/a'})")

    elif event == "call.initiated":
        log(f"[{ccid}] initiated payload: {json.dumps(payload)}")

    return {"ok": True}
