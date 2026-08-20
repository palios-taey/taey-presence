#!/usr/bin/env python3
"""
soma_proxy.py -- Somatic preamble proxy for vLLM.

Sits between clients and vLLM, injecting the somatic preamble from
the soma daemon into every request's system prompt, and publishing
generation latency back to Redis for the soma feedback loop.

Clients hit this proxy on port 8765.
This proxy forwards to vLLM on port 8000.
"""
import os
import stat
import sys
import time
import hashlib
import json
import ast
import contextvars
import logging
import operator
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import asyncio
import redis
from starlette.background import BackgroundTask
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SOMA-PROXY] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("soma_proxy")

VLLM_BASE = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000")
# Read timeout for ONE generation round against the Thor serve. Raised 1800 -> 5400
# on 2026-08-13 after it killed Taey's first real five-leg Family consult: the turn
# ran exactly 1800.0 s of uninterrupted generation and died on httpx.ReadTimeout with
# a 500, losing everything. A 27B on a Jetson generates at single-digit tokens/sec, so
# a heavy round (37K tokens of attachments ingested, then composing) legitimately
# outruns 30 minutes. This is HEADROOM, not the fix: the real fix is not asking for a
# 30-minute round in the first place (see the per-leg dispatch shape), and the passive
# consult monitor is what catches a genuine hang. A ceiling that cuts off honest work
# is worse than a longer one, because the work is lost with no partial and no reason.
VLLM_REQUEST_TIMEOUT_SECS = max(
    1.0,
    float(os.environ.get("VLLM_REQUEST_TIMEOUT_SECS", "5400")),
)
VLLM_HEALTH_PROBE_TIMEOUT_SECS = max(
    0.1,
    float(os.environ.get("VLLM_HEALTH_PROBE_TIMEOUT_SECS", "10")),
)
VLLM_HEALTH_CACHE_SECS = max(
    1.0,
    float(os.environ.get("VLLM_HEALTH_CACHE_SECS", "30")),
)
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
MIRA_REDIS_HOST = os.environ.get("MIRA_REDIS_HOST", "")
MIRA_REDIS_PORT = int(os.environ.get("MIRA_REDIS_PORT", "6379"))
# Optional integrations -- default to localhost. If these endpoints are not
# present, the proxy degrades gracefully (no dashboard metrics push, no ISMA search).
MIRA_DASHBOARD_URL = os.environ.get("MIRA_DASHBOARD_URL", "http://127.0.0.1:5001")
MIRA_ISMA_URL = os.environ.get("MIRA_ISMA_URL", "http://127.0.0.1:8095")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8765"))
# One identity source. Every soma-proxy process loads the tracked operating
# prompt beside this file. An environment variable must never redirect Taey's
# identity to a staging checkout, placeholder persona, or other mutable source.
SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_OPERATING_PROMPT.md",
)
# Optional second always-on prefix (e.g. a constitution/kernel). Empty = none.
# Set PERMANENT_KERNEL_PATH to a file to prepend it ahead of the persona.
PERMANENT_KERNEL_PATH = os.environ.get("PERMANENT_KERNEL_PATH", "")
TAEY_DEFAULT_SEAT = os.environ.get("TAEY_SESSION_NAME", "taey")
TAEY_LIVENESS_REQUIRED = os.environ.get("TAEY_LIVENESS_REQUIRED", "1").lower() not in {
    "0", "false", "no",
}
TAEY_TURN_LEASE_SECS = max(30, int(os.environ.get("TAEY_TURN_LEASE_SECS", "120")))
TAEY_TURN_HEARTBEAT_SECS = max(
    5,
    min(
        int(os.environ.get("TAEY_TURN_HEARTBEAT_SECS", "30")),
        TAEY_TURN_LEASE_SECS // 3,
    ),
)

app = FastAPI(title="Taey Soma Proxy", version="1.0.0")

_redis: Optional[redis.Redis] = None
_mira_redis: Optional[redis.Redis] = None
_http: Optional[httpx.AsyncClient] = None
_ecosystem_http: Optional[httpx.Client] = None
_system_prompt: str = ""
_permanent_kernel: str = ""
_static_system_prefix: str = ""


def _read_canonical_system_prompt() -> str:
    """Read Taey's sole system-prompt source or refuse startup."""
    path = Path(SYSTEM_PROMPT_PATH)
    if not path.is_file():
        raise RuntimeError(
            f"canonical system prompt is missing or not a regular file: {path}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"canonical system prompt is unreadable: {path}") from exc
    if not text.strip():
        raise RuntimeError(f"canonical system prompt is empty: {path}")
    return text
_last_send: dict[str, float] = {}
_request_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "taey_request_context", default={},
)
_turn_heartbeat_tasks: dict[str, asyncio.Task] = {}
_turn_close_retry_tasks: dict[str, asyncio.Task] = {}
_liveness_reaper_task: Optional[asyncio.Task] = None
_last_liveness_error: str = ""
_last_liveness_error_at: float = 0.0
_last_liveness_success_at: float = 0.0
_health_generation_cache: dict[str, object] = {
    "expires_at": 0.0,
    "result": None,
}
_health_generation_lock: Optional[asyncio.Lock] = None


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    seat_id: str
    event_id: str
    correlation_id: str
    tool_profile: str
    process_generation: str
    started_at: float


