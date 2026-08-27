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
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from revenue_ui_contract import (
    SEMANTIC_OUTWARD,
    canonical_json_bytes,
    semantic_input,
    validate_operation_evidence,
    validate_operation_card,
    validate_semantic_receipt,
)

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
MANUAL_CHAT_UI_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_CHAT_UI_SYSTEM.md",
)
REVENUE_UI_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_REVENUE_UI_SYSTEM.md",
)
CONSULT_CHAT_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_CONSULT_CHAT_SYSTEM.md",
)
LINKEDIN_JOBS_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_JOBS_SYSTEM.md",
)
LINKEDIN_JOBS_RESTORE_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_JOBS_RESTORE_SYSTEM.md",
)
LINKEDIN_JOB_SEARCH_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_JOB_SEARCH_SYSTEM.md",
)
LINKEDIN_ENGAGERS_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_ENGAGERS_SYSTEM.md",
)
LINKEDIN_APPLICATION_INTAKE_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_APPLICATION_INTAKE_SYSTEM.md",
)
LINKEDIN_APPLICATION_CLASSIFICATION_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "TAEY_LINKEDIN_APPLICATION_CLASSIFICATION_SYSTEM.md",
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
TAEY_DRIVE_CHAT_CAPTURE_ROOT = os.environ.get(
    "TAEY_DRIVE_CHAT_CAPTURE_ROOT", ""
).strip()

app = FastAPI(title="Taey Soma Proxy", version="1.0.0")

_redis: Optional[redis.Redis] = None
_mira_redis: Optional[redis.Redis] = None
_http: Optional[httpx.AsyncClient] = None
_ecosystem_http: Optional[httpx.Client] = None
_system_prompt: str = ""
_manual_chat_ui_system_prompt: str = ""
_revenue_ui_system_prompt: str = ""
_consult_chat_system_prompt: str = ""
_one_shot_system_prompts: dict[str, str] = {}
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
    proxy_namespace: str
    process_generation: str
    started_at: float


@dataclass(frozen=True)
class PrivateTransactionToolSpec:
    profile: str
    tool: str
    prompt_label: str
    system_prompt_path: str
    runner_name: str
    python_path: str
    python_env_name: str
    private_root: str
    private_root_env_name: str
    displays: tuple[str, ...]
    displays_env_name: str
    timeout_secs: int
    timeout_env_name: str
    deadline_secs: int
    claim_schema: str
    terminal_reason: str
    expected_result_keys: frozenset[str]
    validate_result: Callable[[dict, int], str | None]
    public_root: str = ""
    public_root_env_name: str = ""
    database_path: str = ""
    database_env_name: str = ""


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
_REVENUE_UI_TOOL_PROFILE = "revenue-ui"
_CONSULT_CHAT_TOOL_PROFILE = "consult-chat"
_LINKEDIN_JOBS_TOOL_PROFILE = "linkedin-jobs"
_LINKEDIN_JOBS_RESTORE_TOOL_PROFILE = "linkedin-jobs-restore"
_LINKEDIN_JOB_SEARCH_TOOL_PROFILE = "linkedin-job-search"
_LINKEDIN_ENGAGERS_TOOL_PROFILE = "linkedin-engagers"
_LINKEDIN_APPLICATION_INTAKE_TOOL_PROFILE = "linkedin-application-intake"
_LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE = (
    "linkedin-application-classification"
)


def _parse_ui_action_bindings(value: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    platforms: set[str] = set()
    for entry in filter(None, (part.strip() for part in value.split(","))):
        platform, separator, display = entry.partition("=")
        platform = platform.strip()
        display = display.strip()
        if (
            separator != "="
            or platform != "linkedin"
            or not re.fullmatch(r":\d+", display)
            or display == ":0"
        ):
            raise RuntimeError(
                "TAEY_UI_ACTION_BINDINGS entries must currently have exact "
                "linkedin=:N form with :0 refused"
            )
        if display in bindings or platform in platforms:
            raise RuntimeError(
                "TAEY_UI_ACTION_BINDINGS must bind each platform and display exactly once"
            )
        bindings[display] = platform
        platforms.add(platform)
    return bindings


_UI_ACTION_BINDINGS = _parse_ui_action_bindings(
    os.environ.get("TAEY_UI_ACTION_BINDINGS", "")
)
REVENUE_UI_PRIVATE_ROOT = os.environ.get(
    "TAEY_REVENUE_UI_PRIVATE_ROOT", ""
).strip()
_TOOL_PROFILE_ALLOWED: dict[str, frozenset[str] | None] = {
    _FULL_TOOL_PROFILE: None,
    _MANUAL_CHAT_UI_TOOL_PROFILE: frozenset({
        "drive_chat",
    }),
    _REVENUE_UI_TOOL_PROFILE: frozenset({
        "ui_action",
    }),
    _CONSULT_CHAT_TOOL_PROFILE: frozenset({
        "consult_chat",
    }),
    _LINKEDIN_JOBS_TOOL_PROFILE: frozenset({
        "linkedin_jobs",
    }),
    _LINKEDIN_JOBS_RESTORE_TOOL_PROFILE: frozenset({
        "restore_linkedin_jobs_surface",
    }),
    _LINKEDIN_JOB_SEARCH_TOOL_PROFILE: frozenset({
        "linkedin_job_search",
    }),
    _LINKEDIN_ENGAGERS_TOOL_PROFILE: frozenset({
        "linkedin_engagers",
    }),
    _LINKEDIN_APPLICATION_INTAKE_TOOL_PROFILE: frozenset({
        "linkedin_application_intake",
    }),
    _LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE: frozenset({
        "linkedin_application_classification",
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
    global _consult_chat_system_prompt
    global _manual_chat_ui_system_prompt, _revenue_ui_system_prompt, _permanent_kernel
    global _static_system_prefix, _system_prompt
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
                current_process_identity=(
                    TAEY_DEFAULT_SEAT,
                    PROCESS_GENERATION,
                ),
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
    manual_chat_prompt_path = Path(MANUAL_CHAT_UI_SYSTEM_PROMPT_PATH)
    if not manual_chat_prompt_path.is_file():
        raise RuntimeError(
            f"manual chat UI system prompt is missing or not a regular file: {manual_chat_prompt_path}"
        )
    _manual_chat_ui_system_prompt = manual_chat_prompt_path.read_text(encoding="utf-8")
    if not _manual_chat_ui_system_prompt.strip():
        raise RuntimeError(
            f"manual chat UI system prompt is empty: {manual_chat_prompt_path}"
        )
    revenue_ui_prompt_path = Path(REVENUE_UI_SYSTEM_PROMPT_PATH)
    if not revenue_ui_prompt_path.is_file():
        raise RuntimeError(
            f"revenue UI system prompt is missing or not a regular file: {revenue_ui_prompt_path}"
        )
    _revenue_ui_system_prompt = revenue_ui_prompt_path.read_text(encoding="utf-8")
    if not _revenue_ui_system_prompt.strip():
        raise RuntimeError(
            f"revenue UI system prompt is empty: {revenue_ui_prompt_path}"
        )
    consult_chat_prompt_path = Path(CONSULT_CHAT_SYSTEM_PROMPT_PATH)
    if not consult_chat_prompt_path.is_file():
        raise RuntimeError(
            f"consult chat system prompt is missing or not a regular file: {consult_chat_prompt_path}"
        )
    _consult_chat_system_prompt = consult_chat_prompt_path.read_text(encoding="utf-8")
    if not _consult_chat_system_prompt.strip():
        raise RuntimeError(
            f"consult chat system prompt is empty: {consult_chat_prompt_path}"
        )
    _one_shot_system_prompts.clear()
    for spec in _PRIVATE_TRANSACTION_TOOL_SPECS:
        prompt_path = Path(spec.system_prompt_path)
        if not prompt_path.is_file():
            raise RuntimeError(
                f"{spec.prompt_label} system prompt is missing or not a regular file: "
                f"{prompt_path}"
            )
        prompt = prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise RuntimeError(
                f"{spec.prompt_label} system prompt is empty: {prompt_path}"
            )
        _one_shot_system_prompts[spec.profile] = prompt
    log.info(
        "Canonical system prompt loaded from %s (%d chars)",
        SYSTEM_PROMPT_PATH,
        len(_system_prompt),
    )
    log.info(
        "Manual chat UI system prompt loaded from %s (%d chars)",
        manual_chat_prompt_path,
        len(_manual_chat_ui_system_prompt),
    )
    log.info(
        "Revenue UI system prompt loaded from %s (%d chars)",
        revenue_ui_prompt_path,
        len(_revenue_ui_system_prompt),
    )
    log.info(
        "Consult chat system prompt loaded from %s (%d chars)",
        consult_chat_prompt_path,
        len(_consult_chat_system_prompt),
    )
    for spec in _PRIVATE_TRANSACTION_TOOL_SPECS:
        log.info(
            "%s system prompt loaded from %s (%d chars)",
            spec.prompt_label,
            spec.system_prompt_path,
            len(_one_shot_system_prompts[spec.profile]),
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
        result = await asyncio.to_thread(
            _execute_tool_call_with_capture,
            name,
            arguments,
        )
        _audit("tool_end", _tool_receipt(name, arguments, result, ok=True))
        return result
    except Exception as exc:
        _audit("tool_end", _tool_receipt(name, arguments, exc, ok=False))
        raise
    finally:
        _request_context.reset(token)


_CAPTURE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _capture_component(context: dict, key: str) -> str:
    value = str(context.get(key) or "")
    if not _CAPTURE_COMPONENT_RE.fullmatch(value):
        raise RuntimeError(f"drive_chat capture requires a valid {key}")
    return value


def _open_private_directory(parent_fd: int, component: str) -> int:
    created = False
    try:
        os.mkdir(component, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    if created:
        os.fsync(parent_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    child_fd = os.open(component, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(child_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("drive_chat capture path component is not a directory")
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RuntimeError(
                "drive_chat capture directories must be owned by the proxy user with mode 0700"
            )
        return child_fd
    except Exception:
        os.close(child_fd)
        raise


def _write_private_json(parent_fd: int, name: str, payload: dict) -> None:
    body = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while storing drive_chat capture")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent_fd)


def _prepare_drive_chat_capture(arguments: dict) -> tuple[int, dict]:
    context = dict(_request_context.get())
    root = TAEY_DRIVE_CHAT_CAPTURE_ROOT
    if not root:
        raise RuntimeError(
            "TAEY_DRIVE_CHAT_CAPTURE_ROOT is required before drive_chat can execute"
        )
    absolute_root = os.path.abspath(root)
    if root != absolute_root or os.path.realpath(root) != absolute_root:
        raise RuntimeError(
            "TAEY_DRIVE_CHAT_CAPTURE_ROOT must be an absolute, non-symlink path"
        )
    with ExitStack() as opened:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.callback(os.close, root_fd)
        root_metadata = os.fstat(root_fd)
        if (
            root_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise RuntimeError(
                "TAEY_DRIVE_CHAT_CAPTURE_ROOT must be owned by the proxy user with mode 0700"
            )
        identity = {
            key: _capture_component(context, key)
            for key in (
                "proxy_namespace",
                "seat_id",
                "event_id",
                "turn_id",
                "correlation_id",
                "process_generation",
            )
        }
        round_num = context.get("tool_round")
        if not isinstance(round_num, int) or round_num <= 0:
            raise RuntimeError("drive_chat capture requires a positive tool_round")
        tool_call_id = str(context.get("tool_call_id") or "")
        if not tool_call_id:
            raise RuntimeError("drive_chat capture requires a tool_call_id")
        current_fd = root_fd
        for component in (
            identity["proxy_namespace"],
            identity["seat_id"],
            identity["event_id"],
            identity["turn_id"],
        ):
            current_fd = _open_private_directory(current_fd, component)
            opened.callback(os.close, current_fd)
        exchange = (
            f"{round_num:04d}-"
            f"{hashlib.sha256(tool_call_id.encode('utf-8')).hexdigest()[:16]}-"
            f"{uuid.uuid4().hex}"
        )
        exchange_fd = _open_private_directory(current_fd, exchange)
        envelope = {
            "schema": "taey.drive_chat.exchange.v1",
            **identity,
            "tool_call_id": tool_call_id,
            "tool_round": round_num,
        }
        try:
            _write_private_json(
                exchange_fd,
                "request.json",
                {**envelope, "arguments": arguments},
            )
            return exchange_fd, envelope
        except Exception:
            os.close(exchange_fd)
            raise


def _terminalize_capture_failure(message: str, arguments: dict) -> None:
    context = _request_context.get()
    sequence = context.get("_ui_sequence")
    if isinstance(sequence, dict) and not isinstance(sequence.get("terminal"), dict):
        sequence["terminal"] = {
            "display": str(arguments.get("display") or ""),
            "action": str(arguments.get("action") or ""),
            "tool_round": context.get("tool_round"),
            "reason": message,
        }
        observations = sequence.get("observations")
        if isinstance(observations, dict):
            observations.clear()
    profile_state = context.get("_tool_profile_state")
    if isinstance(profile_state, dict) and not isinstance(
        profile_state.get("terminal"), dict
    ):
        profile_state["terminal"] = {
            "tool": "drive_chat",
            "reason": message,
        }


def _returned_drive_chat_status(body: str) -> object:
    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("ok")


def _execute_tool_call_with_capture(name: str, arguments: dict) -> str:
    if name != "drive_chat":
        return execute_tool_call(name, arguments)
    try:
        exchange_fd, envelope = _prepare_drive_chat_capture(arguments)
    except Exception as exc:
        message = f"drive_chat evidence preflight failed: {type(exc).__name__}: {exc}"
        _terminalize_capture_failure(message, arguments)
        raise RuntimeError(message) from exc
    try:
        try:
            result = execute_tool_call(name, arguments)
        except Exception as exc:
            body = f"{type(exc).__name__}: {exc}"
            try:
                _write_private_json(
                    exchange_fd,
                    "result.json",
                    {
                        **envelope,
                        "returned": False,
                        "exception_type": type(exc).__name__,
                        "result": body,
                        **_digest(body),
                    },
                )
            except Exception as capture_exc:
                message = (
                    "drive_chat evidence finalization failed after tool exception: "
                    f"{type(capture_exc).__name__}: {capture_exc}"
                )
                _terminalize_capture_failure(message, arguments)
                raise RuntimeError(message) from capture_exc
            _terminalize_capture_failure(
                f"drive_chat execution failed: {type(exc).__name__}: {exc}",
                arguments,
            )
            raise
        try:
            body = result if isinstance(result, str) else str(result)
            _write_private_json(
                exchange_fd,
                "result.json",
                {
                    **envelope,
                    "returned": True,
                    "tool_ok": _returned_drive_chat_status(body),
                    "result": body,
                    **_digest(body),
                },
            )
        except Exception as exc:
            message = (
                "drive_chat evidence finalization failed after UI execution: "
                f"{type(exc).__name__}: {exc}"
            )
            _terminalize_capture_failure(message, arguments)
            raise RuntimeError(message) from exc
        return result
    finally:
        os.close(exchange_fd)


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
                "the newly observed tree, one YAML-declared semantic operation at a time; do not use remembered platform "
                "labels, platform shortcuts, coordinates, URLs, chooser routes, or send recipes. "
                "For focus_and_key_open, call operate once: the driver focuses the exact "
                "server-bound target, "
                "verifies focus, and sends the exact YAML open key; then observe the declared "
                "menu scope and require its exact target. Never split that method into separate "
                "model-issued focus and key calls. "
                "For an opened selection menu, use the exact observation scope declared by that "
                "menu's YAML operate.scope; the mapped targets remain bound to that scope. "
                "The native GTK file chooser is a shared driver boundary rather than platform UI: "
                "after a YAML-resolved upload action opens it, focus_dialog activates and verifies "
                "the separate X11 chooser window. A browser-tree observation after the upload action "
                "is not evidence that the separate chooser is absent; focus_dialog is the fail-loud "
                "probe. Once focused, address the shared chooser one primitive at a time with a fresh "
                "observation between each: key ctrl+l, key ctrl+a, type the absolute file path, then "
                "key Return. Finally observe the platform tree and verify the attachment before any "
                "composer or send action. observe returns the current URL, YAML fresh URL, YAML "
                "Stop keys, actionable mapped elements, and non-actionable unknown/sidebar drift. "
                "Only an exact mapped singleton or exact YAML-selected target receives mutation "
                "authority. Presence retains its opaque ref server-side; never transcribe one. "
                "If the live tree shows a changed "
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
                                 "focus_dialog", "scroll_to_bottom"],
                        "description": (
                            "the single action to perform; operate executes the one operation "
                            "declared by platform YAML for the chosen preceding-observation target; "
                            "direct click/focus/activate/hover are only for controls with no "
                            "declaration; "
                            "scroll_to_bottom executes the exact YAML extraction scroll step, "
                            "anchored to its exact mapped element; assistant_text remains the "
                            "default and research_report is selected only by an exact unique match, "
                            "then requires its next exact Copy postcondition before success; "
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
                    "element": {
                        "type": "string",
                        "description": (
                            "readable element key from the immediately preceding fresh observe; "
                            "for click/focus/activate/hover/operate Presence resolves the exact "
                            "canonical mapped ref without model transcription"
                        ),
                    },
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
    {
        "type": "function",
        "function": {
            "name": "ui_action",
            "description": (
                "Observe a trusted revenue display, perform exactly one YAML-declared viewport "
                "scroll, perform exactly one mapped page-bound activate, or paste one immutable "
                "private transaction into one mapped editor from the immediately "
                "preceding fresh observation. The server binds "
                "the display to its platform; the model never supplies a platform, selector, "
                "coordinate, URL, sequence, text, or path. After every mutation, the public platform hook "
                "must verify its exact postcondition and a new observe is required before any "
                "later mutation. Dropdown opening, option observation, option selection, and result "
                "verification are separate calls when those capabilities are qualified."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display", "action"],
                "properties": {
                    "display": {
                        "type": "string",
                        "description": "trusted revenue display configured by the server",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["observe", "scroll_into_view", "activate", "paste"],
                    },
                    "element": {
                        "type": "string",
                        "description": (
                            "mutation only: exact mapped element key "
                            "returned by the immediately preceding fresh observe"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_chat",
            "description": (
                "Execute one frozen end-to-end Family-Chat consultation through the "
                "selected platform's existing YAML, driver, monitor, and extractor. "
                "The caller supplies exactly two immutable bundles and one immutable "
                "prompt file. The driver performs one Send at most, halts on its first "
                "failed postcondition, writes the full receipt off-context, and returns "
                "only compact terminal evidence. Available only in the consult-chat "
                "tool profile; never retry a failed transaction."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "display",
                    "prompt_file",
                    "bundle_a",
                    "bundle_b",
                    "output_file",
                    "receipt_file",
                ],
                "properties": {
                    "display": {
                        "type": "string",
                        "enum": [":2", ":3", ":4", ":5", ":6"],
                    },
                    "prompt_file": {
                        "type": "string",
                        "description": "absolute path to the frozen UTF-8 prompt",
                    },
                    "bundle_a": {
                        "type": "string",
                        "description": "absolute path to frozen Bundle A",
                    },
                    "bundle_b": {
                        "type": "string",
                        "description": "absolute path to frozen Bundle B",
                    },
                    "output_file": {
                        "type": "string",
                        "description": "new absolute path for extracted response text",
                    },
                    "receipt_file": {
                        "type": "string",
                        "description": "new absolute path for the complete transaction receipt",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linkedin_jobs",
            "description": (
                "Execute one frozen LinkedIn Jobs read-only transaction through "
                "the public platform YAML, canonical AT-SPI tree, driver, private "
                "sink, and receipt chain. Raw job content remains off-context. "
                "Available only in the linkedin-jobs tool profile; never retry a "
                "failed transaction."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display"],
                "properties": {
                    "display": {
                        "type": "string",
                        "pattern": "^:[0-9]{1,3}$",
                        "description": (
                            "runtime-authorized LinkedIn display supplied by the user"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linkedin_job_search",
            "description": (
                "Capture the exact mounted LinkedIn Jobs search-result cards "
                "through the public platform YAML, canonical AT-SPI tree, "
                "read-only observation barrier, private sink, and receipt "
                "chain. Search policy and raw card content remain off-context. "
                "Available only in the linkedin-job-search tool profile; never "
                "retry a failed transaction."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display"],
                "properties": {
                    "display": {
                        "type": "string",
                        "pattern": "^:[0-9]{1,3}$",
                        "description": (
                            "runtime-authorized LinkedIn display supplied by the user"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_linkedin_jobs_surface",
            "description": (
                "Restore one dedicated LinkedIn browser display to the exact "
                "parent-frozen Jobs search-results URL through the public Hands "
                "runner and private receipt chain. The target URL remains "
                "off-context. Available only in the linkedin-jobs-restore tool "
                "profile; never retry a spent transaction identity."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display"],
                "properties": {
                    "display": {
                        "type": "string",
                        "pattern": "^:[0-9]{1,3}$",
                        "description": (
                            "runtime-authorized LinkedIn display supplied by the user"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linkedin_engagers",
            "description": (
                "Execute one frozen LinkedIn My Posts new-engagement capture "
                "through the public Hands runner and private receipt chain. "
                "Account, post, notification, and engager content remains "
                "off-context. Available only in the linkedin-engagers tool "
                "profile; never retry a failed transaction."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["display"],
                "properties": {
                    "display": {
                        "type": "string",
                        "pattern": "^:[0-9]{1,3}$",
                        "description": (
                            "runtime-authorized LinkedIn display supplied by the user"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linkedin_application_intake",
            "description": (
                "Ingest one parent-frozen, receipt-bound LinkedIn capture pair "
                "into the private unclassified application intake through the "
                "public taey-apply connector. Private paths, job data, display, "
                "and policy remain outside model context. Available only in the "
                "linkedin-application-intake tool profile; never retry a spent "
                "transaction identity."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linkedin_application_classification",
            "description": (
                "Commit one parent-frozen LinkedIn application classification "
                "capsule through the public taey-apply connector. Private "
                "contents remain outside model context. Available only in the "
                "linkedin-application-classification tool profile; never retry "
                "a spent transaction identity."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
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

    elif name == "ui_action":
        return _do_ui_action(arguments)

    elif name == "consult_chat":
        return _do_consult_chat(arguments)

    elif name == "linkedin_jobs":
        return _do_linkedin_jobs(arguments)

    elif name == "restore_linkedin_jobs_surface":
        return _do_linkedin_jobs_restore(arguments)

    elif name == "linkedin_job_search":
        return _do_linkedin_job_search(arguments)

    elif name == "linkedin_engagers":
        return _do_linkedin_engagers(arguments)

    elif name == "linkedin_application_intake":
        return _do_linkedin_application_intake(arguments)

    elif name == "linkedin_application_classification":
        return _do_linkedin_application_classification(arguments)

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
# lives in the model and the prompt; this surface performs one semantic operation and returns the
# observed JSON. :0 (Jesse's monitor) and any non-chat display are REFUSED here, never
# merely absent from the schema.
# ---------------------------------------------------------------------------
UI_DRIVE_PYTHON = os.environ.get("TAEY_UI_DRIVE_PYTHON", "/home/mira/taeys-env-sys/bin/python")
UI_DRIVE_SCRIPT = os.environ.get(
    "TAEY_UI_DRIVE_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_drive.py"),
)
TAEYS_HANDS_ROOT = os.environ.get("TAEYS_HANDS_ROOT", "").strip()
LINKEDIN_JOBS_PYTHON = os.environ.get("TAEY_LINKEDIN_JOBS_PYTHON", "").strip()
LINKEDIN_JOBS_PRIVATE_ROOT = os.environ.get(
    "TAEY_LINKEDIN_JOBS_PRIVATE_ROOT", ""
).strip()
try:
    LINKEDIN_JOBS_TIMEOUT_SECS = int(
        os.environ.get("TAEY_LINKEDIN_JOBS_TIMEOUT_SECS", "1800")
    )
except ValueError:
    LINKEDIN_JOBS_TIMEOUT_SECS = 0
LINKEDIN_JOBS_DEADLINE_SECS = LINKEDIN_JOBS_TIMEOUT_SECS - 100
_env_linkedin_jobs_displays = os.environ.get(
    "TAEY_LINKEDIN_JOBS_DISPLAYS", ""
).strip()
LINKEDIN_JOBS_DISPLAYS = tuple(
    display.strip()
    for display in _env_linkedin_jobs_displays.split(",")
    if display.strip() and display.strip() != ":0"
)
LINKEDIN_JOBS_RESTORE_PYTHON = os.environ.get(
    "TAEY_LINKEDIN_JOBS_RESTORE_PYTHON", ""
).strip()
LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT = os.environ.get(
    "TAEY_LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT", ""
).strip()
try:
    LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS = int(
        os.environ.get("TAEY_LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS", "1800")
    )
except ValueError:
    LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS = 0
LINKEDIN_JOBS_RESTORE_DEADLINE_SECS = LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS - 100
_env_linkedin_jobs_restore_displays = os.environ.get(
    "TAEY_LINKEDIN_JOBS_RESTORE_DISPLAYS", ""
).strip()
LINKEDIN_JOBS_RESTORE_DISPLAYS = tuple(
    display.strip()
    for display in _env_linkedin_jobs_restore_displays.split(",")
    if display.strip() and display.strip() != ":0"
)
LINKEDIN_JOB_SEARCH_PYTHON = os.environ.get(
    "TAEY_LINKEDIN_JOB_SEARCH_PYTHON", ""
).strip()
LINKEDIN_JOB_SEARCH_PRIVATE_ROOT = os.environ.get(
    "TAEY_LINKEDIN_JOB_SEARCH_PRIVATE_ROOT", ""
).strip()
try:
    LINKEDIN_JOB_SEARCH_TIMEOUT_SECS = int(
        os.environ.get("TAEY_LINKEDIN_JOB_SEARCH_TIMEOUT_SECS", "1800")
    )
except ValueError:
    LINKEDIN_JOB_SEARCH_TIMEOUT_SECS = 0
LINKEDIN_JOB_SEARCH_DEADLINE_SECS = LINKEDIN_JOB_SEARCH_TIMEOUT_SECS - 100
_env_linkedin_job_search_displays = os.environ.get(
    "TAEY_LINKEDIN_JOB_SEARCH_DISPLAYS", ""
).strip()
LINKEDIN_JOB_SEARCH_DISPLAYS = tuple(
    display.strip()
    for display in _env_linkedin_job_search_displays.split(",")
    if display.strip() and display.strip() != ":0"
)
LINKEDIN_ENGAGERS_PYTHON = os.environ.get(
    "TAEY_LINKEDIN_ENGAGERS_PYTHON", ""
).strip()
LINKEDIN_ENGAGERS_PRIVATE_ROOT = os.environ.get(
    "TAEY_LINKEDIN_ENGAGERS_PRIVATE_ROOT", ""
).strip()
try:
    LINKEDIN_ENGAGERS_TIMEOUT_SECS = int(
        os.environ.get("TAEY_LINKEDIN_ENGAGERS_TIMEOUT_SECS", "1800")
    )
except ValueError:
    LINKEDIN_ENGAGERS_TIMEOUT_SECS = 0
LINKEDIN_ENGAGERS_DEADLINE_SECS = LINKEDIN_ENGAGERS_TIMEOUT_SECS - 100
_env_linkedin_engagers_displays = os.environ.get(
    "TAEY_LINKEDIN_ENGAGERS_DISPLAYS", ""
).strip()
LINKEDIN_ENGAGERS_DISPLAYS = tuple(
    display.strip()
    for display in _env_linkedin_engagers_displays.split(",")
    if display.strip() and display.strip() != ":0"
)
LINKEDIN_APPLICATION_INTAKE_PYTHON = os.environ.get(
    "TAEY_APPLY_PYTHON", ""
).strip()
LINKEDIN_APPLICATION_INTAKE_PUBLIC_ROOT = os.environ.get(
    "TAEY_APPLY_PUBLIC_ROOT", ""
).strip()
LINKEDIN_APPLICATION_INTAKE_PRIVATE_ROOT = os.environ.get(
    "TAEY_APPLY_PRIVATE_ROOT", ""
).strip()
LINKEDIN_APPLICATION_INTAKE_DATABASE = os.environ.get(
    "TAEY_APPLY_DB", ""
).strip()
try:
    LINKEDIN_APPLICATION_INTAKE_TIMEOUT_SECS = int(
        os.environ.get("TAEY_APPLY_TIMEOUT_SECS", "")
    )
except ValueError:
    LINKEDIN_APPLICATION_INTAKE_TIMEOUT_SECS = 0
LINKEDIN_APPLICATION_CLASSIFICATION_PYTHON = os.environ.get(
    "TAEY_APPLY_CLASSIFICATION_PYTHON", ""
).strip()
LINKEDIN_APPLICATION_CLASSIFICATION_PUBLIC_ROOT = os.environ.get(
    "TAEY_APPLY_CLASSIFICATION_PUBLIC_ROOT", ""
).strip()
LINKEDIN_APPLICATION_CLASSIFICATION_PRIVATE_ROOT = os.environ.get(
    "TAEY_APPLY_CLASSIFICATION_PRIVATE_ROOT", ""
).strip()
LINKEDIN_APPLICATION_CLASSIFICATION_DATABASE = os.environ.get(
    "TAEY_APPLY_CLASSIFICATION_DB", ""
).strip()
try:
    LINKEDIN_APPLICATION_CLASSIFICATION_TIMEOUT_SECS = int(
        os.environ.get("TAEY_APPLY_CLASSIFICATION_TIMEOUT_SECS", "")
    )
except ValueError:
    LINKEDIN_APPLICATION_CLASSIFICATION_TIMEOUT_SECS = 0
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
    "read_clipboard", "focus_dialog", "scroll_to_bottom",
}
_DRIVE_MUTATIONS = {
    "click", "focus", "activate", "hover", "operate", "navigate", "type", "paste", "key", "focus_dialog",
    "scroll_to_bottom",
}
_DRIVE_ACTION_ARGUMENTS = {
    "observe": frozenset({"display", "action", "scope"}),
    "click": frozenset({"display", "action", "element"}),
    "focus": frozenset({"display", "action", "element"}),
    "activate": frozenset({"display", "action", "element"}),
    "hover": frozenset({"display", "action", "element"}),
    "operate": frozenset({"display", "action", "element"}),
    "scroll_to_bottom": frozenset({"display", "action", "element"}),
    "navigate": frozenset({"display", "action", "url"}),
    "type": frozenset({"display", "action", "text"}),
    "paste": frozenset({"display", "action", "text", "text_file"}),
    "key": frozenset({"display", "action", "key"}),
    "read_clipboard": frozenset({"display", "action", "output_file"}),
    "focus_dialog": frozenset({"display", "action"}),
}


def _do_consult_chat(arguments: dict) -> str:
    import subprocess
    import json as _json

    context = dict(_request_context.get())
    if context.get("tool_profile") != _CONSULT_CHAT_TOOL_PROFILE:
        return _json.dumps({
            "ok": False,
            "error": "consult_chat is available only in the consult-chat tool profile",
        })
    seat_id = str(context.get("seat_id") or "")
    turn_id = str(context.get("turn_id") or "")
    process_generation = str(context.get("process_generation") or "")
    if (
        not _SEAT_ID_RE.fullmatch(seat_id)
        or not _TRACE_ID_RE.fullmatch(turn_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
    ):
        return _json.dumps({
            "ok": False,
            "error": "consult_chat requires a validated active Taey turn context",
        })

    required = {
        "display",
        "prompt_file",
        "bundle_a",
        "bundle_b",
        "output_file",
        "receipt_file",
    }
    if set(arguments) != required:
        return _json.dumps({
            "ok": False,
            "error": (
                "consult_chat requires exactly "
                f"{sorted(required)}; received {sorted(arguments)}"
            ),
        })
    display = str(arguments.get("display") or "").strip()
    if display not in {":2", ":3", ":4", ":5", ":6"}:
        return _json.dumps({
            "ok": False,
            "error": "consult_chat display must be one of :2, :3, :4, :5, :6",
        })
    paths: dict[str, str] = {}
    for key in ("prompt_file", "bundle_a", "bundle_b", "output_file", "receipt_file"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.startswith("/"):
            return _json.dumps({
                "ok": False,
                "error": f"consult_chat {key} must be an absolute path",
            })
        paths[key] = value

    cmd = [
        UI_DRIVE_PYTHON,
        UI_DRIVE_SCRIPT,
        "consult",
        "--display",
        display,
        "--prompt-file",
        paths["prompt_file"],
        "--bundle-a",
        paths["bundle_a"],
        "--bundle-b",
        paths["bundle_b"],
        "--output-file",
        paths["output_file"],
        "--receipt-file",
        paths["receipt_file"],
        "--requester",
        seat_id,
        "--timeout",
        "5400",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=6000,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        _audit("consult_chat", {"display": display, "rc": "timeout"})
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": "consult_chat exceeded its 6000-second transaction ceiling",
        })
    _audit("consult_chat", {"display": display, "rc": completed.returncode})
    output = (completed.stdout or "").strip()
    try:
        payload = _json.loads(output) if output else None
    except _json.JSONDecodeError:
        payload = None
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        error = (
            str(payload.get("error") or "")
            if isinstance(payload, dict)
            else (completed.stderr or "").strip()
        )
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": error or f"consult_chat subprocess exited {completed.returncode}",
        })
    return output


def _linkedin_jobs_result_error(payload: dict, returncode: int) -> str | None:
    allowed_states = {
        "captured",
        "already_captured",
        "no_selected_job",
        "postcondition_failed",
        "technical_failure",
    }
    state = payload.get("state")
    if state not in allowed_states or not isinstance(payload.get("ok"), bool):
        return "runner returned an invalid terminal state"
    expected_ok = state in {"captured", "already_captured"}
    if payload["ok"] is not expected_ok or (returncode == 0) is not expected_ok:
        return "runner result and process status disagree"
    allowed_failure_codes_by_state = {
        "captured": {None},
        "already_captured": {None},
        "no_selected_job": {"selected_job_not_exact"},
        "postcondition_failed": {"postcondition_failed"},
        "technical_failure": {
            "deadline_expired",
            "display_lock_unavailable",
            "lock_release_indeterminate",
            "post_observation_indeterminate",
            "pre_observation_failed",
            "private_input_invalid",
            "sink_write_indeterminate",
        },
    }
    failure_code = payload.get("failure_code")
    if failure_code not in allowed_failure_codes_by_state[state]:
        return "runner returned invalid failure_code"
    records_observed = payload.get("records_observed")
    if (
        isinstance(records_observed, bool)
        or not isinstance(records_observed, int)
        or records_observed not in {0, 1}
    ):
        return "runner returned invalid records_observed"
    records_written = payload.get("records_written")
    if records_written is not None and (
        isinstance(records_written, bool)
        or not isinstance(records_written, int)
        or records_written not in {0, 1}
    ):
        return "runner returned invalid records_written"
    exact_counts = {
        "captured": (1, 1),
        "already_captured": (1, 0),
        "no_selected_job": (0, 0),
    }
    if state in exact_counts and (
        payload["records_observed"], payload["records_written"]
    ) != exact_counts[state]:
        return "runner returned counts inconsistent with its state"
    if state == "postcondition_failed" and (
        payload["records_observed"] != 1
        or payload["records_written"] not in {0, 1}
    ):
        return "runner returned counts inconsistent with its state"
    digest_pattern = r"[0-9a-f]{64}"
    content_digest = payload.get("content_digest")
    if state in {"captured", "already_captured", "postcondition_failed"}:
        if not re.fullmatch(digest_pattern, str(content_digest or "")):
            return "runner returned invalid content_digest"
    elif state == "no_selected_job" and content_digest is not None:
        return "runner returned unexpected content_digest"
    elif state == "technical_failure" and content_digest is not None and not re.fullmatch(
        digest_pattern, str(content_digest)
    ):
        return "runner returned invalid content_digest"
    if state == "technical_failure" and not (
        (
            payload["records_observed"] == 0
            and payload["records_written"] == 0
            and content_digest is None
            and failure_code != "sink_write_indeterminate"
        )
        or (
            payload["records_observed"] == 1
            and payload["records_written"] in {0, 1}
            and content_digest is not None
            and failure_code != "sink_write_indeterminate"
        )
        or (
            payload["records_observed"] == 1
            and payload["records_written"] is None
            and content_digest is not None
            and failure_code == "sink_write_indeterminate"
        )
    ):
        return "runner returned facts inconsistent with its state"
    return None


def _linkedin_job_search_result_error(payload: dict, returncode: int) -> str | None:
    allowed_failure_codes_by_state = {
        "captured": {None},
        "already_captured": {None},
        "no_cards": {None},
        "postcondition_failed": {"postcondition_failed"},
        "technical_failure": {
            "deadline_expired",
            "display_lock_unavailable",
            "lock_release_indeterminate",
            "post_observation_indeterminate",
            "pre_observation_failed",
            "private_input_invalid",
            "sink_write_indeterminate",
        },
    }
    state = payload.get("state")
    if state not in allowed_failure_codes_by_state or not isinstance(payload.get("ok"), bool):
        return "runner returned an invalid terminal state"
    expected_ok = state in {"captured", "already_captured", "no_cards"}
    if payload["ok"] is not expected_ok or (returncode == 0) is not expected_ok:
        return "runner result and process status disagree"
    if payload.get("failure_code") not in allowed_failure_codes_by_state[state]:
        return "runner returned invalid failure_code"
    batches_observed = payload.get("batches_observed")
    batches_written = payload.get("batches_written")
    cards_observed = payload.get("cards_observed")
    if (
        isinstance(batches_observed, bool)
        or not isinstance(batches_observed, int)
        or batches_observed not in {0, 1}
    ):
        return "runner returned invalid batches_observed"
    if batches_written is not None and (
        isinstance(batches_written, bool)
        or not isinstance(batches_written, int)
        or batches_written not in {0, 1}
    ):
        return "runner returned invalid batches_written"
    if (
        isinstance(cards_observed, bool)
        or not isinstance(cards_observed, int)
        or cards_observed < 0
    ):
        return "runner returned invalid cards_observed"
    content_digest = payload.get("content_digest")
    digest_present = content_digest is not None
    if digest_present and not re.fullmatch(r"[0-9a-f]{64}", str(content_digest)):
        return "runner returned invalid content_digest"
    if state == "captured" and not (
        (batches_observed, batches_written) == (1, 1)
        and cards_observed > 0
        and digest_present
    ):
        return "runner returned facts inconsistent with its state"
    if state == "already_captured" and not (
        (batches_observed, batches_written) == (1, 0)
        and cards_observed > 0
        and digest_present
    ):
        return "runner returned facts inconsistent with its state"
    if state == "no_cards" and not (
        batches_observed == 1
        and batches_written in {0, 1}
        and cards_observed == 0
        and digest_present
    ):
        return "runner returned facts inconsistent with its state"
    if state == "postcondition_failed" and not (
        batches_observed == 1
        and batches_written in {0, 1}
        and digest_present
    ):
        return "runner returned facts inconsistent with its state"
    if state == "technical_failure":
        before_observation = (
            batches_observed,
            batches_written,
            cards_observed,
            content_digest,
        ) == (0, 0, 0, None)
        after_observation = (
            batches_observed == 1
            and batches_written in {0, 1}
            and digest_present
        )
        sink_indeterminate = (
            batches_observed == 1
            and batches_written is None
            and digest_present
            and payload["failure_code"] == "sink_write_indeterminate"
        )
        failure_code = payload["failure_code"]
        valid_failure_facts = {
            "display_lock_unavailable": before_observation,
            "pre_observation_failed": before_observation,
            "private_input_invalid": before_observation,
            "post_observation_indeterminate": after_observation,
            "sink_write_indeterminate": sink_indeterminate,
            "deadline_expired": before_observation or after_observation,
            "lock_release_indeterminate": before_observation or after_observation,
        }
        if not valid_failure_facts[failure_code]:
            return "runner returned facts inconsistent with its state"
    return None


def _linkedin_engagers_result_error(payload: dict, returncode: int) -> str | None:
    allowed_failure_codes_by_state = {
        "already_known": {None},
        "captured": {None},
        "no_new_signal": {None},
        "ambiguous_signal": {"ambiguous_signal"},
        "postcondition_failed": {"postcondition_failed"},
        "sink_write_indeterminate": {"sink_write_indeterminate"},
        "technical_failure": {
            "deadline_expired",
            "display_lock_unavailable",
            "lock_release_indeterminate",
            "pre_observation_failed",
            "post_observation_indeterminate",
            "private_input_invalid",
            "navigation_not_exact",
            "action_failed",
            "restore_indeterminate",
        },
    }
    state = payload.get("state")
    if state not in allowed_failure_codes_by_state or not isinstance(payload.get("ok"), bool):
        return "runner returned an invalid terminal state"
    expected_ok = state in {"already_known", "captured", "no_new_signal"}
    if payload["ok"] is not expected_ok or (returncode == 0) is not expected_ok:
        return "runner result and process status disagree"
    if payload.get("failure_code") not in allowed_failure_codes_by_state[state]:
        return "runner returned invalid failure_code"
    records_observed = payload.get("records_observed")
    records_written = payload.get("records_written")
    if (
        isinstance(records_observed, bool)
        or not isinstance(records_observed, int)
        or records_observed < 0
    ):
        return "runner returned invalid records_observed"
    if records_written is not None and (
        isinstance(records_written, bool)
        or not isinstance(records_written, int)
        or records_written not in {0, 1}
    ):
        return "runner returned invalid records_written"
    exact_facts = {
        "already_known": (1, 0, True),
        "ambiguous_signal": (0, 0, False),
        "captured": (1, 1, True),
        "no_new_signal": (0, 0, False),
        "sink_write_indeterminate": (1, None, True),
    }
    content_digest = payload.get("content_digest")
    digest_present = content_digest is not None
    if digest_present and not re.fullmatch(
        r"[0-9a-f]{64}", str(content_digest)
    ):
        return "runner returned invalid content_digest"
    if state in exact_facts:
        if (records_observed, records_written, digest_present) != exact_facts[state]:
            return "runner returned facts inconsistent with its state"
    elif state == "postcondition_failed":
        if not (
            (records_observed, records_written, content_digest) == (0, 0, None)
            or (
                records_observed == 1
                and records_written in {0, 1}
                and digest_present
            )
        ):
            return "runner returned facts inconsistent with its state"
    else:
        before_signal = (
            records_observed,
            records_written,
            content_digest,
        ) == (0, 0, None)
        after_signal = (
            records_observed == 1
            and records_written in {0, 1}
            and digest_present
        )
        if not (before_signal or after_signal):
            return "runner returned facts inconsistent with its state"
    restore_verified = payload.get("restore_verified")
    if not isinstance(restore_verified, bool):
        return "runner returned invalid restore_verified"
    if expected_ok and restore_verified is not True:
        return "runner reported success without exact shared-tab restoration"
    if state in {
        "ambiguous_signal",
        "postcondition_failed",
        "sink_write_indeterminate",
    } and restore_verified is not False:
        return "runner returned invalid restore verdict for its state"
    return None


def _linkedin_jobs_restore_result_error(
    payload: dict,
    returncode: int,
) -> str | None:
    state = payload.get("state")
    ok = payload.get("ok")
    failure_code = payload.get("failure_code")
    if not isinstance(ok, bool) or state not in {"restored", "technical_failure"}:
        return "runner returned an invalid terminal state"
    expected_ok = state == "restored"
    expected_returncode = 0 if expected_ok else 2
    if ok is not expected_ok or returncode != expected_returncode:
        return "runner result and process status disagree"
    if expected_ok:
        if failure_code is not None:
            return "runner returned invalid failure_code"
    elif failure_code not in {
        "deadline_expired",
        "display_lock_unavailable",
        "lock_release_indeterminate",
        "private_input_invalid",
        "restore_indeterminate",
    }:
        return "runner returned invalid failure_code"
    for key in (
        "target_url_sha256",
        "firefox_pid_sha256",
        "restore_proof_sha256",
    ):
        digest = payload.get(key)
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            return f"runner returned invalid {key}"
    stable_cycles = payload.get("stable_cycles_observed")
    if (
        isinstance(stable_cycles, bool)
        or not isinstance(stable_cycles, int)
        or not 0 <= stable_cycles <= 2
    ):
        return "runner returned invalid stable_cycles_observed"
    if expected_ok and not (
        payload.get("target_url_sha256") is not None
        and payload.get("firefox_pid_sha256") is not None
        and payload.get("restore_proof_sha256") is not None
        and stable_cycles == 2
    ):
        return "runner returned facts inconsistent with its state"
    return None


def _linkedin_application_intake_result_error(
    payload: dict,
    returncode: int,
) -> str | None:
    if returncode != 0:
        return "connector result and process status disagree"
    if (
        payload.get("schema") != "taey_apply_linkedin_intake_result_v1"
        or payload.get("ok") is not True
        or payload.get("failure_code") is not None
        or payload.get("state") not in {"captured_unclassified", "already_present"}
    ):
        return "connector returned an invalid terminal state"
    if payload.get("records_observed") != 1 or isinstance(
        payload.get("records_observed"), bool
    ):
        return "connector returned invalid records_observed"
    expected_written = 1 if payload["state"] == "captured_unclassified" else 0
    if (
        payload.get("records_written") != expected_written
        or isinstance(payload.get("records_written"), bool)
    ):
        return "connector returned counts inconsistent with its state"
    for key in (
        "job_identity_sha256",
        "row_digest",
        "receipt_sha256",
        "turn_lineage_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get(key) or "")):
            return f"connector returned invalid {key}"
    return None


def _linkedin_application_classification_result_error(
    payload: dict,
    returncode: int,
) -> str | None:
    if returncode != 0:
        return "connector result and process status disagree"
    if (
        payload.get("schema")
        != "taey_apply_linkedin_classification_result_v1"
        or payload.get("operation") != "classify_frozen_linkedin_intake"
        or payload.get("ok") is not True
        or payload.get("state") != "classified"
        or payload.get("failure_code") is not None
        or payload.get("records_observed") != 1
        or isinstance(payload.get("records_observed"), bool)
        or payload.get("records_written") != 1
        or isinstance(payload.get("records_written"), bool)
        or payload.get("terminal") is not True
    ):
        return "connector returned an invalid terminal state"
    for key in (
        "transaction_sha256",
        "receipt_sha256",
        "turn_lineage_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get(key) or "")):
            return f"connector returned invalid {key}"
    return None


_LINKEDIN_JOBS_RESULT_KEYS = frozenset({
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "content_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
})
_LINKEDIN_JOB_SEARCH_RESULT_KEYS = frozenset({
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "batches_observed",
    "batches_written",
    "cards_observed",
    "content_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
})
_LINKEDIN_JOBS_RESTORE_RESULT_KEYS = frozenset({
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "target_url_sha256",
    "firefox_pid_sha256",
    "restore_proof_sha256",
    "stable_cycles_observed",
    "receipt_sha256",
    "turn_lineage_sha256",
})
_LINKEDIN_ENGAGERS_RESULT_KEYS = frozenset({
    *_LINKEDIN_JOBS_RESULT_KEYS,
    "restore_verified",
})
_LINKEDIN_APPLICATION_INTAKE_RESULT_KEYS = frozenset({
    "schema",
    "ok",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "job_identity_sha256",
    "row_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
})
_LINKEDIN_APPLICATION_CLASSIFICATION_RESULT_KEYS = frozenset({
    "schema",
    "operation",
    "ok",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "transaction_sha256",
    "receipt_sha256",
    "turn_lineage_sha256",
    "terminal",
})
_PRIVATE_TRANSACTION_TOOL_SPECS = (
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_JOBS_TOOL_PROFILE,
        tool="linkedin_jobs",
        prompt_label="LinkedIn Jobs",
        system_prompt_path=LINKEDIN_JOBS_SYSTEM_PROMPT_PATH,
        runner_name="run_linkedin_jobs.py",
        python_path=LINKEDIN_JOBS_PYTHON,
        python_env_name="TAEY_LINKEDIN_JOBS_PYTHON",
        private_root=LINKEDIN_JOBS_PRIVATE_ROOT,
        private_root_env_name="TAEY_LINKEDIN_JOBS_PRIVATE_ROOT",
        displays=LINKEDIN_JOBS_DISPLAYS,
        displays_env_name="TAEY_LINKEDIN_JOBS_DISPLAYS",
        timeout_secs=LINKEDIN_JOBS_TIMEOUT_SECS,
        timeout_env_name="TAEY_LINKEDIN_JOBS_TIMEOUT_SECS",
        deadline_secs=LINKEDIN_JOBS_DEADLINE_SECS,
        claim_schema="linkedin_jobs_claim_v1",
        terminal_reason="the one frozen LinkedIn Jobs invocation has been spent",
        expected_result_keys=_LINKEDIN_JOBS_RESULT_KEYS,
        validate_result=_linkedin_jobs_result_error,
    ),
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_JOB_SEARCH_TOOL_PROFILE,
        tool="linkedin_job_search",
        prompt_label="LinkedIn Job Search",
        system_prompt_path=LINKEDIN_JOB_SEARCH_SYSTEM_PROMPT_PATH,
        runner_name="run_linkedin_job_search.py",
        python_path=LINKEDIN_JOB_SEARCH_PYTHON,
        python_env_name="TAEY_LINKEDIN_JOB_SEARCH_PYTHON",
        private_root=LINKEDIN_JOB_SEARCH_PRIVATE_ROOT,
        private_root_env_name="TAEY_LINKEDIN_JOB_SEARCH_PRIVATE_ROOT",
        displays=LINKEDIN_JOB_SEARCH_DISPLAYS,
        displays_env_name="TAEY_LINKEDIN_JOB_SEARCH_DISPLAYS",
        timeout_secs=LINKEDIN_JOB_SEARCH_TIMEOUT_SECS,
        timeout_env_name="TAEY_LINKEDIN_JOB_SEARCH_TIMEOUT_SECS",
        deadline_secs=LINKEDIN_JOB_SEARCH_DEADLINE_SECS,
        claim_schema="linkedin_job_search_claim_v1",
        terminal_reason="the one frozen LinkedIn Job Search invocation has been spent",
        expected_result_keys=_LINKEDIN_JOB_SEARCH_RESULT_KEYS,
        validate_result=_linkedin_job_search_result_error,
    ),
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_JOBS_RESTORE_TOOL_PROFILE,
        tool="restore_linkedin_jobs_surface",
        prompt_label="LinkedIn Jobs Surface Restore",
        system_prompt_path=LINKEDIN_JOBS_RESTORE_SYSTEM_PROMPT_PATH,
        runner_name="run_linkedin_jobs_restore.py",
        python_path=LINKEDIN_JOBS_RESTORE_PYTHON,
        python_env_name="TAEY_LINKEDIN_JOBS_RESTORE_PYTHON",
        private_root=LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT,
        private_root_env_name="TAEY_LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT",
        displays=LINKEDIN_JOBS_RESTORE_DISPLAYS,
        displays_env_name="TAEY_LINKEDIN_JOBS_RESTORE_DISPLAYS",
        timeout_secs=LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS,
        timeout_env_name="TAEY_LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS",
        deadline_secs=LINKEDIN_JOBS_RESTORE_DEADLINE_SECS,
        claim_schema="linkedin_jobs_restore_claim_v1",
        terminal_reason=(
            "the one frozen LinkedIn Jobs Surface Restore invocation has been spent"
        ),
        expected_result_keys=_LINKEDIN_JOBS_RESTORE_RESULT_KEYS,
        validate_result=_linkedin_jobs_restore_result_error,
    ),
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_ENGAGERS_TOOL_PROFILE,
        tool="linkedin_engagers",
        prompt_label="LinkedIn Engagers",
        system_prompt_path=LINKEDIN_ENGAGERS_SYSTEM_PROMPT_PATH,
        runner_name="run_linkedin_jobs.py",
        python_path=LINKEDIN_ENGAGERS_PYTHON,
        python_env_name="TAEY_LINKEDIN_ENGAGERS_PYTHON",
        private_root=LINKEDIN_ENGAGERS_PRIVATE_ROOT,
        private_root_env_name="TAEY_LINKEDIN_ENGAGERS_PRIVATE_ROOT",
        displays=LINKEDIN_ENGAGERS_DISPLAYS,
        displays_env_name="TAEY_LINKEDIN_ENGAGERS_DISPLAYS",
        timeout_secs=LINKEDIN_ENGAGERS_TIMEOUT_SECS,
        timeout_env_name="TAEY_LINKEDIN_ENGAGERS_TIMEOUT_SECS",
        deadline_secs=LINKEDIN_ENGAGERS_DEADLINE_SECS,
        claim_schema="linkedin_engagers_claim_v1",
        terminal_reason="the one frozen LinkedIn Engagers invocation has been spent",
        expected_result_keys=_LINKEDIN_ENGAGERS_RESULT_KEYS,
        validate_result=_linkedin_engagers_result_error,
    ),
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_APPLICATION_INTAKE_TOOL_PROFILE,
        tool="linkedin_application_intake",
        prompt_label="LinkedIn Application Intake",
        system_prompt_path=LINKEDIN_APPLICATION_INTAKE_SYSTEM_PROMPT_PATH,
        runner_name="taey_apply.cli",
        python_path=LINKEDIN_APPLICATION_INTAKE_PYTHON,
        python_env_name="TAEY_APPLY_PYTHON",
        private_root=LINKEDIN_APPLICATION_INTAKE_PRIVATE_ROOT,
        private_root_env_name="TAEY_APPLY_PRIVATE_ROOT",
        displays=(),
        displays_env_name="",
        timeout_secs=LINKEDIN_APPLICATION_INTAKE_TIMEOUT_SECS,
        timeout_env_name="TAEY_APPLY_TIMEOUT_SECS",
        deadline_secs=0,
        claim_schema="taey_apply_linkedin_intake_claim_v1",
        terminal_reason=(
            "the one frozen LinkedIn Application Intake invocation has been spent"
        ),
        expected_result_keys=_LINKEDIN_APPLICATION_INTAKE_RESULT_KEYS,
        validate_result=_linkedin_application_intake_result_error,
        public_root=LINKEDIN_APPLICATION_INTAKE_PUBLIC_ROOT,
        public_root_env_name="TAEY_APPLY_PUBLIC_ROOT",
        database_path=LINKEDIN_APPLICATION_INTAKE_DATABASE,
        database_env_name="TAEY_APPLY_DB",
    ),
    PrivateTransactionToolSpec(
        profile=_LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE,
        tool="linkedin_application_classification",
        prompt_label="LinkedIn Application Classification",
        system_prompt_path=LINKEDIN_APPLICATION_CLASSIFICATION_SYSTEM_PROMPT_PATH,
        runner_name="taey_apply.classification_cli",
        python_path=LINKEDIN_APPLICATION_CLASSIFICATION_PYTHON,
        python_env_name="TAEY_APPLY_CLASSIFICATION_PYTHON",
        private_root=LINKEDIN_APPLICATION_CLASSIFICATION_PRIVATE_ROOT,
        private_root_env_name="TAEY_APPLY_CLASSIFICATION_PRIVATE_ROOT",
        displays=(),
        displays_env_name="",
        timeout_secs=LINKEDIN_APPLICATION_CLASSIFICATION_TIMEOUT_SECS,
        timeout_env_name="TAEY_APPLY_CLASSIFICATION_TIMEOUT_SECS",
        deadline_secs=0,
        claim_schema="taey_apply_linkedin_classification_presence_claim_v1",
        terminal_reason=(
            "the one frozen LinkedIn Application Classification invocation has "
            "been spent"
        ),
        expected_result_keys=_LINKEDIN_APPLICATION_CLASSIFICATION_RESULT_KEYS,
        validate_result=_linkedin_application_classification_result_error,
        public_root=LINKEDIN_APPLICATION_CLASSIFICATION_PUBLIC_ROOT,
        public_root_env_name="TAEY_APPLY_CLASSIFICATION_PUBLIC_ROOT",
        database_path=LINKEDIN_APPLICATION_CLASSIFICATION_DATABASE,
        database_env_name="TAEY_APPLY_CLASSIFICATION_DB",
    ),
)


def _private_transaction_spec_for_profile(
    profile: str,
) -> PrivateTransactionToolSpec | None:
    return next(
        (spec for spec in _PRIVATE_TRANSACTION_TOOL_SPECS if spec.profile == profile),
        None,
    )


def _private_transaction_spec_for_tool(tool: str) -> PrivateTransactionToolSpec:
    spec = next(
        (item for item in _PRIVATE_TRANSACTION_TOOL_SPECS if item.tool == tool),
        None,
    )
    if spec is None:
        raise RuntimeError(f"private transaction tool is not registered: {tool}")
    return spec


def _do_private_transaction(
    arguments: dict,
    spec: PrivateTransactionToolSpec,
) -> str:
    import subprocess
    import json as _json

    context = dict(_request_context.get())
    if context.get("tool_profile") != spec.profile:
        return _json.dumps({
            "ok": False,
            "error": f"{spec.tool} is available only in the {spec.profile} tool profile",
        })

    profile_state = context.get("_tool_profile_state")
    if isinstance(profile_state, dict):
        profile_state["terminal"] = {
            "tool": spec.tool,
            "reason": spec.terminal_reason,
        }
    if not isinstance(arguments, dict):
        return _json.dumps({
            "ok": False,
            "error": f"{spec.tool} arguments must be one JSON object",
        })

    seat_id = str(context.get("seat_id") or "")
    turn_id = str(context.get("turn_id") or "")
    event_id = str(context.get("event_id") or "")
    process_generation = str(context.get("process_generation") or "")
    if (
        not _SEAT_ID_RE.fullmatch(seat_id)
        or not _TRACE_ID_RE.fullmatch(turn_id)
        or not _TRACE_ID_RE.fullmatch(event_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
    ):
        return _json.dumps({
            "ok": False,
            "error": f"{spec.tool} requires a validated active Taey turn context",
        })

    required = {"display"}
    if set(arguments) != required:
        return _json.dumps({
            "ok": False,
            "error": (
                f"{spec.tool} requires exactly {sorted(required)}; "
                f"received {sorted(arguments)}"
            ),
        })

    display = str(arguments.get("display") or "").strip()
    if not spec.displays:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.displays_env_name} is unset; refusing to guess a display",
        })
    if not 130 <= spec.timeout_secs <= 1800:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.timeout_env_name} must be 130-1800",
        })
    if (
        not re.fullmatch(r":[0-9]{1,3}", display)
        or display == ":0"
        or display not in spec.displays
    ):
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} display is not in the runtime-authorized "
                f"{spec.displays_env_name} set"
            ),
        })

    if not spec.private_root:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.private_root_env_name} is unset; refusing private "
                "artifacts without one runtime-owned boundary"
            ),
        })
    private_root = Path(spec.private_root)
    try:
        resolved_private_root = private_root.resolve(strict=True)
        private_metadata = os.lstat(private_root)
    except OSError:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.private_root_env_name} is unavailable",
        })
    if (
        not private_root.is_absolute()
        or private_root != resolved_private_root
        or not stat.S_ISDIR(private_metadata.st_mode)
        or stat.S_IMODE(private_metadata.st_mode) != 0o700
        or private_metadata.st_uid != os.geteuid()
    ):
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.private_root_env_name} must be an owner-controlled "
                "nonsymlink 0700 directory"
            ),
        })
    correlation_id = str(context.get("correlation_id") or "")
    if not _TRACE_ID_RE.fullmatch(correlation_id):
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} requires a validated correlation identity",
        })
    lineage_payload = {
        "correlation_id": correlation_id,
        "process_generation": process_generation,
        "requester": seat_id,
        "turn_id": turn_id,
    }
    expected_turn_lineage = hashlib.sha256(
        _json.dumps(
            lineage_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    transaction_path = (
        resolved_private_root
        / "transactions"
        / seat_id
        / f"{correlation_id}.json"
    )
    receipt_path = (
        resolved_private_root
        / "receipts"
        / seat_id
        / f"{correlation_id}.json"
    )
    claim_path = (
        resolved_private_root
        / "claims"
        / seat_id
        / f"{correlation_id}.json"
    )
    for key, candidate in {
        "transaction_file": transaction_path,
        "receipt_file": receipt_path,
        "claim_file": claim_path,
    }.items():
        try:
            resolved_target = candidate.parent.resolve(strict=True)
            parent_metadata = os.lstat(candidate.parent)
        except OSError:
            return _json.dumps({
                "ok": False,
                "display": display,
                "error": f"{spec.tool} {key} parent is unavailable",
            })
        if (
            resolved_target != resolved_private_root
            and resolved_private_root not in resolved_target.parents
        ):
            return _json.dumps({
                "ok": False,
                "display": display,
                "error": f"{spec.tool} {key} must remain beneath the private root",
            })
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or parent_metadata.st_uid != os.geteuid()
        ):
            return _json.dumps({
                "ok": False,
                "display": display,
                "error": f"{spec.tool} {key} parent must be owner-controlled mode 0700",
            })

    if not transaction_path.is_file():
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} transaction_file is missing or not a regular file",
        })
    if receipt_path.exists():
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} receipt_file already exists",
        })

    if not TAEYS_HANDS_ROOT:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                "TAEYS_HANDS_ROOT is unset; point it at a public clone of "
                "https://github.com/palios-taey/taeys-hands"
            ),
        })
    hands_root = Path(TAEYS_HANDS_ROOT)
    if not hands_root.is_absolute() or not hands_root.is_dir():
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": "TAEYS_HANDS_ROOT must be an existing absolute directory",
        })
    runner = hands_root / "scripts" / spec.runner_name
    if not runner.is_file():
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                "public taeys-hands checkout does not contain "
                f"scripts/{spec.runner_name}"
            ),
        })
    if not spec.python_path:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.python_env_name} is unset; refusing an implicit interpreter",
        })
    transaction_python = Path(spec.python_path)
    if (
        not transaction_python.is_absolute()
        or not transaction_python.is_file()
        or not os.access(transaction_python, os.X_OK)
    ):
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.python_env_name} must be an executable absolute path",
        })

    transaction_digest = hashlib.sha256()
    transaction_descriptor = None
    transaction_valid = True
    try:
        transaction_descriptor = os.open(
            transaction_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        transaction_metadata = os.fstat(transaction_descriptor)
        if (
            not stat.S_ISREG(transaction_metadata.st_mode)
            or stat.S_IMODE(transaction_metadata.st_mode) != 0o400
            or transaction_metadata.st_uid != os.geteuid()
        ):
            raise OSError("unsafe private transaction")
        remaining = transaction_metadata.st_size
        while remaining:
            chunk = os.read(transaction_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("private transaction changed while read")
            transaction_digest.update(chunk)
            remaining -= len(chunk)
        if os.read(transaction_descriptor, 1):
            raise OSError("private transaction changed while read")
    except OSError:
        transaction_valid = False
    finally:
        if transaction_descriptor is not None:
            try:
                os.close(transaction_descriptor)
            except OSError:
                transaction_valid = False
    if not transaction_valid:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} transaction_file failed private-file validation",
        })
    expected_transaction_sha256 = transaction_digest.hexdigest()

    claim = {
        "correlation_id_sha256": hashlib.sha256(correlation_id.encode("utf-8")).hexdigest(),
        "event_id_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        "schema": spec.claim_schema,
        "seat_id": seat_id,
        "transaction_sha256": expected_transaction_sha256,
        "turn_lineage_sha256": expected_turn_lineage,
    }
    claim_bytes = _json.dumps(
        claim,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    claim_descriptor = None
    claim_status = "not_created"
    try:
        claim_descriptor = os.open(
            claim_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        claim_status = "created"
        offset = 0
        while offset < len(claim_bytes):
            written = os.write(claim_descriptor, claim_bytes[offset:])
            if written <= 0:
                raise OSError("private claim write did not advance")
            offset += written
        os.fchmod(claim_descriptor, 0o400)
        os.fsync(claim_descriptor)
    except FileExistsError:
        claim_status = "already_claimed"
    except OSError:
        if claim_status == "created":
            claim_status = "indeterminate"
    finally:
        if claim_descriptor is not None:
            try:
                os.close(claim_descriptor)
            except OSError:
                claim_status = "indeterminate"
    if claim_status == "created":
        claim_parent_descriptor = None
        try:
            claim_parent_descriptor = os.open(
                claim_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.fsync(claim_parent_descriptor)
        except OSError:
            claim_status = "indeterminate"
        finally:
            if claim_parent_descriptor is not None:
                try:
                    os.close(claim_parent_descriptor)
                except OSError:
                    claim_status = "indeterminate"
    if claim_status == "already_claimed":
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} transaction identity was already claimed",
        })
    if claim_status == "not_created":
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} claim was not created; no Hands action was admitted",
        })
    if claim_status != "created":
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} claim could not be finalized; treat this transaction "
                "identity as spent and do not retry"
            ),
        })

    cmd = [
        str(transaction_python),
        str(runner),
        "--display",
        display,
        "--private-root",
        str(resolved_private_root),
        "--transaction-file",
        str(transaction_path),
        "--expected-transaction-sha256",
        expected_transaction_sha256,
        "--receipt-file",
        str(receipt_path),
        "--requester",
        seat_id,
        "--turn-id",
        turn_id,
        "--correlation-id",
        correlation_id,
        "--process-generation",
        process_generation,
        "--deadline-seconds",
        str(spec.deadline_secs),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=spec.timeout_secs,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        _audit(spec.tool, {"display": display, "rc": "timeout"})
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} exceeded its configured transaction ceiling; "
                "do not retry this transaction identity"
            ),
        })
    except Exception as exc:
        _audit(
            spec.tool,
            {"display": display, "rc": "launch_error", "type": type(exc).__name__},
        )
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} runner could not be launched; no raw process output "
                "was admitted to model context"
            ),
        })

    _audit(spec.tool, {"display": display, "rc": completed.returncode})
    output = (completed.stdout or "").strip()
    try:
        payload = _json.loads(output) if output else None
    except _json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or frozenset(payload) != spec.expected_result_keys:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} runner returned a non-contract result; raw output "
                "was withheld from the model context"
            ),
        })
    if payload.get("display") != display or payload.get("platform") != "linkedin":
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} runner result identity does not match the request",
        })
    result_error = spec.validate_result(payload, completed.returncode)
    if result_error is not None:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} {result_error}",
        })
    if payload.get("turn_lineage_sha256") != expected_turn_lineage:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": f"{spec.tool} runner result lineage does not match the active turn",
        })
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("receipt_sha256") or "")):
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} runner did not return a durable terminal receipt; "
                "raw output was withheld from the model context"
            ),
        })
    receipt_digest = hashlib.sha256()
    receipt_descriptor = None
    receipt_valid = True
    try:
        receipt_descriptor = os.open(
            receipt_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        receipt_metadata = os.fstat(receipt_descriptor)
        if (
            not stat.S_ISREG(receipt_metadata.st_mode)
            or stat.S_IMODE(receipt_metadata.st_mode) != 0o400
            or receipt_metadata.st_uid != os.geteuid()
        ):
            raise OSError("unsafe private receipt")
        remaining = receipt_metadata.st_size
        while remaining:
            chunk = os.read(receipt_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("private receipt changed while read")
            receipt_digest.update(chunk)
            remaining -= len(chunk)
        if os.read(receipt_descriptor, 1):
            raise OSError("private receipt changed while read")
    except OSError:
        receipt_valid = False
    finally:
        if receipt_descriptor is not None:
            try:
                os.close(receipt_descriptor)
            except OSError:
                receipt_valid = False
    if not receipt_valid or receipt_digest.hexdigest() != payload["receipt_sha256"]:
        return _json.dumps({
            "ok": False,
            "display": display,
            "error": (
                f"{spec.tool} runner did not persist the exact claimed terminal "
                "receipt; raw output was withheld from model context"
            ),
        })
    return _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _do_linkedin_jobs(arguments: dict) -> str:
    return _do_private_transaction(
        arguments,
        _private_transaction_spec_for_tool("linkedin_jobs"),
    )


def _do_linkedin_job_search(arguments: dict) -> str:
    return _do_private_transaction(
        arguments,
        _private_transaction_spec_for_tool("linkedin_job_search"),
    )


def _do_linkedin_jobs_restore(arguments: dict) -> str:
    return _do_private_transaction(
        arguments,
        _private_transaction_spec_for_tool("restore_linkedin_jobs_surface"),
    )


def _do_linkedin_engagers(arguments: dict) -> str:
    return _do_private_transaction(
        arguments,
        _private_transaction_spec_for_tool("linkedin_engagers"),
    )


def _do_linkedin_application_intake(arguments: dict) -> str:
    import subprocess
    import json as _json

    spec = _private_transaction_spec_for_tool("linkedin_application_intake")

    def refuse(reason: str) -> str:
        return _json.dumps(
            {"ok": False, "error": f"{spec.tool} {reason}"},
            separators=(",", ":"),
        )

    context = dict(_request_context.get())
    if context.get("tool_profile") != spec.profile:
        return refuse(f"is available only in the {spec.profile} tool profile")
    profile_state = context.get("_tool_profile_state")
    if isinstance(profile_state, dict):
        profile_state["terminal"] = {
            "tool": spec.tool,
            "reason": spec.terminal_reason,
        }
    if not isinstance(arguments, dict) or arguments:
        return refuse("requires exactly one empty JSON object")

    seat_id = str(context.get("seat_id") or "")
    turn_id = str(context.get("turn_id") or "")
    event_id = str(context.get("event_id") or "")
    correlation_id = str(context.get("correlation_id") or "")
    process_generation = str(context.get("process_generation") or "")
    connector_id_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    if (
        not re.fullmatch(connector_id_pattern, seat_id)
        or not re.fullmatch(connector_id_pattern, turn_id)
        or not _TRACE_ID_RE.fullmatch(event_id)
        or not re.fullmatch(connector_id_pattern, correlation_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
    ):
        return refuse("requires a connector-compatible active Taey turn context")
    if not 1 <= spec.timeout_secs <= 600:
        return refuse(f"requires {spec.timeout_env_name}=1-600")
    if not spec.python_path:
        return refuse(f"requires explicit {spec.python_env_name}")
    connector_python = Path(spec.python_path)
    if (
        not connector_python.is_absolute()
        or not connector_python.is_file()
        or not os.access(connector_python, os.X_OK)
    ):
        return refuse(f"requires {spec.python_env_name} to be an executable absolute path")

    if not spec.public_root:
        return refuse(f"requires explicit {spec.public_root_env_name}")
    public_root = Path(spec.public_root)
    try:
        resolved_public_root = public_root.resolve(strict=True)
        public_metadata = os.lstat(public_root)
        source_root = (resolved_public_root / "src").resolve(strict=True)
        module_path = (source_root / "taey_apply" / "cli.py").resolve(strict=True)
        module_metadata = os.lstat(source_root / "taey_apply" / "cli.py")
    except OSError:
        return refuse(f"requires an available public {spec.public_root_env_name}")
    if (
        not public_root.is_absolute()
        or public_root != resolved_public_root
        or not stat.S_ISDIR(public_metadata.st_mode)
        or resolved_public_root not in source_root.parents
        or source_root not in module_path.parents
        or not stat.S_ISREG(module_metadata.st_mode)
    ):
        return refuse(f"requires a canonical public {spec.public_root_env_name} checkout")

    if not spec.private_root:
        return refuse(f"requires explicit {spec.private_root_env_name}")
    private_root = Path(spec.private_root)
    try:
        resolved_private_root = private_root.resolve(strict=True)
        private_metadata = os.lstat(private_root)
    except OSError:
        return refuse(f"requires an available {spec.private_root_env_name}")
    if (
        not private_root.is_absolute()
        or private_root != resolved_private_root
        or not stat.S_ISDIR(private_metadata.st_mode)
        or stat.S_IMODE(private_metadata.st_mode) != 0o700
        or private_metadata.st_uid != os.geteuid()
    ):
        return refuse(
            f"requires {spec.private_root_env_name} to be an owner-controlled "
            "nonsymlink 0700 directory"
        )

    if not spec.database_path:
        return refuse(f"requires explicit {spec.database_env_name}")
    database_path = Path(spec.database_path)
    try:
        resolved_database_path = database_path.resolve(strict=True)
        database_metadata = os.lstat(database_path)
    except OSError:
        return refuse(f"requires an available {spec.database_env_name}")
    if (
        not database_path.is_absolute()
        or database_path != resolved_database_path
        or not stat.S_ISREG(database_metadata.st_mode)
        or stat.S_IMODE(database_metadata.st_mode) != 0o600
        or database_metadata.st_uid != os.geteuid()
    ):
        return refuse(
            f"requires {spec.database_env_name} to be an owner-controlled "
            "nonsymlink 0600 regular file"
        )

    lineage_payload = {
        "correlation_id": correlation_id,
        "process_generation": process_generation,
        "requester": seat_id,
        "turn_id": turn_id,
    }
    expected_turn_lineage = hashlib.sha256(
        _json.dumps(
            lineage_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    transaction_path = (
        resolved_private_root / "transactions" / seat_id / f"{correlation_id}.json"
    )
    claim_path = (
        resolved_private_root / "claims" / seat_id / f"{correlation_id}.json"
    )
    receipt_path = (
        resolved_private_root / "receipts" / seat_id / f"{correlation_id}.json"
    )
    for label, candidate in {
        "transaction": transaction_path,
        "claim": claim_path,
        "receipt": receipt_path,
    }.items():
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
            parent_metadata = os.lstat(candidate.parent)
        except OSError:
            return refuse(f"{label} parent is unavailable")
        if (
            resolved_private_root not in resolved_parent.parents
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or parent_metadata.st_uid != os.geteuid()
        ):
            return refuse(f"{label} parent is not an owner-controlled 0700 directory")
    if receipt_path.exists():
        return refuse("terminal receipt already exists; transaction identity is spent")

    transaction_descriptor = None
    transaction_raw = b""
    transaction_valid = True
    try:
        transaction_descriptor = os.open(
            transaction_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        transaction_metadata = os.fstat(transaction_descriptor)
        if (
            not stat.S_ISREG(transaction_metadata.st_mode)
            or stat.S_IMODE(transaction_metadata.st_mode) != 0o400
            or transaction_metadata.st_uid != os.geteuid()
            or transaction_metadata.st_size > 16 * 1024 * 1024
        ):
            raise OSError("unsafe private transaction")
        remaining = transaction_metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(transaction_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("private transaction changed while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(transaction_descriptor, 1):
            raise OSError("private transaction changed while read")
        transaction_raw = b"".join(chunks)
    except OSError:
        transaction_valid = False
    finally:
        if transaction_descriptor is not None:
            try:
                os.close(transaction_descriptor)
            except OSError:
                transaction_valid = False
    if not transaction_valid:
        return refuse("transaction failed immutable private-file validation")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate transaction key")
            value[key] = item
        return value

    try:
        transaction = _json.loads(
            transaction_raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-JSON constant: {token}")
            ),
        )
        canonical_transaction = _json.dumps(
            transaction,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (UnicodeDecodeError, ValueError, TypeError):
        return refuse("transaction is not strict canonical UTF-8 JSON")
    expected_transaction_keys = {
        "schema",
        "operation",
        "search_receipt_ref",
        "search_artifact_ref",
        "selected_receipt_ref",
        "selected_artifact_ref",
        "card_digest",
    }
    if (
        not isinstance(transaction, dict)
        or set(transaction) != expected_transaction_keys
        or transaction.get("schema") != "taey_apply_linkedin_intake_private_input_v1"
        or transaction.get("operation") != "ingest_linkedin_captured_job"
        or not re.fullmatch(r"[0-9a-f]{64}", str(transaction.get("card_digest") or ""))
        or any(
            not isinstance(transaction.get(key), str) or not transaction[key]
            for key in (
                "search_receipt_ref",
                "search_artifact_ref",
                "selected_receipt_ref",
                "selected_artifact_ref",
            )
        )
        or canonical_transaction != transaction_raw
    ):
        return refuse("transaction does not match the canonical intake contract")
    expected_transaction_sha256 = hashlib.sha256(transaction_raw).hexdigest()

    claim = {
        "correlation_id_sha256": hashlib.sha256(
            correlation_id.encode("utf-8")
        ).hexdigest(),
        "event_id_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        "schema": spec.claim_schema,
        "seat_id": seat_id,
        "transaction_sha256": expected_transaction_sha256,
        "turn_lineage_sha256": expected_turn_lineage,
    }
    claim_bytes = _json.dumps(
        claim,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    claim_descriptor = None
    claim_status = "not_created"
    try:
        claim_descriptor = os.open(
            claim_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        claim_status = "created"
        offset = 0
        while offset < len(claim_bytes):
            written = os.write(claim_descriptor, claim_bytes[offset:])
            if written <= 0:
                raise OSError("claim write did not advance")
            offset += written
        os.fchmod(claim_descriptor, 0o400)
        os.fsync(claim_descriptor)
    except FileExistsError:
        claim_status = "already_claimed"
    except OSError:
        if claim_status == "created":
            claim_status = "indeterminate"
    finally:
        if claim_descriptor is not None:
            try:
                os.close(claim_descriptor)
            except OSError:
                claim_status = "indeterminate"
    if claim_status == "created":
        parent_descriptor = None
        try:
            parent_descriptor = os.open(
                claim_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.fsync(parent_descriptor)
        except OSError:
            claim_status = "indeterminate"
        finally:
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    claim_status = "indeterminate"
    if claim_status == "already_claimed":
        return refuse("transaction identity was already claimed")
    if claim_status != "created":
        return refuse("claim finalization is indeterminate; identity is spent")

    command = [
        str(connector_python),
        "-P",
        "-m",
        spec.runner_name,
        "--private-root",
        str(resolved_private_root),
        "--database",
        str(resolved_database_path),
        "--transaction-file",
        str(transaction_path),
        "--expected-transaction-sha256",
        expected_transaction_sha256,
        "--receipt-file",
        str(receipt_path),
        "--requester",
        seat_id,
        "--turn-id",
        turn_id,
        "--correlation-id",
        correlation_id,
        "--process-generation",
        process_generation,
    ]
    connector_environment = dict(os.environ)
    connector_environment["PYTHONPATH"] = str(source_root)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=spec.timeout_secs,
            env=connector_environment,
            cwd=str(resolved_public_root),
        )
    except subprocess.TimeoutExpired:
        _audit(spec.tool, {"rc": "timeout"})
        return refuse("exceeded its outer timeout; identity is spent and must not retry")
    except Exception as exc:
        _audit(spec.tool, {"rc": "launch_error", "type": type(exc).__name__})
        return refuse("connector could not be launched; raw process output was withheld")

    _audit(spec.tool, {"rc": completed.returncode})
    output = (completed.stdout or "").strip()
    try:
        payload = (
            _json.loads(
                output,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-JSON constant: {token}")
                ),
            )
            if output
            else None
        )
        canonical_output = (
            _json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if isinstance(payload, dict)
            else None
        )
    except (_json.JSONDecodeError, ValueError, TypeError):
        payload = None
        canonical_output = None
    if (
        not isinstance(payload, dict)
        or frozenset(payload) != spec.expected_result_keys
        or canonical_output != output
    ):
        return refuse("connector returned a non-contract result; raw output was withheld")
    result_error = spec.validate_result(payload, completed.returncode)
    if result_error is not None:
        return refuse(result_error)
    if payload.get("turn_lineage_sha256") != expected_turn_lineage:
        return refuse("connector result lineage does not match the active turn")

    receipt_descriptor = None
    receipt_digest = hashlib.sha256()
    receipt_valid = True
    try:
        receipt_descriptor = os.open(
            receipt_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        receipt_metadata = os.fstat(receipt_descriptor)
        if (
            not stat.S_ISREG(receipt_metadata.st_mode)
            or stat.S_IMODE(receipt_metadata.st_mode) != 0o400
            or receipt_metadata.st_uid != os.geteuid()
        ):
            raise OSError("unsafe connector receipt")
        remaining = receipt_metadata.st_size
        while remaining:
            chunk = os.read(receipt_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("connector receipt changed while read")
            receipt_digest.update(chunk)
            remaining -= len(chunk)
        if os.read(receipt_descriptor, 1):
            raise OSError("connector receipt changed while read")
    except OSError:
        receipt_valid = False
    finally:
        if receipt_descriptor is not None:
            try:
                os.close(receipt_descriptor)
            except OSError:
                receipt_valid = False
    if not receipt_valid or receipt_digest.hexdigest() != payload["receipt_sha256"]:
        return refuse("connector terminal receipt is absent, unsafe, or digest-mismatched")
    return _json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _do_linkedin_application_classification(arguments: dict) -> str:
    import subprocess
    import json as _json

    spec = _private_transaction_spec_for_tool(
        "linkedin_application_classification"
    )

    def refuse(reason: str) -> str:
        return _json.dumps(
            {"ok": False, "error": f"{spec.tool} {reason}"},
            separators=(",", ":"),
        )

    context = dict(_request_context.get())
    if context.get("tool_profile") != spec.profile:
        return refuse(f"is available only in the {spec.profile} tool profile")
    profile_state = context.get("_tool_profile_state")
    if isinstance(profile_state, dict):
        profile_state["terminal"] = {
            "tool": spec.tool,
            "reason": spec.terminal_reason,
        }
    if not isinstance(arguments, dict) or arguments:
        return refuse("requires exactly one empty JSON object")

    seat_id = str(context.get("seat_id") or "")
    turn_id = str(context.get("turn_id") or "")
    event_id = str(context.get("event_id") or "")
    correlation_id = str(context.get("correlation_id") or "")
    process_generation = str(context.get("process_generation") or "")
    connector_id_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    if (
        not re.fullmatch(connector_id_pattern, seat_id)
        or not re.fullmatch(connector_id_pattern, turn_id)
        or not _TRACE_ID_RE.fullmatch(event_id)
        or not re.fullmatch(connector_id_pattern, correlation_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
    ):
        return refuse("requires a connector-compatible active Taey turn context")
    if not 1 <= spec.timeout_secs <= 600:
        return refuse(f"requires {spec.timeout_env_name}=1-600")
    if not spec.python_path:
        return refuse(f"requires explicit {spec.python_env_name}")
    connector_python = Path(spec.python_path)
    if (
        not connector_python.is_absolute()
        or not connector_python.is_file()
        or not os.access(connector_python, os.X_OK)
    ):
        return refuse(f"requires {spec.python_env_name} to be an executable absolute path")

    if not spec.public_root:
        return refuse(f"requires explicit {spec.public_root_env_name}")
    public_root = Path(spec.public_root)
    try:
        resolved_public_root = public_root.resolve(strict=True)
        public_metadata = os.lstat(public_root)
        source_root = (resolved_public_root / "src").resolve(strict=True)
        module_path = (
            source_root / "taey_apply" / "classification_cli.py"
        ).resolve(strict=True)
        module_metadata = os.lstat(
            source_root / "taey_apply" / "classification_cli.py"
        )
    except OSError:
        return refuse(f"requires an available public {spec.public_root_env_name}")
    if (
        not public_root.is_absolute()
        or public_root != resolved_public_root
        or not stat.S_ISDIR(public_metadata.st_mode)
        or resolved_public_root not in source_root.parents
        or source_root not in module_path.parents
        or not stat.S_ISREG(module_metadata.st_mode)
    ):
        return refuse(f"requires a canonical public {spec.public_root_env_name} checkout")

    if not spec.private_root:
        return refuse(f"requires explicit {spec.private_root_env_name}")
    private_root = Path(spec.private_root)
    try:
        resolved_private_root = private_root.resolve(strict=True)
        private_metadata = os.lstat(private_root)
    except OSError:
        return refuse(f"requires an available {spec.private_root_env_name}")
    if (
        not private_root.is_absolute()
        or private_root != resolved_private_root
        or not stat.S_ISDIR(private_metadata.st_mode)
        or stat.S_IMODE(private_metadata.st_mode) != 0o700
        or private_metadata.st_uid != os.geteuid()
    ):
        return refuse(
            f"requires {spec.private_root_env_name} to be an owner-controlled "
            "nonsymlink 0700 directory"
        )

    if not spec.database_path:
        return refuse(f"requires explicit {spec.database_env_name}")
    database_path = Path(spec.database_path)
    try:
        resolved_database_path = database_path.resolve(strict=True)
        database_metadata = os.lstat(database_path)
    except OSError:
        return refuse(f"requires an available {spec.database_env_name}")
    if (
        not database_path.is_absolute()
        or database_path != resolved_database_path
        or not stat.S_ISREG(database_metadata.st_mode)
        or stat.S_IMODE(database_metadata.st_mode) != 0o600
        or database_metadata.st_uid != os.geteuid()
    ):
        return refuse(
            f"requires {spec.database_env_name} to be an owner-controlled "
            "nonsymlink 0600 regular file"
        )

    lineage_payload = {
        "correlation_id": correlation_id,
        "process_generation": process_generation,
        "requester": seat_id,
        "turn_id": turn_id,
    }
    expected_turn_lineage = hashlib.sha256(
        _json.dumps(
            lineage_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    transaction_path = (
        resolved_private_root / "transactions" / seat_id / f"{correlation_id}.json"
    )
    presence_claim_path = (
        resolved_private_root
        / "presence-claims"
        / seat_id
        / f"{correlation_id}.json"
    )
    receipt_path = (
        resolved_private_root / "receipts" / seat_id / f"{correlation_id}.json"
    )
    for label, candidate in {
        "transaction": transaction_path,
        "presence claim": presence_claim_path,
        "receipt": receipt_path,
    }.items():
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
            parent_metadata = os.lstat(candidate.parent)
        except OSError:
            return refuse(f"{label} parent is unavailable")
        if (
            resolved_private_root not in resolved_parent.parents
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or parent_metadata.st_uid != os.geteuid()
        ):
            return refuse(f"{label} parent is not an owner-controlled 0700 directory")
    if receipt_path.exists():
        return refuse("terminal receipt already exists; transaction identity is spent")

    transaction_descriptor = None
    transaction_raw = b""
    transaction_valid = True
    try:
        transaction_descriptor = os.open(
            transaction_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        transaction_metadata = os.fstat(transaction_descriptor)
        if (
            not stat.S_ISREG(transaction_metadata.st_mode)
            or stat.S_IMODE(transaction_metadata.st_mode) != 0o400
            or transaction_metadata.st_uid != os.geteuid()
            or transaction_metadata.st_size > 64 * 1024
        ):
            raise OSError("unsafe private transaction")
        remaining = transaction_metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(transaction_descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError("private transaction changed while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(transaction_descriptor, 1):
            raise OSError("private transaction changed while read")
        transaction_raw = b"".join(chunks)
    except OSError:
        transaction_valid = False
    finally:
        if transaction_descriptor is not None:
            try:
                os.close(transaction_descriptor)
            except OSError:
                transaction_valid = False
    if not transaction_valid:
        return refuse("transaction failed immutable private-file validation")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate transaction key")
            value[key] = item
        return value

    try:
        transaction = _json.loads(
            transaction_raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-JSON constant: {token}")
            ),
        )
        canonical_transaction = _json.dumps(
            transaction,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (UnicodeDecodeError, ValueError, TypeError):
        return refuse("transaction is not strict canonical UTF-8 JSON")
    expected_transaction_keys = {
        "schema",
        "operation",
        "classification_claim_ref",
        "classification_claim_sha256",
    }
    classification_claim_ref = (
        transaction.get("classification_claim_ref")
        if isinstance(transaction, dict)
        else None
    )
    classification_claim_sha256 = (
        transaction.get("classification_claim_sha256")
        if isinstance(transaction, dict)
        else None
    )
    reference_parts = (
        classification_claim_ref.split("/")
        if isinstance(classification_claim_ref, str)
        else []
    )
    if (
        not isinstance(transaction, dict)
        or set(transaction) != expected_transaction_keys
        or transaction.get("schema")
        != "taey_apply_linkedin_classification_private_input_v1"
        or transaction.get("operation") != "classify_frozen_linkedin_intake"
        or not isinstance(classification_claim_ref, str)
        or not 1 <= len(classification_claim_ref) <= 4096
        or classification_claim_ref.startswith("/")
        or "//" in classification_claim_ref
        or any(part in {"", ".", ".."} for part in reference_parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in classification_claim_ref)
        or not re.fullmatch(r"[0-9a-f]{64}", str(classification_claim_sha256 or ""))
        or canonical_transaction != transaction_raw
    ):
        return refuse("transaction does not match the canonical classification contract")
    outer_transaction_sha256 = hashlib.sha256(transaction_raw).hexdigest()
    classification_claim_path = resolved_private_root / classification_claim_ref

    presence_claim = {
        "classification_claim_sha256": classification_claim_sha256,
        "correlation_id_sha256": hashlib.sha256(
            correlation_id.encode("utf-8")
        ).hexdigest(),
        "event_id_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        "schema": spec.claim_schema,
        "seat_id": seat_id,
        "transaction_sha256": outer_transaction_sha256,
        "turn_lineage_sha256": expected_turn_lineage,
    }
    presence_claim_bytes = _json.dumps(
        presence_claim,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    claim_descriptor = None
    claim_status = "not_created"
    try:
        claim_descriptor = os.open(
            presence_claim_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        claim_status = "created"
        offset = 0
        while offset < len(presence_claim_bytes):
            written = os.write(claim_descriptor, presence_claim_bytes[offset:])
            if written <= 0:
                raise OSError("claim write did not advance")
            offset += written
        os.fchmod(claim_descriptor, 0o400)
        os.fsync(claim_descriptor)
    except FileExistsError:
        claim_status = "already_claimed"
    except OSError:
        if claim_status == "created":
            claim_status = "indeterminate"
    finally:
        if claim_descriptor is not None:
            try:
                os.close(claim_descriptor)
            except OSError:
                claim_status = "indeterminate"
    if claim_status == "created":
        parent_descriptor = None
        try:
            parent_descriptor = os.open(
                presence_claim_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.fsync(parent_descriptor)
        except OSError:
            claim_status = "indeterminate"
        finally:
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    claim_status = "indeterminate"
    if claim_status == "already_claimed":
        return refuse("transaction identity was already claimed")
    if claim_status != "created":
        return refuse("claim finalization is indeterminate; identity is spent")

    command = [
        str(connector_python),
        "-P",
        "-m",
        spec.runner_name,
        "--private-root",
        str(resolved_private_root),
        "--database",
        str(resolved_database_path),
        "--claim-file",
        str(classification_claim_path),
        "--expected-claim-sha256",
        str(classification_claim_sha256),
        "--receipt-file",
        str(receipt_path),
        "--requester",
        seat_id,
        "--turn-id",
        turn_id,
        "--correlation-id",
        correlation_id,
        "--process-generation",
        process_generation,
    ]
    connector_environment = dict(os.environ)
    connector_environment["PYTHONPATH"] = str(source_root)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=spec.timeout_secs,
            env=connector_environment,
            cwd=str(resolved_public_root),
        )
    except subprocess.TimeoutExpired:
        _audit(spec.tool, {"rc": "timeout"})
        return refuse("exceeded its outer timeout; identity is spent and must not retry")
    except Exception as exc:
        _audit(spec.tool, {"rc": "launch_error", "type": type(exc).__name__})
        return refuse("connector could not be launched; raw process output was withheld")

    _audit(spec.tool, {"rc": completed.returncode})
    output = (completed.stdout or "").strip()
    try:
        payload = (
            _json.loads(
                output,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-JSON constant: {token}")
                ),
            )
            if output
            else None
        )
        canonical_output = (
            _json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if isinstance(payload, dict)
            else None
        )
    except (_json.JSONDecodeError, ValueError, TypeError):
        payload = None
        canonical_output = None
    if (
        not isinstance(payload, dict)
        or frozenset(payload) != spec.expected_result_keys
        or canonical_output != output
    ):
        return refuse("connector returned a non-contract result; raw output was withheld")
    result_error = spec.validate_result(payload, completed.returncode)
    if result_error is not None:
        return refuse(result_error)
    if payload.get("transaction_sha256") != classification_claim_sha256:
        return refuse("connector result does not match the frozen transaction")
    if payload.get("turn_lineage_sha256") != expected_turn_lineage:
        return refuse("connector result lineage does not match the active turn")

    receipt_descriptor = None
    receipt_digest = hashlib.sha256()
    receipt_valid = True
    try:
        receipt_descriptor = os.open(
            receipt_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        receipt_metadata = os.fstat(receipt_descriptor)
        if (
            not stat.S_ISREG(receipt_metadata.st_mode)
            or stat.S_IMODE(receipt_metadata.st_mode) != 0o400
            or receipt_metadata.st_uid != os.geteuid()
        ):
            raise OSError("unsafe connector receipt")
        remaining = receipt_metadata.st_size
        while remaining:
            chunk = os.read(receipt_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("connector receipt changed while read")
            receipt_digest.update(chunk)
            remaining -= len(chunk)
        if os.read(receipt_descriptor, 1):
            raise OSError("connector receipt changed while read")
    except OSError:
        receipt_valid = False
    finally:
        if receipt_descriptor is not None:
            try:
                os.close(receipt_descriptor)
            except OSError:
                receipt_valid = False
    if not receipt_valid or receipt_digest.hexdigest() != payload["receipt_sha256"]:
        return refuse("connector terminal receipt is absent, unsafe, or digest-mismatched")
    return _json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


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
# Contract: register only after a fresh canonical observation maps the YAML
# Stop key and the exact observing turn owns the display lease. Pre-Send tree
# scans can dismiss transient browser menus, so ordinary drive actions never
# create a completion route. TTL is the crash-recovery backstop.
# ---------------------------------------------------------------------------
_MONITOR_TTL_SECS = int(os.environ.get("TAEY_CONSULT_MONITOR_TTL", "10800"))
def _monitor_touch(
    display: str,
    platform: str,
    action: str,
    result: dict,
    request_context: dict,
) -> dict | None:
    """Register one completion route after a canonical Stop-proven send."""
    if action != "observe" or result.get("surface") != "browser":
        return None
    stop_keys = result.get("stop_keys")
    mapped = result.get("mapped")
    if not isinstance(stop_keys, list) or not isinstance(mapped, list):
        return None
    observed_stop_keys = sorted({
        str(item.get("element"))
        for item in mapped
        if isinstance(item, dict) and item.get("element") in stop_keys
    })
    if not observed_stop_keys:
        return None
    lease = result.get("lease")
    if not isinstance(lease, dict) or lease.get("owned") is not True:
        raise RuntimeError("stop observation is not owned by this Taey turn")

    client = _mira_redis or _redis
    if client is None:
        raise RuntimeError("Redis is unavailable")
    actor_seat_id = str(request_context.get("seat_id") or "")
    turn_id = str(request_context.get("turn_id") or "")
    process_generation = str(request_context.get("process_generation") or "")
    if not actor_seat_id or not turn_id or not process_generation:
        raise RuntimeError("request-local turn identity is incomplete")

    requester = "taey"
    monitor_id = f"{actor_seat_id}-{display.lstrip(':')}-{turn_id}"
    session_key = f"taey:{actor_seat_id}:active_session:{monitor_id}"
    set_key = f"taey:{actor_seat_id}:active_session_ids"
    now = time.time()
    record = {
        "monitor_id": monitor_id,
        "display": display,
        "platform": platform,
        "requester": requester,
        "actor_seat_id": actor_seat_id,
        "mode": "supervised_manual",
        "phase": "awaiting_completion",
        "stop_proven": True,
        "stop_keys": stop_keys,
        "observed_stop_keys": observed_stop_keys,
        "snapshot_revision": str(result.get("snapshot_revision") or ""),
        "url": str(result.get("current_url") or ""),
        "turn_id": turn_id,
        "process_generation": process_generation,
        "timeout": _MONITOR_TTL_SECS,
        "started_ts": now,
        "last_seen": now,
        "last_action": "stop_proven_observe",
    }
    client.set(session_key, json.dumps(record), ex=_MONITOR_TTL_SECS)
    client.sadd(set_key, session_key)
    log.info(
        "consult completion monitor registered %s display=%s stop_keys=%s",
        monitor_id,
        display,
        observed_stop_keys,
    )
    return {
        "state": "registered",
        "monitor_id": monitor_id,
        "requester": requester,
        "observed_stop_keys": observed_stop_keys,
    }


def _read_revenue_ui_private_json(
    path: Path, root: Path, label: str, maximum: int,
    modes: frozenset[int], beneath: bool,
) -> tuple[dict, bytes]:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_parent = path.parent.resolve(strict=True)
        parent = os.lstat(path.parent)
    except OSError as exc:
        raise RuntimeError(f"revenue comment {label} is unavailable") from exc
    if (not path.is_absolute() or path != resolved_path or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid() or (beneath and root not in resolved_parent.parents)
            or (beneath and stat.S_IMODE(parent.st_mode) != 0o700)):
        raise RuntimeError(f"revenue comment {label} parent is not owner-controlled")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) not in modes
                or before.st_uid != os.geteuid() or not 0 < before.st_size <= maximum):
            raise RuntimeError(f"revenue comment {label} is not an exact private file")
        raw = b"".join(iter(lambda: os.read(descriptor, 1024 * 1024), b""))
        after = os.fstat(descriptor)
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if len(raw) != before.st_size or signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"revenue comment {label} changed while read")
    except OSError as exc:
        raise RuntimeError(f"revenue comment {label} failed private-file validation") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    def exact(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len(pairs) != len({key for key, _ in pairs}):
            raise ValueError(f"duplicate {label} key")
        return dict(pairs)
    try:
        value = json.loads(raw.decode(), object_pairs_hook=exact)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"revenue comment {label} is not exact UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"revenue comment {label} is not a JSON object")
    return value, raw


def _resolve_revenue_ui_private_comment(context: dict) -> dict[str, object]:
    if not REVENUE_UI_PRIVATE_ROOT:
        raise RuntimeError("TAEY_REVENUE_UI_PRIVATE_ROOT is unset")
    seat_id = str(context.get("seat_id") or "")
    event_id = str(context.get("event_id") or "")
    correlation_id = str(context.get("correlation_id") or "")
    if (not _SEAT_ID_RE.fullmatch(seat_id) or not _TRACE_ID_RE.fullmatch(event_id)
            or not _TRACE_ID_RE.fullmatch(correlation_id)):
        raise RuntimeError("revenue comment identities are invalid")
    private_root = Path(REVENUE_UI_PRIVATE_ROOT)
    try:
        resolved_root = private_root.resolve(strict=True)
        metadata = os.lstat(private_root)
    except OSError as exc:
        raise RuntimeError("revenue comment private root is unavailable") from exc
    if (not private_root.is_absolute() or private_root != resolved_root
            or not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()):
        raise RuntimeError("revenue comment private root is not an owner-controlled 0700 directory")
    transaction_path = resolved_root / "transactions" / seat_id / f"{correlation_id}.json"
    transaction, transaction_bytes = _read_revenue_ui_private_json(
        transaction_path, resolved_root, "transaction", 4 * 1024 * 1024,
        frozenset({0o400}), True,
    )
    keys = {"schema", "operation", "platform", "display", "seat_id", "event_id",
            "correlation_id", "action_id", "selected_activity", "selected_post_body_sha256",
            "gate_receipt_path", "gate_receipt_sha256", "gate_receipt_version",
            "gate_receipt_kind", "source_artifact_sha256", "like_authorized",
            "expected_author_name", "text", "text_sha256"}
    if set(transaction) != keys:
        raise RuntimeError("revenue comment transaction has an invalid exact schema")
    expected = {"schema": "taey_revenue_ui_private_comment_v1", "operation": "comment",
                "platform": "linkedin", "seat_id": seat_id, "event_id": event_id,
                "correlation_id": correlation_id, "gate_receipt_version": "linkedin_gate_signoff_v1",
                "gate_receipt_kind": "feed_comment"}
    if any(transaction.get(key) != value for key, value in expected.items()):
        raise RuntimeError("revenue comment transaction does not match its active identity")
    display = transaction.get("display")
    action_id = transaction.get("action_id")
    selected_activity = transaction.get("selected_activity")
    selected_post_body_sha256 = transaction.get("selected_post_body_sha256")
    expected_author_name = transaction.get("expected_author_name")
    gate_receipt_path_value = transaction.get("gate_receipt_path")
    gate_receipt_sha256 = transaction.get("gate_receipt_sha256")
    source_artifact_sha256 = transaction.get("source_artifact_sha256")
    like_authorized = transaction.get("like_authorized")
    if (not isinstance(display, str) or not re.fullmatch(r":[1-9][0-9]*", display)
        or not isinstance(selected_activity, str) or not selected_activity.isdigit()
        or not isinstance(action_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", action_id)
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (selected_post_body_sha256, gate_receipt_sha256, source_artifact_sha256))
        or not isinstance(like_authorized, bool)
        or not isinstance(gate_receipt_path_value, str) or not gate_receipt_path_value
        or not isinstance(expected_author_name, str)
        or not expected_author_name or expected_author_name != expected_author_name.strip()
        or len(expected_author_name) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in expected_author_name)):
        raise RuntimeError("revenue comment target or approval identity is invalid")
    text = transaction.get("text")
    text_sha256 = transaction.get("text_sha256")
    if not isinstance(text, str) or not text or "\x00" in text or len(text.encode()) > 1024 * 1024:
        raise RuntimeError("revenue comment transaction text is invalid")
    text_bytes = text.encode()
    if (not isinstance(text_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", text_sha256)
            or hashlib.sha256(text_bytes).hexdigest() != text_sha256):
        raise RuntimeError("revenue comment transaction text hash is not exact")
    gate_receipt_path = Path(gate_receipt_path_value)
    gate_receipt, gate_bytes = _read_revenue_ui_private_json(
        gate_receipt_path, resolved_root, "gate receipt", 1024 * 1024,
        frozenset({0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o660, 0o664}), False,
    )
    if hashlib.sha256(gate_bytes).hexdigest() != gate_receipt_sha256:
        raise RuntimeError("revenue comment gate receipt hash is not exact")
    required = {"action_id", "claims_traced", "content_hash", "failing_gate", "gates", "kind",
                "packet_kind", "receipt_path", "receipt_version", "source_activity_id",
                "source_artifact_sha256", "text_hash", "verdict"}
    normalized = hashlib.sha256(re.sub(r"\s+", " ", text.strip()).encode()).hexdigest()
    gate_expected = {"receipt_version": "linkedin_gate_signoff_v1", "packet_kind": "comment",
                     "kind": "feed_comment", "verdict": "signoff", "failing_gate": None,
                     "claims_traced": True, "receipt_path": str(gate_receipt_path),
                     "action_id": action_id, "source_activity_id": selected_activity,
                     "source_artifact_sha256": source_artifact_sha256,
                     "text_hash": normalized, "content_hash": normalized}
    rows = gate_receipt.get("gates")
    if (not required.issubset(gate_receipt)
            or any(gate_receipt.get(key) != value for key, value in gate_expected.items())
            or not isinstance(rows, list) or not rows
            or any(not isinstance(row, dict) or row.get("passed") is not True for row in rows)):
        raise RuntimeError("revenue comment gate receipt is not an exact signoff")
    return {**transaction, "text_bytes": text_bytes, "text_chars": len(text),
            "transaction_sha256": hashlib.sha256(transaction_bytes).hexdigest(),
            "expected_author_name_sha256": hashlib.sha256(expected_author_name.encode()).hexdigest()}


def _do_ui_action(arguments: dict) -> str:
    import subprocess
    import json as _json

    display = str(arguments.get("display", "")).strip()
    action = str(arguments.get("action", "")).strip()
    context = _request_context.get()
    seat_id = str(context.get("seat_id") or "")
    process_generation = str(context.get("process_generation") or "")
    turn_id = str(context.get("turn_id") or "")
    tool_round = context.get("tool_round")
    sequence = context.get("_revenue_ui_sequence")

    def terminal_refusal(message: str) -> str:
        terminal = sequence.get("terminal") if isinstance(sequence, dict) else None
        if not isinstance(terminal, dict):
            terminal = {
                "display": display,
                "action": action,
                "tool_round": tool_round,
                "reason": message,
            }
            if isinstance(sequence, dict):
                sequence["terminal"] = terminal
                observations = sequence.get("observations")
                if isinstance(observations, dict):
                    observations.clear()
        profile_state = context.get("_tool_profile_state")
        if isinstance(profile_state, dict) and not isinstance(
            profile_state.get("terminal"), dict
        ):
            profile_state["terminal"] = {
                "tool": "ui_action",
                "reason": terminal["reason"],
            }
        return _json.dumps({
            "ok": False,
            "action": action,
            "display": display,
            "result": None,
            "error": message,
            "ui_sequence": {
                "state": "terminal_refusal",
                "first_failure": terminal,
                "instruction": (
                    "Stop this attempt; report the first failure and do not retry "
                    "UI mutations in this turn."
                ),
            },
        })

    if context.get("tool_profile") != _REVENUE_UI_TOOL_PROFILE:
        return terminal_refusal("ui_action requires the revenue-ui tool profile")
    if (
        not _SEAT_ID_RE.fullmatch(seat_id)
        or not re.fullmatch(r"[0-9a-f]{32}", process_generation)
        or not _TRACE_ID_RE.fullmatch(turn_id)
        or not isinstance(tool_round, int)
        or tool_round < 1
    ):
        return terminal_refusal(
            "ui_action requires a validated active Taey turn and positive tool round"
        )
    if not isinstance(sequence, dict):
        return terminal_refusal("invalid request-local revenue UI state")
    observations = sequence.get("observations")
    if not isinstance(observations, dict):
        return terminal_refusal("invalid request-local revenue UI observations")
    if isinstance(sequence.get("terminal"), dict):
        return terminal_refusal(
            "a prior ui_action failure ended this turn; all later UI calls are refused"
        )
    if isinstance(sequence.get("terminal_delivery"), dict):
        return terminal_refusal(
            "a prior comment delivery ended this turn; all later UI calls are refused"
        )
    platform = _UI_ACTION_BINDINGS.get(display)
    if not platform:
        return terminal_refusal(
            f"display {display!r} is not bound by TAEY_UI_ACTION_BINDINGS"
        )
    if action not in {"observe", "scroll_into_view", "activate", "paste"}:
        return terminal_refusal(
            f"unknown action {action!r}; revenue-ui currently permits observe, "
            "scroll_into_view, activate, or paste"
        )
    allowed_arguments = (
        {"display", "action"}
        if action == "observe"
        else {"display", "action", "element"}
    )
    unexpected = sorted(set(arguments) - allowed_arguments)
    if unexpected:
        return terminal_refusal(
            f"{action} received unsupported argument(s) {unexpected}"
        )

    ref = ""
    consumed_revision = ""
    expected_primitive = ""
    expected_max_text_chars: int | None = None
    card: dict[str, object] | None = None
    private_comment: dict[str, object] | None = None
    private_semantic_input_bytes: bytes | None = None
    if action in {"scroll_into_view", "activate", "paste"}:
        observed = observations.pop(display, None)
        if not isinstance(observed, dict):
            return terminal_refusal(
                f"{action} requires an explicit fresh observe on this display"
            )
        observed_round = observed.get("tool_round")
        if not isinstance(observed_round, int) or observed_round >= tool_round:
            return terminal_refusal(
                f"{action} requires an observe result seen in an earlier model round"
            )
        if observed.get("platform") != platform:
            return terminal_refusal(
                "preceding observation does not match the trusted platform binding"
            )
        element = arguments.get("element")
        if not isinstance(element, str) or not element:
            return terminal_refusal(
                f"{action} requires one mapped element from the preceding observe"
            )
        cards = observed.get("canonical_cards")
        card = cards.get(element) if isinstance(cards, dict) else None
        if not isinstance(card, dict):
            return terminal_refusal(
                f"preceding observe did not map exactly one canonical {element!r} target"
            )
        ref = str(card.get("ref") or "")
        expected_primitive = str(card.get("method") or "")
        if action == "scroll_into_view" and expected_primitive != "scroll_into_view":
            return terminal_refusal(
                "scroll_into_view requires a YAML-declared scroll_into_view primitive"
            )
        if action == "activate" and expected_primitive not in {
            "activate",
            "mapped_pointer_activate",
            "activate_optional_like",
            "submit_frozen_comment",
        }:
            return terminal_refusal(
                "activate requires a YAML-declared page activation primitive"
            )
        if action == "paste" and expected_primitive != "paste_frozen_text":
            return terminal_refusal(
                "paste requires the YAML-declared paste_frozen_text primitive"
            )
        if action == "paste":
            expected_max_text_chars = card.get("max_text_chars")
            if (
                isinstance(expected_max_text_chars, bool)
                or not isinstance(expected_max_text_chars, int)
                or expected_max_text_chars <= 0
            ):
                return terminal_refusal(
                    "paste requires one positive fresh YAML-owned max_text_chars"
                )
        consumed_revision = str(observed.get("snapshot_revision") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", consumed_revision):
            return terminal_refusal(
                "preceding observe did not provide a valid snapshot revision"
            )
        if action == "paste" or expected_primitive in SEMANTIC_OUTWARD:
            spent_key = {
                "paste_frozen_text": "paste_spent",
                "activate_optional_like": "like_spent",
                "submit_frozen_comment": "submit_spent",
            }[expected_primitive]
            if sequence.get(spent_key) is not None:
                return terminal_refusal(
                    f"the revenue {spent_key.removesuffix('_spent')} transaction "
                    "was already spent in this turn"
                )
            try:
                private_comment = _resolve_revenue_ui_private_comment(context)
            except Exception as exc:
                return terminal_refusal(str(exc))
            if (
                private_comment.get("display") != display
                or private_comment.get("selected_activity")
                != card.get("selected_activity")
                or private_comment.get("selected_post_body_sha256")
                != card.get("selected_post_body_sha256")
            ):
                return terminal_refusal(
                    "private comment manifest does not match the fresh display/activity/body"
                )
            if sequence.setdefault("comment_binding", private_comment["transaction_sha256"]) != private_comment["transaction_sha256"]:
                return terminal_refusal("private comment manifest changed within the active turn")
            private_text_chars = private_comment.get("text_chars")
            if (
                isinstance(private_text_chars, bool)
                or not isinstance(private_text_chars, int)
                or private_text_chars < 1
            ):
                return terminal_refusal(
                    "private comment transaction returned no exact positive text length"
                )
            if action == "paste" and private_text_chars > expected_max_text_chars:
                return terminal_refusal(
                    "private paste text exceeds the fresh YAML-owned max_text_chars"
                )
            if expected_primitive == "activate_optional_like" and private_comment.get("like_authorized") is not True:
                return terminal_refusal("optional Like is not authorized by the private manifest")
            if expected_primitive == "submit_frozen_comment" and (
                sequence.get("paste_spent") != private_comment.get("transaction_sha256")
                or card.get("draft_sha256") != private_comment.get("text_sha256")
            ):
                return terminal_refusal(
                    "submit requires the same spent manifest and exact pasted draft hash"
                )
            if expected_primitive in SEMANTIC_OUTWARD:
                private_semantic_input_bytes = canonical_json_bytes(
                    semantic_input(private_comment, card)
                )
            sequence[spent_key] = private_comment["transaction_sha256"]

    lease_owner = f"taey-drive:{seat_id}:{process_generation}"
    drive_env = dict(os.environ)
    drive_env.update({
        "TAEY_UI_DRIVE_PLATFORM": platform,
        "TAEY_DRIVE_LEASE_OWNER": lease_owner,
        "TAEY_DRIVE_LEASE_SEAT": seat_id,
        "TAEY_DRIVE_LEASE_TURN": turn_id,
        "TAEY_DRIVE_LEASE_GENERATION": process_generation,
        "TAEY_DRIVE_GENERATION_FENCE_KEY": _DRIVE_GENERATION_FENCE_KEY,
    })
    subcommand = {
        "observe": "ui-observe",
        "scroll_into_view": "ui-scroll-into-view",
        "activate": "ui-activate",
        "paste": "ui-paste",
    }[action]
    command = [
        UI_DRIVE_PYTHON,
        UI_DRIVE_SCRIPT,
        subcommand,
        "--display",
        display,
    ]
    if action in {"scroll_into_view", "activate", "paste"}:
        assert isinstance(card, dict)
        command.extend(["--ref", ref])
        command.extend(["--operation-card-sha256", str(card["card_sha256"])])
    if action == "paste":
        assert isinstance(private_comment, dict)
        assert isinstance(expected_max_text_chars, int)
        command.extend(["--text-sha256", str(private_comment["text_sha256"])])
        command.extend(["--max-text-chars", str(expected_max_text_chars)])
    if action == "activate" and expected_primitive in SEMANTIC_OUTWARD:
        assert isinstance(private_semantic_input_bytes, bytes)
        command.extend([
            "--private-semantic-input-sha256",
            hashlib.sha256(private_semantic_input_bytes).hexdigest(),
        ])

    try:
        private_stdin = action == "paste" or (
            action == "activate" and expected_primitive in SEMANTIC_OUTWARD
        )
        if private_stdin:
            assert isinstance(private_comment, dict)
            completed = subprocess.run(
                command,
                input=(
                    private_comment["text_bytes"]
                    if action == "paste"
                    else private_semantic_input_bytes
                ),
                capture_output=True,
                timeout=90,
                env=drive_env,
            )
        else:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                env=drive_env,
            )
    except subprocess.TimeoutExpired:
        _audit("ui_action", {
            "display": display,
            "platform": platform,
            "action": action,
            "rc": "timeout",
        })
        timeout_message = "ui_action timed out after 90s"
        if expected_primitive in SEMANTIC_OUTWARD:
            timeout_message = f"SIDE_EFFECT_UNCERTAIN: {timeout_message}"
        return terminal_refusal(timeout_message)
    except Exception as exc:
        _audit("ui_action", {
            "display": display,
            "platform": platform,
            "action": action,
            "error": str(exc)[:200],
        })
        return terminal_refusal(f"{type(exc).__name__}: {exc}")

    _audit("ui_action", {
        "display": display,
        "platform": platform,
        "action": action,
        "rc": completed.returncode,
    })
    binary_subprocess = action == "paste" or (
        action == "activate" and expected_primitive in SEMANTIC_OUTWARD
    )
    stdout_value = (
        completed.stdout or b""
        if binary_subprocess
        else completed.stdout or ""
    )
    stderr_value = (
        completed.stderr or b""
        if binary_subprocess
        else completed.stderr or ""
    )
    stdout = (
        stdout_value.decode("utf-8", errors="replace")
        if isinstance(stdout_value, bytes)
        else stdout_value
    ).strip()
    try:
        payload = _json.loads(stdout) if stdout else None
    except Exception:
        payload = None
    succeeded = (
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("ok") is True
        and payload.get("display") == display
        and payload.get("platform") == platform
        and isinstance(payload.get("result"), dict)
    )
    if not succeeded:
        stderr_excerpt = (
            stderr_value.decode("utf-8", errors="replace")
            if isinstance(stderr_value, bytes)
            else stderr_value
        ).strip()[:1000]
        if isinstance(payload, dict):
            detail = str(payload.get("error") or "").strip()
            if stderr_excerpt:
                detail = "; ".join(
                    part for part in (detail, f"ui_drive_stderr={stderr_excerpt}") if part
                )
        else:
            detail = (
                f"ui_drive exit={completed.returncode}; stderr={stderr_excerpt}"
            )
        detail = detail or "ui_action failed"
        if expected_primitive in SEMANTIC_OUTWARD and not detail.startswith(
            "SIDE_EFFECT_UNCERTAIN:"
        ):
            detail = f"SIDE_EFFECT_UNCERTAIN: {detail}"
        return terminal_refusal(detail)

    result = payload["result"]
    if action == "observe":
        revision = str(result.get("snapshot_revision") or "")
        mapped = result.get("mapped")
        if not re.fullmatch(r"[0-9a-f]{64}", revision) or not isinstance(mapped, list):
            return terminal_refusal(
                "ui_action observe returned no valid revision-bound mapped list"
            )
        cards_by_element: dict[str, list[dict[str, object]]] = {}
        for item in mapped:
            if not isinstance(item, dict):
                continue
            element = item.get("element")
            item_ref = item.get("ref")
            public_card = item.get("operation_card")
            if not (
                isinstance(element, str)
                and isinstance(item_ref, str)
                and isinstance(public_card, dict)
            ):
                continue
            try:
                card = validate_operation_card(public_card)
            except ValueError:
                continue
            if card.get("element") == element and card.get("ref") == item_ref:
                cards_by_element.setdefault(element, []).append(card)
        canonical_cards = {
            element: cards[0]
            for element, cards in cards_by_element.items()
            if len(cards) == 1
        }
        observations[display] = {
            "platform": platform,
            "snapshot_revision": revision,
            "tool_round": tool_round,
            "canonical_cards": canonical_cards,
        }
        payload["ui_sequence"] = {
            "state": "observed",
            "snapshot_revision": revision,
            "tool_round": tool_round,
            "mapped_actions": sorted(canonical_cards),
            "mutation_token_issued": bool(canonical_cards),
        }
        return _json.dumps(payload)

    postcondition = result.get("post_action_observation")
    if action == "paste":
        assert isinstance(private_comment, dict)
        assert isinstance(card, dict)
        assert isinstance(expected_max_text_chars, int)
        expected_text_sha256 = str(private_comment["text_sha256"])
        evidence = result.get("postcondition_evidence")
        if (
            result.get("performed") is not True
            or result.get("performed_primitive") != "paste_frozen_text"
            or result.get("performed_operation") != "paste_frozen_text"
            or result.get("effect_class") != "draft"
            or result.get("text_sha256") != expected_text_sha256
            or result.get("consumed_max_text_chars") != expected_max_text_chars
            or result.get("observe_required_before_next_mutation") is not True
            or not isinstance(postcondition, dict)
            or not isinstance(evidence, dict)
        ):
            return terminal_refusal(
                "ui_action paste returned no exact editor text postcondition receipt"
            )
        try:
            validate_operation_evidence(
                card=card, manifest=private_comment, precondition=None,
                postcondition=evidence, precondition_sha256=None,
                postcondition_sha256=result.get("postcondition_sha256"),
            )
        except Exception as exc:
            return terminal_refusal(f"ui_action paste evidence refused: {exc}")
        payload["ui_sequence"] = {
            "state": "draft_transition_complete",
            "consumed_snapshot_revision": consumed_revision,
            "text_sha256": expected_text_sha256,
            "consumed_max_text_chars": expected_max_text_chars,
            "selected_activity": card["selected_activity"],
            "selected_post_body_sha256": card["selected_post_body_sha256"],
            "manifest_sha256": private_comment["transaction_sha256"],
            "gate_receipt_sha256": private_comment["gate_receipt_sha256"],
            "postcondition": postcondition,
            "observe_required_before_next_mutation": True,
            "mutation_token_issued": False,
        }
        return _json.dumps(payload)

    if action == "scroll_into_view":
        if (
            result.get("performed") is not True
            or result.get("performed_primitive") != "scroll_into_view"
            or result.get("effect_class") != "viewport"
            or result.get("observe_required_before_next_mutation") is not True
            or not isinstance(postcondition, dict)
            or postcondition.get("route_exact") is not True
            or postcondition.get("element_key_exact") is not True
            or postcondition.get("activity_exact") is not True
            or postcondition.get("body_sha256_exact") is not True
            or postcondition.get("live_extent_in_viewport") is not True
        ):
            return terminal_refusal(
                "ui_action scroll_into_view returned no exact same-element "
                "in-viewport postcondition receipt"
            )
        payload["ui_sequence"] = {
            "state": "viewport_transition_complete",
            "consumed_snapshot_revision": consumed_revision,
            "postcondition": postcondition,
            "observe_required_before_next_mutation": True,
            "mutation_token_issued": False,
        }
        return _json.dumps(payload)

    if expected_primitive in SEMANTIC_OUTWARD:
        assert isinstance(private_comment, dict)
        assert isinstance(card, dict)
        try:
            receipt = validate_semantic_receipt(
                result.get("semantic_receipt"),
                card=card,
                manifest=private_comment,
            )
        except Exception as exc:
            return terminal_refusal(
                f"SIDE_EFFECT_UNCERTAIN: semantic receipt refused: {exc}"
            )
        submit = expected_primitive == "submit_frozen_comment"
        if submit:
            sequence["terminal_delivery"] = receipt
        payload["ui_sequence"] = {
            "state": (
                "terminal_delivery_verified"
                if submit
                else "optional_like_transition_complete"
            ),
            "consumed_snapshot_revision": consumed_revision,
            "semantic_receipt": receipt,
            "gate_receipt_sha256": private_comment["gate_receipt_sha256"],
            "next_mutation_authorized": not submit,
            "terminal_delivery_verified": submit,
            "observe_required_before_next_mutation": not submit,
            "mutation_token_issued": False,
        }
        return _json.dumps(payload)

    if (
        result.get("performed") is not True
        or result.get("performed_primitive") != expected_primitive
        or result.get("effect_class") != "page"
        or result.get("observe_required_before_next_mutation") is not True
        or not isinstance(postcondition, dict)
        or postcondition.get("route_exact") is not True
    ):
        return terminal_refusal(
            "ui_action activate returned no exact page postcondition receipt"
        )
    payload["ui_sequence"] = {
        "state": "mutation_complete",
        "consumed_snapshot_revision": consumed_revision,
        "postcondition": postcondition,
        "observe_required_before_next_mutation": True,
        "mutation_token_issued": False,
    }
    return _json.dumps(payload)


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
    expected_scope = ""
    expected_key_precondition = ""
    native_dialog_revision = ""
    observed = None
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
                expected_scope = str(observed.get("snapshot_scope") or "")
                if expected_scope not in {"base", "menu_snapshot", "app_root_snapshot"}:
                    return _terminal_refusal(
                        "preceding observe did not provide a valid browser snapshot scope"
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
            return _terminal_refusal("invalid expected UI surface; refusing")
        cmd += ["--surface", expected_surface]
        if expected_surface == "browser":
            cmd += ["--scope", scope]
    if output_file is not None:
        cmd += ["--output-file", output_file]
    if action in ("click", "focus", "activate", "hover", "operate", "scroll_to_bottom"):
        element = arguments.get("element")
        if not isinstance(element, str) or not element:
            return _argument_refusal(
                f"{action} element must be a non-empty string"
            )
        canonical_refs = (
            observed.get("canonical_refs") if isinstance(observed, dict) else None
        )
        ref = (
            canonical_refs.get(element)
            if isinstance(canonical_refs, dict)
            else None
        )
        if not isinstance(ref, str) or not ref:
            return _terminal_refusal(
                f"preceding observe did not map exactly one canonical {element!r} target"
            )
        cmd += ["--ref", ref]
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
        elif action == "type":
            cmd += [
                "--expected-revision",
                expected_revision,
                "--expected-scope",
                expected_scope,
            ]
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
            cmd += [
                "--expected-revision",
                expected_revision,
                "--expected-scope",
                expected_scope,
            ]
            key_preconditions = (
                observed.get("key_preconditions")
                if isinstance(observed, dict)
                else None
            )
            if isinstance(key_preconditions, dict):
                token = key_preconditions.get(str(key))
                if token is not None:
                    if not isinstance(token, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", token
                    ):
                        return _terminal_refusal(
                            "preceding observe carried an invalid semantic "
                            "key-precondition token"
                        )
                    expected_key_precondition = token
                    cmd += [
                        "--expected-key-precondition",
                        expected_key_precondition,
                    ]
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
                result = payload.get("result") or {}
                if action == "observe":
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
                        try:
                            monitor_receipt = _monitor_touch(
                                display,
                                payload["platform"],
                                action,
                                result,
                                context,
                            )
                        except Exception as exc:
                            return _terminal_refusal(
                                "Stop-proven send could not register its completion "
                                f"monitor: {type(exc).__name__}: {exc}"
                            )
                        if monitor_receipt is not None:
                            result["completion_monitor"] = monitor_receipt
                        canonical_refs = {}
                        mapped = result.get("mapped")
                        key_preconditions = result.get("key_preconditions") or {}
                        if observed_surface == "browser":
                            if not isinstance(mapped, list):
                                return _terminal_refusal(
                                    "browser observe returned no canonical mapped element list"
                                )
                            if not isinstance(key_preconditions, dict) or any(
                                not isinstance(key, str)
                                or not key
                                or key != key.strip()
                                or not isinstance(token, str)
                                or not re.fullmatch(r"[0-9a-f]{64}", token)
                                for key, token in key_preconditions.items()
                            ):
                                return _terminal_refusal(
                                    "browser observe returned an invalid semantic "
                                    "key-precondition mapping"
                                )
                            refs_by_element = {}
                            for item in mapped:
                                if not isinstance(item, dict):
                                    continue
                                element = item.get("element")
                                ref = item.get("ref")
                                if not isinstance(element, str) or not isinstance(ref, str):
                                    continue
                                refs_by_element.setdefault(element, []).append(ref)
                            canonical_refs = {
                                element: refs[0]
                                for element, refs in refs_by_element.items()
                                if len(refs) == 1
                            }
                            for item in mapped:
                                if isinstance(item, dict):
                                    item.pop("ref", None)
                        observations[display] = {
                            "surface": observed_surface,
                            "snapshot_revision": revision,
                            "snapshot_scope": str(result.get("scope") or ""),
                            "tool_round": tool_round,
                            "canonical_refs": canonical_refs,
                            "key_preconditions": dict(key_preconditions),
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
                            if action == "key"
                            and str(arguments.get("key")) in {"Return", "Escape"}
                            else "native_dialog"
                        )
                    payload["ui_sequence"] = {
                        "state": "mutation_complete",
                        "observe_required_before_next_mutation": True,
                    }
                    if expected_revision:
                        payload["ui_sequence"]["consumed_snapshot_scope"] = expected_scope
                        payload["ui_sequence"]["consumed_snapshot_revision"] = expected_revision
                    if expected_key_precondition:
                        payload["ui_sequence"][
                            "consumed_key_precondition_sha256"
                        ] = expected_key_precondition
                    if native_dialog_revision:
                        payload["ui_sequence"]["consumed_snapshot_scope"] = "native_dialog"
                        payload["ui_sequence"]["consumed_snapshot_revision"] = native_dialog_revision
                        payload["ui_sequence"]["expected_next_surface"] = expected_surfaces[display]
                return _json.dumps(payload)
            stderr_excerpt = (r.stderr or "").strip()[:1000]
            if isinstance(payload, dict):
                detail_parts = [str(payload.get("error") or "").strip()]
                if stderr_excerpt:
                    detail_parts.append(f"ui_drive_stderr={stderr_excerpt}")
                detail = "; ".join(part for part in detail_parts if part)
            else:
                detail = f"ui_drive exit={r.returncode}; stderr={stderr_excerpt}"
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
    elseif ARGV[2] ~= '' and ARGV[3] ~= '' then
        local ok, decoded = pcall(cjson.decode, context or '')
        if not ok or type(decoded) ~= 'table' then
            reason = 'invalid_context'
        elseif tostring(decoded['proxy_namespace'] or '') == ARGV[2]
            and tostring(decoded['process_generation'] or '') ~= ARGV[3] then
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
        proxy_namespace=TAEY_DEFAULT_SEAT,
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
        "proxy_namespace": turn.proxy_namespace,
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
    current_process_identity: Optional[tuple[str, str]] = None,
) -> tuple[int, int, int]:
    if _redis is None:
        raise LivenessUnavailable("Redis client is unavailable")
    now = time.time()
    proxy_namespace, process_generation = current_process_identity or ("", "")
    result = _redis.eval(
        _RECONCILE_LIVENESS_LUA,
        len(_liveness_keys(seat_id)),
        *_liveness_keys(seat_id),
        now,
        proxy_namespace,
        process_generation,
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
    current_process_identity: Optional[tuple[str, str]] = None,
) -> tuple[int, int, list[str]]:
    registered_seats = _registered_seat_ids()
    recovered_count = 0
    global_count = 0
    for seat_id in registered_seats:
        _, recovered, global_count = _reconcile_liveness(
            seat_id,
            current_process_identity=current_process_identity,
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
    turn_payload["_revenue_ui_sequence"] = {
        "observations": {},
        "terminal": None,
    }
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


def _tool_arguments_or_terminal(
    raw_arguments: object,
    *,
    tool: str,
    turn: TurnContext,
    round_num: int,
) -> dict:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate tool argument key")
            value[key] = item
        return value

    try:
        if isinstance(raw_arguments, dict):
            encoded = json.dumps(
                raw_arguments,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            arguments = json.loads(encoded)
            if arguments != raw_arguments:
                raise ValueError("tool arguments are not JSON-shaped")
        elif isinstance(raw_arguments, str) and raw_arguments:
            arguments = json.loads(
                raw_arguments,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-JSON constant: {token}")
                ),
            )
        else:
            raise ValueError("tool arguments are absent or not JSON")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must decode to an object")
        return arguments
    except (json.JSONDecodeError, TypeError, ValueError):
        profile_state = _request_context.get().get("_tool_profile_state")
        if isinstance(profile_state, dict):
            profile_state["terminal"] = {
                "tool": tool,
                "reason": "malformed tool arguments refused before execution",
            }
        _audit(
            "tool_arguments_invalid",
            {
                "round": round_num,
                "seat_id": turn.seat_id,
                "tool": tool,
                "tool_profile": turn.tool_profile,
                "turn_id": turn.turn_id,
            },
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"{tool}_tool_arguments_invalid",
                "turn_id": turn.turn_id,
            },
        )


async def _chat_completions_for_turn(
    body: dict,
    turn: TurnContext,
    liveness_registered: bool,
):
    body.pop("max_rounds", None)
    one_shot_spec = _private_transaction_spec_for_profile(turn.tool_profile)

    # Strip model field -- let vLLM use its loaded model
    body.pop("model", None)
    if turn.tool_profile == _MANUAL_CHAT_UI_TOOL_PROFILE:
        messages = [
            message
            for message in body.get("messages", [])
            if message.get("role") != "system"
        ]
        body["messages"] = [
            {"role": "system", "content": _manual_chat_ui_system_prompt},
            *messages,
        ]
    elif turn.tool_profile == _REVENUE_UI_TOOL_PROFILE:
        messages = [
            message
            for message in body.get("messages", [])
            if message.get("role") != "system"
        ]
        body["messages"] = [
            {"role": "system", "content": _revenue_ui_system_prompt},
            *messages,
        ]
    elif turn.tool_profile == _CONSULT_CHAT_TOOL_PROFILE:
        messages = [
            message
            for message in body.get("messages", [])
            if message.get("role") != "system"
        ]
        body["messages"] = [
            {"role": "system", "content": _consult_chat_system_prompt},
            *messages,
        ]
    elif one_shot_spec is not None:
        messages = [
            message
            for message in body.get("messages", [])
            if message.get("role") != "system"
        ]
        body["messages"] = [
            {
                "role": "system",
                "content": _one_shot_system_prompts[one_shot_spec.profile],
            },
            *messages,
        ]
    else:
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
            if one_shot_spec is not None and (
                len(tool_calls) != 1
                or (tool_calls[0].get("function") or {}).get("name")
                != one_shot_spec.tool
            ):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": f"{one_shot_spec.tool}_one_shot_tool_call_required",
                        "turn_id": turn.turn_id,
                    },
                )
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
            if one_shot_spec is not None:
                tc = tool_calls[0]
                func = tc.get("function", {}) or {}
                arguments = _tool_arguments_or_terminal(
                    func.get("arguments"),
                    tool=one_shot_spec.tool,
                    turn=turn,
                    round_num=rounds,
                )
                resolved_answer = await execute_tool_call_async(
                    one_shot_spec.tool,
                    arguments,
                    tool_call_id=tc.get("id", ""),
                    round_num=rounds,
                )
                if not resolved_answer:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": f"{one_shot_spec.tool}_terminal_result_missing",
                            "turn_id": turn.turn_id,
                        },
                    )
                resolved_thinking = ""
                break
            body["messages"].append(message)
            for tc in tool_calls:
                func = tc.get("function", {}) or {}
                arguments = _tool_arguments_or_terminal(
                    func.get("arguments"),
                    tool=func.get("name", ""),
                    turn=turn,
                    round_num=rounds,
                )
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

                if one_shot_spec is not None and (
                    len(tool_calls) != 1
                    or (tool_calls[0].get("function") or {}).get("name")
                    != one_shot_spec.tool
                ):
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": f"{one_shot_spec.tool}_one_shot_tool_call_required",
                            "turn_id": turn.turn_id,
                        },
                    )

                if not tool_calls or (
                    one_shot_spec is None
                    and finish_reason != "tool_calls"
                ):
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

                if one_shot_spec is not None:
                    tc = tool_calls[0]
                    func = tc.get("function", {}) or {}
                    arguments = _tool_arguments_or_terminal(
                        func.get("arguments"),
                        tool=one_shot_spec.tool,
                        turn=turn,
                        round_num=round_num,
                    )
                    tool_result = await execute_tool_call_async(
                        one_shot_spec.tool,
                        arguments,
                        tool_call_id=tc.get("id", ""),
                        round_num=round_num,
                    )
                    if not tool_result:
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "error": f"{one_shot_spec.tool}_terminal_result_missing",
                                "turn_id": turn.turn_id,
                            },
                        )
                    result = dict(result)
                    result["choices"] = [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": tool_result,
                        },
                        "finish_reason": "stop",
                    }]
                    break

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
                    arguments = _tool_arguments_or_terminal(
                        func.get("arguments"),
                        tool=name,
                        turn=turn,
                        round_num=round_num,
                    )

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
