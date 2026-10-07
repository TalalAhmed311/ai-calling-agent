"""Learn replies for novel call situations: DB lookup → LLM + TTS cache on miss."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import httpx

from calling_agent import config, db, runtime
from calling_agent.cache import bake_clips
from calling_agent.graph.nodes import Decision, _REQUEUE_CLIPS
from calling_agent.log import fail, log
from calling_agent.tts import synthesize

_FILLER = frozenset(
    "um uh like so well okay ok yeah yea hmm ah oh please".split()
)

_REPLY_SYSTEM = (
    "You write ONE short spoken reply for an outbound sales phone agent.\n"
    'Return ONLY compact JSON: {"reply":"<spoken text>"}\n'
    "\n"
    "Rules:\n"
    f"- You are {config.AGENT_NAME} calling on behalf of {config.COMPANY_NAME}.\n"
    "- Max 2 short sentences. Conversational phone English. No markdown.\n"
    "- Answer the caller's question or comment helpfully, then stop.\n"
    "- Do NOT end with a question — the script will re-ask its own question next.\n"
    "- Do NOT invent prices, discounts, contracts, or legal claims.\n"
    "- Do NOT offer to transfer, email docs, or schedule complex demos.\n"
    "- Do NOT admit you are an AI, bot, or recording — speak as a human agent.\n"
    "- Keep it under 220 characters.\n"
)


def normalize_situation(text: str) -> str:
    low = (text or "").lower()
    low = re.sub(r"[^a-z0-9\s']", " ", low)
    parts = [p for p in low.split() if p and p not in _FILLER]
    return " ".join(parts).strip()


def clip_id_for(node: str, situation_key: str) -> str:
    digest = hashlib.sha1(f"{node}|{situation_key}".encode()).hexdigest()[:16]
    return f"dyn_{digest}"


def _register_clip(clip_id: str, reply_text: str) -> None:
    runtime.CLIP_TEXTS[clip_id] = reply_text


async def _bake_reply(clip_id: str, reply_text: str) -> bool:
    """Synthesize Deepgram TTS into the shared audio cache."""
    _register_clip(clip_id, reply_text)
    try:
        await bake_clips(
            runtime.AUDIO_CACHE,
            {clip_id: reply_text},
            synthesize,
            force=False,
        )
    except Exception as exc:
        fail("situation_bake_failed", str(exc), clip_id=clip_id)
        return False
    wav = runtime.AUDIO_CACHE.get_wav(clip_id)
    if not wav:
        fail("situation_wav_missing", clip_id, clip_id=clip_id)
        return False
    return True


async def _generate_reply(node: str, user_text: str) -> str | None:
    url = config.azure_chat_completions_url()
    if not url:
        return None
    payload = {
        "messages": [
            {"role": "system", "content": _REPLY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f'Call graph node: {node}\n'
                    f'Caller said: "{user_text}"\n'
                    "Write the agent's spoken reply."
                ),
            },
        ],
        "temperature": 0.4,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post(
                url,
                headers={
                    "api-key": config.AZURE_OPENAI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.is_error:
                fail("situation_llm_error", f"{r.status_code}: {r.text[:300]}")
                return None
            content = (
                ((r.json().get("choices") or [{}])[0].get("message") or {})
                .get("content")
                or ""
            )
    except Exception as exc:
        fail("situation_llm_failed", str(exc))
        return None

    data: dict[str, Any] | None
    try:
        data = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content or "", re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None

    reply = str((data or {}).get("reply") or "").strip()
    reply = re.sub(r"\s+", " ", reply)
    if len(reply) < 8:
        return None
    if len(reply) > 240:
        reply = reply[:237].rsplit(" ", 1)[0] + "."
    return reply


async def ensure_audio(row: dict[str, Any]) -> str | None:
    """Make sure clip audio is loaded; rebake from reply_text if missing."""
    clip_id = str(row.get("clip_id") or "")
    reply = str(row.get("reply_text") or "")
    if not clip_id or not reply:
        return None
    _register_clip(clip_id, reply)
    if runtime.AUDIO_CACHE.get_wav(clip_id):
        return clip_id
    # Try disk load then bake.
    runtime.AUDIO_CACHE.load_from_disk([clip_id])
    if runtime.AUDIO_CACHE.get_wav(clip_id):
        return clip_id
    ok = await _bake_reply(clip_id, reply)
    return clip_id if ok else None


async def resolve(node: str, user_text: str) -> dict[str, Any] | None:
    """Lookup or create a cached situation reply.

    Returns {clip_id, reply_text, cached: bool} or None on failure.
    """
    if not config.SITUATION_CACHE:
        return None
    key = normalize_situation(user_text)
    if len(key) < 4:
        return None

    existing = db.find_situation_fuzzy(node, key)
    if existing:
        clip_id = await ensure_audio(existing)
        if clip_id:
            if existing.get("id") is not None:
                db.bump_situation_hit(int(existing["id"]))
            log(
                f"[situation] HIT node={node} key={key!r} "
                f"clip={clip_id} hits={int(existing.get('hit_count') or 0) + 1}"
            )
            return {
                "clip_id": clip_id,
                "reply_text": existing.get("reply_text") or runtime.CLIP_TEXTS.get(clip_id, ""),
                "cached": True,
                "situation_key": key,
            }

    t0 = time.monotonic()
    reply = await _generate_reply(node, user_text)
    if not reply:
        return None
    clip_id = clip_id_for(node, key)
    ok = await _bake_reply(clip_id, reply)
    if not ok:
        return None
    db.upsert_situation(
        node=node,
        situation_key=key,
        user_text=user_text,
        reply_text=reply,
        clip_id=clip_id,
    )
    log(
        f"[situation] MISS→CACHE node={node} key={key!r} clip={clip_id} "
        f"in {time.monotonic() - t0:.2f}s reply={reply!r}"
    )
    return {
        "clip_id": clip_id,
        "reply_text": reply,
        "cached": False,
        "situation_key": key,
    }


def decision_from_resolve(node: str, resolved: dict[str, Any]) -> Decision:
    requeue = list(_REQUEUE_CLIPS.get(node, ["probe_unclear"]))
    clip_id = resolved["clip_id"]
    # Don't immediately re-ask with the same probe if that's all we have and
    # the dynamic clip already ends conversationally — still requeue so the
    # graph question is restored.
    return Decision(
        intent="situation_reply",
        confidence=0.85,
        next_node=node,
        play=[clip_id, *requeue],
        slots={
            "situation_key": resolved.get("situation_key"),
            "situation_cached": bool(resolved.get("cached")),
        },
    )


async def warmup_from_db() -> int:
    """Load known situation clips into CLIP_TEXTS + audio cache at startup."""
    db.init()
    rows = db.list_situations(limit=500)
    if not rows:
        log("[situation] cache empty")
        return 0
    texts = {
        str(r["clip_id"]): str(r["reply_text"])
        for r in rows
        if r.get("clip_id") and r.get("reply_text")
    }
    for cid, text in texts.items():
        _register_clip(cid, text)
    loaded = runtime.AUDIO_CACHE.load_from_disk(texts.keys())
    missing = {
        cid: text
        for cid, text in texts.items()
        if not runtime.AUDIO_CACHE.get_wav(cid)
    }
    if missing:
        try:
            await bake_clips(runtime.AUDIO_CACHE, missing, synthesize, force=False)
        except Exception as exc:
            fail("situation_warmup_bake_failed", str(exc))
    log(f"[situation] warmed {len(texts)} replies ({loaded} raw files from disk)")
    return len(texts)