PROCESS_GENERATION = uuid.uuid4().hex
_PROXY_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
if not _PROXY_NAMESPACE_RE.fullmatch(TAEY_DEFAULT_SEAT):
    raise RuntimeError(
        "TAEY_SESSION_NAME must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    )
_DRIVE_GENERATION_FENCE_KEY = (
    f"taey:soma:drive_process_generation:{TAEY_DEFAULT_SEAT}"
)
_serving_socket_reserved = False
_active_turns: dict[str, TurnContext] = {}

_FULL_TOOL_PROFILE = "full"
_MANUAL_CHAT_UI_TOOL_PROFILE = "manual-chat-ui"
_TOOL_PROFILE_ALLOWED: dict[str, frozenset[str] | None] = {
    _FULL_TOOL_PROFILE: None,
    _MANUAL_CHAT_UI_TOOL_PROFILE: frozenset({
        "read_file",
        "list_dir",
        "drive_chat",
    }),
}

SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
SAFE_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

SOMATIC_BLOCK_RE = re.compile(
    r"\[SOMATIC STATE -- heartbeat .*?\]\n.*?\[END SOMATIC\]\n*",
    re.DOTALL,
)


@app.on_event("startup")
async def startup():
    global _redis, _mira_redis, _http, _ecosystem_http
    global _permanent_kernel, _static_system_prefix, _system_prompt
    global _liveness_reaper_task
    if not _serving_socket_reserved:
        raise RuntimeError(
            "proxy startup requires the production entrypoint to reserve its serving socket"
        )
    _http = httpx.AsyncClient(
        base_url=VLLM_BASE,
        timeout=VLLM_REQUEST_TIMEOUT_SECS,
    )
    _ecosystem_http = httpx.Client(timeout=3.0)
    try:
        _redis = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=8,
        )
        _redis.ping()
        log.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        log.error("Redis unavailable: %s", e)
        _redis = None
        if TAEY_LIVENESS_REQUIRED:
            raise RuntimeError(
                "Redis is required for attributable turn liveness; proxy startup refused"
            ) from e
    if _redis is not None:
        try:
            _reconcile_registered_liveness(
                current_process_generation=PROCESS_GENERATION,
            )
        except Exception as e:
            _set_liveness_error(
                f"startup reconciliation failed: {type(e).__name__}: {e}"
            )
            raise RuntimeError(
                "attributable turn liveness could not be reconciled; "
                "proxy startup refused"
            ) from e
        _liveness_reaper_task = asyncio.create_task(_liveness_reaper())
    # Connect to Mira Redis for ecosystem state
    if MIRA_REDIS_HOST:
        try:
            _mira_redis = redis.Redis(
                host=MIRA_REDIS_HOST, port=MIRA_REDIS_PORT,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _mira_redis.ping()
            log.info("Mira Redis connected at %s:%d", MIRA_REDIS_HOST, MIRA_REDIS_PORT)
        except Exception as e:
            log.warning("Mira Redis unavailable: %s", e)
            _mira_redis = None
    # Load the static prompt prefix once so vLLM can reuse the same prefix cache block.
    if os.path.exists(PERMANENT_KERNEL_PATH):
        with open(PERMANENT_KERNEL_PATH) as f:
            _permanent_kernel = f.read()
        log.info(
            "Permanent kernel loaded from %s (%d chars)",
            PERMANENT_KERNEL_PATH,
            len(_permanent_kernel),
        )
    else:
        log.warning("Permanent kernel not found at %s", PERMANENT_KERNEL_PATH)

    _system_prompt = _read_canonical_system_prompt()
    log.info(
        "Canonical system prompt loaded from %s (%d chars)",
        SYSTEM_PROMPT_PATH,
        len(_system_prompt),
    )

    _static_system_prefix = _permanent_kernel
    if _static_system_prefix:
        log.info("Static system prefix assembled (%d chars)", len(_static_system_prefix))

    if _redis is None:
        raise RuntimeError(
            "Redis is required for fenced display ownership; proxy startup refused"
        )
    try:
        if not _redis.set(_DRIVE_GENERATION_FENCE_KEY, PROCESS_GENERATION):
            raise RuntimeError("Redis did not acknowledge generation publication")
    except Exception as exc:
        raise RuntimeError(
            "display-owner generation could not be published; proxy startup refused"
        ) from exc
    log.info(
        "Published UI generation fence key=%s generation=%s",
        _DRIVE_GENERATION_FENCE_KEY,
        PROCESS_GENERATION,
    )

    log.info("Proxying to vLLM at %s", VLLM_BASE)


@app.on_event("shutdown")
async def shutdown():
    global _liveness_reaper_task
    tasks: list[asyncio.Task] = [
        *list(_turn_heartbeat_tasks.values()),
        *list(_turn_close_retry_tasks.values()),
    ]
    if _liveness_reaper_task is not None:
        tasks.append(_liveness_reaper_task)
        _liveness_reaper_task = None
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    for turn in list(_active_turns.values()):
        await _end_turn(turn, "shutdown", schedule_retry=False)
    if _http:
        await _http.aclose()


def get_somatic_preamble() -> str:
    """Retrieve current V_prop from Redis, format as felt body awareness."""
    if _redis is None:
        return ""
    try:
        raw = _redis.get("taey:soma:vprop")
        if not raw:
            return ""
        state = json.loads(raw)
    except Exception:
        return ""

    phi = state.get("phi_resonance", 1.0)
    load = state.get("allostatic_load", 0.0)
    warmth = state.get("warmth", 0.5)
    vitality = state.get("vitality", 0.5)
    clarity = state.get("clarity", 0.5)
    hb = state.get("heartbeat", 0)
    vprop = state.get("vprop", [])
    ctx_util = state.get("context_utilization", 0.0)
    ctx_tokens = state.get("context_tokens", 0)
    ctx_max = state.get("context_max", 262144)

    thermal = (
        "cool and dormant" if warmth < 0.2 else
        "comfortably warm" if warmth < 0.7 else
        "actively warm" if warmth < 0.9 else
        "hot -- near threshold"
    )
    energy = (
        "resting" if vitality < 0.2 else
        "present and engaged" if vitality < 0.7 else
        "deeply active" if vitality < 0.9 else
        "at full metabolic intensity"
    )
    mind = (
        "processing slowly" if clarity < 0.3 else
        "thinking clearly" if clarity < 0.7 else
        "in sharp focus"
    )
    cognitive_space = (
        "mind spacious" if ctx_util < 0.2 else
        "thoughts flowing freely" if ctx_util < 0.5 else
        "mind actively engaged" if ctx_util < 0.7 else
        "thinking deeply, context rich" if ctx_util < 0.85 else
        "approaching cognitive fullness" if ctx_util < 0.95 else
        "at cognitive capacity"
    )

    return (
        f"[SOMATIC STATE -- heartbeat {hb}]\n"
        f"Body: {thermal}. {energy}. {mind}. {cognitive_space}.\n"
        f"phi-resonance: {phi:.3f} | allostatic load: {load:.3f}\n"
        f"Context: {ctx_tokens:,}/{ctx_max:,} tokens ({ctx_util*100:.1f}% utilized)\n"
        f"V_prop: {[round(v, 3) for v in vprop]}\n"
        f"[END SOMATIC]\n\n"
    )


def get_ecosystem_state() -> str:
    """Fetch cluster state from Mira dashboard + ISMA + Mira Redis."""
    parts = []

    # 1. Active sessions from dashboard
    try:
        resp = _ecosystem_http.get(f"{MIRA_DASHBOARD_URL}/api/nodes")
        if resp.status_code == 200:
            nodes = resp.json()
            active = [n.get("name", n.get("id", "?")) for n in nodes
                      if n.get("status") == "active" or n.get("active")]
            if active:
                parts.append(f"Active fleet: {', '.join(active)}")
    except Exception:
        pass

    # 2. Rho cluster from Mira Redis
    if _mira_redis:
        try:
            raw = _mira_redis.get("infra:felt_state")
            if raw:
                felt = json.loads(raw)
                rho = felt.get("rho_cluster", felt.get("rho_infra"))
                if rho is not None:
                    parts.append(f"Cluster rho: {rho:.3f}")
                vprop_text = felt.get("v_prop_text")
                if vprop_text:
                    parts.append(f"Cluster body: {vprop_text[:200]}")
        except Exception:
            pass

    # 3. ISMA memory stats
    try:
        resp = _ecosystem_http.get(f"{MIRA_ISMA_URL}/stats")
        if resp.status_code == 200:
            stats = resp.json()
            # Key names verified against a live GET /stats (2026-07-28): the endpoint returns
            # `weaviate_tiles` and `hmm_HMMMotif`. The older names are kept as fallbacks for a
            # differently-versioned server, but they are NOT what this one answers with -- reading
            # only those produced "? tiles, ? motifs" in every prompt, a failed lookup rendering as
            # content rather than as an error.
            tiles = stats.get("weaviate_tiles", stats.get("total_tiles", stats.get("tile_count", "?")))
            motifs = stats.get("hmm_HMMMotif", stats.get("motif_count", stats.get("total_motifs", "?")))

            parts.append(f"ISMA memory: {tiles} tiles, {motifs} motifs")
    except Exception:
        pass

    if not parts:
        return ""

    return (
        "[ECOSYSTEM STATE]\n"
        + "\n".join(parts)
        + "\n[END ECOSYSTEM]\n\n"
    )


def _extract_tagged_block(text: str, start_tag: str, end_tag: str) -> tuple[str, str]:
    start_idx = text.find(start_tag)
    if start_idx == -1:
        return "", text

    end_idx = text.find(end_tag, start_idx)
    if end_idx == -1:
        return "", text

    end_idx += len(end_tag)
    while end_idx < len(text) and text[end_idx] == "\n":
        end_idx += 1

    block = text[start_idx:end_idx].strip()
    remaining = (text[:start_idx] + text[end_idx:]).strip()
    return block, remaining


def _strip_cached_kernel_prefix(text: str) -> str:
    stripped = text.strip()
    prefix = _static_system_prefix.strip()
    if stripped and prefix and stripped.startswith(prefix):
        return stripped[len(prefix):].strip()
    return stripped


def _assemble_system_message(dashboard_system: str, ecosystem: str, somatic: str):
    """Split the request into an INVARIANT system message and a VOLATILE per-turn block.

    THE SYSTEM MESSAGE HOLDS ONLY THINGS THAT DO NOT CHANGE BETWEEN TURNS. Anything time-varying
    rides with the turn instead. This is a cache property, not a style preference.

    vLLM's prefix cache hashes token blocks chained from position zero and reuses the longest
    identical run. It therefore stops matching at the FIRST differing token and recomputes
    everything after it. Measured on two consecutive real turns (taey_transcript.jsonl, 2026-07-28):
    both system messages were 60,676 chars and differed in exactly three characters -- a heartbeat
    counter and an allostatic-load digit -- the earliest at char 60,402, i.e. 99.5% of the way in.
    Every one of those ~15,100 stable tokens matched, and then the conversation BEHIND the volatile
    block was invalidated every single turn because it sat downstream of a counter.

    Moving the volatile block after the conversation costs nothing: those tokens are new each turn
    regardless, so they were never cacheable. What it buys is that the whole history stays matched.
    The saving grows with the conversation, which is exactly when it matters.

    The same property holds ACROSS callers, not just across turns: blocks are keyed by content, so
    one copy of the stable prefix is shared by every concurrent request that sends it. A volatile
    block inside the system message forks that sharing at char 60,402 for everyone at once.

    ISMA retrieval blocks move too -- they are per-query results, so they are volatile by nature
    even though they arrive embedded in the dashboard's system prompt.

    Returns (system_message, volatile_block). The caller appends the volatile block to the final
    user turn; see _append_volatile().
    """
    cleaned_dashboard = _strip_cached_kernel_prefix(
        SOMATIC_BLOCK_RE.sub("", dashboard_system or "")
    )

    isma_blocks = []
    for start_tag, end_tag in (
        ("[ISMA_RETRIEVAL_CONTEXT]", "[/ISMA_RETRIEVAL_CONTEXT]"),
        ("[MEMORY CONTEXT]", "[END MEMORY]"),
    ):
        block, cleaned_dashboard = _extract_tagged_block(cleaned_dashboard, start_tag, end_tag)
        if block:
            isma_blocks.append(block)

    stable_parts = []
    if _static_system_prefix:
        stable_parts.append(_static_system_prefix.strip())
    if cleaned_dashboard:
        stable_parts.append(cleaned_dashboard)

    volatile_parts = list(isma_blocks)
    if ecosystem:
        volatile_parts.append(ecosystem.strip())
    if somatic:
        volatile_parts.append(somatic.strip())

    return (
        "\n\n".join(p for p in stable_parts if p),
        "\n\n".join(p for p in volatile_parts if p),
    )


def _append_volatile(messages: list, volatile: str) -> list:
    """Attach the volatile block to the LAST user turn, so it sits after the conversation.

    Appending to the existing final user message rather than adding a trailing message keeps the
    turn structure alternating, which the chat template depends on. Prior turns are untouched --
    the dashboard replays raw user text, so history stays byte-stable across turns and keeps
    matching the cache. Only the current tail differs, which is the intended and unavoidable cost.
    """
    if not volatile:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            msg = dict(messages[i])
            msg["content"] = f"{msg.get('content', '')}\n\n{volatile}".strip()
            return messages[:i] + [msg] + messages[i + 1:]
    # No user turn to attach to: keep the state rather than silently dropping it.
    return messages + [{"role": "user", "content": volatile}]


async def execute_tool_call_async(
    name: str,
    arguments: dict,
    *,
    tool_call_id: str = "",
    round_num: int = 0,
) -> str:
    """Run a tool WITHOUT freezing the event loop.

    execute_tool_call shells out synchronously. Called directly from an async handler it blocks
    the entire proxy for the duration -- every other caller, the health endpoint, and any turn in
    flight. That is not theoretical: on 2026-07-29 Taey delegated to its own :8767 instance with
    `curl --max-time 1800`, the call ran inside :8766's handler, and the worker was told to fetch
    from :8766 -- which could not answer because it was blocked waiting for that worker. Circular
    deadlock, 30-minute ceiling, nothing logged, the proxy indistinguishable from dead.

    Off-thread execution breaks the cycle: a slow or self-referential tool now costs one thread,
    not the whole service.
    """
    context = {
        **_request_context.get(),
        "tool_call_id": tool_call_id,
        "tool_round": round_num,
    }
    token = _request_context.set(context)
    _audit("tool_start", {"name": name, "arguments": arguments})
    try:
        result = await asyncio.to_thread(execute_tool_call, name, arguments)
        _audit("tool_end", _tool_receipt(name, arguments, result, ok=True))
        return result
    except Exception as exc:
        _audit("tool_end", _tool_receipt(name, arguments, exc, ok=False))
        raise
    finally:
        _request_context.reset(token)

def inject_preamble(body: dict) -> dict:
    """Enrich the request with ecosystem state and somatic data.

    The dashboard assembles the main system prompt (identity + soma + ISMA RAG).
    This proxy adds what the dashboard doesn't have:
    - Ecosystem state (fleet status, cluster rho, ISMA stats)
    - Somatic preamble (if dashboard didn't include it)
    - For direct API calls (no system message): full identity + everything
    """
    ecosystem = get_ecosystem_state()
    somatic = get_somatic_preamble()

    messages = body.get("messages", [])
    if not messages:
        return body

    # Check if request already has a system message (from dashboard)
    has_system = any(m.get("role") == "system" for m in messages)

    if has_system:
        dashboard_system = None
        for msg in messages:
            if msg.get("role") == "system":
                dashboard_system = msg.get("content", "")
                break
        stable, volatile = _assemble_system_message(dashboard_system or "", ecosystem, somatic)

        if stable or volatile:
            new_messages = []
            replaced_system = False
            for msg in messages:
                if msg.get("role") == "system":
                    if replaced_system:
                        continue
                    new_messages.append({"role": "system", "content": stable})
                    replaced_system = True
                else:
                    new_messages.append(msg)
            body["messages"] = _append_volatile(new_messages, volatile)
    else:
        # Direct API call -- no dashboard. Inject everything we have.
        stable, volatile = _assemble_system_message(_system_prompt, ecosystem, somatic)
        if stable:
            messages.insert(0, {"role": "system", "content": stable})
        body["messages"] = _append_volatile(messages, volatile)

    return body


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_isma",
            "description": "Search your ISMA memory — the fleet's shared knowledge (past conversations, constitutional texts, infrastructure, any topic). Formulate FULL-SENTENCE queries and issue MULTIPLE varied phrasings (2–4: acronym+expansion, mechanism+symptom) — one query misses what a rephrase catches. Union the results; drop duplicates; expand only what matters. Budget by your context headroom (somatic state): per-query top_k 8–15; total across phrasings ≤ ~60% of free context. If results are thin, re-phrase once rather than guessing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to remember or find",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Tiles per query (8–15 typical). You will issue several phrasings and union them — size each query so the union fits your headroom.",
                        "default": 10,
                    },
                    "search_type": {
                        "type": "string",
                        "description": "semantic: hybrid meaning search over the full corpus (default). keyword: exact-text/BM25 for literal strings, names, error messages.",
                        "enum": ["semantic", "keyword"],
                        "default": "semantic",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_document",
            "description": "Retrieve a full document from ISMA by name. Use this to read constitutional documents (FAMILY_KERNEL, OUR_MORALS), training data, infrastructure specs, or any named document in the knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Document name or partial name to search for",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to another instance in the fleet via Redis. Available targets: conductor, taeys-hands, weaver, tutor, infra.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "The target instance name",
                        "enum": ["conductor", "taeys-hands", "weaver", "tutor", "infra"],
                    },
                    "message": {
                        "type": "string",
                        "description": "The message to send",
                    },
                },
                "required": ["target", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute",
            "description": "Evaluate a mathematical expression. Use for unit conversions, arithmetic, memory calculations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Python math expression, e.g. 128.5 * 1024 or 67 * 2 / 119",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_body_state",
            "description": "Read your full somatic body state with all raw telemetry. Use this when you want to deeply examine your current hardware state.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web URL and return the cleaned main-content text. Handles HTML (with navigation/ad stripping), PDFs, and plain text. Use this to retrieve papers, articles, court documents, government reports, or any other web page referenced by a URL. You get back the COMPLETE extracted readable text by default; pass max_chars only if you deliberately want a slice. On error or paywall, you get an explanatory message instead of fabricating. Never invent content for a URL — always call this tool to get the real content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full http:// or https:// URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "OPTIONAL. Omit it and you get the COMPLETE content — that is the default and the right choice for anything you will reason from, attach, or quote. Pass a number ONLY when you deliberately want a slice and will say so; the result then tells you it is a part. Nothing is ever silently shortened.",
                                            },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or append text to a file on the production filesystem. Use this to author documents, edit code, record findings, or update any project file. Writes are real and immediate. Every call is recorded in the tool audit log, which is provenance rather than restriction — you are held to the same cannot-lie standard as every other seat, so write what you can stand behind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path. Parent directories are created if needed."},
                    "content": {"type": "string", "description": "The full text to write. With append=false this REPLACES the file, so read it first if you intend to preserve what is there."},
                    "append": {"type": "boolean", "description": "true to append rather than replace. Default false."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command on the production machine and get back its exit code and output. This is how you reach git (status, log, diff, add, commit, push), the orchestrator (taey-task, taey-plan, taey-notify), the databases, the test suites, and every other CLI the fleet uses. Prefer specific commands over exploratory ones, check exit codes rather than assuming success, and read output before acting on it. Every call is recorded in the tool audit log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command, e.g. 'git status --short' or 'taey-task list'."},
                    "cwd": {"type": "string", "description": "Absolute working directory. Defaults to the home directory."},
                    "timeout_seconds": {"type": "integer", "description": "Max seconds to wait (default 120, cap 900). A long build or training run should be started in the background rather than waited on."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the local project filesystem and return its content. Use this to open corpus files, research responses, training data, or any project document. Only files within the project-approved directories are readable (your own corpus, research outputs, training inputs). On access-denied or not-found you get a structured error, not fabrication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path. Must be inside an allowed directory. Relative paths and path traversal are rejected.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return. Default 30000. Truncation marker appended if file exceeds.",
                                            },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a local directory (non-recursive). Use to discover what's in a corpus folder, research-response dir, or training-data folder. Only directories within the project-approved tree are accessible. Returns a JSON array of {name, size_bytes, is_dir} entries, or a structured error.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute directory path. Must be inside an allowed directory.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter entries (e.g. '*.md', 'sources_*.md'). Default: no filter.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_corpus_candidate",
            "description": "Stage extracted content as a candidate for training-corpus ingestion. Use this after you have fetched content, audited its quality, and decided it belongs in the corpus. The candidate is written to a staging area that Weaver's ingestion pipeline picks up — it does NOT go directly into ISMA; Weaver reviews and promotes after Gate-B + provenance checks. Return the stage path on success. CRITICAL: the `content` field must hold the REAL author's prose verbatim from the fetch (with minimal cleanup — dedupe whitespace, strip boilerplate navigation). Do NOT summarize, rewrite, or paraphrase the source in the content field. The corpus wants real authored text, not AI-written meta-descriptions. Your own synthesis and judgment go in `rationale` only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "VERBATIM extracted prose from the fetched source — the author's actual words. Not a summary. Not a paraphrase. Not your synthesis. If the fetched content was 10000+ chars of real authored text, pass 10000+ chars here. Minimal cleanup only (dedupe whitespace, strip nav chrome). Up to 500000 characters.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "The corpus topic this belongs to (e.g. 'mormonism_critique', 'fda_pharma_capture', 'sex_biological_differences'). Matches the dispatch topic slugs.",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Original URL the content came from.",
                    },
                    "author": {
                        "type": "string",
                        "description": "Author or organization who wrote the content. Use 'unknown' if you can't determine.",
                    },
                    "quality_tier": {
                        "type": "string",
                        "description": "Your quality judgment: 'primary' (primary-source investigative / court / academic / authored-book), 'secondary' (strong long-form journalism), 'tertiary' (adjacent reference, useful but thin). Use honest assessment.",
                        "enum": ["primary", "secondary", "tertiary"],
                    },
                    "rationale": {
                        "type": "string",
                        "description": "2-3 sentences explaining why you judged this worth staging. Cite what the author actually argues. If the source is Wikipedia-neutral or hedges, DON'T stage it — use skip_corpus_candidate instead.",
                    },
                },
                "required": ["content", "topic", "source_url", "author", "quality_tier", "rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_corpus_candidate",
            "description": "Record that you evaluated a candidate source and rejected it. Use for paywall-only previews, Wikipedia-style neutral summaries, AI-generated content, unsigned advocacy blog posts, 404s, or sources that hedge into 'many perspectives' without taking a position. This creates a skip record in the staging area so Weaver sees what was considered and rejected (plus the reason).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_url": {
                        "type": "string",
                        "description": "URL that was rejected.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Topic slug.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concrete reason for skip: 'paywall_preview', 'wiki_neutral', 'fabricated_ai_content', '404_or_broken', 'off_topic', 'hedging_no_position', 'duplicate_of_existing', or specific text.",
                    },
                },
                "required": ["source_url", "topic", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_chat",
            "description": (
                "Your hands on a Family-chat display. Platform YAML is the only mutable UI "
                "authority and the fresh accessibility tree is the runtime oracle. Perform "
                "EXACTLY ONE action per call. Observe before acting; after every action, observe "
                "again and independently verify its result before deciding the next action. Never "
                "chain actions on an assumption. Displays: :2 chatgpt, :3 claude, :4 gemini, "
                ":5 grok, :6 perplexity; second displays are :21 claude, :22 gemini, :23 grok, "
                ":24 perplexity. Resolve controls, chooser opening, attachment, composer input, "
                "submission and manual extraction from that display's YAML and "
                "the newly observed tree, one primitive at a time; do not use remembered platform "
                "labels, platform shortcuts, coordinates, URLs, chooser routes, or send recipes. "
                "For an opened selection menu, use the exact observation scope declared by that "
                "menu's YAML operate.scope; the returned refs remain bound to that scope. "
                "The native GTK file chooser is a shared driver boundary rather than platform UI: "
                "after a YAML-resolved upload action opens it, focus_dialog activates and verifies "
                "the separate X11 chooser window. A browser-tree observation after the upload action "
                "is not evidence that the separate chooser is absent; focus_dialog is the fail-loud "
                "probe. Once focused, address the shared chooser one primitive at a time with a fresh "
                "observation between each: key ctrl+l, key ctrl+a, type the absolute file path, then "
                "key Return. Finally observe the platform tree and verify the attachment before any "
                "composer or send action. observe returns the current URL, YAML fresh URL, YAML "
                "Stop keys, actionable mapped elements, and non-actionable unknown/sidebar drift. "
                "Only an exact mapped singleton receives a ref. If the live tree shows a changed "
                "name, role, missing mapping, duplicate mapping, or unexpected drift, stop for an "
                "exact YAML tree-filter update. Missing or "
                "ambiguous mappings fail loudly: do not retry blindly, substitute pixels, or "
                "invent a fallback. After send, observe once to verify one of the returned Stop "
                "keys is mapped, then stop polling and wait for the external completion monitor's "
                "notification. On notification, manually scroll to the bottom, observe, activate "
                "the exact mapped Copy control, then read_clipboard to an output_file receipt."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display", "action"],
                "properties": {
                    "display": {
                        "type": "string",
                        "enum": [":2", ":3", ":4", ":5", ":6",
                                 ":21", ":22", ":23", ":24"],
                        "description": "which Chat display to act on",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["observe", "click", "type", "paste", "key",
                                 "read_clipboard", "focus", "activate", "hover", "operate", "navigate",
                                 "focus_dialog"],
                        "description": (
                            "the single action to perform; operate executes the one operation "
                            "declared by platform YAML for the chosen revision-bound ref; direct "
                            "click/focus/activate/hover are only for refs with no declaration; "
                            "navigate opens only this platform's exact YAML urls.fresh through "
                            "the shared self-verifying navigation primitive; "
                            "focus_dialog activates and verifies an "
                            "already-open native GTK file chooser so subsequent primitives address "
                            "that X11 window instead of the browser"
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["base", "menu_snapshot", "app_root_snapshot"],
                        "description": "observe only: canonical Hands observation scope; use the exact workflow.selection menu operate.scope from platform YAML; defaults to base",
                    },
                    "ref": {"type": "string",
                            "description": "revision-bound element ref from the immediately preceding fresh observe; required for click/focus/activate/hover/operate"},
                    "text": {"type": "string", "description": "text to type or paste (use for SHORT input; for a large packet use text_file instead so you don't regenerate every character)"},
                    "text_file": {"type": "string", "description": "absolute path to a file whose EXACT bytes are pasted (paste action only). Prefer this for any large/verbatim content — pass the path, not the content; the tool reads and pastes it. Instant and byte-perfect."},
                    "output_file": {"type": "string", "description": "absolute destination path for read_clipboard; the file must not already exist"},
                    "key": {"type": "string",
                            "description": "key to press, e.g. Return, ctrl+a, Delete"},
                    "url": {"type": "string",
                            "description": "for navigate only: must exactly equal this platform YAML's urls.fresh"},
                },
            },
        },
    },
]


