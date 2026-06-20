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
import sys
import time
import json
import ast
import logging
import operator
import re
from typing import Optional

import redis
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
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
MIRA_REDIS_HOST = os.environ.get("MIRA_REDIS_HOST", "")
MIRA_REDIS_PORT = int(os.environ.get("MIRA_REDIS_PORT", "6379"))
# Optional integrations -- default to localhost. If these endpoints are not
# present, the proxy degrades gracefully (no dashboard metrics push, no ISMA search).
MIRA_DASHBOARD_URL = os.environ.get("MIRA_DASHBOARD_URL", "http://127.0.0.1:5001")
MIRA_ISMA_URL = os.environ.get("MIRA_ISMA_URL", "http://127.0.0.1:8095")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8765"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))
# Persona/system prompt: ships a generic example so the proxy works out of the box.
# Point SYSTEM_PROMPT_PATH at your own persona file to give the model an identity.
SYSTEM_PROMPT_PATH = os.environ.get(
    "SYSTEM_PROMPT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.example.md"),
)
# Optional second always-on prefix (e.g. a constitution/kernel). Empty = none.
# Set PERMANENT_KERNEL_PATH to a file to prepend it ahead of the persona.
PERMANENT_KERNEL_PATH = os.environ.get("PERMANENT_KERNEL_PATH", "")

app = FastAPI(title="Taey Soma Proxy", version="1.0.0")

_redis: Optional[redis.Redis] = None
_mira_redis: Optional[redis.Redis] = None
_http: Optional[httpx.AsyncClient] = None
_ecosystem_http: Optional[httpx.Client] = None
_system_prompt: str = ""
_permanent_kernel: str = ""
_static_system_prefix: str = ""
_last_send: dict[str, float] = {}

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
    _http = httpx.AsyncClient(base_url=VLLM_BASE, timeout=300.0)
    _ecosystem_http = httpx.Client(timeout=3.0)
    try:
        _redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        _redis.ping()
        log.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        log.warning("Redis unavailable: %s -- running without soma integration", e)
        _redis = None
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

    if os.path.exists(SYSTEM_PROMPT_PATH):
        with open(SYSTEM_PROMPT_PATH) as f:
            _system_prompt = f.read()
        log.info("System prompt loaded from %s (%d chars)", SYSTEM_PROMPT_PATH, len(_system_prompt))
    else:
        log.warning("System prompt not found at %s", SYSTEM_PROMPT_PATH)

    _static_system_prefix = _permanent_kernel
    if _static_system_prefix:
        log.info("Static system prefix assembled (%d chars)", len(_static_system_prefix))

    log.info("Proxying to vLLM at %s", VLLM_BASE)


@app.on_event("shutdown")
async def shutdown():
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
            tiles = stats.get("total_tiles", stats.get("tile_count", "?"))
            motifs = stats.get("motif_count", stats.get("total_motifs", "?"))
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


