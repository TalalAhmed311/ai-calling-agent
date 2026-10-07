"""Telnyx media websocket bridge — cache-first playback + STT decide loop."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from calling_agent import config, conversation, runtime
from calling_agent.amd import telnyx_encoding_to_deepgram
from calling_agent.cache import PlayQueue
from calling_agent.decide import decide, decision_log
from calling_agent.log import fail, log
from calling_agent.media.levels import describe_level
from calling_agent.session import SESSIONS, CallSession
from calling_agent.telnyx import telnyx_hangup
from calling_agent.tts import close_session_stt, listen_url


async def media_bridge(telnyx_ws: WebSocket):
    await telnyx_ws.accept()

    state: dict[str, Any] = {
        "stream_id": None,
        "ccid": None,
        "encoding": config.EXPECTED_INBOUND_ENCODING,
        "sample_rate": 8000,
        "start_seen": asyncio.Event(),
        "listen_open": False,
    }
    t_media_first: float | None = None
    t_hot = time.monotonic()

    async def send_media(audio: bytes) -> None:
        if not state["stream_id"]:
            return
        await telnyx_ws.send_json({
            "event": "media",
            "stream_id": state["stream_id"],
            "media": {"payload": base64.b64encode(audio).decode()},
        })

    async def clear_telnyx() -> None:
        if not state["stream_id"]:
            return
        await telnyx_ws.send_json({
            "event": "clear",
            "stream_id": state["stream_id"],
        })

    stt_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    # Rolling window of inbound audio so we can log whether the caller is
    # actually audible, independent of whatever Deepgram returns.
    probe: dict[str, Any] = {"buf": bytearray(), "at": time.monotonic(), "sent": 0}
    dump = bytearray() if config.DUMP_INBOUND_AUDIO else None

    async def read_telnyx():
        nonlocal t_media_first
        while True:
            frame = json.loads(await telnyx_ws.receive_text())
            event = frame.get("event")

            if event == "start":
                log(f"[media] RAW start frame: {json.dumps(frame)}")
                start = frame.get("start", {})
                media_format = start.get("media_format") or {}
                state["stream_id"] = frame.get("stream_id")
                state["ccid"] = start.get("call_control_id")
                state["encoding"] = telnyx_encoding_to_deepgram(media_format.get("encoding"))
                state["sample_rate"] = int(media_format.get("sample_rate") or 8000)
                state["start_seen"].set()
                log(
                    f"[{state['ccid']}] stream started "
                    f"stream_id={state['stream_id']} in={state['encoding']} "
                    f"out={config.PLAYBACK_ENCODING} "
                    f"({config.TELNYX_BIDIRECTIONAL_CODEC}) rate={state['sample_rate']}"
                )

            elif event == "media":
                if t_media_first is None:
                    t_media_first = time.monotonic()
                    log(f"[{state['ccid']}] latency: first media frame t=0")
                audio = base64.b64decode(frame["media"]["payload"])
                if config.ECHO_TEST:
                    await send_media(audio)
                    continue
                if dump is not None:
                    dump.extend(audio)
                probe["buf"].extend(audio)
                now = time.monotonic()
                if now - probe["at"] >= config.AUDIO_LEVEL_LOG_S:
                    log(
                        f"[{state['ccid']}] inbound {state['encoding']} "
                        f"{describe_level(bytes(probe['buf']), state['encoding'])} "
                        f"-> stt={probe['sent']} frames"
                    )
                    probe["buf"].clear()
                    probe["at"] = now
                    probe["sent"] = 0
                if state["listen_open"]:
                    probe["sent"] += 1
                    await stt_queue.put(audio)

            elif event == "stop":
                await stt_queue.put(None)
                break

    reader = asyncio.create_task(read_telnyx())

    try:
        await asyncio.wait_for(state["start_seen"].wait(), timeout=30.0)
    except asyncio.TimeoutError:
        log("[media] no start frame within 30s; closing")
        fail("media_no_start", "Telnyx media websocket never sent a start frame")
        reader.cancel()
        return

    session = SESSIONS.get(state["ccid"])
    if session is None:
        session = CallSession({})
        SESSIONS[state["ccid"]] = session
    if session.connected_at is None:
        session.connected_at = time.monotonic()

    # Only one media bridge should own a live call. Extra WS reconnects from
    # flaky tunnels used to re-play the greeting in a loop.
    session.media_epoch += 1
    media_epoch = session.media_epoch
    session.media_owner = media_epoch
    log(f"[{state['ccid']}] media owner epoch={media_epoch}")

    if session.conversation_active:
        log(
            f"[{state['ccid']}] media ws inbound-only "
            "(conversation engine owns speech)"
        )
        try:
            await reader
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        return

    if config.ECHO_TEST:
        log(f"[{state['ccid']}] config.ECHO_TEST on")
        try:
            await reader
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        return

    encoding = state["encoding"]
    sample_rate = state["sample_rate"]
    ccid = state["ccid"]

    try:
        await asyncio.wait_for(session.amd_known.wait(), timeout=config.AMD_TIMEOUT_S)
    except asyncio.TimeoutError:
        log(f"[{ccid}] AMD timed out, assuming human")
        session.amd_result = "human"

    # Playback encoding tracks the codec we declared to Telnyx, not the inbound
    # track. Using the inbound encoding here made Telnyx decode A-law bytes as
    # PCMU, so the callee heard distortion instead of speech.
    player = PlayQueue(
        send_media,
        ccid=ccid,
        encoding=config.PLAYBACK_ENCODING,
        cache=runtime.AUDIO_CACHE,
        sample_rate=sample_rate,
    )
    player.start()

    def elapsed() -> float:
        return round(time.monotonic() - (session.answered_at or t_hot), 2)

    async def play_and_wait(clip_ids: list[str]) -> None:
        # Log what the agent says here so the transcript has both sides.
        for cid in clip_ids:
            text = runtime.CLIP_TEXTS.get(cid)
            if text:
                session.transcript.append(
                    {"role": "agent", "clip": cid, "content": text, "at": elapsed()}
                )
        await player.play_clips(clip_ids)
        await player.drain()

    async def abandon(disposition: str, summary: str, clips: list[str] | None = None) -> None:
        """Leave the call cleanly. Never leave a live leg with no ears."""
        state["listen_open"] = False
        if clips:
            try:
                await play_and_wait(clips)
            except Exception as exc:
                log(f"[{ccid}] exit clip failed: {exc}")
                fail("exit_clip_failed", str(exc), ccid=ccid)
        runtime.record_outcome(session, disposition, summary)
        session.ended = True
        await telnyx_hangup(ccid, delay=0.5)
        await player.stop()
        reader.cancel()

    # Machine path: play vm clip then hang up.
    if session.amd_result == "machine" or session.abort_to_voicemail:
        await close_session_stt(session)
        await abandon("no_answer_voicemail", "AMD machine / voicemail", ["vm_drop"])
        return

    # ---- Human path: cached greeting once per call ----
    if not session.greeting_played:
        log(f"[{ccid}] cache-first greeting")
        await play_and_wait(["greeting_identity"])
        session.greeting_played = True
        if player.first_byte_at:
            log(
                f"[{ccid}] latency: first outbound byte "
                f"+{player.first_byte_at - t_hot:.3f}s after media ws"
            )
            if session.answered_at:
                log(
                    f"[{ccid}] latency: answer -> first audio "
                    f"+{player.first_byte_at - session.answered_at:.3f}s"
                )
    else:
        log(f"[{ccid}] skip greeting (already played; media reconnect)")

    if session.media_owner != media_epoch or session.ended:
        log(f"[{ccid}] media epoch superseded after greeting — exiting")
        await player.stop()
        reader.cancel()
        return

    if session.abort_to_voicemail:
        await abandon("IVR_OR_MACHINE", "late AMD machine", ["vm_drop"])
        return

    # Reuse the preconnected Listen socket when Telnyx negotiated the encoding
    # we guessed; otherwise reopen against the real one.
    dg_ws = session.dg_ws
    if dg_ws is not None and session.dg_encoding == encoding:
        log(f"[{ccid}] reusing preconnected Deepgram Listen ({encoding})")
        session.dg_ws = None
    else:
        if dg_ws is not None:
            log(
                f"[{ccid}] preconnect encoding {session.dg_encoding} != {encoding}; "
                f"reopening"
            )
        await close_session_stt(session)
        t_stt = time.monotonic()
        try:
            dg_ws = await asyncio.wait_for(
                websockets.connect(
                    listen_url(encoding, sample_rate),
                    additional_headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
                    open_timeout=8,
                ),
                timeout=10.0,
            )
            log(
                f"[{ccid}] Deepgram Listen opened ({encoding}) "
                f"in {time.monotonic()-t_stt:.3f}s"
            )
        except Exception as e:
            log(f"[{ccid}] Deepgram Listen open failed: {e}")
            fail("listen_open_failed", str(e), ccid=ccid, encoding=encoding)
            await abandon("no_decision", f"stt_unavailable: {e}", ["polite_exit"])
            return

    if session.media_owner != media_epoch or session.ended:
        log(f"[{ccid}] media epoch superseded after STT open — exiting")
        try:
            await dg_ws.close()
        except Exception:
            pass
        await player.stop()
        reader.cancel()
        return

    state["listen_open"] = True
    session.last_user_speech = time.monotonic()
    if session.node == "ended":
        session.node = "await_role"
    log(f"[{ccid}] mic open — node={session.node}")

    deciding = asyncio.Lock()

    async def handle_user_text(text: str) -> None:
        text = (text or "").strip()
        if not text or session.ended:
            return
        session.transcript.append(
            {"role": "user", "content": text, "at": elapsed()}
        )
        session.last_user_speech = time.monotonic()
        session.silence_prompts = 0
        log(f"[user] {text}")

        async with deciding:
            if session.ended or session.node == "ended":
                return
            t_decide = time.monotonic()
            d = conversation._quick_decision(session, text)
            if d is None:
                d = await decide(session.node, text, probes_used=session.probes_used)
            d = conversation._cap_probes(session, d)
            log(
                f"[{ccid}] decide {json.dumps(decision_log(d))} "
                f"in {time.monotonic()-t_decide:.3f}s"
            )

            if d.intent == "unclear" or d.play == ["probe_role"] or d.play == ["probe_unclear"]:
                session.probes_used += 1
            else:
                session.probes_used = 0

            # Barge-in: stop current playback before next clips.
            await player.clear()
            await clear_telnyx()

            if d.play:
                await play_and_wait(d.play)

            session.node = d.next_node
            if d.disposition or d.end_call:
                summary = f"intent={d.intent} node={d.next_node}"
                runtime.record_outcome(
                    session,
                    d.disposition or "no_decision",
                    summary,
                    d.slots,
                )
            if d.end_call:
                session.ended = True
                session.node = "ended"
                state["listen_open"] = False
                await telnyx_hangup(ccid, delay=config.END_CALL_GRACE_S)

    async def stt_sender():
        try:
            while True:
                chunk = await stt_queue.get()
                if chunk is None:
                    try:
                        await dg_ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass
                    break
                await dg_ws.send(chunk)
        except Exception as e:
            log(f"[{ccid}] stt_sender error: {e}")
            fail("stt_sender", str(e), ccid=ccid)

    async def stt_receiver():
        # Interim results are on (utterance_end_ms requires them), so finalized
        # segments are buffered and only acted on at end of utterance. Acting on
        # every is_final would decide on half a sentence.
        pending: list[str] = []

        async def flush() -> None:
            if not pending:
                return
            text = " ".join(pending).strip()
            pending.clear()
            await handle_user_text(text)

        try:
            async for message in dg_ws:
                if isinstance(message, bytes):
                    continue
                msg = json.loads(message)
                mtype = msg.get("type")

                if mtype == "UtteranceEnd":
                    log(f"[{ccid}] dg UtteranceEnd")
                    await flush()
                    continue
                if mtype != "Results":
                    # SpeechStarted / Metadata / Error say whether Deepgram is
                    # hearing anything, which empty transcripts do not.
                    log(f"[{ccid}] dg {mtype}: {json.dumps(msg)[:220]}")
                    if mtype == "Error":
                        fail("deepgram_error", json.dumps(msg)[:400], ccid=ccid)
                    continue

                alts = (msg.get("channel") or {}).get("alternatives") or []
                if not alts:
                    continue
                text = (alts[0].get("transcript") or "").strip()
                if not text or not msg.get("is_final"):
                    continue
                log(
                    f"[{ccid}] dg final={text!r} "
                    f"speech_final={bool(msg.get('speech_final'))}"
                )
                pending.append(text)
                if msg.get("speech_final"):
                    await flush()
        except (websockets.ConnectionClosed, WebSocketDisconnect):
            pass
        except Exception as e:
            log(f"[{ccid}] stt_receiver error: {e}")
            fail("stt_receiver", str(e), ccid=ccid)

    async def silence_monitor():
        while not session.ended:
            await asyncio.sleep(3)
            if not state["listen_open"]:
                continue
            quiet = time.monotonic() - session.last_user_speech
            limit = config.SILENCE_PROMPT_S if session.silence_prompts == 0 else config.SILENCE_PROMPT_2_S
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
            await player.clear()
            await clear_telnyx()
            await play_and_wait(["still_there"])

    async def talk_cap_monitor():
        while not session.ended:
            await asyncio.sleep(5)
            started = session.connected_at or t_hot
            if time.monotonic() - started >= config.MAX_TALK_S:
                log(f"[{ccid}] max talk time reached")
                runtime.record_outcome(session, "no_decision", "max talk time")
                session.ended = True
                await play_and_wait(["polite_exit"])
                await telnyx_hangup(ccid, delay=0.5)
                return

    async def late_amd_watch():
        while not session.ended:
            await asyncio.sleep(0.5)
            if session.abort_to_voicemail:
                log(f"[{ccid}] late machine detection")
                state["listen_open"] = False
                await player.clear()
                await clear_telnyx()
                await play_and_wait(["vm_drop"])
                runtime.record_outcome(session, "IVR_OR_MACHINE", "late AMD machine")
                session.ended = True
                await telnyx_hangup(ccid, delay=0.5)
                return

    tasks = [
        asyncio.create_task(stt_sender()),
        asyncio.create_task(stt_receiver()),
        asyncio.create_task(silence_monitor()),
        asyncio.create_task(talk_cap_monitor()),
        asyncio.create_task(late_amd_watch()),
        reader,
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        log(f"[{ccid}] bridge error: {e}")
        fail("bridge_error", str(e), ccid=ccid)
    finally:
        session.ended = True
        state["listen_open"] = False
        for t in tasks:
            t.cancel()
        await player.stop()
        try:
            await dg_ws.close()
        except Exception:
            pass
        await close_session_stt(session)
        if dump:
            config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = config.DEBUG_DIR / f"{ccid.replace(':', '_')}.{encoding}.raw"
            path.write_bytes(bytes(dump))
            log(f"[{ccid}] inbound audio dumped: {path} ({len(dump)}B)")
        log(f"[{ccid}] closed")
