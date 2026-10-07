"""FastAPI application entrypoint."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from calling_agent import config, dialer, runtime, situations
from calling_agent.api import router
from calling_agent.api.campaigns import router as campaigns_router
from calling_agent.cache import bake_clips
from calling_agent.db import init as db_init
from calling_agent.log import fail, log
from calling_agent.media import media_bridge
from calling_agent.tts import HTTP, synthesize
import calling_agent.tts as tts_mod

app = FastAPI(title="Outbound Calling Agent", version="2.0.0")
app.include_router(router)
app.include_router(campaigns_router)
app.add_api_websocket_route("/telnyx/media", media_bridge)


@app.on_event("startup")
async def warmup() -> None:
    tts_mod.HTTP = httpx.AsyncClient(
        timeout=30,
        headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        await tts_mod.HTTP.get("https://api.deepgram.com/v1/projects")
    except Exception as e:
        print(f"warmup deepgram ping: {e}")
    try:
        async with httpx.AsyncClient(timeout=10) as tmp:
            await tmp.get(
                f"{config.TELNYX_API}/balance",
                headers={"Authorization": f"Bearer {config.TELNYX_API_KEY}"},
            )
    except Exception as e:
        print(f"warmup telnyx ping: {e}")

    db_init()
    try:
        await bake_clips(runtime.AUDIO_CACHE, runtime.CLIP_TEXTS, synthesize, force=False)
    except Exception as e:
        fail("cache_bake_failed", str(e))
        raise
    if config.SITUATION_CACHE:
        try:
            n = await situations.warmup_from_db()
            log(f"[situation] enabled — {n} learned replies loaded")
        except Exception as e:
            fail("situation_warmup_failed", str(e))
    else:
        log("[situation] disabled")
    log(f"[cache] ready — {len(runtime.CLIP_TEXTS)} clips x 2 encodings")
    log(
        f"[speech] Telnyx playback ({len(runtime.AUDIO_CACHE.wavs)} wav clips); "
        f"media_stream={'on' if config.MEDIA_STREAM else 'off'}"
    )
    log(
        f"[media] outbound codec={config.TELNYX_BIDIRECTIONAL_CODEC} "
        f"(playback={config.PLAYBACK_ENCODING}); "
        f"expected inbound={config.EXPECTED_INBOUND_ENCODING}"
    )
    if config.azure_openai_configured():
        log(f"[decide] Azure OpenAI enabled: {config.azure_chat_completions_url()}")
    else:
        log("[decide] Azure OpenAI not configured — rules-only decisions")
    dialer.start()
    log(f"[dialer] max concurrent={config.MAX_CONCURRENT_CALLS}")


@app.on_event("shutdown")
async def shutdown() -> None:
    dialer.stop()
    if tts_mod.HTTP is not None:
        await tts_mod.HTTP.aclose()
        tts_mod.HTTP = None
