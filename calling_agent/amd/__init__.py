"""Answering-machine detection helpers."""

from __future__ import annotations

from calling_agent import config
from calling_agent.log import fail, log

AMD_HUMAN = frozenset({
    "human", "human_residence", "human_business", "not_sure", "silence",
})
AMD_MACHINE = frozenset({"machine", "fax_detected"})


def classify_amd(result: str) -> str:
    key = result.strip().lower()
    if key in AMD_MACHINE:
        return "machine"
    if key in AMD_HUMAN:
        return "human"
    log(f"AMD unknown result {result!r}; treating as human")
    return "human"


def telnyx_encoding_to_deepgram(telnyx_encoding: str | None) -> str:
    enc = (telnyx_encoding or "").upper()
    if enc in ("PCMA", "ALAW"):
        return "alaw"
    if enc in ("PCMU", "ULAW", "MULAW"):
        return "mulaw"
    fallback = config.EXPECTED_INBOUND_ENCODING
    log(f"unknown Telnyx encoding {telnyx_encoding!r}; defaulting to {fallback}")
    fail("unknown_encoding", f"{telnyx_encoding!r}; using {fallback}")
    return fallback
