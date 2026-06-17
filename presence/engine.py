"""taey-presence-engine — the unified presence loop.

Runs three concurrent mechanisms against partial user input (debounced) and
publishes their results to Redis for a frontend to render:

  PREDICTION  (prediction/)  -- ghost text: what you'll say next  ("OMG button")
  INTERRUPT   (interrupt/)   -- mid-typing response when confused / urgent
  DCM         (dcm/)         -- inter-instance shared state via Neo4j

ARCHITECTURE BOUNDARY (M:1 multiplexed singleton):
  The engine talks to exactly ONE model endpoint through ONE InferenceGateway.
  M asynchronous mechanisms multiplex their requests onto that single
  serialized path. This is deliberate: hardware memory bounds on edge silicon
  restrict inference to a 1:1 host mapping — a single-process model server hit
  with concurrent requests past its budget OOM-kills. The gateway accepts a
  single scalar endpoint and structurally rejects arrays / load-balancer
  lists. If you need real concurrency, run multiple engines each with their
  own single endpoint and let DCM coordinate them — do not point one engine
  at a pool.

NO AUTH anywhere. Redis, Neo4j, and the model endpoint all run on your own
trusted network with auth disabled. Hosts/ports are config (env, fail-loud);
there are no credentials in this code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import httpx
import redis.asyncio as aioredis

from .dcm.inter_instance import InterInstanceState
from .face.expression import ExpressionWorker
from .interrupt.interrupter import Interrupter
from .prediction.predictor import Predictor
from .retrieval.backend import HTTPRetrievalBackend, NoRetrievalBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [presence-%(name)s] %(message)s")
log = logging.getLogger("engine")

POLL_INTERVAL = 0.3
DEBOUNCE = 0.5
TTL = 30


def _require(name: str) -> str:
    """Fail LOUD on missing config — never silently default to an operator value."""
    v = os.environ.get(name)
    if not v:
        raise SystemExit(
            f"{name} is required. Set it to point at YOUR OWN service "
            f"(no default — this engine ships with no embedded hosts or credentials)."
        )
    return v


def _extract_json_object(content: str):
    """Robustly extract one JSON object from model output. Returns the parsed
    dict, or None if no valid object is present.

    Handles the real-world variance the naive find('{')/rfind('}') approach
    breaks on: markdown ```json fences, leading/trailing prose, and trailing
    extra text after the object. Uses brace-depth scanning from the first '{'
    so a balanced object is found even when more text follows it.
    """
    if not content:
        return None
    text = content.strip()
    # strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        text = text.split("```", 2)
        text = text[1] if len(text) > 1 else content
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    # fast path: the whole thing is a JSON object
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # scan for the first balanced {...} block
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


class InferenceGateway:
    """The single serialized path to the model. M mechanisms multiplex here.

    Accepts ONE scalar endpoint URL. Passing a list / comma-separated pool is
    a configuration error (see ARCHITECTURE BOUNDARY above)."""

    def __init__(self, endpoint: str, model_name: str = "", timeout: float = 30.0):
        if not endpoint or "," in endpoint or endpoint.strip().startswith("["):
            raise ValueError(
                "InferenceGateway takes a single scalar endpoint, not a pool. "
                "Run one engine per endpoint and coordinate via DCM. NOTE: a single "
                "URL fronting a load balancer / DNS round-robin / k8s Service still "
                "fans out — this check stops obvious list-configs, it is NOT a "
                "structural guarantee against concurrency (see docs/architecture.md)."
            )
        self._endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
        # model_name: many OpenAI-compatible servers REQUIRE a non-empty model
        # field. Empty string is accepted by some (e.g. single-model vLLM); set
        # MODEL_NAME if your server rejects requests without it.
        self._model = model_name
        self._http = httpx.AsyncClient(timeout=timeout)
        self._lock = asyncio.Lock()

    async def complete_json(self, messages: list, max_tokens: int = 120,
                            temperature: float = 0.2) -> dict:
        """One serialized completion, parsed as a single JSON object. Fail-soft —
        returns {} on transport OR parse failure (callers treat {} as "no result").
        Parse failures are logged at WARNING, not silently swallowed, so a model
        that stops emitting clean JSON is visible rather than invisibly dead."""
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if self._model:
            payload["model"] = self._model
        async with self._lock:  # serialize — single-process model server
            try:
                resp = await self._http.post(self._endpoint, json=payload)
                content = resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                log.warning("inference transport failed: %s", e)
                return {}
            parsed = _extract_json_object(content)
            if parsed is None:
                log.warning("could not parse JSON object from model output: %.120r", content)
                return {}
            return parsed

    async def close(self):
        await self._http.aclose()


def _parse_history(raw) -> list:
    """Isolate history parsing — malformed JSON yields [] rather than crashing the loop."""
    try:
        h = json.loads(raw or "[]")
        return h if isinstance(h, list) else []
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


