"""Azure OpenAI JSON intent classifier for ambiguous turns."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from calling_agent import config
from calling_agent.graph.nodes import NODES
from calling_agent.log import log

_SYSTEM = """You classify a single spoken reply in an outbound sales phone call.
Return ONLY compact JSON, no markdown:
{"intent":"<one of allowed_intents>","confidence":0.0,"slots":{}}

Rules:
- Pick exactly one intent from allowed_intents.
- If unsure, use "unclear" with low confidence.
- If the reply is cut off mid-sentence, use "unclear".
- Only use callback_time when the reply names an actual time or day.
  "you can", "sure", "okay" alone are NOT times -> "unclear".
- For callback_time, put the time phrase in slots.best_callback_time.
- Never invent intents outside the allowed list.
- confidence is 0..1.
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


async def classify_with_azure(
    node_id: str,
    text: str,
    *,
    timeout_s: float = 4.0,
) -> tuple[str, float, dict] | None:
    """
    Ask Azure OpenAI for intent JSON.
    Returns (intent, confidence, slots) or None on failure / not configured.
    """
    url = config.azure_chat_completions_url()
    if not url:
        return None

    node = NODES.get(node_id)
    allowed = list(node.allowed_intents) if node else ["unclear"]
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "node": node_id,
                        "allowed_intents": allowed,
                        "transcript": text,
                    }
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                url,
                headers={
                    "api-key": config.AZURE_OPENAI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.is_error:
                fail(
                    "azure_error",
                    f"{r.status_code}: {r.text[:300]}",
                    node=node_id,
                )
                return None
            data = r.json()
            content = (
                ((data.get("choices") or [{}])[0].get("message") or {})
                .get("content")
                or ""
            )
    except Exception as e:
        print(f"[decide/llm] Azure request failed: {e}", flush=True)
        return None

    parsed = _extract_json(content)
    if not parsed:
        print(f"[decide/llm] bad JSON: {content!r}", flush=True)
        return None

    intent = str(parsed.get("intent") or "unclear").strip()
    if intent not in allowed:
        print(f"[decide/llm] intent {intent!r} not allowed; -> unclear", flush=True)
        intent = "unclear"

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    slots = parsed.get("slots") if isinstance(parsed.get("slots"), dict) else {}
    if intent == "callback_time" and "best_callback_time" not in slots:
        slots = {**slots, "best_callback_time": text}

    log(f"[decide/llm] node={node_id} intent={intent} conf={confidence:.2f}")
    return intent, confidence, slots
