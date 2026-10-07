"""Paced 20ms PlayQueue for Telnyx media."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from calling_agent.cache.store import AudioCache
from calling_agent.log import fail, log

FRAME_MS = 20
FRAME_BYTES = 160
BYTES_PER_MS = 8

# Stay this far ahead of real time so Telnyx's jitter buffer never underruns.
LEAD_S = 0.10
# Telnyx discards frames sent before the RTP leg is fully up, which clips the
# first word. A short silence pre-roll absorbs that.
PREROLL_MS = 200
SILENCE_BYTE = {"mulaw": 0xFF, "alaw": 0xD5}

SendMedia = Callable[[bytes], Awaitable[None]]


class PlayQueue:
    """Paced 20ms frame player with clear/barge-in support."""

    def __init__(
        self,
        send_media: SendMedia,
        *,
        ccid: str,
        encoding: str,
        cache: AudioCache,
        sample_rate: int = 8000,
    ):
        self.send_media = send_media
        self.ccid = ccid
        self.encoding = encoding
        self.cache = cache
        self.sample_rate = sample_rate
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._clear = asyncio.Event()
        self._playing = False
        self._task: asyncio.Task | None = None
        self._preroll_sent = False
        self.first_byte_at: float | None = None
        self.bytes_sent = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self.clear()
        await self._queue.put(None)
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def clear(self) -> None:
        self._clear.set()
        # drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._clear.clear()
        self._playing = False

    async def play_clip(self, clip_id: str) -> None:
        audio = self.cache.get(clip_id, self.encoding)
        if not audio:
            fail(
                "missing_clip",
                f"{clip_id}/{self.encoding}",
                ccid=self.ccid,
            )
            return
        await self._queue.put(audio)

    async def play_clips(self, clip_ids: list[str]) -> None:
        for cid in clip_ids:
            await self.play_clip(cid)

    async def play_raw(self, audio: bytes, label: str = "raw") -> None:
        log(f"[{self.ccid}] queue raw {label} ({len(audio)}B)")
        await self._queue.put(audio)

    async def drain(self) -> None:
        """Wait until current queue finishes."""
        while self._playing or not self._queue.empty():
            await asyncio.sleep(0.05)

    def _silence(self, ms: int) -> bytes:
        return bytes([SILENCE_BYTE.get(self.encoding, 0xFF)]) * (ms * BYTES_PER_MS)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            await self._stream(item)

    async def _stream(self, audio: bytes) -> None:
        self._playing = True
        if not self._preroll_sent:
            audio = self._silence(PREROLL_MS) + audio
            self._preroll_sent = True
        t0 = time.monotonic()
        for idx, offset in enumerate(range(0, len(audio), FRAME_BYTES)):
            if self._clear.is_set():
                break
            chunk = audio[offset : offset + FRAME_BYTES]
            if self.first_byte_at is None:
                self.first_byte_at = time.monotonic()
            await self.send_media(chunk)
            self.bytes_sent += len(chunk)
            # Absolute deadline, not a fixed sleep: per-frame sleep overshoot
            # used to accumulate and push delivery ~5% slower than real time.
            target = t0 + (idx + 1) * (FRAME_MS / 1000.0) - LEAD_S
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        self._playing = False
        log(
            f"[{self.ccid}] played {len(audio)}B "
            f"({len(audio)/(BYTES_PER_MS*1000):.2f}s audio) "
            f"in {time.monotonic()-t0:.2f}s (sent={self.bytes_sent})"
        )