async def presence_loop(stop: asyncio.Event):
    # config — MODEL_ENDPOINT fail-loud; localhost defaults are safe generics; no creds.
    redis_host = os.environ.get("REDIS_HOST", "127.0.0.1")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    model_endpoint = _require("MODEL_ENDPOINT")            # e.g. http://localhost:8000
    model_name = os.environ.get("MODEL_NAME", "")           # set if your server requires it
    search_url = os.environ.get("SEARCH_URL", "")           # optional; empty = no memory
    neo4j_bolt = os.environ.get("NEO4J_BOLT", "")           # optional; empty = no DCM
    instance_id = os.environ.get("INSTANCE_ID", "presence-0")

    r = aioredis.Redis(host=redis_host, port=redis_port, decode_responses=True)  # async, no auth
    gateway = InferenceGateway(model_endpoint, model_name=model_name)
    predictor = Predictor(gateway)
    interrupter = Interrupter(gateway)
    expression = ExpressionWorker(gateway)
    retrieval = HTTPRetrievalBackend(search_url) if search_url else NoRetrievalBackend()
    dcm = InterInstanceState(neo4j_bolt or None)

    log.info("presence engine up | model=%s name=%s search=%s dcm=%s",
             model_endpoint, model_name or "(unset)", search_url or "(none)", neo4j_bolt or "(none)")

    # last_processed guards against re-inferring an UNCHANGED partial every tick
    # (a paused typist must not generate a steady stream of identical inferences).
    last_partial, last_change, last_processed = "", 0.0, None
    try:
        while not stop.is_set():
            try:
                partial = (await r.get("presence:partial") or "").strip()
                history = _parse_history(await r.get("presence:history"))

                if partial != last_partial:
                    last_partial, last_change = partial, asyncio.get_event_loop().time()

                # nothing to do: empty input, still within debounce window, or this
                # exact (partial, history) was already processed → idle.
                state_key = (partial, json.dumps(history, sort_keys=True))
                if (not partial
                        or asyncio.get_event_loop().time() - last_change < DEBOUNCE
                        or state_key == last_processed):
                    await _idle_or_stop(stop)
                    continue
                last_processed = state_key

                # prediction does NOT need memory; only interrupt does. Run them
                # concurrently so prediction is not latency-coupled to retrieval.
                async def mem_then_interrupt():
                    mem = await retrieval.search(partial)
                    if mem:
                        await r.set("presence:memory", json.dumps(mem), ex=TTL)
                    return await interrupter.consider(partial, history, mem)

                # soma state vector (published by the soma daemon) feeds the face
                soma_state = None
                try:
                    raw_state = await r.get("agent:state:vector")
                    soma_state = json.loads(raw_state) if raw_state else None
                except (json.JSONDecodeError, ValueError, TypeError):
                    soma_state = None

                # prediction does NOT need memory; only interrupt does. face reads
                # soma + reaction. all run concurrently so none is latency-coupled.
                prediction, interrupt, face = await asyncio.gather(
                    predictor.predict(partial, history),
                    mem_then_interrupt(),
                    expression.express(partial, history, soma_state),
                    return_exceptions=True,
                )
                prediction = prediction if isinstance(prediction, dict) else None
                interrupt = interrupt if isinstance(interrupt, dict) else None
                face = face if isinstance(face, dict) else None

                # publish OR tombstone — clear stale keys when a mechanism returns
                # nothing, so the UI never renders a ghost over changed/cleared input.
                if prediction:
                    await r.set("presence:prediction", json.dumps(prediction), ex=TTL)
                else:
                    await r.delete("presence:prediction")
                if interrupt:
                    await r.set("presence:interrupt", json.dumps(interrupt), ex=TTL)
                else:
                    await r.delete("presence:interrupt")
                if face:
                    await r.set("presence:face", json.dumps(face), ex=TTL)

                # DCM write is a sync Neo4j call — offload so it can't block the loop.
                await asyncio.to_thread(dcm.write_state, instance_id, {
                    "prediction": (prediction or {}).get("prediction", ""),
                    "interrupting": bool(interrupt),
                })
            except aioredis.ConnectionError:
                log.warning("Redis connection lost, retrying")
                await _idle_or_stop(stop, 2.0)
            except Exception as e:
                log.error("loop error: %s", e)
                await _idle_or_stop(stop, 1.0)
            else:
                await _idle_or_stop(stop)
    finally:
        log.info("shutting down — closing gateway, dcm, redis")
        await gateway.close()
        dcm.close()
        await r.aclose()


async def _idle_or_stop(stop: asyncio.Event, delay: float = POLL_INTERVAL):
    """Sleep `delay`, but wake immediately if a stop was requested."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


def main():
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # add_signal_handler unsupported on some platforms
    try:
        loop.run_until_complete(presence_loop(stop))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
