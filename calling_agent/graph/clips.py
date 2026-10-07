"""Clip text catalog for cached playback."""
from __future__ import annotations


def build_clip_texts(
    *,
    agent_name: str,
    company_name: str,
    callback_number: str,
    service_summary: str,
) -> dict[str, str]:
    return {
        "greeting_identity": (
            f"Hi, this is {agent_name} with {company_name}. "
            "Am I speaking with the owner or a manager?"
        ),
        "perfect_thanks": "Perfect, thanks.",
        "ask_two_minutes": (
            "This is a sales call, and I only need about two minutes to tell you "
            "about our services. Is now an okay time?"
        ),
        "short_pitch": (
            f"We help businesses like yours with {service_summary}. "
            "A lot of owners we talk to are looking for a simpler and more "
            "cost-effective option, so I wanted to see if it might be useful for you as well."
        ),
        "ask_interest": (
            "Would you be interested in hearing more on a quick follow-up call?"
        ),
        "ask_callback_time": "Great. When is the right time for me to call you back?",
        "ask_callback_time_maybe": (
            "No problem. When would be a good time for a short follow-up call?"
        ),
        "confirm_callback_generic": (
            "Perfect — I'll call you then. Thanks so much, and have a great day."
        ),
        "polite_exit": (
            "Absolutely — I completely respect that. Thanks for your time, "
            "and have a great day."
        ),
        "ask_owner_manager_time": (
            "No problem. When would be a good time to reach the owner or a manager?"
        ),
        "thanks_callback": (
            "Got it — thank you. I'll call back then. Have a great day."
        ),
        "identify_company": (
            f"Of course — I'm {agent_name} with {company_name}. "
            "This is a quick sales call. Are you the owner or a manager there?"
        ),
        "identify_brief": (
            f"Of course — I'm {agent_name}, calling on behalf of {company_name}."
        ),
        # Deny AI / bot; then the graph re-asks the current question.
        "disclose_ai": (
            f"No, I'm not an AI — this is {agent_name} with {company_name}. "
            "Just a quick call."
        ),
        "busy_ask_better_time": (
            "Totally understand. When would be a better time for me to call back?"
        ),
        "probe_role": "Sorry — are you the owner or a manager there?",
        "probe_unclear": "Sorry, I didn't catch that. Could you say that one more time?",
        "still_there": "Sorry, are you still there?",
        "vm_drop": (
            f"Hi, this message is for the owner or manager. My name is {agent_name} "
            f"with {company_name}. I'm calling with a quick sales update about our "
            f"services. Please give me a call back at {callback_number}. Have a great day."
        ),
    }