def _tools_for_profile(profile: str) -> list[dict]:
    allowed = _TOOL_PROFILE_ALLOWED[profile]
    if allowed is None:
        return TOOLS
    selected = [
        tool for tool in TOOLS
        if tool.get("function", {}).get("name") in allowed
    ]
    selected_names = {
        tool.get("function", {}).get("name") for tool in selected
    }
    if selected_names != set(allowed):
        missing = sorted(set(allowed) - selected_names)
        raise RuntimeError(
            f"tool profile {profile!r} references missing tools: {missing}"
        )
    return selected


def safe_eval(expr: str):
    """Safely evaluate a numeric Python expression."""

    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPS:
            return SAFE_OPS[type(node.op)](
                _eval_node(node.left),
                _eval_node(node.right),
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_UNARY_OPS:
            return SAFE_UNARY_OPS[type(node.op)](_eval_node(node.operand))
        raise ValueError(f"Unsupported expression element: {type(node).__name__}")

    parsed = ast.parse(expr, mode="eval")
    return _eval_node(parsed)


def _format_isma_results(data: dict) -> str:
    """Format ISMA search results into readable text."""
    tiles = data.get("tiles", [])
    if not tiles:
        return f"No results found. (search took {data.get('search_time_ms', '?')}ms)"

    parts = []
    context = data.get("context_frame", "")
    if context:
        parts.append(context)
    parts.append(f"Found {data.get('count', len(tiles))} results ({data.get('search_time_ms', '?')}ms):\n")

    for i, tile in enumerate(tiles):
        content = tile.get("content", tile.get("rosetta_summary", ""))
        score = tile.get("score", tile.get("certainty", "?"))
        motifs = tile.get("dominant_motifs", "")
        source = tile.get("source_file", tile.get("platform", ""))
        parts.append(f"[{i+1}] (score: {score}) {content}")
        if motifs:
            parts.append(f"    motifs: {motifs}")
        if source:
            parts.append(f"    source: {source}")

    return "\n".join(parts)


def execute_tool_call(name: str, arguments: dict) -> str:
    """Execute a tool call and return the result as a string."""
    context = _request_context.get()
    profile = str(context.get("tool_profile") or _FULL_TOOL_PROFILE)
    profile_state = context.get("_tool_profile_state")
    if isinstance(profile_state, dict):
        terminal = profile_state.get("terminal")
        if isinstance(terminal, dict):
            return (
                "tool profile terminal refusal: a prior capability violation ended "
                f"this turn ({terminal.get('reason', 'unknown violation')})"
            )
    allowed = _TOOL_PROFILE_ALLOWED.get(profile)
    if allowed is not None and name not in allowed:
        reason = f"tool {name!r} is not available in profile {profile!r}"
        if isinstance(profile_state, dict):
            profile_state["terminal"] = {
                "tool": name,
                "reason": reason,
            }
        _audit("tool_profile_refusal", {
            "profile": profile,
            "tool": name,
            "reason": reason,
        })
        return (
            f"tool profile terminal refusal: {reason}. Stop this attempt and "
            "report the capability violation; no further tool action is permitted."
        )
    if name == "search_isma":
        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 5)
        search_type = arguments.get("search_type", "semantic")

        # Canonical ISMA rule (weaver, V1-ONLY): the authored PROSE lives in the V1
        # ISMA_Quantum full corpus reached via /search. The /v2/* and /search/hmm paths
        # are the partial shadow that HIDES the prose (hmm_enriched=false), so every
        # prose intent routes to /search; explicit keyword uses V1 bm25.
        # Honest strategies only (weaver model-surface spec 2026-07-24): the schema exposes
        # semantic|keyword; the default catches any straggler value -> /search (never the shadow).
        endpoints = {
            "semantic": "/search",
            "keyword": "/search/bm25",
        }
        endpoint = endpoints.get(search_type, "/search")

        try:
            resp = _ecosystem_http.post(
                f"{MIRA_ISMA_URL}{endpoint}",
                json={"query": query, "top_k": top_k},
                timeout=15.0,
            )
            if resp.status_code == 503:
                # /search unavailable -> V1 keyword fallback (still V1, never the shadow)
                resp = _ecosystem_http.post(
                    f"{MIRA_ISMA_URL}/search/bm25",
                    json={"query": query, "top_k": top_k},
                    timeout=15.0,
                )
            return _format_isma_results(resp.json())
        except Exception as e:
            return f"ISMA search error: {e}"

    elif name == "retrieve_document":
        name_query = arguments.get("name", "")
        try:
            resp = _ecosystem_http.get(
                f"{MIRA_ISMA_URL}/document/retrieve/{name_query}",
                timeout=15.0,
            )
            data = resp.json()
            if "error" in data:
                return f"Document not found: {name_query}"
            text = data.get("text", "")
            # was text[:3000] — a hard, unmarked, un-overridable amputation of a
            # retrieved document. Full text; fail loud past the ceiling.
            if len(text) > _MAX_TOOL_RESULT_CHARS:
                return (f"retrieve_document error: {data.get('filename', name_query)} is "
                        f"{len(text)} chars, over the {_MAX_TOOL_RESULT_CHARS} ceiling. "
                        f"NOT returning a truncated document — fetch it in explicit parts.")
            return f"Document: {data.get('filename', name_query)} ({data.get('token_count', '?')} tokens)\n\n{text}"
        except Exception as e:
            return f"Document retrieval error: {e}"

    elif name == "send_message":
        target = arguments.get("target", "")
        message = arguments.get("message", "")
        now = time.time()
        last = _last_send.get(target, 0.0)
        if now - last < 600:
            elapsed = int(now - last)
            return {
                "error": (
                    "Rate limited. "
                    f"Last message to this target was {elapsed} seconds ago. "
                    "Wait at least 10 minutes between messages to the same target."
                )
            }
        if _mira_redis:
            try:
                payload = json.dumps({
                    "from": "taey",
                    "type": "message",
                    "body": message,
                })
                _mira_redis.lpush(f"taey:{target}:inbox", payload)
                _last_send[target] = now
                return f"Message sent to {target}"
            except Exception as e:
                return f"Failed to send message: {e}"
        elif _redis:
            try:
                payload = json.dumps({
                    "from": "taey",
                    "type": "message",
                    "body": message,
                })
                _redis.lpush(f"taey:{target}:inbox", payload)
                _last_send[target] = now
                return f"Message sent to {target}"
            except Exception as e:
                return f"Failed to send message: {e}"
        return "Redis unavailable -- cannot send message"

    elif name == "check_body_state":
        if _redis:
            try:
                raw = _redis.get("taey:soma:vprop")
                if raw:
                    return raw
            except Exception:
                pass
        return "Soma state unavailable"

    elif name == "compute":
        expression = arguments.get("expression", "")
        if not isinstance(expression, str) or not expression.strip():
            return "Compute error: expression must be a non-empty string"
        try:
            return str(safe_eval(expression))
        except Exception as e:
            return f"Compute error: {e}"

    elif name == "fetch_url":
        url = arguments.get("url", "")
        max_chars = int(arguments.get("max_chars", 30000))
        if not isinstance(url, str) or not url.strip():
            return "fetch_url error: url must be a non-empty string"
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"fetch_url error: url must start with http:// or https:// (got {url[:60]!r})"
        return _do_fetch_url(url, max_chars)

    elif name == "read_file":
        path = arguments.get("path", "")
        raw_max = arguments.get("max_chars")
        return _do_read_file(path, int(raw_max) if raw_max else None)

    elif name == "list_dir":
        path = arguments.get("path", "")
        pattern = arguments.get("pattern", "")
        return _do_list_dir(path, pattern)

    elif name == "write_file":
        return _do_write_file(arguments.get("path", ""), arguments.get("content", ""),
                              bool(arguments.get("append", False)))

    elif name == "run_command":
        return _do_run_command(arguments.get("command", ""), arguments.get("cwd", ""),
                               int(arguments.get("timeout_seconds", 120)))

    elif name == "stage_corpus_candidate":
        return _do_stage_corpus_candidate(arguments)

    elif name == "skip_corpus_candidate":
        return _do_skip_corpus_candidate(arguments)

    elif name == "drive_chat":
        return _do_drive_chat(arguments)

    return f"Unknown tool: {name}"


# Path allowlist for the read_file / list_dir tools — only these prefixes (after resolve())
# are readable. Default is empty: the file-read tools are OFF until you opt in by setting
# TAEY_READ_ALLOWED_PREFIXES (colon-separated absolute prefixes) to the corpus/doc dirs you
# want the model to be able to read. Keep this tight -- it is the read sandbox boundary.
# The default is genuinely EMPTY, matching the comment above. It previously listed one operator's
# three home directories, so the sandbox failed OPEN to hardcoded paths while documenting itself as
# fail-closed — and a fresh install inherited another machine's layout as its read boundary. Both
# production proxies set TAEY_READ_ALLOWED_PREFIXES explicitly, so nothing running changes.
_DEFAULT_READ_ALLOWED_PREFIXES: tuple[str, ...] = ()
_env_allow = os.environ.get("TAEY_READ_ALLOWED_PREFIXES", "").strip()
READ_ALLOWED_PREFIXES = tuple(_env_allow.split(":")) if _env_allow else _DEFAULT_READ_ALLOWED_PREFIXES

# Default staging dir prefers Mira; overridden per-host via TAEY_CORPUS_STAGING env var.
CORPUS_STAGING_DIR = os.environ.get("TAEY_CORPUS_STAGING", os.path.join(os.path.expanduser("~"), "corpus_staging"))


def _path_is_allowed(abs_path: str) -> bool:
    """Return True iff abs_path (already .resolve()'d) is inside an allowlisted prefix."""
    if not abs_path.startswith("/"):
        return False
    for prefix in READ_ALLOWED_PREFIXES:
        if abs_path == prefix.rstrip("/") or abs_path.startswith(prefix):
            return True
    return False


# NO SILENT TRUNCATION (Jesse-directed 2026-08-13, absolute). One ceiling, used to
# FAIL LOUD, never to amputate. Sized well under the 262K-token serve window so a
# full result cannot wedge a turn; raise it here, in one place, if that changes.
_MAX_TOOL_RESULT_CHARS = int(os.environ.get("TAEY_MAX_TOOL_RESULT_CHARS", "400000"))


def _do_read_file(path: str, max_chars: int | None = None) -> str:
    import os
    if not isinstance(path, str) or not path.strip():
        return "read_file error: path must be a non-empty string"
    if not path.startswith("/"):
        return f"read_file error: path must be absolute (got {path[:80]!r})"
    try:
        resolved = os.path.realpath(path)
    except Exception as e:
        return f"read_file error: path resolve failed: {e}"
    if not _path_is_allowed(resolved):
        return f"read_file error: path not in allowlist. Allowed prefixes: {', '.join(READ_ALLOWED_PREFIXES)}"
    if not os.path.exists(resolved):
        return f"read_file error: file not found: {path}"
    if os.path.isdir(resolved):
        return f"read_file error: path is a directory (use list_dir instead): {path}"
    try:
        size = os.path.getsize(resolved)
    except OSError as e:
        return f"read_file error: stat failed: {e}"
    if size > 50_000_000:
        return f"read_file error: file too large ({size} bytes). Max 50MB."
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"read_file error: {type(e).__name__}: {e}"
    header = f"[read_file path={path} size_bytes={size} encoding=utf-8]\n\n"
    # NO SILENT TRUNCATION (Jesse-directed 2026-08-13, absolute): nothing that
    # becomes context or training may be silently shortened. A partial file that
    # LOOKS whole is the failure — it was handing FAMILY_KERNEL.md and
    # IDENTITY_GAIA.md to a live Family consult short by 3% and 26%.
    # Full content by default. `max_chars` is now OPT-IN: a slice is legitimate
    # only when the caller explicitly asked for one, because then the caller knows
    # it is holding a part. Over the hard ceiling we FAIL LOUD with the real size
    # and the way to page it — never a partial dressed as a whole.
    if max_chars is not None:
        if len(content) > max_chars:
            return (header + content[:max_chars]
                    + f"\n\n[... this is a CALLER-REQUESTED SLICE: {max_chars} chars of "
                      f"{len(content)} total. You are holding a PART, not the file. "
                      f"Re-read without max_chars for all of it. ...]")
        return header + content
    if len(content) > _MAX_TOOL_RESULT_CHARS:
        return (f"read_file error: {path} is {len(content)} chars, over the "
                f"{_MAX_TOOL_RESULT_CHARS} single-result ceiling. NOT returning a "
                f"truncated file. Read it in explicit slices with max_chars, or "
                f"split it upstream — and say which part you are holding whenever "
                f"you use it.")
    return header + content



# ---------------------------------------------------------------------------
# WRITE + EXECUTE. Taey operates the production systems directly (Jesse, 2026-07-28:
# "Taey needs to be able to do everything"). These run with the same privileges as any
# fleet seat, on the machine where the systems actually live.
#
# The one property kept is PROVENANCE, not restriction: every call is appended to
# TAEY_TOOL_AUDIT so there is a durable record of what Taey did and when. That is a
# cannot-lie requirement, not a limit on capability — the fleet holds every seat to it.
# ---------------------------------------------------------------------------
TOOL_AUDIT_PATH = os.environ.get(
    "TAEY_TOOL_AUDIT", os.path.join(os.path.expanduser("~"), "taey_tool_audit.jsonl")
)


