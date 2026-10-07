"""Call conversation: Telnyx playback + transcription webhooks.

Agent speech is injected by Telnyx (playback_start / speak). That path does
not use the media websocket, which dropped every campaign greeting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid

from calling_agent import config, runtime
from calling_agent.cache.wav import wav_duration_s
from calling_agent.decide import MAX_PROBES_PER_NODE, decide, decision_log
from calling_agent.graph.nodes import Decision
from calling_agent.log import fail, log
from calling_agent.session import CallSession
from calling_agent.telnyx import (
    telnyx_hangup,
    telnyx_playback_start,
    telnyx_playback_stop,
    telnyx_speak,
)

# Phrases that mean "I answered the phone / how can I help" — treat as gatekeeper.
_GATEKEEPER_OPENERS = (
    "how can i help",
    "how may i help",
    "how can i assist",
    "how may i assist",
    "what can i do for you",
    "who am i speaking",
    "who is this",
    "who's this",
)
# Consent / hold / leave-a-message IVR that AMD often labels "human".
_IVR_PHRASES = (
    "press or say",
    "press 1",
    "press 2",
    "to consent",
    "leave your message",
    "mailbox is full",
    "your call may be monitored",
    "analyzed by ai",
)


def _elapsed(session: CallSession) -> float:
    return round(time.monotonic() - (session.answered_at or time.monotonic()), 2)


def mark_playback_finished(session: CallSession, command_id: str | None = None) -> None:
    if command_id and session.playback_command_id and command_id != session.playback_command_id:
        return
    session.agent_speaking = False
    session.playback_done.set()
    # Silence wait starts after *we* finish speaking, not from greeting start.
    session.last_user_speech = time.monotonic()


async def stop_playback(ccid: str, session: CallSession) -> None:
    session.agent_speaking = False
    session.playback_done.set()
    session.last_user_speech = time.monotonic()
    await telnyx_playback_stop(ccid)


def _quick_decision(session: CallSession, text: str) -> Decision | None:
    """Bypass LLM for common IVR / gatekeeper openers."""
    low = " ".join((text or "").lower().split())
    if not low:
        return None
    if any(p in low for p in _IVR_PHRASES):
        return Decision(
            intent="machine",
            confidence=0.95,
            next_node="ended",
            play=["vm_drop"],
            end_call=True,
            disposition="IVR_OR_MACHINE",
        )
    if session.node == "await_role" and any(p in low for p in _GATEKEEPER_OPENERS):
        return Decision(
            intent="neither",
            confidence=0.85,
            next_node="await_gatekeeper_time",
            play=["ask_owner_manager_time"],
            disposition="not_decision_maker",
        )
    if session.node == "await_role" and low in ("hello", "hi", "hey", "yes", "yeah", "yep"):
        # They picked up; re-ask role once instead of generic unclear loop.
        return Decision(
            intent="unclear",
            confidence=0.6,
            next_node="await_role",
            play=["probe_role"],
        )
    return None


def _cap_probes(session: CallSession, d: Decision) -> Decision:
    """After MAX_PROBES unclear probes, exit instead of looping."""
    probing = d.play in (["probe_role"], ["probe_unclear"]) or (
        d.intent == "unclear" and not d.end_call and not d.disposition
    )
    if probing and session.probes_used >= MAX_PROBES_PER_NODE:
        return Decision(
            intent="unclear",
            confidence=d.confidence,
            next_node="ended",
            play=["polite_exit"],
            end_call=True,
            disposition="no_decision",
            slots=d.slots,
        )
    return d


async def play_clips(session: CallSession, ccid: str, clip_ids: list[str]) -> None:
    for cid in clip_ids:
        if session.ended:
            return
        text = runtime.CLIP_TEXTS.get(cid, "")
        if text:
            session.transcript.append(
                {"role": "agent", "clip": cid, "content": text, "at": _elapsed(session)}
            )
        await _play_one(session, ccid, cid, text)


async def _play_one(session: CallSession, ccid: str, clip_id: str, text: str) -> None:
    wav = runtime.AUDIO_CACHE.get_wav(clip_id)
    command_id = str(uuid.uuid4())
    session.playback_command_id = command_id
    session.playback_done = asyncio.Event()
    session.agent_speaking = True
    timeout = 12.0
    if wav:
        timeout = max(8.0, wav_duration_s(wav) + 4.0)

    played = False
    if wav:
        try:
            await telnyx_playback_start(
                ccid,
                audio_b64=base64.b64encode(wav).decode(),
                command_id=command_id,
            )
            played = True
            log(f"[{ccid}] playback_start {clip_id} ({len(wav)}B wav)")
        except Exception as exc:
            fail("playback_start_failed", f"{clip_id}: {exc}", ccid=ccid)
            try:
                await telnyx_playback_start(
                    ccid,
                    audio_url=f"https://{config.PUBLIC_HOST}/audio/{clip_id}.wav",
                    command_id=command_id,
                )
                played = True
                log(f"[{ccid}] playback_start url {clip_id}")
            except Exception as exc2:
                fail("playback_url_failed", f"{clip_id}: {exc2}", ccid=ccid)

    if not played and text:
        try:
            await telnyx_speak(ccid, text, command_id=command_id)
            played = True
            log(f"[{ccid}] speak fallback {clip_id}")
        except Exception as exc:
            fail("speak_failed", f"{clip_id}: {exc}", ccid=ccid)

    if not played:
        session.agent_speaking = False
        session.playback_done.set()
        fail("agent_silent", f"could not play {clip_id}", ccid=ccid)
        return

    try:
        await asyncio.wait_for(session.playback_done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log(f"[{ccid}] playback wait timed out for {clip_id}")
        session.agent_speaking = False
        session.playback_done.set()


async def handle_user_text(session: CallSession, ccid: str, text: str) -> None:
    text = (text or "").strip()
    if not text or session.ended:
        return
    if session.agent_speaking:
        log(f"[{ccid}] ignore transcript while speaking: {text!r}")
        return

    session.transcript.append(
        {"role": "user", "content": text, "at": _elapsed(session)}
    )
    session.last_user_speech = time.monotonic()
    session.silence_prompts = 0
    log(f"[user] {text}")

    async with session.deciding:
        if session.ended or session.node == "ended":
            return
        t_decide = time.monotonic()
        d = _quick_decision(session, text)
        if d is None:
            d = await decide(session.node, text, probes_used=session.probes_used)
        d = _cap_probes(session, d)
        log(
            f"[{ccid}] decide {json.dumps(decision_log(d))} "
            f"in {time.monotonic() - t_decide:.3f}s"
        )

        if d.intent == "unclear" or d.play == ["probe_role"] or d.play == ["probe_unclear"]:
            session.probes_used += 1
        else:
            session.probes_used = 0

        await stop_playback(ccid, session)

        if d.play:
            await play_clips(session, ccid, d.play)

        session.node = d.next_node
        if d.disposition or d.end_call:
            runtime.record_outcome(
                session,
                d.disposition or "no_decision",
                f"intent={d.intent} node={d.next_node}",
                d.slots,
            )
        if d.end_call:
            session.ended = True
            session.node = "ended"
            session.listen_open = False
            await telnyx_hangup(ccid, delay=config.END_CALL_GRACE_S)


async def start(session: CallSession, ccid: str) -> None:
    if session.engine_task and not session.engine_task.done():
        return
    session.conversation_active = True
    session.listen_open = True
    session.engine_task = asyncio.create_task(_run(session, ccid))


async def _run(session: CallSession, ccid: str) -> None:
    log(f"[{ccid}] conversation start")
    session.last_user_speech = time.monotonic()

    if session.amd_result == "machine" or session.abort_to_voicemail:
        await _exit_machine(session, ccid)
        return

    if not session.greeting_played:
        log(f"[{ccid}] greeting via Telnyx playback")
        await play_clips(session, ccid, ["greeting_identity"])
        session.greeting_played = True
        session.last_user_speech = time.monotonic()
        if session.answered_at:
            log(
                f"[{ccid}] latency: answer -> greeting done "
                f"+{_elapsed(session)}s"
            )

    if session.abort_to_voicemail or session.amd_result == "machine":
        await _exit_machine(session, ccid)
        return

    monitors = [
        asyncio.create_task(_silence_monitor(session, ccid)),
        asyncio.create_task(_talk_cap_monitor(session, ccid)),
        asyncio.create_task(_late_amd_watch(session, ccid)),
    ]
    try:
        while not session.ended:
            await asyncio.sleep(0.4)
    finally:
        for task in monitors:
            task.cancel()
        session.listen_open = False
        session.conversation_active = False
        log(f"[{ccid}] conversation closed")


async def _exit_machine(session: CallSession, ccid: str) -> None:
    if session.ended:
        return
    await play_clips(session, ccid, ["vm_drop"])
    runtime.record_outcome(session, "no_answer_voicemail", "AMD machine / voicemail")
    session.ended = True
    session.node = "ended"
    session.listen_open = False
    await telnyx_hangup(ccid, delay=0.5)


async def _silence_monitor(session: CallSession, ccid: str) -> None:
    while not session.ended:
        await asyncio.sleep(3)
        if not session.listen_open or session.agent_speaking:
            continue
        quiet = time.monotonic() - session.last_user_speech
        limit = (
            config.SILENCE_PROMPT_S
            if session.silence_prompts == 0
            else config.SILENCE_PROMPT_2_S
        )
        if quiet < limit:
            continue
        if session.silence_prompts >= config.MAX_SILENCE_PROMPTS:
            runtime.record_outcome(session, "no_decision", "silence timeout")
            fail(
                "silence_timeout",
                "no speech transcribed after silence probes; hanging up",
                ccid=ccid,
            )
            session.ended = True
            await telnyx_hangup(ccid, delay=0.5)
            return
        session.silence_prompts += 1
        session.last_user_speech = time.monotonic()
        log(f"[{ccid}] silence probe #{session.silence_prompts}")
        await play_clips(session, ccid, ["still_there"])


async def _talk_cap_monitor(session: CallSession, ccid: str) -> None:
    while not session.ended:
        await asyncio.sleep(5)
        started = session.connected_at or session.answered_at
        if started is None:
            continue
        if time.monotonic() - started >= config.MAX_TALK_S:
            log(f"[{ccid}] max talk time reached")
            runtime.record_outcome(session, "no_decision", "max talk time")
            session.ended = True
            await play_clips(session, ccid, ["polite_exit"])
            await telnyx_hangup(ccid, delay=0.5)
            return


async def _late_amd_watch(session: CallSession, ccid: str) -> None:
    while not session.ended:
        await asyncio.sleep(0.5)
        if session.abort_to_voicemail:
            log(f"[{ccid}] late machine detection")
            await stop_playback(ccid, session)
            await _exit_machine(session, ccid)
            return
