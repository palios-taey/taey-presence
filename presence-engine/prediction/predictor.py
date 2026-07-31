"""THOUGHT PREDICTION mechanism — "the OMG button".

While the user is still typing, predict what they are going to say next.
The prediction renders as ghost text below the input; an accept control
("OMG, yes — that's what I was going to say") lets the user take it. When the
user's trajectory diverges from the prediction, the ghost is blurred (a
"pivot") rather than replaced abruptly.

This is distinct from the INTERRUPT mechanism (see ../interrupt/): prediction
is anticipatory (what will they say), interrupt is reactive (I'm confused /
I have something urgent). They are separate features that happen to share the
same partial-input signal.
"""
from __future__ import annotations

from typing import Optional


# Confidence floor for surfacing a prediction as a confident "pivot-worthy"
# ghost. A tunable threshold, not a magic constant — set per your taste.
DEFAULT_CONFIDENCE_FLOOR = 0.5
MIN_CHARS = 20  # don't predict on near-empty input

_PRED_SCHEMA = (
    '\n{"prediction":"<what they will say next>",'
    '"state":"<following|thinking|diverging>",'
    '"confidence":<0.0-1.0>}'
)


class Predictor:
    """Predicts the user's next utterance from partial input via an LLM."""

    def __init__(self, inference, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR):
        # `inference` is an InferenceGateway (see ../engine.py) — the single
        # serialized path to the model. The predictor never talks to a model
        # endpoint directly.
        self._infer = inference
        self._floor = confidence_floor

    async def predict(self, partial: str, history: list) -> Optional[dict]:
        if not partial or len(partial.strip()) < MIN_CHARS:
            return None

        messages = [{"role": "system", "content": (
            "Someone is typing a message and has not sent it yet. Predict what "
            "they are about to say. Respond with ONLY JSON:" + _PRED_SCHEMA
        )}]
        for h in history[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": f'[still typing, not sent]: "{partial}"'})

        result = await self._infer.complete_json(messages, max_tokens=120, temperature=0.1)
        if not result or not result.get("prediction"):
            return None

        confidence = float(result.get("confidence", 0) or 0)
        return {
            "prediction": result["prediction"],
            "state": result.get("state", "following"),
            "confidence": confidence,
            # ghost should render dim until confidence clears the floor; the
            # frontend blurs ("pivots") when state == "diverging"
            "ghost_active": confidence >= self._floor,
            "pivot": result.get("state") == "diverging",
        }