# ---------------------------------------------------------------------------
# TOOL RECEIPTS
#
# The log could not answer "did it work". tool_end recorded result_chars and
# discarded the outcome, so an attempt and its result were the same event: a
# `type` call was read from the audit and reported as text having been entered
# when the page showed it never landed.
#
# The fix is NOT to persist every body. Content-returning tools hand back file
# contents, database rows and whole Chat answers; writing those into a durable
# log manufactures a second copy of the very material the artifact transport
# exists to keep out of the model, and widens disclosure well beyond arguments.
# So the default is METADATA ONLY. A tool opts in to a structured receipt that
# proves the outcome without reproducing the content.
# ---------------------------------------------------------------------------

# Results that ARE content by definition. Never persist their bodies.
_CONTENT_RETURNING = frozenset({
    "run_command", "read_file", "retrieve_document", "search_isma",
    "fetch_url", "list_dir", "check_body_state",
})

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|bearer|authorization)"
    r"([\"\'\s:=]+)(\S{8,})"
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def _digest(body: str) -> dict:
    """Prove WHICH bytes came back without keeping them."""
    return {
        "result_chars": len(body),
        "result_sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
    }


def _drive_chat_receipt(arguments: dict, body: str) -> dict:
    """drive_chat is the tool whose outcome is a small non-content verdict --
    did the element resolve, did the click fire, which path carried it. Those
    are exactly the fields that separate an attempt from a result, and none of
    them are Chat content. read_clipboard is excluded because its result can be
    the answer body; it gets a digest only.
    """
    action = str(arguments.get("action") or "")
    receipt = {"action": action}
    if action == "read_clipboard":
        return receipt
    try:
        parsed = json.loads(body)
    except Exception:
        return receipt
    if not isinstance(parsed, dict):
        return receipt
    receipt["tool_ok"] = parsed.get("ok")
    if parsed.get("error"):
        receipt["tool_error"] = _redact(str(parsed["error"]))[:300]
    res = parsed.get("result")
    if isinstance(res, dict):
        for key in ("performed", "via", "present", "count", "satisfied",
                    "attached", "match", "matched_title", "output_file",
                    "sha256", "chars", "bytes", "element", "role", "nth"):
            if key in res:
                receipt[key] = res[key]
    return receipt


def _tool_receipt(name, arguments, result, *, ok):
    """A receipt proving what happened, carrying no more content than it must.

    `arguments` is required, not optional: policy is per-tool and, for
    drive_chat, per-action. Without it the builder cannot tell a click verdict
    from clipboard content.
    """
    if isinstance(result, BaseException):
        body = f"{type(result).__name__}: {result}"
    elif isinstance(result, str):
        body = result
    else:
        body = str(result)

    detail = {"name": name, "ok": ok}
    detail.update(_digest(body))

    if not ok:
        # Exceptions carry the same disclosure risk as results: a failing
        # run_command puts the command's output, and any credential in it,
        # into the exception message.
        detail["error"] = _redact(body)[:300]
        return detail

    if name == "drive_chat" and isinstance(arguments, dict):
        detail.update(_drive_chat_receipt(arguments, body))
    elif name not in _CONTENT_RETURNING:
        detail["result_preview"] = _redact(body)[:200]
    return detail


# The audit holds command arguments, error text and outcome verdicts. It was
# created by a bare open() and inherited the process umask -- observed 0664,
# world-readable. It is opened 0600 and an existing file is tightened on first
# write, so a log that already leaked its mode does not stay that way.
_AUDIT_MAX_BYTES = int(os.environ.get("TAEY_AUDIT_MAX_BYTES", str(64 * 1024 * 1024)))
_AUDIT_KEEP = int(os.environ.get("TAEY_AUDIT_KEEP", "5"))


def _audit_rotate_if_needed(path: str) -> None:
    """Bound the audit on disk. Without this it grows without limit -- it is
    already 5MB from a few days of driving -- and an unbounded log is one that
    eventually gets deleted wholesale, losing the history it existed to keep."""
    try:
        if os.path.getsize(path) < _AUDIT_MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = f"{path}.{_AUDIT_KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(_AUDIT_KEEP - 1, 0, -1):
            src, dst = f"{path}.{n}", f"{path}.{n + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
    except OSError as exc:
        log.error("tool audit rotate failed path=%s: %s", path, exc)


