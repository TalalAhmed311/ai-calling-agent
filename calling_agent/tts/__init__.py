"""Deepgram TTS + Listen helpers."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import websockets

from calling_agent import config
from calling_agent.log import fail, log
from calling_agent.session import CallSession

# Shared HTTP client set by app startup.
HTTP: httpx.AsyncClient | None = None


def listen_url(encoding: str, sample_rate: int = 8000) -> str:
    qs = urlencode({
        "model": "nova-2-phonecall",
        "encoding": encoding,
        "sample_rate": sample_rate,
        "channels": 1,
        "punctuate": "true",
        # utterance_end_ms is only accepted alongside interim results; sending
        # it with interim_results=false makes Deepgram reject the handshake 400.
        "interim_results": "true",
        "endpointing": config.STT_ENDPOINTING_MS,
        "utterance_end_ms": config.STT_UTTERANCE_END_MS,
        "mip_opt_out": "true",
    })
    return f"{config.DEEPGRAM_LISTEN_URL}?{qs}"


async def synthesize(text: str, encoding: str = "mulaw", sample_rate: int = 8000) -> bytes:
    client = HTTP or httpx.AsyncClient(timeout=30)
    owns = HTTP is None
    try:
        r = await client.post(
            config.DEEPGRAM_TTS_URL,
            params={
                "model": config.VOICE,
                "encoding": encoding,
                "sample_rate": sample_rate,
                "container": "none",
            },
            json={"text": text},
            headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
        )
        r.raise_for_status()
        return r.content
    finally:
        if owns:
            await client.aclose()


async def close_session_stt(session: CallSession | None) -> None:
    if session is None:
        return
    task = session.dg_connect_task
    session.dg_connect_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    ws = session.dg_ws
    session.dg_ws = None
    session.dg_encoding = None
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass


async def preconnect_listen(
    session: CallSession,
    ccid: str,
    encoding: str | None = None,
) -> None:
    encoding = encoding or config.EXPECTED_INBOUND_ENCODING
    try:
        ws = await websockets.connect(
            listen_url(encoding),
            additional_headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
            open_timeout=10,
        )
        if session.abort_to_voicemail or session.amd_result == "machine":
            await ws.close()
            return
        session.dg_ws = ws
        session.dg_encoding = encoding
        log(f"[{ccid}] Deepgram Listen preconnected ({encoding})")
    except Exception as e:
        fail("listen_preconnect_failed", str(e), ccid=ccid, encoding=encoding)
