"""Convert telephony mu-law to 16-bit PCM WAV for Telnyx playback."""
from __future__ import annotations

import audioop
import struct

SAMPLE_RATE = 8000


def mulaw_to_wav(mulaw: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    pcm = audioop.ulaw2lin(mulaw, 2)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


def wav_duration_s(wav: bytes) -> float:
    if len(wav) <= 44:
        return 0.0
    return (len(wav) - 44) / float(SAMPLE_RATE * 2)