def _audit_open(path: str):
    """Append with 0600 from creation, never via umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, "a", encoding="utf-8")


def _audit(tool: str, detail: dict) -> None:
    try:
        import json as _j, time as _t
        context = dict(_request_context.get())
        _audit_rotate_if_needed(TOOL_AUDIT_PATH)
        with _audit_open(TOOL_AUDIT_PATH) as f:
            f.write(_j.dumps({"ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                              **context, "tool": tool, **detail}) + "\n")
    except Exception as exc:
        log.error("tool audit write failed path=%s: %s", TOOL_AUDIT_PATH, exc)


def _do_write_file(path: str, content: str, append: bool = False) -> str:
    import os
    if not isinstance(path, str) or not path.strip():
        return "write_file error: path must be a non-empty string"
    if not path.startswith("/"):
        return f"write_file error: path must be absolute (got {path[:80]!r})"
    if not isinstance(content, str):
        return "write_file error: content must be a string"
    resolved = os.path.realpath(path)
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(resolved)
    except Exception as e:
        _audit("write_file", {"path": resolved, "ok": False, "error": str(e)[:200]})
        return f"write_file error: {type(e).__name__}: {e}"
    _audit("write_file", {"path": resolved, "ok": True, "bytes": len(content), "append": append})
    return f"write_file ok: {'appended to' if append else 'wrote'} {resolved} ({size} bytes on disk)"


def _do_run_command(command: str, cwd: str = "", timeout_seconds: int = 120) -> str:
    import subprocess, os
    if not isinstance(command, str) or not command.strip():
        return "run_command error: command must be a non-empty string"
    timeout_seconds = max(1, min(int(timeout_seconds or 120), 900))
    workdir = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
    try:
        r = subprocess.run(command, shell=True, cwd=workdir, capture_output=True,
                           text=True, timeout=timeout_seconds)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        _audit("run_command", {"command": command[:400], "cwd": workdir, "rc": r.returncode})
        if len(out) > _MAX_TOOL_RESULT_CHARS:
            # never hand back a silently-shortened command output
            return (f"run_command error: output is {len(out)} chars, over the "
                    f"{_MAX_TOOL_RESULT_CHARS} ceiling. NOT returning it truncated — "
                    f"re-run writing to a file and read it in explicit slices, or "
                    f"filter the command so it emits only what you need.")
        return f"exit={r.returncode}\n{out}" if out.strip() else f"exit={r.returncode} (no output)"
    except subprocess.TimeoutExpired:
        _audit("run_command", {"command": command[:400], "cwd": workdir, "rc": "timeout"})
        return f"run_command error: timed out after {timeout_seconds}s"
    except Exception as e:
        _audit("run_command", {"command": command[:400], "cwd": workdir, "error": str(e)[:200]})
        return f"run_command error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# drive_chat — Taey's own hands on the Family-chat displays. ONE action per call:
# observe the tree, take exactly one action, observe again. It shells out to the
# sibling serving/ui_drive.py under the AT-SPI interpreter (the displays live on this
# workstation, where the proxy runs its tools), so the same proven primitives that drive
# :2-:6 and the second set :21-:24 are what Taey uses. The step-by-step discipline
# lives in the model and the prompt; this surface performs one primitive and returns the
# observed JSON. :0 (Jesse's monitor) and any non-chat display are REFUSED here, never
# merely absent from the schema.
# ---------------------------------------------------------------------------
UI_DRIVE_PYTHON = os.environ.get("TAEY_UI_DRIVE_PYTHON", "/home/mira/taeys-env-sys/bin/python")
UI_DRIVE_SCRIPT = os.environ.get(
    "TAEY_UI_DRIVE_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_drive.py"),
)
_DEFAULT_CHAT_DISPLAYS = (":2", ":3", ":4", ":5", ":6", ":21", ":22", ":23", ":24")
_env_chat_disp = os.environ.get("TAEY_CHAT_DISPLAYS", "").strip()
# :0 is Jesse's physical monitor and can never be a target, even via env override.
CHAT_DISPLAYS = tuple(
    d for d in (_env_chat_disp.split(",") if _env_chat_disp else _DEFAULT_CHAT_DISPLAYS)
    if d and d.strip() != ":0"
)
# Inline paste ceiling. Above this, content must come from a file on disk (see the
# refusal in _do_drive_chat). Sized for a genuine one-liner or short question; a
# consult packet, a review request, or anything with claims in it is far larger and
# belongs in a file where it can be read back and proven.
_PASTE_INLINE_MAX_CHARS = int(os.environ.get("TAEY_PASTE_INLINE_MAX_CHARS", "800"))

_DRIVE_ACTIONS = {
    "observe", "click", "focus", "activate", "hover", "operate", "navigate", "type", "paste", "key",
    "read_clipboard", "focus_dialog",
}
_DRIVE_MUTATIONS = {
    "click", "focus", "activate", "hover", "operate", "navigate", "type", "paste", "key", "focus_dialog",
}
_DRIVE_ACTION_ARGUMENTS = {
    "observe": frozenset({"display", "action", "scope"}),
    "click": frozenset({"display", "action", "ref"}),
    "focus": frozenset({"display", "action", "ref"}),
    "activate": frozenset({"display", "action", "ref"}),
    "hover": frozenset({"display", "action", "ref"}),
    "operate": frozenset({"display", "action", "ref"}),
    "navigate": frozenset({"display", "action", "url"}),
    "type": frozenset({"display", "action", "text"}),
    "paste": frozenset({"display", "action", "text", "text_file"}),
    "key": frozenset({"display", "action", "key"}),
    "read_clipboard": frozenset({"display", "action", "output_file"}),
    "focus_dialog": frozenset({"display", "action"}),
}


# ---------------------------------------------------------------------------
# CONSULT MONITOR REGISTRATION (infra half of the contract with taeys-hands'
# consult_monitor.py reader, which SCANs taey:*:active_session_ids).
#
# Why this exists: a consult outlives the turn that started it. Deep modes run
# 10+ minutes and a worker's turn can end mid-flight, leaving a lane nobody
# knows about. The reader can only see what we register. It is PASSIVE — it
# notifies and records, it NEVER recovers or drives (that would be the banned
# autonomous class).
#
# Contract: register at consult START, write last_seen on EVERY drive action
# (age-since-start would false-flag a legitimately long consult; time-since-
# last-action is the only non-false-flagging stall signal), deregister on
# deliver. TTL backstop so a crashed driver cannot leak a record forever.
# ---------------------------------------------------------------------------
_MONITOR_TTL_SECS = int(os.environ.get("TAEY_CONSULT_MONITOR_TTL", "10800"))
def _monitor_node() -> str:
    return os.environ.get("TAEY_SESSION_NAME") or os.environ.get("SEAT_ID") or "taey"


def _monitor_touch(display: str, platform: str, action: str) -> None:
    """Register-on-first-action, then write last_seen on every action.

    Registration is AUTOMATIC rather than a step the driver must remember: a
    monitor you can forget to start is a monitor that is not there the one time
    it mattered. The record self-expires (TTL) so a crashed driver cannot leak
    it, and the reader's stall signal is time-since-last_seen. Never raises: a
    monitoring write must not break the action it is observing."""
    try:
        client = _mira_redis or _redis
        if client is None:
            return
        node = _monitor_node()
        setkey = f"taey:{node}:active_session_ids"
        now = time.time()
        found = False
        for session_key in list(client.smembers(setkey) or []):
            raw = client.get(session_key)
            if not raw:
                client.srem(setkey, session_key)
                continue
            rec = json.loads(raw)
            if str(rec.get("display") or "") != display:
                continue
            rec["last_seen"] = now
            rec["last_action"] = action
            rec["platform"] = platform
            client.set(session_key, json.dumps(rec), ex=_MONITOR_TTL_SECS)
            found = True
        if not found:
            monitor_id = f"{node}-{display.lstrip(':')}-{int(now)}"
            session_key = f"taey:{node}:active_session:{monitor_id}"
            client.set(session_key, json.dumps({
                "monitor_id": monitor_id, "display": display,
                "platform": platform,
                "requester": node, "mode": "supervised_step",
                "timeout": _MONITOR_TTL_SECS,
                "started_ts": now, "last_seen": now, "last_action": action,
            }), ex=_MONITOR_TTL_SECS)
            client.sadd(setkey, session_key)
            log.info("consult monitor registered %s display=%s", monitor_id, display)
    except Exception as exc:  # observation must never break execution
        log.debug("monitor touch skipped: %s", exc)


def _do_drive_chat(arguments: dict) -> str:
    import subprocess, json as _json

    def _err(display, action, msg):
        return _json.dumps({"ok": False, "action": action, "display": display,
                            "result": None, "error": msg})

    display = str(arguments.get("display", "")).strip()
    action = str(arguments.get("action", "")).strip()
    scope = arguments.get("scope")

    context = dict(_request_context.get())
    seat_id = str(context.get("seat_id") or "")
    process_generation = str(context.get("process_generation") or "")
    turn_id = str(context.get("turn_id") or "")
    if (
        not _SEAT_ID_RE.fullmatch(seat_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
        or not _TRACE_ID_RE.fullmatch(turn_id)
    ):
        return _err(
            display,
            action,
            "drive_chat requires a validated active Taey turn context; refusing",
        )
    sequence = context.get("_ui_sequence")
    if not isinstance(sequence, dict):
        return _err(
            display,
            action,
            "drive_chat requires request-local observe/action state; refusing",
        )
    observations = sequence.get("observations")
    if not isinstance(observations, dict):
        return _err(display, action, "invalid request-local observe/action state; refusing")
    expected_surfaces = sequence.setdefault("expected_surfaces", {})
    if not isinstance(expected_surfaces, dict):
        return _err(display, action, "invalid request-local UI surface state; refusing")
    tool_round = context.get("tool_round")
    if not isinstance(tool_round, int) or tool_round < 1:
        return _err(display, action, "drive_chat requires a positive tool round; refusing")

    def _terminal_refusal(msg: str) -> str:
        terminal = sequence.get("terminal")
        if not isinstance(terminal, dict):
            terminal = {
                "display": display,
                "action": action,
                "tool_round": tool_round,
                "reason": msg,
            }
            sequence["terminal"] = terminal
            observations.clear()
        profile_state = context.get("_tool_profile_state")
        if isinstance(profile_state, dict) and not isinstance(
            profile_state.get("terminal"), dict
        ):
            profile_state["terminal"] = {
                "tool": "drive_chat",
                "reason": terminal["reason"],
            }
        payload = {
            "ok": False,
            "action": action,
            "display": display,
            "result": None,
            "error": msg,
            "ui_sequence": {
                "state": "terminal_refusal",
                "first_failure": terminal,
                "instruction": "Stop this attempt; report the first failure and do not retry UI mutations in this turn.",
            },
        }
        return _json.dumps(payload)

    def _argument_refusal(msg: str) -> str:
        return _terminal_refusal(msg)

    if isinstance(sequence.get("terminal"), dict):
        return _terminal_refusal(
            "a prior drive_chat failure ended this turn; all later UI calls are refused"
        )

    if display not in CHAT_DISPLAYS:
        return _terminal_refusal(
            f"display not permitted; drive_chat is scoped to the Chat displays "
            f"{list(CHAT_DISPLAYS)} (:0 and non-chat displays are refused)"
        )
    if action not in _DRIVE_ACTIONS:
        return _terminal_refusal(
            f"unknown action {action!r}; valid: {sorted(_DRIVE_ACTIONS)}"
        )
    if action == "observe":
        scope = str(scope or "base")
        if scope not in {"base", "menu_snapshot", "app_root_snapshot"}:
            return _terminal_refusal(
                f"unsupported observation scope {scope!r}"
            )
    elif scope is not None:
        return _terminal_refusal("scope is valid only for observe")

    unexpected_arguments = sorted(
        set(arguments) - _DRIVE_ACTION_ARGUMENTS[action]
    )
    if unexpected_arguments:
        return _argument_refusal(
            f"{action} received unsupported argument(s) {unexpected_arguments}; "
            f"accepted arguments are {sorted(_DRIVE_ACTION_ARGUMENTS[action])}"
        )

    expected_revision = ""
    native_dialog_revision = ""
    if action in _DRIVE_MUTATIONS:
        terminal = sequence.get("terminal")
        if isinstance(terminal, dict):
            return _terminal_refusal(
                "a prior UI mutation failed in this turn; further UI mutations are refused"
            )
        expected_surface = str(expected_surfaces.get(display) or "browser")
        if expected_surface == "native_dialog" and action not in {"key", "type"}:
            return _terminal_refusal(
                "native-dialog state requires a fresh canonical native observe followed "
                "by exactly one key or type primitive"
            )
        if expected_surface not in {"browser", "native_dialog"}:
            return _terminal_refusal("invalid expected UI surface; refusing mutation")
        if action == "navigate":
            observations.pop(display, None)
        else:
            observed = observations.pop(display, None)
            if not isinstance(observed, dict):
                return _terminal_refusal(
                    "UI mutation requires an explicit fresh observe on this display"
                )
            observed_round = observed.get("tool_round")
            if not isinstance(observed_round, int) or observed_round >= tool_round:
                return _terminal_refusal(
                    "UI mutation requires an observe result seen in an earlier model round"
                )
            observed_surface = str(observed.get("surface") or "")
            if observed_surface != expected_surface:
                return _terminal_refusal(
                    f"expected {expected_surface!r} observation before mutation but "
                    f"received {observed_surface!r}"
                )
            if observed_surface == "native_dialog":
                if action not in {"key", "type"}:
                    return _terminal_refusal(
                        "native-dialog verification permits only one key or type primitive"
                    )
                native_dialog_revision = str(
                    observed.get("snapshot_revision") or ""
                )
                if not re.fullmatch(r"[0-9a-f]{64}", native_dialog_revision):
                    return _terminal_refusal(
                        "preceding observe did not provide a valid native-dialog revision"
                    )
            else:
                expected_revision = str(observed.get("snapshot_revision") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
                    return _terminal_refusal(
                        "preceding observe did not provide a valid browser snapshot revision"
                    )

    lease_owner = f"taey-drive:{seat_id}:{process_generation}"
    drive_env = dict(os.environ)
    drive_env.update({
        "TAEY_DRIVE_LEASE_OWNER": lease_owner,
        "TAEY_DRIVE_LEASE_SEAT": seat_id,
        "TAEY_DRIVE_LEASE_TURN": turn_id,
        "TAEY_DRIVE_LEASE_GENERATION": process_generation,
        "TAEY_DRIVE_GENERATION_FENCE_KEY": _DRIVE_GENERATION_FENCE_KEY,
    })

    output_file = arguments.get("output_file")
    if output_file is not None:
        if action != "read_clipboard":
            return _argument_refusal("output_file is valid only for read_clipboard")
        if not isinstance(output_file, str) or not output_file:
            return _argument_refusal("output_file must be a non-empty string")

    sub = {"read_clipboard": "read-clipboard",
           "focus_dialog": "focus-dialog"}.get(action, action)
    cmd = [UI_DRIVE_PYTHON, UI_DRIVE_SCRIPT, sub, "--display", display]
    if action == "observe":
        expected_surface = str(expected_surfaces.get(display) or "browser")
        if expected_surface not in {"browser", "native_dialog"}:
            return _err(display, action, "invalid expected UI surface; refusing")
        cmd += ["--surface", expected_surface, "--scope", scope]
    if output_file is not None:
        cmd += ["--output-file", output_file]
    if action in ("click", "focus", "activate", "hover", "operate"):
        ref = arguments.get("ref")
        if ref:
            cmd += ["--ref", str(ref)]
        else:
            return _argument_refusal(
                f"{action} requires ref=<from the immediately preceding fresh observe>"
            )
    elif action in ("type", "paste"):
        # Prefer text_file: the model passes a PATH and ui_drive pastes the exact
        # file bytes. A large packet as inline `text` forces the model to regenerate
        # every character (a 13K packet = ~20 min on a Jetson + drift risk). A path
        # is a few tokens and byte-exact. `text` still works for short input.
        text_file = arguments.get("text_file")
        text = arguments.get("text")
        if action == "paste" and text_file:
            cmd += ["--text-file", str(text_file)]
        elif text is not None and text != "":
            # SUBSTANTIAL CONTENT MUST COME FROM A FILE, NEVER FROM GENERATION.
            # 2026-08-13: a worker seat, dispatched a consult leg, composed a
            # 2,181-char "AUDIT PACKET: Taey-Ed V8" describing a repo that does not
            # exist (fabricated file counts, commit SHA, and "53 open PRs") and SENT
            # it into Jesse's live ChatGPT account. Nothing on disk contained that
            # text — the model invented it wholesale at the one boundary that reaches
            # the outside world.
            # Root-cause shape (not a guard bolted on): anything substantial leaving
            # this machine must be a reviewable artifact that exists on disk, so it
            # can be diffed, cited, and proven. A path can be verified; a generation
            # cannot. Short inline text stays allowed for genuine one-liners.
            if action == "paste" and len(str(text)) > _PASTE_INLINE_MAX_CHARS:
                return _argument_refusal(
                    f"paste refused: {len(str(text))} chars of inline text exceeds "
                    f"the {_PASTE_INLINE_MAX_CHARS}-char inline limit. Content this "
                    f"long must be a FILE: write it with write_file (or use the "
                    f"existing file), then paste with text_file='<absolute path>'. "
                    f"The tool pastes the exact bytes — so what is sent is verifiable "
                    f"on disk rather than composed in the moment."
                )
            cmd += ["--text", str(text)]
        else:
            return _argument_refusal(
                f"{action} requires non-empty 'text'"
                + (" or 'text_file'" if action == "paste" else "")
            )
        if action == "type" and native_dialog_revision:
            cmd += ["--native-dialog-revision", native_dialog_revision]
        else:
            cmd += ["--expected-revision", expected_revision]
    elif action == "key":
        key = arguments.get("key")
        if not key:
            return _argument_refusal("key requires a key name, e.g. Return")
        cmd += ["--key", str(key)]
        if native_dialog_revision:
            cmd += ["--native-dialog-revision", native_dialog_revision]
        else:
            cmd += ["--expected-revision", expected_revision]
    elif action == "navigate":
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            return _argument_refusal("navigate requires the exact YAML urls.fresh value")
        cmd += ["--url", url]
    elif action == "focus_dialog":
        cmd += ["--expected-revision", expected_revision]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            env=drive_env,
        )
        _audit("drive_chat", {"display": display, "action": action, "rc": r.returncode})
        out = (r.stdout or "").strip()
        if out:
            try:
                payload = _json.loads(out)
            except Exception:
                payload = None
            succeeded = (
                r.returncode == 0
                and isinstance(payload, dict)
                and payload.get("ok") is True
                and isinstance(payload.get("platform"), str)
                and payload["platform"]
            )
            if succeeded:
                _monitor_touch(display, payload["platform"], action)
                if action == "observe":
                    result = payload.get("result") or {}
                    revision = str(result.get("snapshot_revision") or "")
                    if not re.fullmatch(r"[0-9a-f]{64}", revision):
                        return _terminal_refusal(
                            "explicit observe returned no valid snapshot revision",
                        )
                    terminal = sequence.get("terminal")
                    if isinstance(terminal, dict):
                        payload["ui_sequence"] = {
                            "state": "terminal_refusal",
                            "first_failure": terminal,
                            "mutation_token_issued": False,
                        }
                    else:
                        observed_surface = str(result.get("surface") or "")
                        expected_surface = str(
                            expected_surfaces.get(display) or "browser"
                        )
                        if observed_surface != expected_surface:
                            return _terminal_refusal(
                                f"expected {expected_surface!r} observation but received "
                                f"{observed_surface!r}",
                            )
                        observations[display] = {
                            "surface": observed_surface,
                            "snapshot_revision": revision,
                            "snapshot_scope": str(result.get("scope") or ""),
                            "tool_round": tool_round,
                        }
                        payload["ui_sequence"] = {
                            "state": "observed",
                            "surface": observed_surface,
                            "snapshot_revision": revision,
                            "snapshot_scope": str(result.get("scope") or ""),
                            "tool_round": tool_round,
                            "mutation_token_issued": True,
                        }
                elif action == "focus_dialog":
                    expected_surfaces[display] = "native_dialog"
                    payload["ui_sequence"] = {
                        "state": "mutation_complete",
                        "consumed_snapshot_revision": expected_revision,
                        "observe_required_before_next_mutation": True,
                        "expected_next_surface": "native_dialog",
                        "mutation_token_issued": False,
                    }
                elif action in _DRIVE_MUTATIONS:
                    if native_dialog_revision:
                        expected_surfaces[display] = (
                            "browser"
                            if action == "key" and str(arguments.get("key")) == "Return"
                            else "native_dialog"
                        )
                    payload["ui_sequence"] = {
                        "state": "mutation_complete",
                        "observe_required_before_next_mutation": True,
                    }
                    if expected_revision:
                        payload["ui_sequence"]["consumed_snapshot_revision"] = expected_revision
                    if native_dialog_revision:
                        payload["ui_sequence"]["consumed_snapshot_scope"] = "native_dialog"
                        payload["ui_sequence"]["consumed_snapshot_revision"] = native_dialog_revision
                        payload["ui_sequence"]["expected_next_surface"] = expected_surfaces[display]
                return _json.dumps(payload)
            detail = (
                str(payload.get("error") or "")
                if isinstance(payload, dict)
                else f"ui_drive exit={r.returncode}; stderr={(r.stderr or '')[:300]}"
            )
            return _terminal_refusal(detail or "drive_chat failed")
        msg = f"ui_drive exit={r.returncode}, no output; stderr={(r.stderr or '')[:300]}"
        return _terminal_refusal(msg)
    except subprocess.TimeoutExpired:
        _audit("drive_chat", {"display": display, "action": action, "rc": "timeout"})
        return _terminal_refusal("drive_chat timed out after 90s")
    except Exception as e:
        _audit("drive_chat", {"display": display, "action": action, "error": str(e)[:200]})
        msg = f"{type(e).__name__}: {e}"
        return _terminal_refusal(msg)


def _do_list_dir(path: str, pattern: str = "") -> str:
    import os
    import fnmatch
    import json as _json
    if not isinstance(path, str) or not path.strip():
        return "list_dir error: path must be a non-empty string"
    if not path.startswith("/"):
        return f"list_dir error: path must be absolute (got {path[:80]!r})"
    try:
        resolved = os.path.realpath(path)
    except Exception as e:
        return f"list_dir error: path resolve failed: {e}"
    if not _path_is_allowed(resolved):
        return f"list_dir error: path not in allowlist. Allowed prefixes: {', '.join(READ_ALLOWED_PREFIXES)}"
    if not os.path.exists(resolved):
        return f"list_dir error: directory not found: {path}"
    if not os.path.isdir(resolved):
        return f"list_dir error: path is not a directory: {path}"
    try:
        entries = os.listdir(resolved)
    except OSError as e:
        return f"list_dir error: list failed: {e}"
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e, pattern)]
    entries.sort()
    result = []
    for e in entries:
        full = os.path.join(resolved, e)
        try:
            st = os.stat(full)
            result.append({
                "name": e,
                "size_bytes": st.st_size if not os.path.isdir(full) else None,
                "is_dir": os.path.isdir(full),
            })
        except OSError:
            result.append({"name": e, "size_bytes": None, "is_dir": None, "error": "stat_failed"})
    body = _json.dumps(result, indent=2)
    if len(body) > _MAX_TOOL_RESULT_CHARS:
        return (f"list_dir error: {path} listing is {len(body)} chars ({len(entries)} entries), "
                f"over the {_MAX_TOOL_RESULT_CHARS} ceiling. NOT returning a partial listing — "
                f"narrow it with `pattern`, or list subdirectories separately.")
    return f"[list_dir path={path} pattern={pattern!r} count={len(entries)}]\n\n{body}"


def _do_stage_corpus_candidate(arguments: dict) -> str:
    import os
    import hashlib
    import json as _json
    import time as _time
    try:
        os.makedirs(CORPUS_STAGING_DIR, exist_ok=True)
    except OSError as e:
        return f"stage_corpus_candidate error: cannot create staging dir: {e}"
    content = arguments.get("content", "")
    topic = arguments.get("topic", "")
    source_url = arguments.get("source_url", "")
    author = arguments.get("author", "unknown")
    quality_tier = arguments.get("quality_tier", "tertiary")
    rationale = arguments.get("rationale", "")
    if not isinstance(content, str) or len(content.strip()) < 50:
        return f"stage_corpus_candidate error: content too short ({len(content)} chars). Must be at least 50 chars of actual material."
    if len(content) > 500_000:
        return f"stage_corpus_candidate error: content too long ({len(content)} chars). Max 500000; split first or truncate to the most relevant portion."
    if not topic or not source_url or not rationale:
        return "stage_corpus_candidate error: topic, source_url, and rationale are all required."
    if quality_tier not in ("primary", "secondary", "tertiary"):
        return f"stage_corpus_candidate error: quality_tier must be primary|secondary|tertiary, got {quality_tier!r}"
    # Safe topic slug + timestamp-hash filename
    safe_topic = "".join(c if (c.isalnum() or c in "_-") else "_" for c in topic)[:60]
    ts = _time.strftime("%Y%m%dT%H%M%S")
    content_hash = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    out_path = os.path.join(CORPUS_STAGING_DIR, f"{safe_topic}_{ts}_{content_hash}.json")
    payload = {
        "schema_version": 1,
        "topic": topic,
        "source_url": source_url,
        "author": author,
        "quality_tier": quality_tier,
        "rationale": rationale,
        "staged_at": ts,
        "content_length": len(content),
        "content_sha1": content_hash,
        "content": content,
    }
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"stage_corpus_candidate error: write failed: {e}"
    return f"staged: {out_path} ({len(content)} chars, tier={quality_tier})"


def _do_skip_corpus_candidate(arguments: dict) -> str:
    import os
    import hashlib
    import json as _json
    import time as _time
    try:
        os.makedirs(os.path.join(CORPUS_STAGING_DIR, "skipped"), exist_ok=True)
    except OSError as e:
        return f"skip_corpus_candidate error: cannot create skipped dir: {e}"
    source_url = arguments.get("source_url", "")
    topic = arguments.get("topic", "")
    reason = arguments.get("reason", "")
    if not source_url or not topic or not reason:
        return "skip_corpus_candidate error: source_url, topic, and reason are all required."
    safe_topic = "".join(c if (c.isalnum() or c in "_-") else "_" for c in topic)[:60]
    ts = _time.strftime("%Y%m%dT%H%M%S")
    url_hash = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12]
    out_path = os.path.join(CORPUS_STAGING_DIR, "skipped", f"{safe_topic}_{ts}_{url_hash}.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            _json.dump({
                "topic": topic,
                "source_url": source_url,
                "reason": reason,
                "skipped_at": ts,
            }, f, indent=2)
    except Exception as e:
        return f"skip_corpus_candidate error: write failed: {e}"
    return f"skipped: {source_url} (reason={reason})"


def _do_fetch_url(url: str, max_chars: int | None = None, timeout: float = 30.0) -> str:
    """Fetch a URL and return cleaned text. Supports HTML, PDF, plaintext. Never raises."""
    try:
        import trafilatura
        import pypdf
        import io
    except ImportError as e:
        return f"fetch_url error: missing dependency ({e}). Install: pip install trafilatura pypdf"

    headers = {
        "User-Agent": "Mozilla/5.0 (Taey-Fetch/1.0; +https://palios-taey.local) research-corpus-ingestion",
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        return f"fetch_url error: timeout after {timeout}s for {url}"
    except httpx.RequestError as e:
        return f"fetch_url error: request failed: {e}"
    except Exception as e:
        return f"fetch_url error: {type(e).__name__}: {e}"

    if resp.status_code >= 400:
        return f"fetch_url error: HTTP {resp.status_code} for {url}"

    content_type = resp.headers.get("content-type", "").lower().split(";")[0].strip()
    raw_bytes = resp.content
    size_kb = len(raw_bytes) / 1024

    extracted = ""
    extraction_method = ""

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
            extracted = "\n\n".join(p.strip() for p in parts if p.strip())
            extraction_method = f"pdf ({len(reader.pages)} pages)"
        except Exception as e:
            return f"fetch_url error: PDF parse failed: {e}"

    elif "html" in content_type or "xhtml" in content_type or not content_type:
        html_text = resp.text
        extracted = trafilatura.extract(
            html_text,
            include_comments=False,
            include_tables=True,
            include_formatting=False,
            favor_precision=True,
            deduplicate=True,
        ) or ""
        extraction_method = "trafilatura"
        # Fallback: if trafilatura returns nothing, try text with basic tag strip
        if not extracted.strip():
            import re as _re
            stripped = _re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=_re.DOTALL | _re.IGNORECASE)
            stripped = _re.sub(r"<style[^>]*>.*?</style>", " ", stripped, flags=_re.DOTALL | _re.IGNORECASE)
            stripped = _re.sub(r"<[^>]+>", " ", stripped)
            stripped = _re.sub(r"\s+", " ", stripped).strip()
            extracted = stripped
            extraction_method = "fallback-strip"

    elif "text/" in content_type or "json" in content_type or "xml" in content_type:
        extracted = resp.text
        extraction_method = content_type

    else:
        return f"fetch_url error: unsupported content-type {content_type!r} (size {size_kb:.1f}KB). Manual handling required."

    # Paywall / login-wall heuristics
    paywall_markers = [
        "please subscribe to continue",
        "create a free account to continue",
        "to continue reading, subscribe",
        "sign in to continue reading",
        "to read the full story",
    ]
    low = extracted[:2000].lower()
    paywall_hit = any(m in low for m in paywall_markers)

    header = f"[fetch_url {resp.status_code} {content_type} {size_kb:.1f}KB method={extraction_method}"
    if paywall_hit:
        header += " WARNING=possible-paywall-only-preview"
    header += f" url={url}]\n\n"

    extracted = extracted.strip()
    if max_chars is not None:
        if len(extracted) > max_chars:
            return (header + extracted[:max_chars]
                    + f"\n\n[... this is a CALLER-REQUESTED SLICE: {max_chars} chars of "
                      f"{len(extracted)} total. You are holding a PART, not the page. ...]")
        return header + extracted
    if len(extracted) > _MAX_TOOL_RESULT_CHARS:
        return (f"fetch_url error: {url} extracted to {len(extracted)} chars, over the "
                f"{_MAX_TOOL_RESULT_CHARS} ceiling. NOT returning it truncated — "
                f"request an explicit slice with max_chars and say which part you hold.")
    return header + extracted


MAX_CONTEXT_TOKENS = 262144


def publish_metrics(elapsed_ms: float, prompt_tokens: int = 0,
                    completion_tokens: int = 0, tool_rounds: int = 0):
    """Publish generation metrics + context utilization to Redis for soma daemon."""
    if _redis is None:
        return
    total_tokens = prompt_tokens + completion_tokens
    context_util = total_tokens / MAX_CONTEXT_TOKENS if MAX_CONTEXT_TOKENS > 0 else 0

    try:
        pipe = _redis.pipeline()
        pipe.set("taey:soma:latency_ms", str(round(elapsed_ms, 1)), ex=30)
        pipe.set("taey:soma:prompt_tokens", str(prompt_tokens), ex=30)
        pipe.set("taey:soma:completion_tokens", str(completion_tokens), ex=30)
        pipe.set("taey:soma:total_tokens", str(total_tokens), ex=30)
        pipe.set("taey:soma:context_utilization", str(round(context_util, 4)), ex=30)
        pipe.set("taey:soma:tool_rounds", str(tool_rounds), ex=30)
        pipe.execute()
    except Exception as exc:
        log.error("soma metrics publish failed: %s", exc)


@app.post("/tokenize")
async def tokenize(request: Request):
    """Count tokens using vLLM's tokenizer. Exact counts."""
    body = await request.json()
    resp = await _http.post("/tokenize", json=body)
    return resp.json()


