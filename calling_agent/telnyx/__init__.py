"""Telnyx REST client + webhook signature verification."""

from __future__ import annotations

import base64
import time

import httpx
from fastapi import HTTPException
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from calling_agent import config
from calling_agent.log import fail, log
import calling_agent.tts as tts_mod


async def telnyx_post(path: str, payload: dict) -> dict:
    client = tts_mod.HTTP or httpx.AsyncClient(timeout=15)
    owns = tts_mod.HTTP is None
    try:
        r = await client.post(
            f"{config.TELNYX_API}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {config.TELNYX_API_KEY}"},
        )
        if r.is_error:
            fail(
                "telnyx_api",
                f"POST {path} -> {r.status_code}: {r.text[:400]}",
                status=r.status_code,
                path=path,
            )
            r.raise_for_status()
        return r.json() if r.content else {}
    finally:
        if owns:
            await client.aclose()


async def telnyx_hangup(ccid: str, delay: float = 0.0) -> None:
    if delay:
        import asyncio
        await asyncio.sleep(delay)
    try:
        await telnyx_post(f"/calls/{ccid}/actions/hangup", {})
    except Exception as e:
        fail("hangup_command_failed", str(e), ccid=ccid)


async def telnyx_playback_start(
    ccid: str,
    *,
    audio_b64: str | None = None,
    audio_url: str | None = None,
    command_id: str | None = None,
) -> dict:
    payload: dict = {
        "audio_type": "wav",
        "cache_audio": True,
        "stop": "all",
        "target_legs": "self",
    }
    if audio_b64:
        payload["playback_content"] = audio_b64
    elif audio_url:
        payload["audio_url"] = audio_url
    else:
        raise ValueError("playback needs playback_content or audio_url")
    if command_id:
        payload["command_id"] = command_id
    return await telnyx_post(f"/calls/{ccid}/actions/playback_start", payload)


async def telnyx_playback_stop(ccid: str) -> None:
    try:
        await telnyx_post(f"/calls/{ccid}/actions/playback_stop", {"stop": "all"})
    except Exception as e:
        fail("playback_stop_failed", str(e), ccid=ccid)


async def telnyx_speak(ccid: str, text: str, command_id: str | None = None) -> dict:
    payload: dict = {
        "payload": text,
        "voice": "female",
        "language": "en-US",
    }
    if command_id:
        payload["command_id"] = command_id
    return await telnyx_post(f"/calls/{ccid}/actions/speak", payload)


async def telnyx_transcription_start(ccid: str) -> dict:
    return await telnyx_post(
        f"/calls/{ccid}/actions/transcription_start",
        {
            "transcription_engine": "Google",
            "transcription_engine_config": {
                "transcription_engine": "Google",
                "language": "en",
                "interim_results": False,
            },
            "transcription_tracks": "inbound",
        },
    )


def verify_telnyx_signature(
    raw_body: bytes,
    signature_b64: str | None,
    timestamp: str | None,
) -> None:
    if not config.TELNYX_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="TELNYX_PUBLIC_KEY not configured")
    if not signature_b64 or not timestamp:
        raise HTTPException(status_code=401, detail="missing telnyx signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="bad telnyx-timestamp") from exc
    if abs(time.time() - ts) > config.WEBHOOK_MAX_AGE_S:
        raise HTTPException(status_code=401, detail="webhook timestamp too old")
    try:
        verify_key = VerifyKey(base64.b64decode(config.TELNYX_PUBLIC_KEY))
        signed = f"{timestamp}|".encode() + raw_body
        verify_key.verify(signed, base64.b64decode(signature_b64))
    except (BadSignatureError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc
