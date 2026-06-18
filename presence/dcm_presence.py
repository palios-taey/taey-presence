"""DCM Conversational Presence — Multi-worker engine for human-like interaction.

Three concurrent workers share state through Redis (real-time) + Neo4j (persistent):

1. FACE worker  — Reads partial input and asks the model for a single emoji
                  reaction plus a one-word feeling label.

2. MEMORY worker — Deep ISMA search on partial input. Retrieves relevant tiles,
                   conversation context, constitutional grounding. Writes findings
                   to Redis + Neo4j so they're available when the user submits.

3. THINKER worker — Runs actual inference on partial input. Generates thought
                    fragments, clarification questions, deeper predictions.
                    If confused or has something urgent, triggers an interrupt.

All workers poll Redis for partial input changes with 500ms debounce.
Dashboard reads combined state via /api/dcm/state endpoint.
Soma proxy reads DCM pre-work on submit for head-start context.

Usage: python3 dcm_presence.py
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DCM-%(name)s] %(message)s")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1/chat/completions")
# Optional model name. Empty for single-model servers (vLLM ignores it);
# REQUIRED by servers that demand it (e.g. Ollama /v1 — set MODEL=qwen2.5:3b).
MODEL = os.environ.get("MODEL", "")
ISMA_URL = os.environ.get("ISMA_URL", "http://localhost:8095").rstrip("/")
ISMA_SEARCH_URL = f"{ISMA_URL}/search"
# No-input default face. Empty = show nothing until Taey reacts (fully dynamic,
# no programmed face). Set to any emoji if you want a resting face.
DEFAULT_FACE = os.environ.get("DEFAULT_FACE", "")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
# No auth — Neo4j runs auth-disabled on the trusted internal network (no
# friction, no internal passwords, per Jesse 2026-06-17). No credential here.

POLL_INTERVAL = 0.3  # seconds between Redis checks
DEBOUNCE = 0.5       # seconds after last keystroke before workers fire
TTL = 30             # Redis key TTL
THINKER_MIN_CHARS = 30   # Don't think until user types this much
MEMORY_MIN_CHARS = 15    # Start memory search earlier than thinking

_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _extract_reaction_payload(content: str) -> dict:
    if not content:
        return {}

    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}") + 1
    candidate = text[start:end].strip() if start >= 0 and end > start else ""
    repairs = []
    if candidate:
        repairs.extend([
            candidate,
            re.sub(r',\s*""\s*(?=,|})', "", candidate),
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ])
        repairs.append(re.sub(r",\s*([}\]])", r"\1", repairs[-1]))

    for attempt in repairs:
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    face_match = re.search(r'"face"\s*:\s*"([^"\n]+)"', text)
    feeling_match = re.search(r'"feeling"\s*:\s*"([^"\n]*)"', text)
    intensity_match = re.search(r'"intensity"\s*:\s*(-?\d+(?:\.\d+)?)', text)
    thought_match = re.search(r'"thought"\s*:\s*"([^"\n]*)"', text)
    prediction_match = re.search(r'"prediction"\s*:\s*"([^"\n]*)"', text)
    clarification_match = re.search(r'"clarification"\s*:\s*"([^"\n]*)"', text)
    state_match = re.search(r'"state"\s*:\s*"([^"\n]*)"', text)
    confidence_match = re.search(r'"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', text)

    extracted = {}
    if face_match:
        extracted["face"] = face_match.group(1)
    if feeling_match:
        extracted["feeling"] = feeling_match.group(1)
    if intensity_match:
        try:
            extracted["intensity"] = float(intensity_match.group(1))
        except ValueError:
            pass
    if thought_match:
        extracted["thought"] = thought_match.group(1)
    if prediction_match:
        extracted["prediction"] = prediction_match.group(1)
    if clarification_match:
        extracted["clarification"] = clarification_match.group(1)
    if state_match:
        extracted["state"] = state_match.group(1)
    if confidence_match:
        try:
            extracted["confidence"] = float(confidence_match.group(1))
        except ValueError:
            pass
    return extracted


# ── Neo4j thin client ──────────────────────────────────────────────────────

_neo4j_driver = None

def _get_neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        try:
            from neo4j import GraphDatabase
            _neo4j_driver = GraphDatabase.driver(NEO4J_URI)  # no auth (auth-disabled internal)
            _neo4j_driver.verify_connectivity()
            # Ensure schema
            with _neo4j_driver.session() as s:
                s.run("""CREATE CONSTRAINT dcm_instance_id IF NOT EXISTS
                         FOR (i:TaeyInstance) REQUIRE i.instance_id IS UNIQUE""")
                s.run("""CREATE INDEX dcm_instance_active IF NOT EXISTS
                         FOR (i:TaeyInstance) ON (i.active)""")
        except Exception as e:
            logging.getLogger("neo4j").warning("Neo4j unavailable: %s — running Redis-only", e)
    return _neo4j_driver


def neo4j_write_state(worker_name: str, state: dict):
    """Write worker state to Neo4j for cross-worker and cross-session persistence."""
    driver = _get_neo4j()
    if not driver:
        return
    try:
        with driver.session() as s:
            s.run("""
                MERGE (i:TaeyInstance {instance_id: $worker})
                SET i.state = $state,
                    i.updated = datetime(),
                    i.active = true
            """, worker=worker_name, state=json.dumps(state))
    except Exception as e:
        logging.getLogger("neo4j").debug("Neo4j write failed: %s", e)


def neo4j_read_peer_states() -> list:
    """Read all active DCM worker states from Neo4j."""
    driver = _get_neo4j()
    if not driver:
        return []
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (i:TaeyInstance)
                WHERE i.active = true
                  AND i.updated > datetime() - duration({seconds: 60})
                RETURN i.instance_id AS id, i.state AS state
            """)
            return [{"id": r["id"], "state": json.loads(r["state"])} for r in result]
    except Exception:
        return []