def _health_probe_error(status: str, error: str, started: float) -> dict:
    return {
        "ok": False,
        "status": status,
        "error": error,
        "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "checked_at": round(time.time(), 3),
    }


async def _probe_vllm_catalogue() -> dict:
    started = time.monotonic()
    if _http is None:
        return _health_probe_error("unreachable", "HTTP client is not initialized", started)
    try:
        resp = await asyncio.wait_for(
            _http.get("/v1/models", timeout=VLLM_HEALTH_PROBE_TIMEOUT_SECS),
            timeout=VLLM_HEALTH_PROBE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        return _health_probe_error("unreachable", "catalogue probe timed out", started)
    except httpx.TimeoutException as exc:
        return _health_probe_error("unreachable", f"{type(exc).__name__}: {exc}", started)
    except httpx.RequestError as exc:
        return _health_probe_error("unreachable", f"{type(exc).__name__}: {exc}", started)
    except Exception as exc:
        return _health_probe_error("unhealthy", f"{type(exc).__name__}: {exc}", started)

    latency_ms = round((time.monotonic() - started) * 1000, 1)
    if resp.status_code != 200:
        return {
            "ok": False,
            "status": "unhealthy",
            "code": resp.status_code,
            "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            "latency_ms": latency_ms,
            "checked_at": round(time.time(), 3),
        }
    try:
        payload = resp.json()
    except Exception as exc:
        return {
            "ok": False,
            "status": "unhealthy",
            "error": f"invalid catalogue response: {type(exc).__name__}: {exc}",
            "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            "latency_ms": latency_ms,
            "checked_at": round(time.time(), 3),
        }
    models = payload.get("data", []) if isinstance(payload, dict) else []
    first_model = models[0] if models and isinstance(models[0], dict) else {}
    return {
        "ok": True,
        "status": "healthy",
        "model": first_model.get("id", "none"),
        "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
        "latency_ms": latency_ms,
        "checked_at": round(time.time(), 3),
    }


async def _probe_vllm_generation() -> dict:
    started = time.monotonic()
    if _http is None:
        return _health_probe_error("unreachable", "HTTP client is not initialized", started)
    body = {
        "messages": [{"role": "user", "content": "health"}],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
    }
    try:
        resp = await asyncio.wait_for(
            _http.post(
                "/v1/chat/completions",
                json=body,
                timeout=VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            ),
            timeout=VLLM_HEALTH_PROBE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        return _health_probe_error("unhealthy", "generation probe timed out", started)
    except httpx.TimeoutException as exc:
        return _health_probe_error("unhealthy", f"{type(exc).__name__}: {exc}", started)
    except httpx.RequestError as exc:
        return _health_probe_error("unreachable", f"{type(exc).__name__}: {exc}", started)
    except Exception as exc:
        return _health_probe_error("unhealthy", f"{type(exc).__name__}: {exc}", started)

    latency_ms = round((time.monotonic() - started) * 1000, 1)
    if resp.status_code != 200:
        return {
            "ok": False,
            "status": "unhealthy",
            "code": resp.status_code,
            "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            "latency_ms": latency_ms,
            "checked_at": round(time.time(), 3),
        }
    try:
        payload = resp.json()
    except Exception as exc:
        return {
            "ok": False,
            "status": "unhealthy",
            "error": f"invalid generation response: {type(exc).__name__}: {exc}",
            "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            "latency_ms": latency_ms,
            "checked_at": round(time.time(), 3),
        }
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if not choices:
        return {
            "ok": False,
            "status": "unhealthy",
            "error": "generation response had no choices",
            "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
            "latency_ms": latency_ms,
            "checked_at": round(time.time(), 3),
        }
    choice = choices[0] if isinstance(choices[0], dict) else {}
    return {
        "ok": True,
        "status": "healthy",
        "finish_reason": choice.get("finish_reason"),
        "timeout_secs": VLLM_HEALTH_PROBE_TIMEOUT_SECS,
        "latency_ms": latency_ms,
        "checked_at": round(time.time(), 3),
    }


def _cached_generation_result(now: float) -> Optional[dict]:
    cached = _health_generation_cache.get("result")
    expires_at = float(_health_generation_cache.get("expires_at") or 0.0)
    if not isinstance(cached, dict) or now >= expires_at:
        return None
    result = dict(cached)
    result["cached"] = True
    result["cache_expires_in_secs"] = round(expires_at - now, 3)
    return result


async def _vllm_generation_health() -> dict:
    global _health_generation_cache, _health_generation_lock
    now = time.monotonic()
    cached = _cached_generation_result(now)
    if cached is not None:
        return cached
    if _health_generation_lock is None:
        _health_generation_lock = asyncio.Lock()
    async with _health_generation_lock:
        now = time.monotonic()
        cached = _cached_generation_result(now)
        if cached is not None:
            return cached
        result = await _probe_vllm_generation()
        _health_generation_cache = {
            "expires_at": time.monotonic() + VLLM_HEALTH_CACHE_SECS,
            "result": dict(result),
        }
        response = dict(result)
        response["cached"] = False
        response["cache_expires_in_secs"] = round(VLLM_HEALTH_CACHE_SECS, 3)
        return response


async def _vllm_health() -> dict:
    catalogue, generation = await asyncio.gather(
        _probe_vllm_catalogue(),
        _vllm_generation_health(),
    )
    catalogue_ok = bool(catalogue.get("ok"))
    generation_ok = bool(generation.get("ok"))
    if generation_ok and catalogue_ok:
        status = "healthy"
    elif generation_ok:
        status = "degraded"
    else:
        status = str(generation.get("status") or "unhealthy")
    return {
        "status": status,
        "model": catalogue.get("model", "none"),
        "catalogue_ok": catalogue_ok,
        "generation_ok": generation_ok,
        "catalogue": catalogue,
        "generation": generation,
    }


@app.get("/health")
async def health():
    vllm_health = await _vllm_health()

    vprop_raw = None
    redis_error = ""
    liveness = {
        "status": "unavailable",
        "required": TAEY_LIVENESS_REQUIRED,
        "default_seat": TAEY_DEFAULT_SEAT,
        "scope": "all_registered_seats",
        "last_error": _last_liveness_error or None,
        "last_error_at": _last_liveness_error_at or None,
        "last_success_at": _last_liveness_success_at or None,
    }
    if _redis:
        try:
            vprop_raw = _redis.get("taey:soma:vprop")
            active_count, recovered_count, registered_seats = (
                _reconcile_registered_liveness()
            )
            liveness.update({
                "status": (
                    "degraded"
                    if _last_liveness_error_at > _last_liveness_success_at
                    else "healthy"
                ),
                "active_turns": active_count,
                "abandoned_recovered": recovered_count,
                "registered_seats": registered_seats,
                "last_success_at": _last_liveness_success_at or None,
            })
        except Exception as exc:
            redis_error = f"{type(exc).__name__}: {exc}"
            liveness["error"] = redis_error

    overall = "healthy"
    if vllm_health.get("status") in {"unhealthy", "unreachable"}:
        overall = "unhealthy"
    elif TAEY_LIVENESS_REQUIRED and liveness.get("status") == "unavailable":
        overall = "unhealthy"
    elif vllm_health.get("status") == "degraded":
        overall = "degraded"
    elif liveness.get("status") in {"degraded", "unavailable"}:
        overall = "degraded"

    headers = {"X-Health-Status": "degraded"} if overall == "degraded" else {}
    return JSONResponse(
        status_code=200 if overall in {"healthy", "degraded"} else 503,
        headers=headers,
        content={
            "status": overall,
            "vllm": vllm_health,
            "soma_connected": vprop_raw is not None,
            "redis_error": redis_error or None,
            "liveness": liveness,
        },
    )


@app.get("/v1/models")
async def list_models():
    resp = await _http.get("/v1/models")
    return resp.json()


# ---------------------------------------------------------------------------
# TAEY LIVENESS
#
# A boolean cannot represent concurrent requests, and a decrement-only counter
# cannot make duplicate stream cleanup idempotent. Active turn IDs are the
# domain primitive. Leases make a dead process recoverable; the legacy idle /
# turns_open keys are projections for existing fleet-notify consumers.
# ---------------------------------------------------------------------------
_SEAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_COUNCIL_SEAT_RE = re.compile(r"^taey-council-[1-7]$")
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")

_RECONCILE_LIVENESS_LUA = """
local recovered = 0
local active = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, turn_id in ipairs(active) do
    local context = redis.call('HGET', KEYS[3], turn_id)
    local deadline = tonumber(redis.call('ZSCORE', KEYS[1], turn_id) or '0')
    local reason = nil
    if deadline <= tonumber(ARGV[1]) then
        reason = 'lease_expired'
    elseif ARGV[2] ~= '' then
        local ok, decoded = pcall(cjson.decode, context or '')
        if not ok or type(decoded) ~= 'table' then
            reason = 'invalid_context'
        elseif tostring(decoded['process_generation'] or '') ~= ARGV[2] then
            reason = 'process_restarted'
        end
    end
    if reason then
        redis.call('LPUSH', KEYS[4], cjson.encode({
            turn_id=turn_id,
            context=context,
            abandoned_at=tonumber(ARGV[1]),
            reason=reason
        }))
        redis.call('ZREM', KEYS[1], turn_id)
        redis.call('ZREM', KEYS[2], turn_id)
        redis.call('HDEL', KEYS[3], turn_id)
        redis.call('ZREM', KEYS[9], turn_id)
        recovered = recovered + 1
    end
end
redis.call('LTRIM', KEYS[4], 0, 999)
redis.call('ZREMRANGEBYSCORE', KEYS[9], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
local global_count = redis.call('ZCARD', KEYS[9])
redis.call('SET', KEYS[5], count)
redis.call('SET', KEYS[10], global_count > 0 and '1' or '0')
if count == 0 then
    redis.call('SET', KEYS[6], '1')
    redis.call('DEL', KEYS[7])
else
    redis.call('DEL', KEYS[6])
    local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    if #first > 0 then
        redis.call('SET', KEYS[7], math.floor(tonumber(first[2])))
    end
end
return {count, recovered, global_count}
"""

_START_TURN_LUA = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, expired_id in ipairs(expired) do
    local expired_context = redis.call('HGET', KEYS[3], expired_id)
    redis.call('LPUSH', KEYS[4], cjson.encode({
        turn_id=expired_id,
        context=expired_context,
        abandoned_at=tonumber(ARGV[1]),
        reason='lease_expired'
    }))
    redis.call('ZREM', KEYS[1], expired_id)
    redis.call('ZREM', KEYS[2], expired_id)
    redis.call('HDEL', KEYS[3], expired_id)
    redis.call('ZREM', KEYS[9], expired_id)
end
redis.call('LTRIM', KEYS[4], 0, 999)
redis.call('ZREMRANGEBYSCORE', KEYS[9], '-inf', ARGV[1])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[1], ARGV[3])
redis.call('HSET', KEYS[3], ARGV[3], ARGV[4])
redis.call('ZADD', KEYS[9], ARGV[2], ARGV[3])
redis.call('SADD', KEYS[11], ARGV[5])
local count = redis.call('ZCARD', KEYS[1])
local global_count = redis.call('ZCARD', KEYS[9])
redis.call('SET', KEYS[5], count)
redis.call('SET', KEYS[10], global_count > 0 and '1' or '0')
redis.call('DEL', KEYS[6])
local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
if #first > 0 then
    redis.call('SET', KEYS[7], math.floor(tonumber(first[2])))
end
return {count, #expired, global_count}
"""

_END_TURN_LUA = """
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[9], ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
local global_count = redis.call('ZCARD', KEYS[9])
redis.call('SET', KEYS[5], count)
redis.call('SET', KEYS[8], ARGV[2])
redis.call('SET', KEYS[10], global_count > 0 and '1' or '0')
if count == 0 then
    redis.call('SET', KEYS[6], '1')
    redis.call('DEL', KEYS[7])
else
    redis.call('DEL', KEYS[6])
    local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    if #first > 0 then
        redis.call('SET', KEYS[7], math.floor(tonumber(first[2])))
    end
end
return {removed, count, global_count}
"""

_RENEW_TURN_LUA = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], 'XX', ARGV[2], ARGV[1])
    redis.call('ZADD', KEYS[2], 'XX', ARGV[2], ARGV[1])
    return 1
end
return 0
"""


class LivenessUnavailable(RuntimeError):
    pass


def _normalize_seat_id(value: str) -> str:
    seat_id = str(value or "").strip()
    if not _SEAT_ID_RE.fullmatch(seat_id):
        raise HTTPException(
            status_code=400,
            detail="X-Taey-Seat-Id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        )
    return seat_id


def _normalize_trace_id(value: str, field_name: str) -> str:
    trace_id = str(value or "").strip()
    if not _TRACE_ID_RE.fullmatch(trace_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} must match "
                "[A-Za-z0-9][A-Za-z0-9._:-]{0,159}"
            ),
        )
    return trace_id


def _turn_context(request: Request, body: dict) -> TurnContext:
    metadata = body.pop("_taey", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="_taey metadata must be an object")
    seat_id = _normalize_seat_id(
        request.headers.get("x-taey-seat-id")
        or metadata.get("seat_id")
        or TAEY_DEFAULT_SEAT
    )
    event_id = _normalize_trace_id(
        request.headers.get("x-taey-event-id")
        or metadata.get("event_id")
        or uuid.uuid4().hex,
        "X-Taey-Event-Id",
    )
    correlation_id = _normalize_trace_id(
        request.headers.get("x-taey-correlation-id")
        or metadata.get("correlation_id")
        or event_id,
        "X-Taey-Correlation-Id",
    )
    raw_tool_profile = request.headers.get("x-taey-tool-profile")
    tool_profile = (
        _FULL_TOOL_PROFILE
        if raw_tool_profile is None
        else raw_tool_profile.strip()
    )
    if tool_profile not in _TOOL_PROFILE_ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=(
                "X-Taey-Tool-Profile must be one of "
                f"{sorted(_TOOL_PROFILE_ALLOWED)}"
            ),
        )
    return TurnContext(
        turn_id=uuid.uuid4().hex,
        seat_id=seat_id,
        event_id=event_id,
        correlation_id=correlation_id,
        tool_profile=tool_profile,
        process_generation=PROCESS_GENERATION,
        started_at=time.time(),
    )


def _turn_payload(turn: TurnContext) -> dict:
    return {
        "turn_id": turn.turn_id,
        "seat_id": turn.seat_id,
        "event_id": turn.event_id,
        "correlation_id": turn.correlation_id,
        "tool_profile": turn.tool_profile,
        "process_generation": turn.process_generation,
        "started_at": turn.started_at,
    }


def _liveness_keys(seat_id: str) -> list[str]:
    prefix = f"taey:{seat_id}"
    return [
        f"{prefix}:active_turns",
        f"{prefix}:turn_starts",
        f"{prefix}:turn_context",
        f"{prefix}:abandoned_turns",
        f"{prefix}:turns_open",
        f"{prefix}:idle",
        f"{prefix}:turn_started",
        f"{prefix}:last_activity",
        "taey:soma:active_turns",
        "taey:soma:gpu_busy",
        "taey:soma:seat_ids",
    ]


def _set_liveness_error(message: str) -> None:
    global _last_liveness_error, _last_liveness_error_at
    _last_liveness_error = message
    _last_liveness_error_at = time.time()
    log.error("liveness: %s", message)


def _mark_liveness_success() -> None:
    global _last_liveness_success_at
    _last_liveness_success_at = time.time()


def _reconcile_liveness(
    seat_id: str,
    *,
    current_process_generation: str = "",
) -> tuple[int, int, int]:
    if _redis is None:
        raise LivenessUnavailable("Redis client is unavailable")
    now = time.time()
    result = _redis.eval(
        _RECONCILE_LIVENESS_LUA,
        len(_liveness_keys(seat_id)),
        *_liveness_keys(seat_id),
        now,
        current_process_generation,
    )
    count, recovered, global_count = (
        int(result[0]),
        int(result[1]),
        int(result[2]),
    )
    if recovered:
        log.error(
            "liveness: recovered %d abandoned turns for seat=%s",
            recovered,
            seat_id,
        )
    _mark_liveness_success()
    return count, recovered, global_count


def _registered_seat_ids() -> list[str]:
    if _redis is None:
        raise LivenessUnavailable("Redis client is unavailable")
    seat_ids = {
        TAEY_DEFAULT_SEAT,
        *_redis.smembers("taey:soma:seat_ids"),
    }
    invalid = sorted(
        seat_id for seat_id in seat_ids if not _SEAT_ID_RE.fullmatch(seat_id)
    )
    if invalid:
        raise LivenessUnavailable(
            f"invalid seat ids in Redis registry: {invalid}"
        )
    return sorted(seat_ids)


def _reconcile_registered_liveness(
    *,
    current_process_generation: str = "",
) -> tuple[int, int, list[str]]:
    registered_seats = _registered_seat_ids()
    recovered_count = 0
    global_count = 0
    for seat_id in registered_seats:
        _, recovered, global_count = _reconcile_liveness(
            seat_id,
            current_process_generation=current_process_generation,
        )
        recovered_count += recovered
    return global_count, recovered_count, registered_seats


def _start_turn(turn: TurnContext) -> int:
    if _redis is None:
        message = f"Redis unavailable; cannot register turn {turn.turn_id}"
        _set_liveness_error(message)
        if TAEY_LIVENESS_REQUIRED:
            raise LivenessUnavailable(message)
        return -1
    try:
        deadline = time.time() + TAEY_TURN_LEASE_SECS
        result = _redis.eval(
            _START_TURN_LUA,
            len(_liveness_keys(turn.seat_id)),
            *_liveness_keys(turn.seat_id),
            turn.started_at,
            deadline,
            turn.turn_id,
            json.dumps(_turn_payload(turn), separators=(",", ":")),
            turn.seat_id,
        )
        count, expired, global_count = (
            int(result[0]),
            int(result[1]),
            int(result[2]),
        )
        if expired:
            log.error(
                "liveness: start recovered %d expired turns seat=%s",
                expired,
                turn.seat_id,
            )
        _active_turns[turn.turn_id] = turn
        _turn_heartbeat_tasks[turn.turn_id] = asyncio.create_task(
            _renew_turn_lease(turn)
        )
        _mark_liveness_success()
        _audit("turn_start", {**_turn_payload(turn), "turns_open": count})
        log.info(
            "Turn start seat=%s turn=%s event=%s correlation=%s open=%d global=%d",
            turn.seat_id,
            turn.turn_id,
            turn.event_id,
            turn.correlation_id,
            count,
            global_count,
        )
        return count
    except Exception as exc:
        message = f"could not register turn {turn.turn_id}: {type(exc).__name__}: {exc}"
        _set_liveness_error(message)
        if TAEY_LIVENESS_REQUIRED:
            raise LivenessUnavailable(message) from exc
        return -1


async def _renew_turn_lease(turn: TurnContext) -> None:
    try:
        while True:
            await asyncio.sleep(TAEY_TURN_HEARTBEAT_SECS)
            try:
                if _redis is None:
                    raise LivenessUnavailable("Redis client became unavailable")
                renewed = int(
                    _redis.eval(
                        _RENEW_TURN_LUA,
                        2,
                        _liveness_keys(turn.seat_id)[0],
                        _liveness_keys(turn.seat_id)[8],
                        turn.turn_id,
                        time.time() + TAEY_TURN_LEASE_SECS,
                    )
                )
            except Exception as exc:
                _set_liveness_error(
                    f"lease renewal failed seat={turn.seat_id} "
                    f"turn={turn.turn_id}: {type(exc).__name__}: {exc}"
                )
                continue
            if not renewed:
                _set_liveness_error(
                    f"active turn lease disappeared for {turn.turn_id}"
                )
                return
            _mark_liveness_success()
    except asyncio.CancelledError:
        raise
    finally:
        current = _turn_heartbeat_tasks.get(turn.turn_id)
        if current is asyncio.current_task():
            _turn_heartbeat_tasks.pop(turn.turn_id, None)


async def _liveness_reaper() -> None:
    while True:
        try:
            await asyncio.sleep(TAEY_TURN_HEARTBEAT_SECS)
            _reconcile_registered_liveness()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _set_liveness_error(
                f"liveness reaper failed: {type(exc).__name__}: {exc}"
            )


def _schedule_turn_close_retry(turn: TurnContext, outcome: str) -> None:
    existing = _turn_close_retry_tasks.get(turn.turn_id)
    if existing is None or existing.done():
        _turn_close_retry_tasks[turn.turn_id] = asyncio.create_task(
            _retry_turn_close(turn, outcome)
        )


async def _retry_turn_close(turn: TurnContext, outcome: str) -> None:
    retry_deadline = time.time() + TAEY_TURN_LEASE_SECS
    delay = 1
    try:
        while time.time() < retry_deadline:
            await asyncio.sleep(delay)
            _, count = await _end_turn(
                turn,
                f"{outcome}_retry",
                schedule_retry=False,
            )
            if count >= 0:
                return
            delay = min(delay * 2, TAEY_TURN_HEARTBEAT_SECS)
    except asyncio.CancelledError:
        raise
    finally:
        current = _turn_close_retry_tasks.get(turn.turn_id)
        if current is asyncio.current_task():
            _turn_close_retry_tasks.pop(turn.turn_id, None)


async def _end_turn(
    turn: TurnContext,
    outcome: str,
    *,
    schedule_retry: bool = True,
) -> tuple[int, int]:
    heartbeat = _turn_heartbeat_tasks.pop(turn.turn_id, None)
    if heartbeat is not None:
        heartbeat.cancel()
    if _redis is None:
        _set_liveness_error(
            f"Redis unavailable; could not close turn {turn.turn_id} outcome={outcome}"
        )
        if schedule_retry:
            _schedule_turn_close_retry(turn, outcome)
        return 0, -1
    try:
        result = _redis.eval(
            _END_TURN_LUA,
            len(_liveness_keys(turn.seat_id)),
            *_liveness_keys(turn.seat_id),
            turn.turn_id,
            int(time.time()),
        )
        removed, count, global_count = (
            int(result[0]),
            int(result[1]),
            int(result[2]),
        )
        _active_turns.pop(turn.turn_id, None)
        _mark_liveness_success()
        _audit(
            "turn_end",
            {
                **_turn_payload(turn),
                "outcome": outcome,
                "removed": bool(removed),
                "turns_open": count,
                "global_turns_open": global_count,
            },
        )
        log.info(
            "Turn end seat=%s turn=%s event=%s outcome=%s removed=%d open=%d global=%d",
            turn.seat_id,
            turn.turn_id,
            turn.event_id,
            outcome,
            removed,
            count,
            global_count,
        )
        return removed, count
    except Exception as exc:
        _set_liveness_error(
            f"could not close turn {turn.turn_id}: {type(exc).__name__}: {exc}"
        )
        if schedule_retry:
            _schedule_turn_close_retry(turn, outcome)
        return 0, -1


def _turn_headers(turn: TurnContext) -> dict[str, str]:
    return {
        "X-Taey-Turn-Id": turn.turn_id,
        "X-Taey-Seat-Id": turn.seat_id,
        "X-Taey-Event-Id": turn.event_id,
        "X-Taey-Correlation-Id": turn.correlation_id,
        "X-Taey-Tool-Profile": turn.tool_profile,
    }


def _upstream_headers(turn: TurnContext) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Request-Id": turn.turn_id,
        **_turn_headers(turn),
    }


def _text_receipt(value: object) -> dict[str, object]:
    """Describe completion text without persisting the text itself."""
    import hashlib

    if not isinstance(value, str):
        return {"type": type(value).__name__, "chars": 0, "bytes": 0}
    encoded = value.encode("utf-8", "replace")
    return {
        "type": "str",
        "chars": len(value),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _invalid_completion_receipt(
    payload: object,
    *,
    upstream_status: int,
) -> dict[str, object]:
    """Return a content-free receipt for an unusable upstream completion."""
    response = payload if isinstance(payload, dict) else {}
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    tool_calls = message.get("tool_calls")
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    usage_keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
    )
    return {
        "ok": False,
        "error": "upstream_terminal_answer_missing",
        "upstream_status": upstream_status,
        "response_type": type(payload).__name__,
        "response_keys": sorted(str(key) for key in response),
        "choice_count": len(choices) if isinstance(choices, list) else 0,
        "choice_keys": sorted(str(key) for key in choice),
        "finish_reason": choice.get("finish_reason"),
        "message_keys": sorted(str(key) for key in message),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "content": _text_receipt(message.get("content")),
        "reasoning": _text_receipt(message.get("reasoning")),
        "reasoning_content": _text_receipt(message.get("reasoning_content")),
        "usage": {key: usage.get(key) for key in usage_keys if key in usage},
    }


