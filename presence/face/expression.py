"""FACE / expression mechanism.

Produces a single emoji that reflects TWO things at once, the way a listener's
face does:
  1. the engine's own runtime state (from the soma daemon's state vector), and
  2. its reaction to what the user is currently typing.

This is the visible-presence surface: while you type, the face shifts. It reads
the soma `agent:state:vector` (published by the soma daemon) for internal state
and asks the model for a reaction to the partial input, then blends them.

Not required: if no soma vector is present, the face is driven by the reaction
alone; if the model reaction is weak, it falls back to a neutral face.
"""
from __future__ import annotations

from typing import Optional

MIN_CHARS = 8
NEUTRAL = "🙂"

_REACTION_SCHEMA = (
    '\n{"face":"<single emoji>","feeling":"<one word>","intensity":<0.0-1.0>}'
)


class ExpressionWorker:
    """Blends runtime state + typing reaction into one emoji."""

    def __init__(self, inference, neutral: str = NEUTRAL):
        self._infer = inference
        self._neutral = neutral

    async def _reaction(self, partial: str, history: list) -> dict:
        messages = [{"role": "system", "content": (
            "You are listening while someone types. Based on what they've typed "
            "so far, what expression would you make? Respond with ONLY JSON:"
            + _REACTION_SCHEMA
        )}]
        for h in history[-4:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": f'[still typing]: {partial}'})
        return await self._infer.complete_json(messages, max_tokens=24, temperature=0.3)

    def _blend(self, reaction: dict, state: Optional[dict]) -> str:
        """Reaction wins when strong; else a couple of runtime-state extremes
        drive the face; else neutral."""
        face = reaction.get("face", "")
        intensity = float(reaction.get("intensity", 0) or 0)
        if face and intensity > 0.5:
            return face
        if state:
            # extreme runtime states get their own expression
            if float(state.get("capacity", 1.0)) < 0.1:   # memory-pressured
                return "😅"
            if float(state.get("warmth", 0.0)) > 0.9 and float(state.get("fluency", 0.0)) > 0.7:
                return "😊"
        if face and intensity > 0.3:
            return face
        return self._neutral

    async def express(self, partial: str, history: list,
                      state: Optional[dict] = None) -> dict:
        """Return {face, feeling}. `state` is the soma runtime-state vector (or None)."""
        if not partial or len(partial.strip()) < MIN_CHARS:
            return {"face": self._blend({}, state), "feeling": "present"}
        reaction = await self._reaction(partial, history)
        return {"face": self._blend(reaction, state),
                "feeling": reaction.get("feeling", "attentive")}
