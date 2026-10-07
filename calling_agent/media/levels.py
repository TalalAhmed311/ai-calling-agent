"""Inbound audio telemetry.

When Deepgram returns nothing it is impossible to tell from the outside
whether the caller's voice never arrived, arrived as digital silence, or
arrived in the wrong codec. These helpers make that visible.
"""

from __future__ import annotations

try:  # audioop is slated for removal in 3.13
    import audioop  # type: ignore
except ImportError:  # pragma: no cover
    audioop = None  # type: ignore

# Digital silence for each 8-bit telephony codec.
SILENCE_BYTE = {"mulaw": 0xFF, "alaw": 0xD5}


def describe_level(audio: bytes, encoding: str) -> str:
    """Summarise a chunk of 8-bit telephony audio for logging."""
    if not audio:
        return "no audio"

    silence = SILENCE_BYTE.get(encoding)
    quiet = audio.count(bytes([silence])) if silence is not None else 0
    quiet_pct = quiet / len(audio) * 100
    distinct = len(set(audio))

    if audioop is None:
        return f"{len(audio)}B silence={quiet_pct:.0f}% distinct={distinct}"

    if encoding == "alaw":
        pcm = audioop.alaw2lin(audio, 2)
    elif encoding == "mulaw":
        pcm = audioop.ulaw2lin(audio, 2)
    else:
        return f"{len(audio)}B unknown encoding {encoding!r}"

    rms = audioop.rms(pcm, 2)
    peak = audioop.max(pcm, 2)
    # Same bytes read as the other codec; a large gap means we guessed wrong.
    other = audioop.ulaw2lin(audio, 2) if encoding == "alaw" else audioop.alaw2lin(audio, 2)
    return (
        f"{len(audio)}B rms={rms} peak={peak} silence={quiet_pct:.0f}% "
        f"distinct={distinct} (as-other rms={audioop.rms(other, 2)})"
    )
