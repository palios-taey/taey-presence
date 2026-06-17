"""INTERRUPT mechanism — confusion-triggered response while you type.

When the user is still typing and the assistant is CONFUSED by something (or
has something genuinely urgent), it drops a response in mid-typing — a
clarifying question, or a flag — instead of waiting for submit. This is the
thing chat assistants almost never do: they wait for you to finish. This one
decides, on its own, that what it has to say is worth saying now.

Distinct from THOUGHT PREDICTION (../prediction/): interrupt is REACTIVE (I'm
confused / this is urgent), prediction is ANTICIPATORY (here's what you'll
say). An interrupt is a real semantic act — the engine asserting that
speaking now beats letting you finish.
"""
from __future__ import annotations

from typing import Optional

# An interrupt fires only above this confidence — it must be worth breaking
# the user's flow. Tune it; higher = more reluctant to interrupt.
DEFAULT_INTERRUPT_FLOOR = 0.8
MIN_CHARS = 30  # don't interrupt on near-empty input

_THINK_SCHEMA = (
    '\n{"clarification":"<question if genuinely confused, else empty string>",'
    '"message":"<what to say if urgent, else empty string>",'
    '"state":"<following|confused|urgent>",'
    '"confidence":<0.0-1.0>}'
)


class Interrupter:
    """Decides whether to interrupt the user mid-typing, and with what."""

    def __init__(self, inference, interrupt_floor: float = DEFAULT_INTERRUPT_FLOOR):
        self._infer = inference
        self._floor = interrupt_floor

    async def consider(self, partial: str, history: list,
                       memory: Optional[list] = None) -> Optional[dict]:
        """Return an interrupt payload if one is warranted, else None."""
        if not partial or len(partial.strip()) < MIN_CHARS:
            return None

        memory_context = ""
        if memory:
            snippets = [f"- {h.get('title', '')}: {h.get('snippet', '')[:200]}" for h in memory[:3]]
            memory_context = "\nRelevant context you already have:\n" + "\n".join(snippets)

        messages = [{"role": "system", "content": (
            "Someone is typing and has not sent yet. Are you genuinely confused "
            "about what they mean, or is something urgent enough to say BEFORE "
            "they finish? Most of the time the answer is no — only interrupt if "
            "it's truly warranted. Respond with ONLY JSON:" + _THINK_SCHEMA
            + memory_context
        )}]
        for h in history[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": f'[still typing, not sent]: "{partial}"'})

        result = await self._infer.complete_json(messages, max_tokens=120, temperature=0.2)
        if not result:
            return None

        clarification = (result.get("clarification") or "").strip()
        urgent_message = (result.get("message") or "").strip()
        state = result.get("state", "following")
        confidence = float(result.get("confidence", 0) or 0)

        # The confidence floor applies to BOTH paths — interrupting is socially
        # expensive, so it must clear the floor regardless of type. (Earlier this
        # gated only the urgent path, so a low-confidence clarification fired an
        # interrupt; that broke the restraint the floor exists to enforce.)
        if confidence < self._floor:
            return None

        # Confusion path: a clarifying question, above floor.
        if state == "confused" and clarification:
            return {"worthy": True, "text": clarification, "type": "clarification",
                    "confidence": confidence}
        # Urgent path: a message, above floor. Must have non-empty text — never
        # publish a content-free urgent bubble.
        if state == "urgent" and urgent_message:
            return {"worthy": True, "text": urgent_message, "type": "urgent",
                    "confidence": confidence}
        return None
