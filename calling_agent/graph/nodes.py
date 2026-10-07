"""Call graph nodes and deterministic transitions."""
from __future__ import annotations

from dataclasses import dataclass, field

@dataclass(frozen=True)
class Node:
    id: str
    allowed_intents: tuple[str, ...]
    listen: bool = True
    barge_in: bool = True


NODES: dict[str, Node] = {
    "await_role": Node(
        "await_role",
        ("owner", "manager", "neither", "who_is_this", "not_interested",
         "do_not_call", "machine", "unclear"),
    ),
    "await_two_minutes": Node(
        "await_two_minutes",
        ("yes_now", "busy", "not_interested", "do_not_call", "machine", "unclear"),
    ),
    "await_interest": Node(
        "await_interest",
        ("interested", "maybe", "not_interested", "busy", "do_not_call",
         "machine", "unclear"),
    ),
    "await_callback_time": Node(
        "await_callback_time",
        ("callback_time", "not_interested", "do_not_call", "machine", "unclear"),
    ),
    "await_gatekeeper_time": Node(
        "await_gatekeeper_time",
        ("callback_time", "not_interested", "do_not_call", "machine", "unclear"),
    ),
    "await_busy_time": Node(
        "await_busy_time",
        ("callback_time", "not_interested", "do_not_call", "machine", "unclear"),
    ),
    "ended": Node("ended", (), listen=False, barge_in=False),
}


@dataclass
class Decision:
    intent: str
    confidence: float
    next_node: str
    play: list[str] = field(default_factory=list)
    end_call: bool = False
    disposition: str | None = None
    slots: dict = field(default_factory=dict)
    fallback_tts: str | None = None


def transition(node_id: str, intent: str, confidence: float, slots: dict | None = None) -> Decision:
    """Deterministic graph edges after an intent is chosen."""
    slots = slots or {}

    if intent == "machine":
        return Decision(
            intent=intent,
            confidence=confidence,
            next_node="ended",
            play=["vm_drop"],
            end_call=True,
            disposition="IVR_OR_MACHINE",
            slots=slots,
        )
    if intent == "do_not_call":
        return Decision(
            intent=intent,
            confidence=confidence,
            next_node="ended",
            play=["polite_exit"],
            end_call=True,
            disposition="do_not_call",
            slots=slots,
        )
    if intent == "not_interested":
        return Decision(
            intent=intent,
            confidence=confidence,
            next_node="ended",
            play=["polite_exit"],
            end_call=True,
            disposition="not_interested",
            slots=slots,
        )

    if node_id == "await_role":
        if intent in ("owner", "manager"):
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_two_minutes",
                play=["perfect_thanks", "ask_two_minutes"],
                slots=slots,
            )
        if intent == "neither":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_gatekeeper_time",
                play=["ask_owner_manager_time"],
                disposition="not_decision_maker",
                slots=slots,
            )
        if intent == "who_is_this":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_role",
                play=["identify_company"],
                slots=slots,
            )
        return Decision(
            intent="unclear",
            confidence=confidence,
            next_node="await_role",
            play=["probe_role"],
            slots=slots,
        )

    if node_id == "await_two_minutes":
        if intent == "yes_now":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_interest",
                play=["short_pitch", "ask_interest"],
                slots=slots,
            )
        if intent == "busy":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_busy_time",
                play=["busy_ask_better_time"],
                disposition="callback_requested",
                slots=slots,
            )
        return Decision(
            intent="unclear",
            confidence=confidence,
            next_node="await_two_minutes",
            play=["probe_unclear"],
            slots=slots,
        )

    if node_id == "await_interest":
        if intent == "interested":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_callback_time",
                play=["ask_callback_time"],
                slots=slots,
            )
        if intent == "maybe":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_callback_time",
                play=["ask_callback_time_maybe"],
                slots=slots,
            )
        if intent == "busy":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="await_busy_time",
                play=["busy_ask_better_time"],
                disposition="callback_requested",
                slots=slots,
            )
        return Decision(
            intent="unclear",
            confidence=confidence,
            next_node="await_interest",
            play=["probe_unclear"],
            slots=slots,
        )

    if node_id in ("await_callback_time", "await_gatekeeper_time", "await_busy_time"):
        if intent == "callback_time":
            return Decision(
                intent=intent,
                confidence=confidence,
                next_node="ended",
                play=["confirm_callback_generic"],
                end_call=True,
                disposition=(
                    "callback_requested"
                    if node_id != "await_callback_time"
                    else "owner_manager_interested"
                ),
                slots=slots,
            )
        return Decision(
            intent="unclear",
            confidence=confidence,
            next_node=node_id,
            play=["probe_unclear"],
            slots=slots,
        )

    return Decision(
        intent="unclear",
        confidence=confidence,
        next_node=node_id,
        play=["probe_unclear"],
        slots=slots,
    )
