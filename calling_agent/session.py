"""Per-call session state."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class CallSession:
    def __init__(self, lead: dict):
        self.lead = lead
        self.amd_result: str | None = None
        self.amd_known = asyncio.Event()
        self.greeting_ended = asyncio.Event()
        self.abort_to_voicemail = False
        self.last_user_speech = time.monotonic()
        self.silence_prompts = 0
        self.transcript: list[dict] = []
        self.node = "await_role"
        self.probes_used = 0
        self.connected_at: float | None = None
        self.answered_at: float | None = None
        self.outcome: dict[str, Any] = {}
        self.dg_ws: Any | None = None
        self.dg_encoding: str | None = None
        self.dg_connect_task: asyncio.Task | None = None
        self.ended = False
        # Media reconnect guards (Cloudflare/Telnyx may open WS more than once)
        self.greeting_played = False
        self.media_epoch = 0
        self.media_owner: int | None = None
        self.conversation_active = False
        self.agent_speaking = False
        self.listen_open = False
        self.playback_command_id: str | None = None
        self.playback_done = asyncio.Event()
        self.playback_done.set()
        self.deciding = asyncio.Lock()
        self.engine_task: asyncio.Task | None = None


SESSIONS: dict[str, CallSession] = {}