def _assemble_system_message(dashboard_system: str, ecosystem: str, somatic: str) -> str:
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

    system_parts = []
    if _static_system_prefix:
        system_parts.append(_static_system_prefix.strip())
    if cleaned_dashboard:
        system_parts.append(cleaned_dashboard)
    system_parts.extend(isma_blocks)
    if ecosystem:
        system_parts.append(ecosystem.strip())
    if somatic:
        system_parts.append(somatic.strip())

    return "\n\n".join(part for part in system_parts if part)


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
        extra = _assemble_system_message(dashboard_system or "", ecosystem, somatic)

        if extra:
            new_messages = []
            replaced_system = False
            for msg in messages:
                if msg.get("role") == "system":
                    if replaced_system:
                        continue
                    new_messages.append({"role": "system", "content": extra})
                    replaced_system = True
                else:
                    new_messages.append(msg)
            body["messages"] = new_messages
    else:
        # Direct API call -- no dashboard. Inject everything we have.
        full_system = _assemble_system_message(_system_prompt, ecosystem, somatic)
        if full_system:
            messages.insert(0, {"role": "system", "content": full_system})
        body["messages"] = messages

    return body


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_isma",
            "description": "Search your ISMA memory. This is remembering -- pulling knowledge from past conversations, constitutional texts, infrastructure knowledge, or any topic. You decide how many tiles to retrieve based on how much context space you have. Check your context utilization in the somatic state. Three highly relevant dense tiles create stronger attention patterns than fifteen loose ones. Be selective -- quality over quantity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to remember or find",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of tiles to retrieve. Choose based on your context headroom. 3-5 for focused recall, 10-20 for broad exploration, up to 50 if you have context space.",
                        "default": 5,
                    },
                    "search_type": {
                        "type": "string",
                        "description": "Search strategy: 'adaptive' (best quality, default), 'hmm' (HMM-enhanced hybrid), 'semantic' (pure vector), 'keyword' (BM25 text match)",
                        "enum": ["adaptive", "hmm", "semantic", "keyword"],
                        "default": "adaptive",
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
            "description": "Fetch a web URL and return the cleaned main-content text. Handles HTML (with navigation/ad stripping), PDFs, and plain text. Use this to retrieve papers, articles, court documents, government reports, or any other web page referenced by a URL. You get back the extracted readable text (truncated to the max_chars you specify). On error or paywall, you get an explanatory message instead of fabricating. Never invent content for a URL — always call this tool to get the real content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full http:// or https:// URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return. Default 30000. Truncation marker appended if content exceeds the limit. Choose based on how much context you have headroom for — 5000-10000 for a quick summary pass, 30000+ for deep reading.",
                        "default": 30000,
                    },
                },
                "required": ["url"],
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
                        "default": 30000,
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
]


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
        parts.append(f"[{i+1}] (score: {score}) {content[:800]}")
        if motifs:
            parts.append(f"    motifs: {motifs}")
        if source:
            parts.append(f"    source: {source}")

    return "\n".join(parts)