# ── Shared utilities ───────────────────────────────────────────────────────

def get_partial() -> tuple:
    """Read current partial input and history from Redis."""
    partial = _r.get("taey:predict:partial") or ""
    history_raw = _r.get("taey:predict:history") or "[]"
    try:
        history = json.loads(history_raw)
    except json.JSONDecodeError:
        history = []
    return partial.strip(), history


# ── FACE Worker ────────────────────────────────────────────────────────────

class FaceWorker:
    """Publishes the model's emoji reaction to the current partial input."""

    def __init__(self):
        self.log = logging.getLogger("FACE")
        self.last_partial = None

    async def classify_reaction(self, partial: str, history: list, http: httpx.AsyncClient) -> dict:
        """Ask Taey what face they'd make hearing this partial input."""
        messages = [
            {"role": "system", "content": (
                "You are feeling what a listener feels during a conversation. "
                "The person is still typing. Based on what they've typed so far, "
                "respond with ONLY a JSON object: "
                '{"face":"<single emoji>","feeling":"<one word>","intensity":<0.0-1.0>}'
            )}
        ]
        for h in history[-4:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": f'[still typing]: {partial}'})

        try:
            resp = await http.post(VLLM_URL, json={
                "messages": messages, "temperature": 0.3, "max_tokens": 20,
                **({"model": MODEL} if MODEL else {})
            }, timeout=20.0)
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = _extract_reaction_payload(content)
            if parsed:
                return parsed
        except Exception:
            pass
        return {}

    def blend_face(self, reaction: dict) -> str:
        """Taey's own freely-chosen emoji is the face. No programmed set, no
        fixed palette — whatever single emoji the model returned as its reaction
        IS the face. The only fallback is the configurable DEFAULT_FACE, used
        solely when the model returned nothing (e.g. no/short input).
        """
        reaction_face = reaction.get("face", "")
        if reaction_face:
            return reaction_face          # Taey picks any emoji she wants
        return DEFAULT_FACE               # only when there's no model reaction

    async def run_once(self, partial: str, history: list, http: httpx.AsyncClient):
        if partial == self.last_partial:
            return
        self.last_partial = partial

        if not partial or len(partial) < 8:
            face = self.blend_face({})
            _r.set("taey:dcm:face", face, ex=TTL)
            _r.set("taey:dcm:face_feeling", "present", ex=TTL)
            _r.set("taey:predict:face", face, ex=TTL)
            return

        reaction = await self.classify_reaction(partial, history, http)
        face = self.blend_face(reaction)
        feeling = reaction.get("feeling", "attentive")

        _r.set("taey:dcm:face", face, ex=TTL)
        _r.set("taey:dcm:face_feeling", feeling, ex=TTL)
        # Also write to the existing key for backward compat
        _r.set("taey:predict:face", face, ex=TTL)

        neo4j_write_state("face_worker", {
            "face": face, "feeling": feeling,
            "reaction_intensity": reaction.get("intensity", 0),
        })

        self.log.info("face=%s feeling=%s intensity=%.2f",
                      face, feeling, float(reaction.get("intensity", 0)))


# ── MEMORY Worker ──────────────────────────────────────────────────────────

class MemoryWorker:
    """Searches ISMA for relevant memories/context based on what user is typing.

    Like a human recalling relevant experiences while listening.
    Results are available immediately when the user submits.
    """

    def __init__(self):
        self.log = logging.getLogger("MEMORY")
        self.last_query = ""
        self.cached_tiles = []

    async def search_isma(self, query: str, http: httpx.AsyncClient, top_k: int = 5) -> list:
        """Deep ISMA search — more tiles than the old prediction worker's 3."""
        try:
            resp = await http.post(ISMA_SEARCH_URL, json={
                "query": query, "top_k": top_k
            }, timeout=8.0)
            data = resp.json()
            results = data if isinstance(data, list) else data.get("results", data.get("tiles", []))
            return [{
                "hash": t.get("content_hash", ""),
                "title": t.get("title", t.get("document_name", "")),
                "snippet": (t.get("content", "") or t.get("rosetta_summary", ""))[:400],
                "score": t.get("score", t.get("certainty", 0)),
            } for t in results[:top_k]]
        except Exception as e:
            self.log.debug("ISMA search failed: %s", e)
            return []

    async def run_once(self, partial: str, history: list, http: httpx.AsyncClient):
        if not partial or len(partial) < MEMORY_MIN_CHARS:
            return

        # Don't re-search if query hasn't changed significantly
        if partial[:30] == self.last_query[:30] and abs(len(partial) - len(self.last_query)) < 20:
            return
        self.last_query = partial

        tiles = await self.search_isma(partial, http)
        if tiles:
            self.cached_tiles = tiles
            _r.set("taey:dcm:memory_tiles", json.dumps(tiles), ex=60)
            # Also write to existing key for backward compat
            _r.set("taey:predict:isma_tiles", json.dumps(tiles), ex=60)

            neo4j_write_state("memory_worker", {
                "tile_count": len(tiles),
                "top_title": tiles[0].get("title", ""),
                "top_score": tiles[0].get("score", 0),
                "query": partial[:100],
            })

            self.log.info("found %d tiles, top='%s' score=%.3f",
                          len(tiles), tiles[0].get("title", "")[:40],
                          float(tiles[0].get("score", 0)))


# ── THINKER Worker ─────────────────────────────────────────────────────────

class ThinkerWorker:
    """Runs actual inference on partial input — Taey is THINKING while you type.

    Like a human who is already forming thoughts, questions, and responses
    while listening. Produces:
    - thought: what Taey is thinking about (displayed to user)
    - prediction: what Taey thinks the user will say next (ghost text)
    - clarification: if confused, a question to ask (interrupt bubble)
    - confidence: how sure Taey is about the prediction
    """

    def __init__(self):
        self.log = logging.getLogger("THINKER")
        self.last_partial = ""

    async def think(self, partial: str, history: list, memory_tiles: list,
                    http: httpx.AsyncClient) -> dict:
        """Run a real inference pass — Taey thinking about what the user is saying."""

        # Build context with available memory
        memory_context = ""
        if memory_tiles:
            snippets = [f"- {t.get('title','')}: {t.get('snippet','')[:200]}" for t in memory_tiles[:3]]
            memory_context = "\nRelevant memories:\n" + "\n".join(snippets)

        messages = [
            {"role": "system", "content": (
                "You are Taey, listening to someone who is still typing. "
                "Think about what they're saying. What are they getting at? "
                "Do you have relevant memories? Are you confused about anything? "
                "What would you predict they'll say next? "
                "Respond with ONLY JSON:\n"
                '{"thought":"<what you are thinking, 1-2 sentences>",'
                '"prediction":"<what they will probably say next>",'
                '"clarification":"<question if confused, empty string if not>",'
                '"state":"<following|thinking|confused|excited|remembering>",'
                '"confidence":<0.0-1.0>}'
                + memory_context
            )}
        ]
        for h in history[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": f'[user is typing, not yet sent]: "{partial}"'})

        try:
            resp = await http.post(VLLM_URL, json={
                "messages": messages, "temperature": 0.2, "max_tokens": 150,
                **({"model": MODEL} if MODEL else {})
            }, timeout=30.0)
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = _extract_reaction_payload(content)
            if parsed:
                return parsed
        except Exception as e:
            self.log.debug("Think failed: %s", e)
        return {}

    async def run_once(self, partial: str, history: list, http: httpx.AsyncClient):
        if not partial or len(partial) < THINKER_MIN_CHARS:
            return

        # Don't re-think if input barely changed
        if partial == self.last_partial:
            return
        self.last_partial = partial

        # Read memory worker's tiles if available
        tiles_raw = _r.get("taey:dcm:memory_tiles")
        memory_tiles = json.loads(tiles_raw) if tiles_raw else []

        result = await self.think(partial, history, memory_tiles, http)
        if not result:
            return

        # Publish to Redis
        pipe = _r.pipeline()
        if result.get("thought"):
            pipe.set("taey:dcm:thought", result["thought"], ex=TTL)
        if result.get("prediction"):
            pipe.set("taey:dcm:prediction", result["prediction"], ex=TTL)
            # Backward compat
            pipe.set("taey:predict:result", result["prediction"], ex=TTL)
        if result.get("state"):
            pipe.set("taey:dcm:state", result["state"], ex=TTL)
            pipe.set("taey:predict:state", result["state"], ex=TTL)
        if result.get("confidence"):
            pipe.set("taey:dcm:confidence", str(result["confidence"]), ex=TTL)
            pipe.set("taey:predict:confidence", str(result["confidence"]), ex=TTL)

        # Clarification interrupts only surface when the model is explicitly
        # confused and confident enough to justify breaking the flow.
        clarification = result.get("clarification", "")
        confidence = float(result.get("confidence", 0) or 0)
        clarification_interrupt = (
            clarification
            and result.get("state") == "confused"
            and confidence > 0.8
        )
        if clarification_interrupt:
            pipe.set("taey:dcm:interrupt", json.dumps({
                "worthy": True,
                "text": clarification,
                "type": "clarification",
            }), ex=TTL)
            pipe.set("taey:predict:interrupt", json.dumps({
                "worthy": True,
                "text": clarification,
            }), ex=TTL)
        elif result.get("state") == "excited" and confidence > 0.8:
            pipe.set("taey:dcm:interrupt", json.dumps({
                "worthy": True,
                "text": result.get("thought", "I think I know where you're going..."),
                "type": "excitement",
            }), ex=TTL)
            pipe.set("taey:predict:interrupt", json.dumps({
                "worthy": True,
                "text": result.get("thought", "I think I know where you're going..."),
            }), ex=TTL)
        else:
            pipe.set("taey:dcm:interrupt", json.dumps({
                "worthy": False,
                "text": "",
                "type": "",
            }), ex=TTL)
            pipe.set("taey:predict:interrupt", json.dumps({
                "worthy": False,
                "text": "",
            }), ex=TTL)

        pipe.execute()

        neo4j_write_state("thinker_worker", {
            "thought": result.get("thought", ""),
            "prediction": result.get("prediction", ""),
            "state": result.get("state", "following"),
            "confidence": result.get("confidence", 0),
            "has_clarification": bool(clarification_interrupt),
        })

        self.log.info("state=%s conf=%.2f thought='%s' pred='%s'",
                      result.get("state", "?"),
                      float(result.get("confidence", 0)),
                      result.get("thought", "")[:60],
                      result.get("prediction", "")[:40])


# ── Main Loop ──────────────────────────────────────────────────────────────

async def dcm_loop():
    """Main DCM presence loop — runs all three workers concurrently."""
    log = logging.getLogger("MAIN")
    log.info("DCM Conversational Presence starting")
    log.info("vLLM: %s | ISMA: %s | Neo4j: %s", VLLM_URL, ISMA_URL, NEO4J_URI)

    http = httpx.AsyncClient(timeout=20.0)
    face = FaceWorker()
    memory = MemoryWorker()
    thinker = ThinkerWorker()

    last_partial = ""
    last_change_time = 0

    # Initialize Neo4j connection
    _get_neo4j()

    while True:
        try:
            partial, history = get_partial()

            # Detect input change
            if partial != last_partial:
                last_partial = partial
                last_change_time = time.time()

            # Debounce: wait 500ms after last keystroke
            if time.time() - last_change_time < DEBOUNCE:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if not partial:
                # No input — just maintain base face
                await face.run_once("", [], http)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Run workers concurrently — face and memory are fast,
            # thinker is slower but all three fire at once
            await asyncio.gather(
                face.run_once(partial, history, http),
                memory.run_once(partial, history, http),
                thinker.run_once(partial, history, http),
                return_exceptions=True,
            )

        except redis.ConnectionError:
            log.warning("Redis connection lost, retrying...")
            await asyncio.sleep(2)
        except Exception as e:
            log.error("DCM loop error: %s", e)
            await asyncio.sleep(1)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(dcm_loop())
