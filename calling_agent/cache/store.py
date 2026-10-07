"""In-memory telephony audio cache + bake helpers."""
from __future__ import annotations

from calling_agent.cache.wav import mulaw_to_wav, wav_duration_s
from calling_agent.log import fail, log

import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable, Iterable

ENCODINGS = ("mulaw", "alaw")
Synthesize = Callable[[str, str, int], Awaitable[bytes]]


class AudioCache:
    """encoding -> clip_id -> raw pcm bytes."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.data: dict[str, dict[str, bytes]] = {e: {} for e in ENCODINGS}
        self.wavs: dict[str, bytes] = {}
        self.manifest: dict = {"clips": {}, "version": "v1"}

    def get(self, clip_id: str, encoding: str) -> bytes | None:
        return self.data.get(encoding, {}).get(clip_id)

    def get_wav(self, clip_id: str) -> bytes | None:
        wav = self.wavs.get(clip_id)
        if wav:
            return wav
        return self.ensure_wav(clip_id)

    def ensure_wav(self, clip_id: str) -> bytes | None:
        existing = self.wavs.get(clip_id)
        if existing:
            return existing
        path = self.root / "v1" / "wav_8k" / f"{clip_id}.wav"
        if path.exists():
            wav = path.read_bytes()
            self.wavs[clip_id] = wav
            return wav
        mulaw = self.get(clip_id, "mulaw")
        if not mulaw:
            return None
        wav = mulaw_to_wav(mulaw)
        self.wavs[clip_id] = wav
        self.persist_wav(clip_id, wav)
        return wav

    def persist_wav(self, clip_id: str, wav: bytes) -> None:
        folder = self.root / "v1" / "wav_8k"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{clip_id}.wav").write_bytes(wav)
        meta = self.manifest["clips"].setdefault(clip_id, {"text": "", "encodings": {}})
        meta["wav_bytes"] = len(wav)
        meta["wav_duration_ms"] = int(wav_duration_s(wav) * 1000)

    def put(self, clip_id: str, encoding: str, audio: bytes, text: str) -> None:
        self.data.setdefault(encoding, {})[clip_id] = audio
        meta = self.manifest["clips"].setdefault(clip_id, {"text": text, "encodings": {}})
        meta["text"] = text
        meta["encodings"][encoding] = {
            "bytes": len(audio),
            "duration_ms": int(len(audio) / 8),  # 8k 8-bit
            "sha1": hashlib.sha1(audio).hexdigest()[:12],
        }

    def missing(self, clip_ids: Iterable[str]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for cid in clip_ids:
            for enc in ENCODINGS:
                if not self.get(cid, enc):
                    out.append((cid, enc))
        return out

    def save_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")

    def persist_clip(self, clip_id: str, encoding: str, audio: bytes) -> None:
        folder = self.root / "v1" / f"{encoding}_8k"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{clip_id}.raw").write_bytes(audio)

    def load_from_disk(self, clip_ids: Iterable[str]) -> int:
        loaded = 0
        for cid in clip_ids:
            for enc in ENCODINGS:
                path = self.root / "v1" / f"{enc}_8k" / f"{cid}.raw"
                if path.exists():
                    self.data.setdefault(enc, {})[cid] = path.read_bytes()
                    loaded += 1
        return loaded


async def bake_clips(
    cache: AudioCache,
    texts: dict[str, str],
    synthesize: Synthesize,
    *,
    force: bool = False,
) -> None:
    """Synthesize any missing clips into memory (+ disk)."""
    loaded = cache.load_from_disk(texts.keys())
    log(f"[cache] loaded {loaded} clip files from disk")

    for clip_id, text in texts.items():
        for enc in ENCODINGS:
            if not force and cache.get(clip_id, enc):
                # refresh manifest metadata only
                audio = cache.get(clip_id, enc) or b""
                cache.put(clip_id, enc, audio, text)
                continue
            log(f"[cache] baking {clip_id} ({enc})…")
            audio = await synthesize(text, enc, 8000)
            cache.put(clip_id, enc, audio, text)
            cache.persist_clip(clip_id, enc, audio)
            log(
                f"[cache] {clip_id}/{enc}: {len(audio)} bytes "
                f"({len(audio)/8000:.1f}s)"
            )
        wav = cache.ensure_wav(clip_id)
        if wav:
            log(f"[cache] {clip_id}/wav: {len(wav)} bytes ({wav_duration_s(wav):.1f}s)")
        else:
            fail("cache_wav_missing", clip_id)
    cache.save_manifest()
    missing = cache.missing(texts.keys())
    if missing:
        fail("cache_incomplete", str(missing[:8]))
        raise RuntimeError(f"cache incomplete: {missing[:5]}…")