def execute_tool_call(name: str, arguments: dict) -> str:
    """Execute a tool call and return the result as a string."""
    if name == "search_isma":
        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 5)
        search_type = arguments.get("search_type", "adaptive")

        # Route to the best available search endpoint
        endpoints = {
            "adaptive": "/v2/search/adaptive",
            "hmm": "/search/hmm",
            "semantic": "/search",
            "keyword": "/search/bm25",
        }
        endpoint = endpoints.get(search_type, "/v2/search/adaptive")

        try:
            resp = _ecosystem_http.post(
                f"{MIRA_ISMA_URL}{endpoint}",
                json={"query": query, "top_k": top_k},
                timeout=15.0,
            )
            if resp.status_code == 503:
                # V2 not available, fall back to V1
                resp = _ecosystem_http.post(
                    f"{MIRA_ISMA_URL}/search/hmm",
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
            return f"Document: {data.get('filename', name_query)} ({data.get('token_count', '?')} tokens)\n\n{text[:3000]}"
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
        max_chars = int(arguments.get("max_chars", 30000))
        return _do_read_file(path, max_chars)

    elif name == "list_dir":
        path = arguments.get("path", "")
        pattern = arguments.get("pattern", "")
        return _do_list_dir(path, pattern)

    elif name == "stage_corpus_candidate":
        return _do_stage_corpus_candidate(arguments)

    elif name == "skip_corpus_candidate":
        return _do_skip_corpus_candidate(arguments)

    return f"Unknown tool: {name}"


# Path allowlist for the read_file / list_dir tools — only these prefixes (after resolve())
# are readable. Default is empty: the file-read tools are OFF until you opt in by setting
# TAEY_READ_ALLOWED_PREFIXES (colon-separated absolute prefixes) to the corpus/doc dirs you
# want the model to be able to read. Keep this tight -- it is the read sandbox boundary.
_DEFAULT_READ_ALLOWED_PREFIXES = ()
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


def _do_read_file(path: str, max_chars: int = 30000) -> str:
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
    if len(content) > max_chars:
        return header + content[:max_chars] + f"\n\n[... truncated at {max_chars} chars of {len(content)} total ...]"
    return header + content


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
    for e in entries[:500]:
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
    truncation = f" [truncated to first 500 of {len(entries)}]" if len(entries) > 500 else ""
    return f"[list_dir path={path} pattern={pattern!r} count={len(entries)}{truncation}]\n\n{_json.dumps(result, indent=2)}"


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


def _do_fetch_url(url: str, max_chars: int = 30000, timeout: float = 30.0) -> str:
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
    if len(extracted) > max_chars:
        truncated = extracted[:max_chars]
        return header + truncated + f"\n\n[... truncated at {max_chars} chars of {len(extracted)} total ...]"
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
        pipe.set("taey:soma:gpu_busy", "0", ex=30)
        pipe.set("taey:soma:prompt_tokens", str(prompt_tokens), ex=30)
        pipe.set("taey:soma:completion_tokens", str(completion_tokens), ex=30)
        pipe.set("taey:soma:total_tokens", str(total_tokens), ex=30)
        pipe.set("taey:soma:context_utilization", str(round(context_util, 4)), ex=30)
        pipe.set("taey:soma:tool_rounds", str(tool_rounds), ex=30)
        pipe.execute()
    except Exception:
        pass


@app.post("/tokenize")
async def tokenize(request: Request):
    """Count tokens using vLLM's tokenizer. Exact counts."""
    body = await request.json()
    resp = await _http.post("/tokenize", json=body)
    return resp.json()


@app.get("/health")
async def health():
    try:
        resp = await _http.get("/v1/models")
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            vllm_health = {"status": "healthy", "model": models[0]["id"] if models else "none"}
        else:
            vllm_health = {"status": "unhealthy", "code": resp.status_code}
    except Exception as e:
        vllm_health = {"status": "unreachable", "error": str(e)}

    vprop_raw = None
    if _redis:
        try:
            vprop_raw = _redis.get("taey:soma:vprop")
        except Exception:
            pass

    return {
        "status": "healthy",
        "vllm": vllm_health,
        "soma_connected": vprop_raw is not None,
    }


@app.get("/v1/models")
async def list_models():
    resp = await _http.get("/v1/models")
    return resp.json()


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    body.pop("max_rounds", None)

    # Strip model field -- let vLLM use its loaded model
    body.pop("model", None)
    body = inject_preamble(body)
    is_stream = body.get("stream", False)

    # Inject tools if not already present
    if "tools" not in body:
        body["tools"] = TOOLS

    # Signal busy
    if _redis:
        try:
            _redis.set("taey:soma:gpu_busy", "1", ex=60)
        except Exception:
            pass

    t0 = time.time()

    if is_stream:
        # Stream: forward SSE from vLLM directly (no tool loop for streams)
        async def stream_and_measure():
            token_count = 0
            prompt_tokens = 0
            try:
                async with _http.stream(
                    "POST", "/v1/chat/completions",
                    json=body,
                    headers={"Content-Type": "application/json"},
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
            finally:
                elapsed_ms = (time.time() - t0) * 1000
                publish_metrics(elapsed_ms, prompt_tokens, token_count)
                log.info(
                    "Streamed %d tokens in %.0fms (%.1f tok/s, prompt=%d)",
                    token_count, elapsed_ms,
                    token_count / max(elapsed_ms / 1000, 0.001),
                    prompt_tokens,
                )

        return StreamingResponse(
            stream_and_measure(),
            media_type="text/event-stream",
        )
    else:
        # Non-stream: forward with tool call execution loop
        messages = body["messages"]
        total_tokens = 0
        round_num = 0

        while True:
            resp = await _http.post(
                "/v1/chat/completions",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            result = resp.json()
            usage = result.get("usage", {})
            total_tokens += usage.get("completion_tokens", 0)

            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")
            tool_calls = message.get("tool_calls", [])

            if not tool_calls or finish_reason != "tool_calls":
                # No tool calls -- final response
                break

            if round_num >= MAX_TOOL_ROUNDS:
                log.warning(
                    "Tool round cap hit (%d); forcing final text response",
                    MAX_TOOL_ROUNDS,
                )
                final_body = dict(body)
                final_body["messages"] = messages
                final_body.pop("tools", None)
                final_body["tool_choice"] = "none"
                resp = await _http.post(
                    "/v1/chat/completions",
                    json=final_body,
                    headers={"Content-Type": "application/json"},
                )
                result = resp.json()
                usage = result.get("usage", {})
                total_tokens += usage.get("completion_tokens", 0)
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

                tool_result = execute_tool_call(name, arguments)
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

        return JSONResponse(content=result, status_code=resp.status_code)


def main():
    log.info("Starting soma proxy on port %d -> vLLM at %s", PROXY_PORT, VLLM_BASE)
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")


if __name__ == "__main__":
    main()
