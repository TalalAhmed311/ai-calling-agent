"""Application configuration loaded from environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "audio_cache"
SCRIPT_PATH = DATA_DIR / "script.txt"
CALLS_DIR = DATA_DIR / "calls"
RECORDINGS_DIR = DATA_DIR / "recordings"
LOGS_DIR = ROOT_DIR / "logs"
FAILURES_LOG = LOGS_DIR / "failures.log"
LEADS_DB = DATA_DIR / "leads.db"

REQUIRED_ENV = [
    "DEEPGRAM_API_KEY",
    "TELNYX_API_KEY",
    "TELNYX_CONNECTION_ID",
    "TELNYX_FROM_NUMBER",
    "PUBLIC_HOST",
    "ENDPOINT_SECRET",
]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def require_env() -> dict[str, str]:
    missing: list[str] = []
    values: dict[str, str] = {}
    print("=== env check ===")
    for name in REQUIRED_ENV:
        raw = os.environ.get(name, "").strip()
        if not raw:
            missing.append(name)
            print(f"  {name}: MISSING")
        else:
            values[name] = raw
            print(f"  {name}: set {_mask(raw)}")
    pub = os.environ.get("TELNYX_PUBLIC_KEY", "").strip()
    if pub:
        values["TELNYX_PUBLIC_KEY"] = pub
        print(f"  TELNYX_PUBLIC_KEY: set {_mask(pub)}")
    else:
        print("  TELNYX_PUBLIC_KEY: unset (webhook signatures will be rejected)")
    if missing:
        print(f"FATAL: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if values["PUBLIC_HOST"].startswith(("http://", "https://", "wss://", "ws://")):
        print("FATAL: PUBLIC_HOST must be hostname only (no scheme)", file=sys.stderr)
        sys.exit(1)
    if values["PUBLIC_HOST"].endswith("/"):
        print("FATAL: PUBLIC_HOST must not have a trailing slash", file=sys.stderr)
        sys.exit(1)
    print("=== env ok ===")
    return values


ENV = require_env()

DEEPGRAM_API_KEY = ENV["DEEPGRAM_API_KEY"]
TELNYX_API_KEY = ENV["TELNYX_API_KEY"]
TELNYX_CONNECTION_ID = ENV["TELNYX_CONNECTION_ID"]
TELNYX_FROM_NUMBER = ENV["TELNYX_FROM_NUMBER"]
PUBLIC_HOST = ENV["PUBLIC_HOST"]
ENDPOINT_SECRET = ENV["ENDPOINT_SECRET"]
TELNYX_PUBLIC_KEY = ENV.get("TELNYX_PUBLIC_KEY", "")

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"
DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
# Direct Deepgram Listen model (media websocket path). nova-2-phonecall is
# best for 8 kHz telephony; Telnyx transcription_start cannot select it.
DEEPGRAM_LISTEN_MODEL = (
    os.environ.get("DEEPGRAM_LISTEN_MODEL", "nova-2-phonecall").strip()
    or "nova-2-phonecall"
)
TELNYX_API = "https://api.telnyx.com/v2"
VOICE = "aura-2-thalia-en"

# Telnyx decodes the payload we send back using the bidirectional codec we
# declared in streaming_start, so cached playback must use the matching
# encoding regardless of what the inbound track negotiated.
_CODEC_ENCODINGS = {"PCMU": "mulaw", "PCMA": "alaw"}
TELNYX_BIDIRECTIONAL_CODEC = (
    os.environ.get("TELNYX_BIDIRECTIONAL_CODEC", "PCMU").strip().upper() or "PCMU"
)
if TELNYX_BIDIRECTIONAL_CODEC not in _CODEC_ENCODINGS:
    print(
        f"WARNING: unsupported TELNYX_BIDIRECTIONAL_CODEC="
        f"{TELNYX_BIDIRECTIONAL_CODEC!r}; falling back to PCMU",
        file=sys.stderr,
    )
    TELNYX_BIDIRECTIONAL_CODEC = "PCMU"
PLAYBACK_ENCODING = _CODEC_ENCODINGS[TELNYX_BIDIRECTIONAL_CODEC]

# Telnyx usually negotiates A-law on international PSTN legs. Preconnecting
# Deepgram with the right encoding lets the bridge reuse that socket.
EXPECTED_INBOUND_ENCODING = (
    os.environ.get("EXPECTED_INBOUND_ENCODING", "alaw").strip().lower() or "alaw"
)

AGENT_NAME = os.environ.get("AGENT_NAME", "Sarah").strip() or "Sarah"
COMPANY_NAME = os.environ.get("COMPANY_NAME", "CraftPay").strip() or "CraftPay"
CALLBACK_NUMBER = (
    os.environ.get("CALLBACK_NUMBER", TELNYX_FROM_NUMBER).strip() or TELNYX_FROM_NUMBER
)
SERVICE_SUMMARY = (
    os.environ.get(
        "SERVICE_SUMMARY",
        "payment processing and POS services that can lower card fees",
    ).strip()
    or "payment processing and POS services that can lower card fees"
)

# Azure OpenAI — optional decision-boundary LLM (ambiguous turns only)
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_VERSION = (
    os.environ.get("AZURE_OPENAI_API_VERSION", "").strip() or "2024-02-01"
)
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()


def azure_chat_completions_url() -> str | None:
    """Build Azure chat completions URL from env (full path or resource base)."""
    if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
        return None
    endpoint = AZURE_OPENAI_ENDPOINT
    version = AZURE_OPENAI_API_VERSION
    if "/chat/completions" in endpoint:
        return f"{endpoint.split('?', 1)[0]}?api-version={version}"
    base = endpoint.rstrip("/")
    deployment = AZURE_OPENAI_DEPLOYMENT or "gpt-4o"
    # If user pasted .../deployments/<name> without chat/completions
    if "/openai/deployments/" in base and not base.endswith("/chat/completions"):
        return f"{base}/chat/completions?api-version={version}"
    return (
        f"{base}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={version}"
    )


def azure_openai_configured() -> bool:
    return azure_chat_completions_url() is not None


# Timers (plan §11)
AMD_TIMEOUT_S = 0.35
RING_TIMEOUT_SECS = 45
# Shorter defaults: 25s dead air sounded like the bot froze on recordings.
SILENCE_PROMPT_S = float(os.environ.get("SILENCE_PROMPT_S", "12"))
SILENCE_PROMPT_2_S = float(os.environ.get("SILENCE_PROMPT_2_S", "8"))
MAX_SILENCE_PROMPTS = max(0, int(os.environ.get("MAX_SILENCE_PROMPTS", "1")))
MAX_TALK_S = 240.0
END_CALL_GRACE_S = 2.5
WEBHOOK_MAX_AGE_S = 300
# Dialer: Telnyx default concurrent outbound is 2 until Level 2 verification.
MAX_CONCURRENT_CALLS = max(1, int(os.environ.get("MAX_CONCURRENT_CALLS", "2")))

# Deepgram endpointing. 400ms was marking speech_final during mid-sentence
# pauses, which truncated callers. utterance_end_ms has a 1000ms floor.
STT_ENDPOINTING_MS = int(os.environ.get("STT_ENDPOINTING_MS", "700"))
STT_UTTERANCE_END_MS = max(
    1000, int(os.environ.get("STT_UTTERANCE_END_MS", "1200"))
)

# Bidirectional media WSS is optional. Campaign calls lost the agent voice
# when the Cloudflare tunnel dropped that socket; speech now goes through
# Telnyx playback_start. Keep the WS off unless you are debugging inbound RTP.
MEDIA_STREAM = os.environ.get("MEDIA_STREAM", "0").strip() in ("1", "true", "yes")
# Learn novel unclear replies: LLM answer → Deepgram TTS → SQLite + audio cache.
SITUATION_CACHE = os.environ.get("SITUATION_CACHE", "1").strip() in ("1", "true", "yes")
# Debug-only: echo inbound RTP back out on the media WebSocket.
ECHO_TEST = os.environ.get("ECHO_TEST", "0").strip() in ("1", "true", "yes")

# Call recording (Telnyx-side). Recording stops automatically on hangup and
# arrives as a short-lived pre-signed URL in call.recording.saved.
CALL_RECORDING = os.environ.get("CALL_RECORDING", "1").strip() in ("1", "true", "yes")
CALL_RECORDING_FORMAT = os.environ.get("CALL_RECORDING_FORMAT", "mp3").strip() or "mp3"
CALL_RECORDING_CHANNELS = (
    os.environ.get("CALL_RECORDING_CHANNELS", "dual").strip() or "dual"
)

# Live STT via Telnyx transcription_start.
# Ranked for this dialer: Deepgram deepgram/nova-2 (or nova-3) >> Google.
# Telnyx does not expose Deepgram's "nova-2-phonecall"; that needs MEDIA_STREAM.
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "Deepgram").strip() or "Deepgram"
TRANSCRIPTION_MODEL = (
    os.environ.get("TRANSCRIPTION_MODEL", "deepgram/nova-2").strip() or "deepgram/nova-2"
)
TRANSCRIPTION_LANGUAGE = os.environ.get("TRANSCRIPTION_LANGUAGE", "en").strip() or "en"

# Inbound audio diagnostics
AUDIO_LEVEL_LOG_S = float(os.environ.get("AUDIO_LEVEL_LOG_S", "2.0"))
DUMP_INBOUND_AUDIO = os.environ.get("DUMP_INBOUND_AUDIO", "").strip() in (
    "1",
    "true",
    "yes",
)
DEBUG_DIR = DATA_DIR / "debug"

# Keyword → scrape → timezone dialer
SCRAPER_URL = os.environ.get("SCRAPER_URL", "http://127.0.0.1:8000").strip().rstrip("/")
SCRAPER_VERSION = os.environ.get("SCRAPER_VERSION", "v2").strip() or "v2"
SCRAPE_TIMEOUT_S = float(os.environ.get("SCRAPE_TIMEOUT_S", "1800"))
# Local civil-time call window (per lead timezone).
CALL_WINDOW_START = os.environ.get("CALL_WINDOW_START", "10:00").strip() or "10:00"
CALL_WINDOW_END = os.environ.get("CALL_WINDOW_END", "14:00").strip() or "14:00"
CALL_WEEKDAYS_ONLY = os.environ.get("CALL_WEEKDAYS_ONLY", "1").strip() in (
    "1",
    "true",
    "yes",
)