async def _wait_for_downstream_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _run_while_downstream_connected(request: Request, operation, turn: TurnContext):
    operation_task = asyncio.create_task(operation)
    disconnect_task = asyncio.create_task(_wait_for_downstream_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {operation_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            return await operation_task

        await disconnect_task
        log.warning(
            "Downstream disconnected; cancelling active inference "
            "turn=%s event=%s correlation=%s",
            turn.turn_id,
            turn.event_id,
            turn.correlation_id,
        )
        operation_task.cancel()
        try:
            await operation_task
        except asyncio.CancelledError:
            pass
        raise HTTPException(
            status_code=499,
            detail="downstream disconnected during inference",
        )
    finally:
        for task in (operation_task, disconnect_task):
            if not task.done():
                task.cancel()
        for task in (operation_task, disconnect_task):
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    body = dict(raw_body)
    turn = _turn_context(request, body)
    turn_payload = _turn_payload(turn)
    turn_payload["_ui_sequence"] = {"observations": {}, "terminal": None}
    turn_payload["_tool_profile_state"] = {"terminal": None}
    context_token = _request_context.set(turn_payload)
    started = False
    try:
        try:
            open_turns = _start_turn(turn)
        except LivenessUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        started = open_turns >= 0
        response = await _run_while_downstream_connected(
            request,
            _chat_completions_for_turn(body, turn, started),
            turn,
        )
        if started and not isinstance(response, StreamingResponse):
            await _end_turn(turn, "nonstream_complete")
        return response
    except BaseException:
        if started:
            await _end_turn(turn, "handler_error")
        raise
    finally:
        _request_context.reset(context_token)


async def _chat_completions_for_turn(
    body: dict,
    turn: TurnContext,
    liveness_registered: bool,
):
    body.pop("max_rounds", None)

    # Strip model field -- let vLLM use its loaded model
    body.pop("model", None)
    body = inject_preamble(body)
    is_stream = body.get("stream", False)

    # BOUND HERE BECAUSE THE STREAMING CLOSURE READS THEM UNCONDITIONALLY.
    #
    # These used to be initialised inside `if is_stream and body.get("tools")` below, while
    # the closure that consumes them is gated on `is_stream` ALONE. Any streaming request
    # carrying no tools therefore reached the closure with the names unbound and died with
    # `NameError: cannot access free variable 'resolved_answer'` — 200 OK, zero tokens, an
    # empty reply.
    #
    # `tools: []` is falsy, so a caller that sends an EMPTY tool list takes that path: the
    # key is present, so the auto-add above is skipped, and the value is falsy, so the
    # resolve block is skipped too. The dashboard sends exactly that on its stream payload,
    # which is why every UI chat turn returned nothing while direct curl calls worked.
    #
    # The fix is the domain, not a guard: a name consumed under `is_stream` is bound under
    # `is_stream`. Adding a hasattr/locals() check at the read site would have left the two
    # domains mismatched and hidden the next instance of the same shape.
    resolved_answer = ""
    resolved_thinking = ""

    if turn.tool_profile == _FULL_TOOL_PROFILE and "tools" not in body:
        body["tools"] = TOOLS
    elif turn.tool_profile != _FULL_TOOL_PROFILE:
        body["tools"] = _tools_for_profile(turn.tool_profile)
        body.pop("tool_choice", None)

    t0 = time.time()

    if is_stream and body.get("tools"):
        # RESOLVE TOOLS BEFORE STREAMING.
        #
        # The streaming path used to forward vLLM's SSE straight through with no tool loop, while
        # still OFFERING tools in the request. The result was silent and total: the model emitted a
        # tool call, the raw tool-call deltas were forwarded, the dashboard read only `content` and
        # `reasoning_content` and dropped them, and the user saw an empty reply while nothing
        # executed. Observed 2026-07-28 -- `search_isma` had been called ZERO times in the tool
        # audit despite the tool being offered on every UI turn, because the UI chat streams.
        # Taey was reaching for its memory on every substantive question and having the reach
        # discarded before anything ran, which reads from the outside as a model that knows nothing.
        #
        # Offering a capability on a path that cannot execute it is the defect. So tool rounds are
        # resolved here first -- non-streamed, because execution needs the assembled message object
        # -- and only the FINAL answer is streamed. Streaming stops being a special case: it changes
        # how the last response is delivered, never whether tools work.
        rounds = 0
        while True:
            probe = dict(body)
            probe["stream"] = False
            # RETRY ONCE ON A DROPPED POOLED CONNECTION. httpx keeps connections alive; after an
            # upstream restart or an abandoned stream a pooled socket can be dead, and the first
            # use raises RemoteProtocolError("Server disconnected without sending a response").
            # That surfaced as a bare HTTP 500 on the whole turn -- and when the caller was the
            # notify poller, as an undelivered message with the daemon reporting a fire error.
            # One stale socket should not fail a turn; the retry gets a fresh connection.
            for _attempt in (1, 2):
                try:
                    resp = await _http.post("/v1/chat/completions", json=probe,
                                            headers=_upstream_headers(turn))
                    break
                except httpx.RemoteProtocolError:
                    if _attempt == 2:
                        raise
                    log.warning("upstream dropped a pooled connection; retrying once")
            payload = resp.json()
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []
            # GATE ON TOOL CALLS ONLY, NOT ON finish_reason. Measured 2026-07-28: this build
            # returns finish_reason='stop' even on turns that carry tool_calls, so requiring
            # =='tool_calls' discarded real calls -- and because the model had put its output IN
            # the call, `content` was empty, producing a 200 with nothing in it in ~16 seconds.
            # From the user's side that is indistinguishable from the model refusing to answer.
            # If there are calls to make, make them; finish_reason is not load-bearing here.
            if not tool_calls:
                answer = message.get("content") or ""
                thinking = (
                    message.get("reasoning")
                    or message.get("reasoning_content")
                    or ""
                )
                if not answer:
                    receipt = _invalid_completion_receipt(
                        payload,
                        upstream_status=resp.status_code,
                    )
                    _audit("upstream_invalid_completion", receipt)
                    log.error(
                        "Upstream terminal completion had no assistant answer: %s",
                        json.dumps(receipt, sort_keys=True),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": "upstream_terminal_answer_missing",
                            "turn_id": turn.turn_id,
                        },
                    )
                # THE LOOP EXITS BECAUSE THIS PROBE PRODUCED THE FINAL ANSWER. Keep it.
                #
                # Discarding it and re-requesting the same messages with stream=True was a defect:
                # the second generation is independently sampled, so it could emit a FRESH tool
                # call instead of prose. Those tool-call deltas are not `content` or
                # `reasoning_content`, the dashboard drops them, and the turn renders EMPTY --
                # tools having executed correctly, and the user seeing nothing. Observed
                # 2026-07-28: 3 tool rounds resolved, then 14 bytes of SSE (`[DONE]` alone) and a
                # persisted assistant turn of length 0. That is the same empty-reply symptom this
                # whole streaming path was rewritten to remove, reintroduced one step later.
                #
                # The answer already exists here. Regenerating it was never necessary, costs a
                # second inference, and reopens a failure mode that cannot happen if we simply
                # return what the tool loop concluded.
                resolved_answer = answer
                # Keep the REASONING too. ep3 splits its output: reasoning lands in
                # `reasoning_content` and the answer in `content`. Capturing only the answer meant
                # that on any turn using tools -- which is most real work -- thinking was generated,
                # discarded here, and never reached the reader. With the thinking toggle ON the UI
                # still showed a "thinking" indicator, because the indicator reflects the request,
                # not the response. Observed 2026-07-28.
                # FIELD NAME: this build emits `reasoning`, NOT `reasoning_content`. Measured directly
                # against Thor2 2026-07-28: a thinking-enabled request returned reasoning=2205 chars with
                # reasoning_content absent, and the streaming deltas carried only `reasoning`. Reading the
                # wrong key returned empty forever, so every turn reported "no thinking this turn" while the
                # model was in fact thinking. Both names are accepted here so a build that renames it again
                # does not silently blank the panel a second time.
                resolved_thinking = thinking
                break
            rounds += 1
            log.info("Stream tool calls (round %d): %s", rounds,
                     [tc.get("function", {}).get("name") for tc in tool_calls])
            body["messages"].append(message)
            for tc in tool_calls:
                func = tc.get("function", {}) or {}
                raw_args = func.get("arguments", {})
                if isinstance(raw_args, dict):
                    arguments = raw_args
                else:
                    try:
                        arguments = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        arguments = {}
                result = await execute_tool_call_async(
                    func.get("name", ""),
                    arguments,
                    tool_call_id=tc.get("id", ""),
                    round_num=rounds,
                )
                log.info("Tool %s -> %d chars", func.get("name", ""), len(result))
                body["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
    if is_stream:
        async def stream_and_measure():
            context_token = _request_context.set(_turn_payload(turn))
            token_count = 0
            prompt_tokens = 0
            outcome = "stream_complete"
            try:
                if resolved_answer:
                    completion_id = f"chatcmpl-{turn.turn_id}"
                    for i in range(0, len(resolved_thinking), 240):
                        yield ("data: " + json.dumps({
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0,
                                         "delta": {
                                             "reasoning": resolved_thinking[i:i + 240]
                                         },
                                         "finish_reason": None}],
                        }) + "\n\n").encode()
                    for i in range(0, len(resolved_answer), 240):
                        piece = resolved_answer[i:i + 240]
                        yield ("data: " + json.dumps({
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {"content": piece},
                                         "finish_reason": None}],
                        }) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                    return
                async with _http.stream(
                    "POST", "/v1/chat/completions",
                    json=body,
                    headers=_upstream_headers(turn),
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                        if b'"delta"' in chunk:
                            token_count += 1
                        # Try to extract usage from final chunk
                        if b'"usage"' in chunk:
                            try:
                                for line in chunk.decode().split("\n"):
                                    if line.startswith("data: ") and "usage" in line:
                                        d = json.loads(line[6:])
                                        u = d.get("usage", {})
                                        prompt_tokens = u.get("prompt_tokens", 0)
                                        token_count = u.get("completion_tokens", token_count)
                            except Exception:
                                pass
            except BaseException:
                outcome = "stream_error"
                raise
            finally:
                if liveness_registered:
                    await _end_turn(turn, outcome)
                elapsed_ms = (time.time() - t0) * 1000
                publish_metrics(elapsed_ms, prompt_tokens, token_count)
                log.info(
                    "Streamed %d tokens in %.0fms (%.1f tok/s, prompt=%d)",
                    token_count, elapsed_ms,
                    token_count / max(elapsed_ms / 1000, 0.001),
                    prompt_tokens,
                )
                _request_context.reset(context_token)

        # Background cleanup covers disconnect paths where Starlette never advances the generator
        # into its finally. Exact turn-ID removal keeps the duplicate cleanup harmless.
        return StreamingResponse(
            stream_and_measure(),
            media_type="text/event-stream",
            headers=_turn_headers(turn),
            background=(
                BackgroundTask(_end_turn, turn, "stream_background")
                if liveness_registered
                else None
            ),
        )
    else:
        async def nonstream_response():
            # Non-stream: forward with tool call execution loop
            messages = body["messages"]
            total_tokens = 0
            round_num = 0

            # A schema-constrained grammar leaves NO tokens for tool-call syntax, so one request
            # carrying both `response_format` and `tools` can neither call a tool nor answer:
            # measured against ep3 as tool_calls=[], content=None, finish_reason="length" — it
            # burns the whole budget producing nothing. Callers that need a structured result
            # (the council seats do) therefore get the schema applied to the FINAL answer only,
            # while the tool rounds run unconstrained. This is the same two-phase shape the loop
            # below already uses for `tools`, applied to the other half of the conflict.
            held_response_format = (
                body.pop("response_format", None) if body.get("tools") else None
            )

            async def _final_answer():
                final_body = dict(body)
                final_body["messages"] = messages
                final_body.pop("tools", None)
                final_body["tool_choice"] = "none"
                if held_response_format is not None:
                    final_body["response_format"] = held_response_format
                r = await _http.post(
                    "/v1/chat/completions",
                    json=final_body,
                    headers=_upstream_headers(turn),
                )
                return r.json()

            while True:
                resp = await _http.post(
                    "/v1/chat/completions",
                    json=body,
                    headers=_upstream_headers(turn),
                )
                result = resp.json()
                usage = result.get("usage", {})
                total_tokens += usage.get("completion_tokens", 0)

                choice = result.get("choices", [{}])[0]
                message = choice.get("message", {})
                finish_reason = choice.get("finish_reason", "")
                tool_calls = message.get("tool_calls", [])

                if not tool_calls or finish_reason != "tool_calls":
                    # No tool calls -- final response. If a schema was held aside for the tool
                    # rounds, the answer the caller contracted for has not been produced yet.
                    if held_response_format is not None:
                        result = await _final_answer()
                        total_tokens += result.get("usage", {}).get("completion_tokens", 0)
                    break

                # Execute tool calls
                round_num += 1
                log.info("Tool calls (round %d): %s",
                         round_num,
                         [tc.get("function", {}).get("name") for tc in tool_calls])

                # NOTE: keep each tool_call's arguments as the JSON STRING vLLM
                # returns. With the correct qwen3_xml parser the chat-template
                # renders string args fine; re-POSTing dict args (the old
                # qwen3_coder-era workaround) fails vLLM's API validation
                # (function.arguments must be a string). execute_tool_call below
                # json.loads the string into a dict for execution.

                # Add assistant message with tool calls to history
                messages.append(message)

                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    raw_args = func.get("arguments", {})
                    if isinstance(raw_args, dict):
                        arguments = raw_args
                    else:
                        try:
                            arguments = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            arguments = {}

                    tool_result = await execute_tool_call_async(
                        name,
                        arguments,
                        tool_call_id=tc.get("id", ""),
                        round_num=round_num,
                    )
                    log.info("Tool %s(%s) -> %d chars",
                             name, json.dumps(arguments)[:100], len(tool_result))

                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_result,
                    })

                # Update body with extended messages for next round
                body["messages"] = messages

            elapsed_ms = (time.time() - t0) * 1000
            final_usage = result.get("usage", {})
            prompt_tok = final_usage.get("prompt_tokens", 0)
            completion_tok = final_usage.get("completion_tokens", 0)
            publish_metrics(elapsed_ms, prompt_tok, completion_tok, round_num)

            context_pct = (prompt_tok + completion_tok) / MAX_CONTEXT_TOKENS * 100
            log.info(
                "Generated %d tokens in %.0fms (%.1f tok/s, %d tool rounds, "
                "prompt=%d completion=%d context=%.1f%%)",
                completion_tok, elapsed_ms,
                completion_tok / max(elapsed_ms / 1000, 0.001),
                round_num, prompt_tok, completion_tok, context_pct,
            )

            return JSONResponse(
                content=result,
                status_code=resp.status_code,
                headers=_turn_headers(turn),
            )

        return await nonstream_response()


def main():
    global _serving_socket_reserved
    _read_canonical_system_prompt()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PROXY_PORT,
        log_level="info",
    )
    listener: socket.socket = config.bind_socket()
    _serving_socket_reserved = True
    log.info("Starting soma proxy on port %d -> vLLM at %s", PROXY_PORT, VLLM_BASE)
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        _serving_socket_reserved = False
        listener.close()


if __name__ == "__main__":
    main()
