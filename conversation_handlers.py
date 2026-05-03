"""
conversation_handlers.py — Optional multi-turn handler module.
Exposes a standalone `respond()` function for the challenge's optional multi-turn scoring.
"""
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    category: dict
    merchant: dict
    trigger: Optional[dict]
    customer: Optional[dict]
    history: list = field(default_factory=list)
    auto_reply_count: int = 0
    turn_number: int = 0
    status: str = "open"   # open | ended | waiting


def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Given current conversation state + the merchant's latest message,
    produce the bot's next action.

    Returns dict with keys: action, body (if send), cta, wait_seconds (if wait), rationale.
    """
    # Import the async helpers — for sync use, we wrap in asyncio
    import asyncio
    from bot import (
        detect_auto_reply, detect_intent_transition, detect_not_interested,
        build_reply_prompt, call_claude, conversations
    )

    state.turn_number += 1
    state.history.append({"from": "merchant", "body": merchant_message, "ts": "now"})

    is_auto = detect_auto_reply(merchant_message)
    intent_yes = detect_intent_transition(merchant_message)
    not_int = detect_not_interested(merchant_message)

    if is_auto:
        state.auto_reply_count += 1
    else:
        state.auto_reply_count = 0

    if state.auto_reply_count >= 2:
        state.status = "ended"
        return {
            "action": "end",
            "rationale": "2+ consecutive auto-replies detected. Graceful exit.",
        }

    if not_int:
        state.status = "ended"
        return {
            "action": "end",
            "rationale": "Merchant opted out. Exiting cleanly.",
        }

    if state.turn_number >= 6:
        state.status = "ended"
        return {
            "action": "end",
            "rationale": "Max turn depth reached.",
        }

    prompt = build_reply_prompt(
        merchant_message,
        state.history,
        state.merchant,
        state.category,
        trigger=state.trigger,
        is_auto_reply=is_auto,
        intent_transition=intent_yes,
        not_interested=not_int,
    )

    result = asyncio.run(call_claude(prompt))

    action = result.get("action", "send")
    if action == "send" and result.get("body"):
        state.history.append({"from": "vera", "body": result["body"], "ts": "now"})

    return result
