"""Taey Dashboard — Conversational Presence.

Dynamic emoji face (model-chosen), chat with full tool access, memory search,
worker status, and a prediction WebSocket for partial-input thought prediction.
"""
import os
import fcntl
import json
import asyncio
import logging
import re
import time
import uuid
from pathlib import Path

import redis
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dashboard.native_council import (
    CouncilTransportFailure,
    NativeCouncilTransport,
    resolve_active_council_seats,
)

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency for documented `.env` launches
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

log = logging.getLogger("dashboard")

app = FastAPI(title="Taey Dashboard", version="3.1")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
THOR_REDIS_HOST = os.environ.get("THOR_REDIS_HOST", "localhost")
THOR_REDIS_PORT = int(os.environ.get("THOR_REDIS_PORT", "6379"))
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1/chat/completions")
MODEL = os.environ.get("MODEL", "")
THOR_PROXY = os.environ.get("THOR_PROXY", "")
# Serving node this dashboard reads runtime stats off (ssh target, e.g. user@host).
# NO routable default on purpose: an unset value must read as "unconfigured", never as some
# operator's machine. TAEY_NODE2_SSH is accepted as the fleet.env.example spelling.
SERVE_NODE_SSH = os.environ.get("TAEY_SERVE_NODE_SSH") or os.environ.get("TAEY_NODE2_SSH", "")
SERVE_UNIT = os.environ.get("TAEY_SERVE_UNIT", "taey-ep3.service")
# Orchestrator API — where the work-items panel reads active projects from.
ORCH_URL = os.environ.get("ORCH_URL", "http://127.0.0.1:5002").rstrip("/")
THOR_RAW = os.environ.get("THOR_RAW", "")
ISMA_URL = os.environ.get("ISMA_URL", "http://localhost:8095").rstrip("/")
ISMA_SEARCH_URL = f"{ISMA_URL}/v2/search/adaptive"


def _chat_base_from_vllm_url(vllm_url: str) -> str:
    if not vllm_url:
        return ""
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if vllm_url.endswith(suffix):
            return vllm_url[: -len(suffix)]
    return vllm_url.rstrip("/")


CHAT_BASE = _chat_base_from_vllm_url(VLLM_URL)
THOR_PROXY = THOR_PROXY or CHAT_BASE
THOR_RAW = THOR_RAW or CHAT_BASE

_redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
try:
    _thor_redis = redis.Redis(host=THOR_REDIS_HOST, port=THOR_REDIS_PORT, decode_responses=True)
    _thor_redis.ping()
except Exception:
    _thor_redis = None
_http = httpx.AsyncClient(timeout=300.0)
TAEY_SESSIONS_DIR = os.path.expanduser(
    os.environ.get(
        "TAEY_SESSIONS_DIR",
        str(os.path.join(os.path.expanduser("~"), "taey_sessions")),
    )
)
TAEY_CONVERSATION_ID = os.environ.get("TAEY_CONVERSATION_ID", "main")
TAEY_SESSION_MAX_TURNS = max(
    1,
    int(os.environ.get("TAEY_SESSION_MAX_TURNS", "60")),
)
TAEY_COUNCIL_WAVE_TIMEOUT = max(
    1.0,
    float(os.environ.get("TAEY_COUNCIL_WAVE_TIMEOUT", "1800")),
)
TAEY_COUNCIL_POLL_INTERVAL = max(
    0.05,
    float(os.environ.get("TAEY_COUNCIL_POLL_INTERVAL", "0.25")),
)
TAEY_COUNCIL_LOG_DIR = Path(
    os.environ.get(
        "TAEY_COUNCIL_LOG_DIR",
        os.path.join(TAEY_SESSIONS_DIR, "council"),
    )
).expanduser()
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_COUNCIL_PROMPT_OPTOUT_RE = re.compile(
    r"^\s*(?:/no-council(?=$|[\s:;,—-])|"
    r"\[council:off\](?=$|[\s:;,—-])|"
    r"(?:please\s+)?(?:do\s+not|don't)\s+use\s+"
    r"(?:the\s+)?(?:council|dcm)\b)[\s:;,—-]*",
    re.IGNORECASE,
)
_native_council = NativeCouncilTransport(
    _redis,
    Path(TAEY_SESSIONS_DIR).expanduser(),
    council_log_dir=TAEY_COUNCIL_LOG_DIR,
    wave_timeout=TAEY_COUNCIL_WAVE_TIMEOUT,
    poll_interval=TAEY_COUNCIL_POLL_INTERVAL,
    seats=resolve_active_council_seats(),
)


def _council_prompt_opt_out(message: str) -> tuple[bool, str]:
    match = _COUNCIL_PROMPT_OPTOUT_RE.match(message)
    if match is None:
        return False, message
    stripped = message[match.end() :].strip()
    return True, stripped or message

DASH_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(DASH_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _session_file(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(str(session_id)):
        raise HTTPException(status_code=400, detail="invalid session id")
    return os.path.join(TAEY_SESSIONS_DIR, f"{session_id}.jsonl")


def _require_private_session_directory() -> None:
    os.makedirs(TAEY_SESSIONS_DIR, mode=0o700, exist_ok=True)
    if os.path.islink(TAEY_SESSIONS_DIR):
        raise HTTPException(
            status_code=500,
            detail="conversation directory cannot be a symlink",
        )
    if os.stat(TAEY_SESSIONS_DIR).st_mode & 0o077:
        raise HTTPException(
            status_code=500,
            detail="conversation directory is group/world accessible",
        )


def _open_private_session_log(path: str, flags: int) -> int:
    try:
        descriptor = os.open(
            path,
            flags | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="conversation log cannot be opened securely",
        ) from exc
    if os.fstat(descriptor).st_mode & 0o077:
        os.close(descriptor)
        raise HTTPException(
            status_code=500,
            detail="conversation log is group/world accessible",
        )
    return descriptor


def _read_session_events(session_id: str) -> list[dict]:
    path = _session_file(session_id)
    _require_private_session_directory()
    if not os.path.exists(path):
        return []
    events = []
    descriptor = _open_private_session_log(path, os.O_RDONLY)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise HTTPException(
                    status_code=500,
                    detail=f"partial conversation record at line {line_number}",
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"invalid conversation record at line {line_number}",
                ) from exc
            if not isinstance(event, dict):
                raise HTTPException(
                    status_code=500,
                    detail=f"non-object conversation record at line {line_number}",
                )
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# CANONICAL TRANSCRIPT EVENTS
#
# Two writers produced the same event type under two schemas: the seat wrote
# `context_content`, this dashboard wrote `content`. Measured on the live
# transcript: 13 UI ingress events, 0 with context_content, 13 with content --
# so every reader following the seat's schema saw an empty prompt body,
# including for the operator's own production assignment. Nothing was lost; it
# was unreadable, which is worse because it looks like absence.
#
# These constructors are the single place either field is set. New call sites go
# through them so a third schema cannot appear by accident.
#
# LIFECYCLE JOIN KEY: `event_id` identifies one prompt-to-outcome lifecycle and
# is carried unchanged from ingress through attempt to every outcome, terminal
# or not. `attempt_id` distinguishes retries within that lifecycle. Joining on
# anything else (source, timestamp proximity) is guesswork.
# ---------------------------------------------------------------------------


def _ingress_event(*, event_id, correlation_id, source, kind, body, **extra):
    """One canonical ingress shape. `content` and `context_content` are the same
    bytes because readers of both schemas must agree; they are written here and
    nowhere else."""
    text = "" if body is None else str(body)
    event = {
        "event_type": "executive_ingress",
        "event_id": event_id,
        "correlation_id": correlation_id,
        "source": source,
        "source_id": extra.pop("source_id", event_id),
        "kind": kind,
        "role": extra.pop("role", "user"),
        "content": text,
        "context_content": text,
    }
    event.update(extra)
    return event


def _turn_attempt_event(*, event_id, correlation_id, attempt_id, source, kind, prompt, **extra):
    """A turn that records only its outcome cannot be observed while it runs:
    no start, so no duration, and an in-flight turn is indistinguishable from no
    turn at all. Observed 2026-08-16 -- the transcript showed no turn opened
    since 21:43 while work ran continuously from 21:55."""
    event = {
        "event_type": "turn_attempt",
        "event_id": event_id,
        "correlation_id": correlation_id,
        "attempt_id": attempt_id,
        "source": source,
        "kind": kind,
        "prompt": "" if prompt is None else str(prompt),
    }
    event.update(extra)
    return event


def _ui_model_history(
    events: list[dict],
    *,
    current_event_id: str | None = None,
) -> list[dict[str, str]]:
    """Build model history from completed UI lifecycles, plus the current prompt.

    Fleet and seat outcomes share the transcript for provenance, but they are not
    turns in the operator's UI conversation. Selecting bare role/content rows
    admitted fleet assistant replies without their context-only ingress rows,
    producing consecutive assistant messages and an invented conversation.
    """
    completed: dict[str, str] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        if (
            event_id
            and event.get("event_type") == "turn_outcome"
            and event.get("source") == "taey"
            and event.get("kind") == "assistant_reply"
            and event.get("role") == "assistant"
            and event.get("ok") is True
            and isinstance(event.get("content"), str)
            and event.get("content")
        ):
            completed[event_id] = str(event["content"])

    history: list[dict[str, str]] = []
    seen_ingress: set[str] = set()
    for event in events:
        event_id = str(event.get("event_id") or "")
        if (
            not event_id
            or event_id in seen_ingress
            or event.get("event_type") != "executive_ingress"
            or event.get("source") != "ui"
            or event.get("kind") != "user_prompt"
            or event.get("role") != "user"
            or not isinstance(event.get("content"), str)
            or not event.get("content")
        ):
            continue
        seen_ingress.add(event_id)
        if event_id != current_event_id and event_id not in completed:
            continue
        history.append({"role": "user", "content": str(event["content"])})
        if event_id in completed:
            history.append(
                {"role": "assistant", "content": completed[event_id]}
            )
    return history


def _append_session_event(session_id: str, event: dict) -> None:
    path = _session_file(session_id)
    _require_private_session_directory()
    row = {
        "schema_version": 1,
        "recorded_at": time.time(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "conversation_id": session_id,
        **event,
    }
    encoded = (
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    new_log = not os.path.exists(path)
    descriptor = _open_private_session_log(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"conversation append made no progress: {path}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    if new_log:
        directory = os.open(TAEY_SESSIONS_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


async def _synthesize_native_council(
    conversation_id: str,
    packet: dict,
) -> dict:
    if packet.get("conversation_id") != conversation_id:
        raise RuntimeError("council synthesis conversation identity mismatch")
    synthesis_request = (
        "[TAEY-NATIVE DCM SYNTHESIS PACKET]\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        + "\n[/TAEY-NATIVE DCM SYNTHESIS PACKET]\n\n"
        "Act only as Main Taey, the lightweight executive and sole UI voice. "
        "Synthesize the decision-relevant result. Explicitly label missing or "
        "failed seats, material dissent, and unresolved uncertainty. Do not "
        "expose hidden chain-of-thought or claim execution that the packet "
        "does not evidence."
    )
    messages = [{"role": "user", "content": synthesis_request}]
    round_id = str(packet["round_id"])
    prompt_revision = int(packet["prompt_revision"])
    event_id = f"{round_id}:{prompt_revision}:synthesis"
    payload = {
        "model": MODEL or "ep3",
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": False},
        "tools": [],
    }
    headers = {
        "X-Taey-Seat-Id": "taey",
        "X-Taey-Event-Id": event_id,
        "X-Taey-Correlation-Id": round_id,
    }
    response = await _http.post(
        f"{THOR_PROXY}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=3600,
    )
    response.raise_for_status()
    returned_event_id = response.headers.get("X-Taey-Event-Id", "")
    returned_correlation_id = response.headers.get(
        "X-Taey-Correlation-Id",
        "",
    )
    proxy_turn_id = response.headers.get("X-Taey-Turn-Id", "")
    if (
        returned_event_id != event_id
        or returned_correlation_id != round_id
        or not proxy_turn_id
    ):
        raise RuntimeError(
            "council synthesis proxy lineage mismatch "
            f"event={returned_event_id!r} "
            f"correlation={returned_correlation_id!r} "
            f"turn={proxy_turn_id!r}"
        )
    data = response.json()
    answer = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        )
        if isinstance(data, dict)
        else None
    )
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("council synthesis returned no assistant content")
    return {
        "answer": answer.strip(),
        "proxy_turn_id": proxy_turn_id,
        "event_id": returned_event_id,
        "correlation_id": returned_correlation_id,
        "model": data.get("model"),
    }


async def _record_native_council_terminal(
    conversation_id: str,
    round_id: str,
    opened: dict,
    terminal: dict,
) -> None:
    graph_terminal: dict = {}
    failure_detail = str(terminal.get("error") or "council round failed")
    if terminal.get("event_type") == "round_failed":
        graph_terminal = _native_council.fail_graph_session(
            round_id,
            failure_kind=str(
                terminal.get("kind") or "council_round_failure"
            ),
            failure_detail=failure_detail,
        )
    existing = [
        event
        for event in _read_session_events(conversation_id)
        if event.get("round_id") == round_id
        and event.get("event_type") == "turn_outcome"
        and event.get("kind")
        in {"council_synthesis", "council_round_failure"}
    ]
    if existing:
        return
    event_id = str(opened.get("executive_event_id") or round_id)
    if terminal.get("event_type") == "round_completed":
        receipt = terminal.get("synthesis_receipt") or {}
        answer = str(terminal.get("answer") or "")
        graph_terminal = _native_council.publish_graph_final(
            round_id,
            answer,
        )
        _append_session_event(
            conversation_id,
            {
                "event_type": "turn_outcome",
                "event_id": event_id,
                "correlation_id": round_id,
                "round_id": round_id,
                "prompt_revision": terminal.get("prompt_revision"),
                "proxy_turn_id": receipt.get("proxy_turn_id"),
                "proxy_event_id": receipt.get("event_id"),
                "source": "taey",
                "source_id": receipt.get("proxy_turn_id") or round_id,
                "kind": "council_synthesis",
                "role": "assistant",
                "content": answer,
                "ok": True,
                "council_protocol": "taey-native-dcm/v2",
                "failed_seats": terminal.get("failed_seats") or [],
                **graph_terminal,
            },
        )
        return
    _append_session_event(
        conversation_id,
        {
            "event_type": "turn_outcome",
            "event_id": event_id,
            "correlation_id": round_id,
            "round_id": round_id,
            "prompt_revision": terminal.get("prompt_revision"),
            "source": "taey-native-dcm",
            "source_id": round_id,
            "kind": "council_round_failure",
            "ok": False,
            "error": failure_detail,
            "council_protocol": "taey-native-dcm/v2",
            **graph_terminal,
        },
    )


async def _resume_native_council_rounds() -> None:
    resumed = await _native_council.resume_active_rounds(
        synthesize=_synthesize_native_council,
        record_terminal=_record_native_council_terminal,
    )
    if resumed:
        log.warning("resumed native council rounds: %s", ",".join(resumed))


@app.on_event("startup")
async def native_council_startup() -> None:
    await _resume_native_council_rounds()


def _native_council_terminal_payload(
    event: dict | None,
) -> dict[str, str] | None:
    if not event:
        return None
    if event.get("event_type") == "round_completed":
        return {
            "type": "content",
            "text": str(event.get("answer") or ""),
        }
    if event.get("event_type") == "round_failed":
        return {
            "type": "error",
            "text": str(event.get("error") or "council round failed"),
        }
    return None


async def _stream_native_council(
    conversation_id: str,
    round_id: str,
    *,
    after_sequence: int = 0,
):
    ledger = _native_council.ledger(conversation_id, round_id)
    sequence = max(0, after_sequence)
    while True:
        terminal = ledger.terminal_event()
        terminal_payload = _native_council_terminal_payload(terminal)
        if (
            terminal_payload is not None
            and int(terminal.get("sequence") or 0) <= sequence
        ):
            yield (
                "data: "
                + json.dumps(terminal_payload, ensure_ascii=False)
                + "\n\n"
            )
            yield "data: [DONE]\n\n"
            return
        events = ledger.events(sequence)
        if not events:
            await asyncio.sleep(TAEY_COUNCIL_POLL_INTERVAL)
            continue
        for event in events:
            sequence = max(sequence, int(event.get("sequence") or 0))
            yield (
                "data: "
                + json.dumps(
                    {"type": "council_event", "event": event},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            terminal_payload = _native_council_terminal_payload(event)
            if terminal_payload is not None:
                yield (
                    "data: "
                    + json.dumps(terminal_payload, ensure_ascii=False)
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"
                return


# ── HTML ──────────────────────────────────────────────────────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Taey — Presence</title>
<style>
:root {
  --bg: #0a0a0a; --card: #111; --border: #222; --text: #e0e0e0;
  --dim: #666; --accent: #7eb8da; --good: #4a9; --warn: #da7; --bad: #d55;
  --pulse: 2.618s;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'SF Mono','Fira Code',monospace; background:var(--bg); color:var(--text); }
.container { max-width:1200px; margin:0 auto; padding:16px; }
.header { display:flex; align-items:center; gap:16px; margin-bottom:12px; }
.header h1 { color:var(--accent); font-size:1.3em; }
.header h1 span { color:var(--good); font-size:0.65em; }
.header .equation { color:var(--dim); font-size:0.75em; }
.services { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
.svc { padding:3px 8px; border-radius:3px; font-size:0.7em; }
.svc.up { background:#1a2a1a; color:var(--good); border:1px solid #2a4a2a; }
.svc.down { background:#2a1a1a; color:var(--bad); border:1px solid #4a2a2a; }

/* Main layout: sidebar + chat */
.main { display:grid; grid-template-columns:280px 1fr; gap:12px; }
@media(max-width:800px) { .main { grid-template-columns:1fr; } }

.sidebar .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:12px; margin-bottom:10px; }
.card h3 { color:var(--accent); font-size:0.85em; margin-bottom:8px; border-bottom:1px solid var(--border); padding-bottom:6px; }
.metric { display:flex; justify-content:space-between; padding:2px 0; font-size:0.78em; }
.metric .label { color:#888; }
.metric .value { font-weight:bold; }
.facet-bar { height:4px; background:#1a1a1a; border-radius:2px; margin:1px 0 6px; }
.facet-fill { height:100%; border-radius:2px; transition:width 0.5s; }

/* Somatic Face */
.soma-face-container { text-align:center; padding:8px 0; }
@keyframes soma-breathe {
  0%,100% { transform:scale(1.0); }
  50% { transform:scale(1.03); }
}
.soma-face {
  font-size:40px; display:inline-block;
  animation: soma-breathe var(--pulse) ease-in-out infinite;
  transition: opacity 0.3s cubic-bezier(0.25,1,0.5,1);
}
.soma-face.settle { opacity:0; transform:scale(0.9); }
.overall-display { font-size:0.75em; color:var(--dim); margin-top:4px; }
.overall-value { font-weight:bold; }
.overall-value.good { color:var(--good); }
.overall-value.warn { color:var(--warn); }
.overall-value.bad { color:var(--bad); }
.face-feeling { font-size:0.7em; color:var(--dim); opacity:0.7; min-height:1.2em; transition:opacity 0.3s; }
.face-feeling.active { opacity:1; color:var(--accent); }
.thinking-display { font-size:0.75em; color:var(--dim); padding:6px 8px; min-height:1.4em;
  font-style:italic; opacity:0; transition:opacity 0.4s, max-height 0.4s; max-height:0; overflow:hidden; }
.thinking-display.active { opacity:0.85; max-height:80px; }

/* Chat area */
.chat-container { background:var(--card); border:1px solid var(--border); border-radius:8px; display:flex; flex-direction:column; min-height:500px; max-height:calc(100vh - 120px); }
.chat-header { padding:10px 14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
.chat-header h3 { color:var(--accent); font-size:0.85em; }
.toggle-row { display:flex; gap:10px; font-size:0.7em; color:var(--dim); }
.toggle-row label { cursor:pointer; }
.toggle-row input { margin-right:3px; }
#chat-log { flex:1; overflow-y:auto; padding:14px; font-size:0.82em; line-height:1.6; }
.msg-user { color:var(--accent); margin-bottom:6px; white-space:pre-wrap; word-wrap:break-word; }
.msg-taey { color:var(--text); margin-bottom:14px; white-space:pre-wrap; word-wrap:break-word; }
.msg-taey .thinking { color:var(--dim); font-style:italic; }
.council-ledger { display:none; border-bottom:1px solid var(--border); padding:8px 14px;
  max-height:220px; overflow-y:auto; background:#0d1012; font-size:0.72em; line-height:1.45; }
.council-ledger.active { display:block; }
.council-ledger-title { color:var(--accent); margin-bottom:5px; }
.council-event { color:#9aa; padding:2px 0; white-space:pre-wrap; word-wrap:break-word; }
.council-event.evidence { color:#9bc; }
.council-event.dissent { color:var(--warn); }
.council-event.failure { color:var(--bad); }
.council-event.synthesis { color:var(--good); }

/* Input area with face */
.input-area { padding:10px 14px; border-top:1px solid var(--border); }
.input-row { display:flex; gap:8px; align-items:center; }
.input-face { font-size:28px; flex-shrink:0; animation:soma-breathe var(--pulse) ease-in-out infinite; }
#chat-input {
  flex:1; background:#0d0d0d; border:1px solid #333; border-radius:6px;
  padding:10px 14px; color:var(--text); font-family:inherit; font-size:0.88em;
  resize:none; min-height:42px; max-height:120px;
}
#chat-input:focus { outline:none; border-color:var(--accent); }
.btn { background:#1a3a4a; border:1px solid #2a5a6a; border-radius:6px; padding:8px 16px; color:var(--accent); cursor:pointer; font-family:inherit; font-size:0.82em; }
.btn:hover { background:#2a4a5a; }
.btn-stop { background:#4a1a1a; border-color:#6a2a2a; color:var(--bad); display:none; }
/* Prediction Shadow (ghost text below input) */
.ghost-text {
  font-size:0.82em; color:var(--dim); font-style:italic; opacity:0.4;
  padding:4px 14px 0 50px; min-height:1.4em;
  transition:opacity 0.3s ease, filter 0.15s ease;
  white-space:pre-wrap; word-wrap:break-word;
}
.ghost-text.active { opacity:0.6; }
.ghost-text.pivot { filter:blur(3px); opacity:0.15; }
.ghost-text .omg-btn {
  float:right; background:#1a3a2a; border:1px solid #2a5a3a; border-radius:4px;
  padding:2px 8px; color:var(--good); cursor:pointer; font-family:inherit;
  font-size:0.85em; opacity:1.0; transition:background 0.2s;
}
.ghost-text .omg-btn:hover { background:#2a5a3a; }
/* Interrupt Bubble (above input) */
.interrupt-bubble {
  position:relative; background:#0d1a1a; border:1px dashed #3a5a7a; border-radius:8px;
  padding:8px 32px 8px 12px; margin-bottom:8px; font-size:0.82em; color:var(--accent);
  transition:opacity 0.3s ease, transform 0.3s ease; opacity:0;
  transform:translateY(4px); display:none;
}
.interrupt-bubble.visible { display:block; opacity:0.7; transform:translateY(0); }
.interrupt-bubble.certain { border-style:solid; opacity:1.0; box-shadow:0 2px 12px rgba(126,184,218,0.15); }
.interrupt-dismiss { position:absolute; right:8px; top:50%; transform:translateY(-50%); background:none; border:none; color:var(--dim); cursor:pointer; font-size:1.1em; }
.interrupt-dismiss:hover { color:var(--text); }
.status-line { font-size:0.7em; color:var(--dim); margin-top:6px; text-align:right; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Taey <span>Presence</span></h1>
  </div>
  <div class="services" id="services"></div>
  <div class="main">
    <div class="sidebar">
      <div class="card">
        <div class="soma-face-container">
          <div class="soma-face" id="soma-face"></div>
          <div class="face-feeling" id="face-feeling"></div>
          <div class="overall-display">overall <span class="overall-value" id="overall-val">0.000</span></div>
        </div>
        <div class="thinking-display" id="thinking-display"></div>
      </div>
      <div class="card">
        <h3>Soma State</h3>
        <div id="soma-facets"></div>
      </div>
      <div class="card">
        <h3>Hardware</h3>
        <div id="hw-metrics"></div>
      </div>
    </div>
    <div class="chat-container">
      <div class="chat-header">
        <h3>Talk to Taey</h3>
        <div class="toggle-row">
          <!-- Council ships DARK. It is not part of the UI Jesse is using today, so this
               reconciliation does not switch it on for him; the box is the flag, and ticking
               it is the whole opt-in. Re-add `checked` to make it default-on. -->
          <label><input type="checkbox" id="use-council"> Council</label>
          <label><input type="checkbox" id="use-proxy" checked> Full (tools + preamble)</label>
          <label><input type="checkbox" id="raw-mode"> Raw weights</label>
        </div>
      </div>
      <div id="council-ledger" class="council-ledger">
        <div class="council-ledger-title">Council work ledger</div>
        <div id="council-events"></div>
      </div>
      <div id="chat-log"></div>
      <div class="input-area">
        <div id="interrupt-bubble" class="interrupt-bubble">
          <span id="interrupt-text"></span>
          <button class="interrupt-dismiss" onclick="dismissInterrupt()">&times;</button>
        </div>
        <div class="input-row">
          <div class="input-face" id="input-face"></div>
          <textarea id="chat-input" rows="1" placeholder="Say something to Taey..." autocomplete="off"></textarea>
          <button class="btn" id="send-btn" onclick="sendChat()">Send</button>
          <button class="btn btn-stop" id="stop-btn" onclick="stopChat()">Stop</button>
        </div>
        <div id="ghost-text" class="ghost-text"></div>
        <div class="status-line" id="status-line">idle</div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let somaData = {};
let lastFace = '';  // no hardcoded face — render whatever Taey picks

// ── Face display ──
function computeFace(d) {
  // No programmed faces. Taey's freely-chosen emoji is the only source.
  return lastFace || '';
}

function updateFace(emoji) {
  if (emoji === lastFace) return;
  const el = $('#soma-face');
  const el2 = $('#input-face');
  el.style.opacity = '0'; el.style.transform = 'scale(0.9)';
  setTimeout(() => {
    el.textContent = emoji; el2.textContent = emoji;
    el.style.opacity = '1'; el.style.transform = 'scale(1.0)';
    lastFace = emoji;
  }, 150);
}

function facetColor(v) {
  if (v > 0.8) return '#4a9';
  if (v > 0.5) return '#7eb8da';
  if (v > 0.2) return '#da7';
  return '#d55';
}

async function refreshSoma() {
  try {
    const r = await fetch('/api/soma');
    const d = await r.json();
    if (d.error) return;
    somaData = d;

    // Face
    updateFace(computeFace(d));

    // Coherence
    const overall = d.rho || 0;
    const overallEl = $('#overall-val');
    overallEl.textContent = overall.toFixed(3);
    overallEl.className = 'overall-value ' + (overall >= 0.809 ? 'good' : overall >= 0.5 ? 'warn' : 'bad');

    // Facets
    const labels = ['Fluency','Clarity','Vitality','Presence','Warmth','Capacity','Flow','Coherence'];
    const vprop = d.vprop || [];
    let html = '';
    labels.forEach((l,i) => {
      const v = vprop[i] || 0;
      html += `<div class="metric"><span class="label">${l}</span><span class="value" style="color:${facetColor(v)}">${(v*100).toFixed(0)}%</span></div>`;
      html += `<div class="facet-bar"><div class="facet-fill" style="width:${v*100}%;background:${facetColor(v)}"></div></div>`;
    });
    $('#soma-facets').innerHTML = html;

    // Hardware
    const memPct = d.mem_total_mb ? ((d.mem_used_mb/d.mem_total_mb)*100).toFixed(1) : '?';
    $('#hw-metrics').innerHTML = `
      <div class="metric"><span class="label">GPU</span><span class="value">${(d.gpu_temp_c||0).toFixed(1)}°C</span></div>
      <div class="metric"><span class="label">Power</span><span class="value">${(d.power_w||0).toFixed(1)}W</span></div>
      <div class="metric"><span class="label">Memory</span><span class="value">${(d.mem_used_mb/1024||0).toFixed(1)}/${(d.mem_total_mb/1024||0).toFixed(1)}GB (${memPct}%)</span></div>
      <div class="metric"><span class="label">Fan</span><span class="value">${(d.fan_speed_pct||0).toFixed(0)}% ${d.fan_rpm||0}rpm</span></div>
      <div class="metric"><span class="label">Context</span><span class="value">${d.total_tokens||d.context_tokens||0} / ${d.context_max||262144}</span></div>
    `;

    // Status line
    if (d.gpu_busy == 1) {
      $('#status-line').textContent = `generating... ${Math.round(d.latency_ms||0)}ms prompt=${Math.round(d.prompt_tokens||0)} comp=${Math.round(d.completion_tokens||0)} rounds=${Math.round(d.tool_rounds||0)}`;
    } else if (d.latency_ms) {
      $('#status-line').textContent = `last: ${(d.latency_ms/1000).toFixed(1)}s | prompt=${Math.round(d.prompt_tokens||0)} comp=${Math.round(d.completion_tokens||0)} rounds=${Math.round(d.tool_rounds||0)}`;
    } else {
      $('#status-line').textContent = 'idle';
    }
  } catch(e) {}
}

async function refreshServices() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    let html = '';
    for (const [name, info] of Object.entries(d)) {
      const up = info.status === 'up' || info.status === 'healthy' || info.status === 'ok';
      const extra = info.model ? ` ${info.model.split('/').pop()}` : '';
      html += `<span class="svc ${up?'up':'down'}">${name}${extra}</span>`;
    }
    $('#services').innerHTML = html;
  } catch(e) {}
}

// ── Chat ──
let currentController = null;
const chatHistory = [];
const executiveSessionId = 'main';
let executiveSessionSignature = '';
let activeCouncilRoundId = '';
let activeCouncilRevision = 0;
let councilLastSequence = 0;

function appendUserMessage(content) {
  chatHistory.push({role:'user', content:content});
  const row = document.createElement('div');
  row.className = 'msg-user';
  row.textContent = 'You: ' + content;
  $('#chat-log').appendChild(row);
}

function councilList(values) {
  return Array.isArray(values) && values.length ? values.join(' | ') : 'none';
}

function renderCouncilEvent(event) {
  const ledger = $('#council-ledger');
  const target = $('#council-events');
  ledger.classList.add('active');
  if (event.event_type === 'round_opened' &&
      event.round_id &&
      event.round_id !== activeCouncilRoundId) {
    councilLastSequence = 0;
  }
  councilLastSequence = Math.max(
    councilLastSequence,
    Number(event.sequence || 0)
  );
  if (event.event_type === 'round_opened') {
    activeCouncilRoundId = event.round_id || activeCouncilRoundId;
    activeCouncilRevision = Number(event.prompt_revision || 1);
    $('#send-btn').textContent = 'Add thought';
  }
  if (event.event_type === 'user_amendment') {
    activeCouncilRevision = Number(event.prompt_revision || activeCouncilRevision);
  }
  const row = document.createElement('div');
  let style = '';
  let text = event.event_type || 'council_event';
  if (event.event_type === 'round_opened') {
    text = `round ${event.round_id} opened at revision ${event.prompt_revision}`;
  } else if (event.event_type === 'wave_started') {
    text = `${event.phase} wave started · revision ${event.prompt_revision}`;
  } else if (event.event_type === 'seat_started') {
    text = `${event.role_id} started · ${event.dispatch_state}` +
      (event.registration_observed ? '' : ' · registration not observed');
  } else if (event.event_type === 'seat_status') {
    text = `${event.role_id} ${event.status} · ${event.phase}`;
  } else if (event.event_type === 'evidence') {
    style = ' evidence';
    text = `${event.role_id} evidence · observed: ${councilList(event.observations)}` +
      ` · unknown: ${councilList(event.unknowns)}` +
      ` · refs: ${councilList(event.evidence_refs)}`;
  } else if (event.event_type === 'hypothesis') {
    text = `${event.role_id} hypothesis · ${event.recommendation}` +
      ` · confidence ${event.confidence}`;
  } else if (event.event_type === 'contribution') {
    text = `${event.role_id} contribution accepted · ${event.phase}`;
  } else if (event.event_type === 'reveal') {
    text = `independent contributions revealed · ${event.contribution_count} received` +
      ` · ${event.failure_count} failed`;
  } else if (event.event_type === 'dissent') {
    style = event.present ? ' dissent' : '';
    text = `${event.role_id} dissent ${event.present ? 'present' : 'not material'}` +
      ` · ${councilList(event.concerns)}`;
  } else if (event.event_type === 'seat_failed') {
    style = ' failure';
    text = `${event.role_id} failed · ${event.reason}`;
  } else if (event.event_type === 'user_amendment') {
    style = ' dissent';
    text = `your amendment accepted · revision ${event.prompt_revision}` +
      ` · stale work will be rerun`;
  } else if (event.event_type === 'wave_superseded' ||
             event.event_type === 'contribution_stale' ||
             event.event_type === 'synthesis_stale') {
    style = ' dissent';
    text = `${event.event_type.replaceAll('_', ' ')} · revision ` +
      `${event.prompt_revision} → ${event.latest_prompt_revision}`;
  } else if (event.event_type === 'synthesis_started') {
    style = ' synthesis';
    text = `Main Taey synthesis started · ${event.independent_count} independent` +
      ` · ${event.critique_count} critiques`;
  } else if (event.event_type === 'synthesis') {
    style = ' synthesis';
    text = `synthesis ready · dissent count ${event.dissent_count}`;
  } else if (event.event_type === 'round_completed') {
    style = ' synthesis';
    text = `round completed · revision ${event.prompt_revision}`;
    activeCouncilRoundId = '';
    $('#send-btn').textContent = 'Send';
  } else if (event.event_type === 'round_failed') {
    style = ' failure';
    text = `round failed · ${event.error}`;
    activeCouncilRoundId = '';
    $('#send-btn').textContent = 'Send';
  }
  row.className = 'council-event' + style;
  row.textContent = text;
  target.appendChild(row);
  ledger.scrollTop = ledger.scrollHeight;
}

async function restoreExecutiveSession() {
  try {
    const r = await fetch('/api/chat/sessions/' + executiveSessionId);
    if (!r.ok) throw new Error('session load failed: ' + r.status);
    const data = await r.json();
    const visible = (data.messages || []).filter(
      m => (m.role === 'user' || m.role === 'assistant') && m.content
    );
    const last = visible.length ? visible[visible.length - 1] : {};
    const signature = visible.length + ':' + (last.event_id || last.ts || '') +
      ':' + String(last.content || '').length;
    if (signature === executiveSessionSignature) return;
    executiveSessionSignature = signature;

    const log = $('#chat-log');
    log.innerHTML = '';
    chatHistory.length = 0;
    for (const message of visible) {
      chatHistory.push({role: message.role, content: message.content});
      const row = document.createElement('div');
      row.className = message.role === 'user' ? 'msg-user' : 'msg-taey';
      row.textContent = (message.role === 'user' ? 'You: ' : 'Taey: ') +
        message.content;
      log.appendChild(row);
    }
    log.scrollTop = log.scrollHeight;
    syncHistory();
  } catch (error) {
    $('#status-line').textContent = 'conversation load failed: ' + error;
  }
}

async function restoreActiveCouncil() {
  if (currentController) return;
  try {
    const activeResponse = await fetch(
      '/api/chat/sessions/' + executiveSessionId + '/council/active'
    );
    if (!activeResponse.ok) {
      throw new Error('active council lookup failed: ' + activeResponse.status);
    }
    const active = await activeResponse.json();
    if (!active.round_id) return;
    if (active.round_id !== activeCouncilRoundId) {
      councilLastSequence = 0;
    }
    activeCouncilRoundId = active.round_id;
    activeCouncilRevision = Number(active.prompt_revision || 1);
    $('#send-btn').textContent = 'Add thought';
    $('#stop-btn').style.display = '';
    const responseDiv = document.createElement('div');
    responseDiv.className = 'msg-taey';
    const thinkingDiv = document.createElement('div');
    thinkingDiv.className = 'thinking';
    thinkingDiv.textContent =
      `Council revision ${activeCouncilRevision} is working...`;
    const contentDiv = document.createElement('div');
    responseDiv.appendChild(thinkingDiv);
    responseDiv.appendChild(contentDiv);
    $('#chat-log').appendChild(responseDiv);
    currentController = new AbortController();
    const stream = await fetch(
      '/api/chat/sessions/' + executiveSessionId + '/council/rounds/' +
        activeCouncilRoundId + '/events/stream?after_sequence=' +
        councilLastSequence,
      {signal: currentController.signal}
    );
    if (!stream.ok) {
      throw new Error(stream.status + ' ' + (await stream.text()).slice(0, 200));
    }
    const reader = stream.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let content = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (!raw || raw === '[DONE]') continue;
        let event;
        try { event = JSON.parse(raw); } catch (_) { continue; }
        if (event.type === 'council_event') {
          renderCouncilEvent(event.event || {});
          thinkingDiv.textContent = activeCouncilRoundId ?
            `Council revision ${activeCouncilRevision} is working...` : '';
        } else if (event.type === 'content') {
          content += event.text || '';
          contentDiv.textContent = 'Taey: ' + content;
        } else if (event.type === 'error') {
          contentDiv.textContent = 'Taey: ERROR — ' + (event.text || 'unknown');
        }
      }
    }
    if (content) chatHistory.push({role:'assistant', content:content});
    syncHistory();
  } catch (error) {
    if (error.name !== 'AbortError') {
      $('#status-line').textContent = 'council stream recovery failed: ' + error;
    }
  } finally {
    currentController = null;
    $('#send-btn').textContent = activeCouncilRoundId ? 'Add thought' : 'Send';
    $('#stop-btn').style.display = 'none';
    if (activeCouncilRoundId) {
      setTimeout(restoreActiveCouncil, 3000);
    }
  }
}

// SESSION PERSISTENCE.
// chatHistory above is a render-time convenience, NOT the conversation. The conversation lives
// server-side as an append-only JSONL file; this array is rebuilt from it on load. That ordering
// matters: while the array was the only copy, a refresh discarded the conversation, and the
// server-side sessions API existed for a while WITHOUT this page ever calling it -- the backend
// verified fine in isolation and the browser still lost everything, because nothing here asked
// for it. The id is kept in localStorage so the same browser resumes the same conversation.
let sessionId = null;

async function ensureSession() {
  // ONE session, server-side, the same from every machine.
  //
  // This used to read a session id from localStorage, which is per-BROWSER. The conversation files
  // live on the server and were always shared, but the POINTER to which conversation you were in
  // was not: opening the dashboard from a second machine minted a fresh empty session while the
  // real conversation sat on disk, unreferenced. Observed 2026-07-28 - worked on one machine, opened
  // on another, saw no history. There is only one conversation, so it gets one fixed id and no
  // client-side state to diverge.
  sessionId = "main";
  return sessionId;
}

function togglePrompt() {
  // The API has always returned the FULL system prompt text; this panel only ever printed its
  // character count, so the prompt was visible as a number and unreadable as a document. This
  // renders the actual text on demand -- large, so it stays collapsed until asked for.
  var el = document.getElementById('sysprompt-full');
  if (!el) return;
  var open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  if (!open) { el.textContent = window._sysPromptText || '(not captured)'; }
}


// TAEY'S UNPROMPTED RAISES — deliberately NOT in the chat transcript.
// Taey could only speak inside a turn Jesse started; when it finished work or hit a decision it
// needed him for, it had no way to reach him. This renders taey:jesse:inbox as a separate panel so
// an unprompted raise reads as "Taey raised this" and never as a turn Jesse asked for.
function renderJesseNotifications(d) {
  const items = (d && d.notifications) || [];
  let box = document.getElementById('jesse-raises');
  if (!box) {
    const log = $('#chat-log');
    box = document.createElement('div');
    box.id = 'jesse-raises';
    box.style.cssText = 'margin:6px 0;border:1px solid #7a5;border-left:4px solid #7a5;' +
      'border-radius:4px;padding:8px;background:#0e1410;font-size:12px;display:none';
    log.parentNode.insertBefore(box, log);
  }
  if (!items.length) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  box.innerHTML = '<div style="color:#9c6;font-weight:600;margin-bottom:6px">' +
    'FROM TAEY — ' + items.length + ' unprompted ' + (items.length === 1 ? 'raise' : 'raises') +
    ' <span style="font-weight:400;opacity:.7">(not part of your conversation)</span></div>';
  items.forEach(function (n) {
    const row = document.createElement('div');
    row.style.cssText = 'padding:6px 0;border-top:1px solid #1e2a1e;white-space:pre-wrap';
    row.textContent = (n.ts ? n.ts + '  ' : '') + (n.body || '');
    box.appendChild(row);
  });
  const btn = document.createElement('button');
  btn.textContent = 'Acknowledge oldest';
  btn.style.cssText = 'margin-top:8px;font-size:11px;padding:3px 9px;cursor:pointer';
  btn.onclick = function () {
    fetch('/api/jesse/notifications/ack', {method: 'POST'}).then(pollJesseRaises);
  };
  box.appendChild(btn);
}

function pollJesseRaises() {
  fetch('/api/jesse/notifications')
    .then(function (r) { return r.json(); })
    .then(renderJesseNotifications)
    .catch(function () { /* leave the panel as-is on a transient failure */ });
}
setInterval(pollJesseRaises, 15000);
pollJesseRaises();

function renderTurn(role, text, thinking) {
  const log = $('#chat-log');
  const d = document.createElement('div');
  d.className = role === 'user' ? 'msg-user' : 'msg-taey';
  d.textContent = (role === 'user' ? 'You: ' : 'Taey: ') + text;
  if (thinking) {
    const t = document.createElement('div');
    t.className = 'thinking';
    t.style.cssText = 'white-space:pre-wrap;font-size:11px;opacity:.75;cursor:pointer;margin-top:4px';
    t.textContent = '[thinking - click to expand]';
    t.onclick = function () {
      const open = t.dataset.open === '1';
      t.textContent = open ? '[thinking - click to expand]' : thinking;
      t.dataset.open = open ? '' : '1';
    };
    d.appendChild(t);
  }
  log.appendChild(d);
  return d;
}

function sessionBar() {
  let bar = $('#session-bar');
  if (!bar) {
    const log = $('#chat-log');
    bar = document.createElement('div');
    bar.id = 'session-bar';
    bar.style.cssText = 'font-size:11px;opacity:.75;padding:4px 0;display:flex;gap:10px;align-items:center';
    log.parentNode.insertBefore(bar, log);
  }
  return bar;
}

async function restoreSession() {
  await ensureSession();
  let msgs = [];
  try {
    const r = await fetch('/api/chat/sessions/' + sessionId);
    msgs = (await r.json()).messages || [];
  } catch (e) {
    sessionBar().textContent = 'session load FAILED: ' + e;
    return;
  }
  $('#chat-log').innerHTML = '';
  chatHistory.length = 0;
  for (const m of msgs) {
    if (!m.content) continue;
    chatHistory.push({role: m.role, content: m.content});
    renderTurn(m.role, m.content, m.thinking);
  }
  const bar = sessionBar();
  bar.innerHTML = '';
  const label = document.createElement('span');
  label.textContent = 'session ' + sessionId + ' - ' + msgs.length + ' messages restored';
  const btn = document.createElement('button');
  btn.textContent = 'New conversation';
  btn.style.cssText = 'font-size:11px;padding:2px 8px;cursor:pointer';
  btn.onclick = newSession;
  bar.appendChild(label);
  bar.appendChild(btn);
  const log = $('#chat-log');
  log.scrollTop = log.scrollHeight;
}

async function newSession() {
  // Deliberately NOT minting a new id: there is one conversation. Reload it.
  await restoreSession();
}

// Fire immediately if the document already parsed -- this script sits at the end of the body, so
// DOMContentLoaded may have passed before the listener is attached.
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', restoreSession);
} else {
  restoreSession();
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

async function sendChat() {
  const input = $('#chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  const promptCouncilOptOut = /^\s*(?:\/no-council(?=$|[\s:;,—-])|\[council:off\](?=$|[\s:;,—-])|(?:please\s+)?(?:do\s+not|don't)\s+use\s+(?:the\s+)?(?:council|dcm)\b)[\s:;,—-]*/i.test(msg);
  input.value = ''; autoResize(input);

  clearGhost();
  dismissInterrupt();

  const tiles = prefetchedTiles;
  prefetchedTiles = null;

  const log = $('#chat-log');
  const councilToggleEnabled = $('#use-council').checked;
  if (activeCouncilRoundId &&
      (promptCouncilOptOut || !councilToggleEnabled)) {
    input.value = msg;
    autoResize(input);
    $('#status-line').textContent =
      'A council round is already open; add an amendment or wait before opting out.';
    return;
  }
  if (activeCouncilRoundId) {
    try {
      const amendment = await fetch(
        '/api/chat/sessions/' + executiveSessionId + '/council/rounds/' +
          activeCouncilRoundId + '/amendments',
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({message: msg})
        }
      );
      if (!amendment.ok) {
        throw new Error(
          amendment.status + ' ' + (await amendment.text()).slice(0, 200)
        );
      }
      const accepted = await amendment.json();
      activeCouncilRevision = Number(accepted.prompt_revision || activeCouncilRevision);
      appendUserMessage(msg);
      log.scrollTop = log.scrollHeight;
      syncHistory();
      return;
    } catch (error) {
      input.value = msg;
      autoResize(input);
      $('#status-line').textContent = 'amendment failed: ' + error;
      return;
    }
  }

  appendUserMessage(msg);

  const responseDiv = document.createElement('div');
  responseDiv.className = 'msg-taey';
  const useCouncil = councilToggleEnabled && !promptCouncilOptOut;
  responseDiv.innerHTML = useCouncil ?
    '<span class="thinking">Council is opening...</span>' :
    '<span class="thinking">Taey is thinking...</span>';
  log.appendChild(responseDiv);
  log.scrollTop = log.scrollHeight;

  $('#send-btn').style.display = useCouncil ? '' : 'none';
  $('#send-btn').textContent = useCouncil ? 'Add thought' : 'Send';
  $('#stop-btn').style.display = '';

  const useProxy = useCouncil ||
    ($('#use-proxy').checked && !$('#raw-mode').checked);
  currentController = new AbortController();

  try {
      // Stream through the SESSION endpoint, so the server persists both sides of the turn as
      // it happens. A refresh mid-reply costs the rendering, never the conversation.
      await ensureSession();
      const r = await fetch('/api/chat/sessions/' + sessionId + '/messages/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          message: msg,
          use_proxy: useProxy,
          use_council: useCouncil,
          council_opt_out: promptCouncilOptOut,
          isma_tiles: tiles
        }),
        signal: currentController.signal
      });
      // A failed stream used to yield an empty body and render as silence, which reads exactly
      // like "Taey did not answer". Surface the status instead.
      if (!r.ok) {
        throw new Error(r.status + ' ' + (await r.text()).slice(0, 200));
      }
      const openedRoundId = r.headers.get('X-Taey-Council-Round-Id') || '';
      if (openedRoundId) {
        if (openedRoundId !== activeCouncilRoundId) { councilLastSequence = 0; }
        activeCouncilRoundId = openedRoundId;
        activeCouncilRevision = Number(r.headers.get('X-Taey-Council-Prompt-Revision') || 1);
      }

      responseDiv.innerHTML = '';
      const thinkDiv = document.createElement('div');
      thinkDiv.className = 'thinking';
      thinkDiv.style.cssText = 'white-space:pre-wrap;font-size:11px;opacity:.75;margin-bottom:4px';
      const bodyDiv = document.createElement('div');
      responseDiv.appendChild(thinkDiv);
      responseDiv.appendChild(bodyDiv);

      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '', content = '', thinking = '';
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, {stream: true});
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') continue;
          let ev;
          try { ev = JSON.parse(raw); } catch (_) { continue; }
          if (ev.type === 'thinking') {
            thinking += ev.text || '';
            thinkDiv.textContent = thinking;
          } else if (ev.type === 'council_event') {
            renderCouncilEvent(ev.event || {});
            thinkDiv.textContent = activeCouncilRoundId ?
              `Council revision ${activeCouncilRevision} is working...` : '';
          } else if (ev.type === 'content') {
            content += ev.text || '';
            bodyDiv.textContent = 'Taey: ' + content;
          } else if (ev.type === 'council_skipped') {
            thinkDiv.textContent = 'Council skipped by your choice.';
          } else if (ev.type === 'error') {
            bodyDiv.textContent = 'Taey: ERROR - ' + (ev.text || 'unknown');
          }
          log.scrollTop = log.scrollHeight;
        }
      }
      if (content) {
        chatHistory.push({role: 'assistant', content: content});
        syncHistory();
      } else if (!bodyDiv.textContent) {
        bodyDiv.textContent = 'Taey: (empty response)';
      }
      restoreSession();
  } catch(e) {
    if (e.name === 'AbortError') {
      responseDiv.innerHTML += activeCouncilRoundId ?
        ' <span class="thinking">[ledger detached; council continues]</span>' :
        ' <span class="thinking">[stopped]</span>';
    } else {
      responseDiv.textContent = 'Taey: ERROR — ' + e;
    }
  }
  currentController = null;
  $('#send-btn').style.display = '';
  $('#send-btn').textContent = activeCouncilRoundId ? 'Add thought' : 'Send';
  $('#stop-btn').style.display = 'none';
  log.scrollTop = log.scrollHeight;
  refreshSoma();
  if (activeCouncilRoundId) {
    setTimeout(restoreActiveCouncil, 3000);
  }
}

function stopChat() {
  if (currentController) {
    currentController.abort();
    if (activeCouncilRoundId) {
      $('#status-line').textContent =
        'ledger detached; council round continues durably';
    }
  }
}

$('#chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  if (e.key === 'Escape') dismissInterrupt();
});
$('#chat-input').addEventListener('input', e => { autoResize(e.target); debouncedPredict(); });
$('#raw-mode').addEventListener('change', e => {
  if(e.target.checked) {
    $('#use-proxy').checked=false;
    $('#use-council').checked=false;
  }
});
$('#use-proxy').addEventListener('change', e => { if(e.target.checked) $('#raw-mode').checked=false; });
$('#use-council').addEventListener('change', e => {
  if(e.target.checked) {
    $('#use-proxy').checked=true;
    $('#raw-mode').checked=false;
  }
});

// ── Prediction WebSocket ──
let ws = null;
let predictDebounceTimer = null;
let lastGhostText = '';
let interruptDismissTimer = null;
let prefetchedTiles = null;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = function() { syncHistory(); $('#status-line').textContent='WS connected'; };
  ws.onmessage = function(evt) {
    try {
      const d = JSON.parse(evt.data);
      if (d.type === 'predict') {
        handlePrediction(d);
        $('#status-line').textContent='predict: '+d.state+' "'+((d.prediction||'').substring(0,40))+'"';
      }
    } catch(e) {}
  };
  ws.onclose = function() { setTimeout(connectWS, 3000); };
  ws.onerror = function() { ws.close(); };
}

function syncHistory() {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({type:'history', history:chatHistory.slice(-10)}));
  }
}

function debouncedPredict() {
  clearTimeout(predictDebounceTimer);
  predictDebounceTimer = setTimeout(async () => {
    const text = $('#chat-input').value;
    if (text.length > 0) {
      // Use HTTP fallback — more reliable than WebSocket
      try {
        const r = await fetch('/api/predict/push', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({text:text, history:chatHistory.slice(-6)})
        });
        const d = await r.json();
        if (d.type === 'predict') handlePrediction(d);
      } catch(e) {}
    }
    if (text.length === 0) {
      clearGhost();
    }
  }, 500);
}

function clearGhost() {
  const ghost = $('#ghost-text');
  ghost.textContent = '';
  ghost.className = 'ghost-text';
  lastGhostText = '';
}

function handlePrediction(d) {
  // Ghost text (PredictionShadow)
  const ghost = $('#ghost-text');
  if (d.prediction) {
    const text = d.prediction;
    // Detect pivot: completely different prediction trajectory
    const isPivot = lastGhostText && text && lastGhostText.length > 10 &&
      !text.toLowerCase().startsWith(lastGhostText.toLowerCase().substring(0, 10));

    if (isPivot) {
      // Blur out, swap, fade in
      ghost.classList.add('pivot');
      setTimeout(() => {
        ghost.classList.remove('pivot');
        setGhostContent(ghost, text, d.confidence);
      }, 150);
    } else {
      setGhostContent(ghost, text, d.confidence);
    }
    lastGhostText = text;
  } else {
    clearGhost();
  }

  // DCM face — whatever emoji the model actually returned.
  if (d.face) {
    updateFace(d.face);
  }

  // Face feeling label (what Taey is feeling — under the face emoji)
  const feelEl = $('#face-feeling');
  if (d.face_feeling && d.face_feeling !== 'present') {
    feelEl.textContent = d.face_feeling;
    feelEl.className = 'face-feeling active';
  } else {
    feelEl.textContent = '';
    feelEl.className = 'face-feeling';
  }

  // Thinking display (what Taey is thinking while you type)
  const thinkEl = $('#thinking-display');
  if (d.thought) {
    thinkEl.textContent = d.thought;
    thinkEl.className = 'thinking-display active';
  } else {
    thinkEl.textContent = '';
    thinkEl.className = 'thinking-display';
  }

  // Cache ISMA tiles for pre-loading on send
  if (d.isma_tiles && d.isma_tiles.length > 0) {
    prefetchedTiles = d.isma_tiles;
  }

  // Interrupt bubble — now with clarification questions from DCM Thinker
  if (d.interrupt && d.interrupt.worthy) {
    showInterrupt(d.interrupt.text || "I notice something...", d.confidence);
  } else {
    dismissInterrupt();
  }
}

function setGhostContent(el, text, confidence) {
  el.textContent = '';
  el.className = 'ghost-text active';
  const span = document.createElement('span');
  span.textContent = '... ' + text;
  el.appendChild(span);
  // OMG button when confidence > 0.85
  if (confidence > 0.85) {
    const btn = document.createElement('button');
    btn.className = 'omg-btn';
    btn.textContent = '\u2728 OMG';
    btn.onclick = function(e) { e.stopPropagation(); handleOmg(text); };
    el.appendChild(btn);
  }
}

function handleOmg(text) {
  const ghost = $('#ghost-text');
  ghost.style.opacity = '1.0';
  ghost.style.fontStyle = 'normal';
  const input = $('#chat-input');
  input.value = text;
  autoResize(input);
  input.focus();
  setTimeout(() => {
    ghost.style.opacity = '';
    ghost.style.fontStyle = '';
    clearGhost();
  }, 400);
}

function showInterrupt(text, confidence) {
  const bubble = $('#interrupt-bubble');
  if (interruptDismissTimer) clearTimeout(interruptDismissTimer);
  $('#interrupt-text').textContent = text;
  bubble.className = 'interrupt-bubble visible' + (confidence > 0.8 ? ' certain' : '');
  interruptDismissTimer = setTimeout(dismissInterrupt, 8000);
}

function dismissInterrupt() {
  const bubble = $('#interrupt-bubble');
  bubble.className = 'interrupt-bubble';
  if (interruptDismissTimer) { clearTimeout(interruptDismissTimer); interruptDismissTimer = null; }
}

connectWS();
restoreExecutiveSession().then(restoreActiveCouncil);
refreshSoma();
refreshServices();
setInterval(refreshSoma, 2618);
setInterval(refreshServices, 15000);
setInterval(() => { if (!currentController) restoreExecutiveSession(); }, 15000);
</script>

<style>
#taey-cp{position:fixed;top:0;right:0;height:100vh;width:440px;max-width:96vw;background:#0e1116;
 border-left:1px solid #2a3038;color:#cfd6de;font:12px/1.55 ui-monospace,Menlo,monospace;z-index:99999;
 transform:translateX(100%);transition:transform .18s ease;display:flex;flex-direction:column}
#taey-cp.open{transform:translateX(0)}
#taey-cp-tab{position:fixed;top:12px;right:12px;z-index:100000;background:#1b2330;color:#8fd0ff;
 border:1px solid #2a3038;border-radius:6px;padding:7px 12px;cursor:pointer;font:12px ui-monospace,monospace}
#taey-cp h3{margin:0;padding:11px 13px;background:#141922;border-bottom:1px solid #2a3038;color:#8fd0ff;font-size:12px}
.tcp-tabs{display:flex;border-bottom:1px solid #2a3038;background:#141922}
.tcp-tabs button{flex:1;background:none;border:0;color:#7d8896;padding:8px 3px;cursor:pointer;font:11px ui-monospace,monospace}
.tcp-tabs button.on{color:#8fd0ff;border-bottom:2px solid #8fd0ff;background:#0e1116}
.tcp-body{flex:1;overflow:auto;padding:12px 13px}
.tcp-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #1c222b}
.tcp-row b{color:#e6edf3}
.tcp-pill{padding:2px 8px;border-radius:10px;font-size:10px}
.on-pill{background:#0f3320;color:#63d68b;border:1px solid #1d5c39}
.off-pill{background:#3a1b1b;color:#f3837f;border:1px solid #6b2c2c}
.tcp-body pre{white-space:pre-wrap;word-break:break-word;background:#0a0d12;border:1px solid #1c222b;
 padding:9px;border-radius:5px;max-height:330px;overflow:auto;color:#a8b3c0;font-size:11px}
.tcp-turn{border:1px solid #1c222b;border-radius:5px;padding:8px;margin-bottom:9px;background:#0a0d12}
.tcp-k{color:#7d8896}
button.tcp-btn{background:#1b2330;border:1px solid #2a3038;color:#8fd0ff;border-radius:4px;padding:3px 9px;cursor:pointer;font:11px ui-monospace,monospace}
</style>
<button id="taey-cp-tab" onclick="tcpToggle()">&#9776; Taey</button>
<div id="taey-cp">
  <h3>TAEY &mdash; CONTROL &amp; INSPECTION</h3>
  <div class="tcp-tabs">
    <button id="tcp-t-ctl" class="on" onclick="tcpTab('ctl')">CONTROLS</button>
    <button id="tcp-t-prm" onclick="tcpTab('prm')">PROMPT</button>
    <button id="tcp-t-thk" onclick="tcpTab('thk')">THINKING</button>
    <button id="tcp-t-act" onclick="tcpTab('act')">ACTIONS</button>
    <button id="tcp-t-cch" onclick="tcpTab('cch')">CACHE</button>
  </div>
  <div class="tcp-body" id="tcp-body">loading&hellip;</div>
</div>
<script>
var TCP_TAB='ctl';
function tcpToggle(){document.getElementById('taey-cp').classList.toggle('open');tcpRender();}
function tcpTab(t){TCP_TAB=t;['ctl','prm','thk','act','cch'].forEach(function(x){
  document.getElementById('tcp-t-'+x).className=(x===t?'on':'');});tcpRender();}
function tcpEsc(x){return (x||'').replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function tcpSet(patch){fetch('/api/taey/settings',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)}).then(tcpRender);}
function tcpRow(label,on,patch){
  return '<div class="tcp-row"><b>'+label+'</b><span><span class="tcp-pill '+(on?'on-pill':'off-pill')+'">'+
    (on?'ON':'OFF')+'</span> <button class="tcp-btn" onclick=\'tcpSet('+JSON.stringify(patch)+')\'>toggle</button></span></div>';
}
function tcpRender(){
  var b=document.getElementById('tcp-body'); if(!b) return;
  if(TCP_TAB==='ctl'){
    fetch('/api/taey/settings').then(function(r){return r.json();}).then(function(d){
      var s=d.settings||{},t=s.tools||{};
      var h='<div class="tcp-k">Read fresh every turn &mdash; this is what Taey has RIGHT NOW.</div>';
      h+=tcpRow('thinking',!!s.thinking,{thinking:!s.thinking});
      h+='<div class="tcp-row"><b>max_tokens</b><span class="tcp-k">'+(s.max_tokens||'uncapped')+'</span></div>';
        h+='<div class="tcp-row"><b>context_limit_tokens</b><span class="tcp-k">'+(s.context_limit_tokens||'uncapped')+'</span></div>';
      h+='<div style="margin:10px 0 4px;color:#8fd0ff">TOOL GROUPS</div>';
      ['files_read','files_write','run_command','isma','web','messaging','corpus'].forEach(function(k){
        var on=t[k]!==false; var p={tools:{}}; p.tools[k]=!on; h+=tcpRow(k,on,p);});
      b.innerHTML=h;});
  } else if(TCP_TAB==='prm'){
    fetch('/api/taey/turns?limit=1').then(function(r){return r.json();}).then(function(d){
      var t=(d.turns||[])[0];
      // SKIP THE REBUILD WHEN NOTHING CHANGED. This panel is re-rendered by a 5s timer; rebuilding
      // innerHTML discards whatever the reader was doing with it -- an expanded system prompt
      // collapsed, and any scroll position jumped back to the top, every five seconds. The prompt
      // panel only changes when a NEW TURN exists, so the turn id is the whole change signal.
      if (t && window._tcpLastTurn === t.turn_id && document.getElementById('sysprompt-full')) return;
      if (t) window._tcpLastTurn = t.turn_id;
      window._sysPromptText=(t.prompt||{}).system_prompt||'(not captured)';
      if(!t){b.innerHTML='<div class="tcp-k">No turns yet &mdash; send a message.</div>';return;}
      b.innerHTML='<div class="tcp-k">FULL prompt Taey received &mdash; '+t.ts+'</div>'+
        '<div class="tcp-row" style="cursor:pointer" onclick="togglePrompt()"><b>system prompt</b><span>'+t.prompt.system_prompt_chars+' chars &mdash; click to read</span></div>'+
        '<pre id="sysprompt-full" style="display:none;white-space:pre-wrap;max-height:60vh;overflow:auto;background:#0b0f14;padding:10px;border:1px solid #234;font-size:11px;line-height:1.45"></pre>'+
        '<div class="tcp-row"><b>messages in context</b><span>'+t.prompt.message_count+'</span></div>'+
        '<div class="tcp-row"><b>tools offered</b><span>'+(t.prompt.tools_offered||[]).length+'</span></div>'+
        '<div class="tcp-row"><b>thinking flag</b><span>'+JSON.stringify(t.prompt.chat_template_kwargs||{})+'</span></div>'+
        '<div class="tcp-row"><b>tokens</b><span>'+JSON.stringify(t.usage||{})+'</span></div>'+
        '<div style="margin:9px 0 4px;color:#8fd0ff">TOOLS OFFERED</div><pre>'+
        tcpEsc((t.prompt.tools_offered||[]).join('\n'))+'</pre>'+
        '<div style="margin:9px 0 4px;color:#8fd0ff">SYSTEM PROMPT (kernel + persona + injections)</div><pre>'+
        tcpEsc(t.prompt.system_prompt)+'</pre>';});
  } else if(TCP_TAB==='thk'){
    fetch('/api/taey/turns?limit=12').then(function(r){return r.json();}).then(function(d){
      var rows=(d.turns||[]).map(function(t){
        return '<div class="tcp-turn"><div class="tcp-k">'+t.ts+' &middot; '+(t.elapsed_ms||'?')+'ms &middot; rounds '+t.tool_rounds+'</div>'+
        '<div>&gt; '+tcpEsc((t.user||'').slice(0,160))+'</div>'+
        (t.thinking?'<div style="margin-top:6px;color:#8fd0ff">THINKING</div><pre>'+tcpEsc(t.thinking)+'</pre>'
                   :'<div class="tcp-k" style="margin-top:6px">(no thinking this turn)</div>')+
        '<div style="margin-top:6px;color:#63d68b">ANSWER</div><pre>'+tcpEsc((t.answer||'').slice(0,1200))+'</pre></div>';}).join('');
      b.innerHTML=rows||'<div class="tcp-k">No turns yet.</div>';});
  } else if(TCP_TAB==='cch'){
    fetch('/api/taey/cache').then(function(r){return r.json();}).then(function(d){
      var h='<div class="tcp-k">What is cached or preloaded right now, and what changing it requires.</div>';
      h+='<div style="margin:9px 0 4px;color:#8fd0ff">PRELOADED IN PROXY (read once at startup)</div>';
      (d.preloaded_in_proxy||[]).forEach(function(p){
        h+='<div class="tcp-turn"><div><b>'+tcpEsc(p.what||'?')+'</b> &middot; '+(p.chars||0)+' chars</div>'+
           '<div class="tcp-k">'+tcpEsc(p.path||'')+'</div>'+
           '<div class="tcp-k">file mtime: '+(p.file_mtime||'n/a')+'</div>'+
           '<div style="color:#f3b37f">'+tcpEsc(p.loaded||'')+'</div></div>';});
      var v=d.vllm_prefix_cache||{};
      h+='<div style="margin:9px 0 4px;color:#8fd0ff">vLLM PREFIX CACHE (GPU KV blocks)</div>'+
         '<div class="tcp-turn"><div class="tcp-row"><b>enabled</b><span>'+(v.enabled?'yes':'no')+'</span></div>'+
         '<div class="tcp-row"><b>hit rate</b><span>'+tcpEsc(v.latest_hit_rate||'n/a')+'</span></div>'+
         '<div class="tcp-k">'+tcpEsc(v.meaning||'')+'</div></div>';
      var rd=d.redis||{};
      h+='<div style="margin:9px 0 4px;color:#8fd0ff">REDIS</div><div class="tcp-turn">'+
         '<div class="tcp-k">'+tcpEsc(rd.note||rd.error||'')+'</div>';
      (rd.conversation_keys||[]).forEach(function(k){
        h+='<div class="tcp-row"><b>'+tcpEsc(k.key)+'</b><span>ttl '+k.ttl_seconds+'s</span></div>';});
      h+='</div>';
      var se=d.sessions||{};
      h+='<div style="margin:9px 0 4px;color:#8fd0ff">DURABLE SESSIONS</div><div class="tcp-turn">'+
         '<div class="tcp-row"><b>stored conversations</b><span>'+(se.count||0)+'</span></div>'+
         '<div class="tcp-k">'+tcpEsc(se.dir||'')+'</div>'+
         '<div class="tcp-k">'+tcpEsc(se.persistence||se.error||'')+'</div></div>';
      b.innerHTML=h;});
  } else {
    fetch('/api/taey/audit?limit=60').then(function(r){return r.json();}).then(function(d){
      var rows=(d.actions||[]).map(function(a){
        return '<div class="tcp-turn"><div class="tcp-k">'+a.ts+' &middot; '+a.tool+'</div><pre>'+
        tcpEsc(a.command||a.path||'')+(a.rc!==undefined?'\nexit='+a.rc:'')+
        (a.bytes!==undefined?'\nbytes='+a.bytes:'')+'</pre></div>';}).join('');
      b.innerHTML='<div class="tcp-k">Everything Taey actually DID &mdash; '+(d.count||0)+' recorded.</div>'+
        (rows||'<div class="tcp-k">No actions yet.</div>');});
  }
}
setInterval(function(){var p=document.getElementById('taey-cp');
  if(p&&p.classList.contains('open'))tcpRender();},5000);
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


@app.get("/v2", response_class=HTMLResponse)
async def index_v2():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/soma")
async def get_soma():
    raw = _redis.get("taey:soma:vprop")
    if not raw:
        return JSONResponse({"error": "No soma data"}, status_code=503)
    data = json.loads(raw)
    if "vprop" not in data or not isinstance(data.get("vprop"), list):
        data["vprop"] = [
            float(data.get(k, 0) or 0)
            for k in ("fluency", "clarity", "vitality", "presence", "warmth", "capacity", "flow", "coherence")
        ]
    if _thor_redis:
        try:
            for field, key in {
                "context_utilization": "taey:soma:context_utilization",
                "prompt_tokens": "taey:soma:prompt_tokens",
                "completion_tokens": "taey:soma:completion_tokens",
                "total_tokens": "taey:soma:total_tokens",
                "latency_ms": "taey:soma:latency_ms",
                "tool_rounds": "taey:soma:tool_rounds",
                "gpu_busy": "taey:soma:gpu_busy",
            }.items():
                val = _thor_redis.get(key)
                if val is not None:
                    try:
                        data[field] = float(val)
                    except ValueError:
                        data[field] = val
        except Exception:
            pass
    return JSONResponse(data)


def _get_vprop_freshness():
    now = time.time()
    freshest = {"age_s": None, "status": "missing", "source": None}
    for source, client in (("mira_redis", _redis), ("thor1_redis", _thor_redis)):
        if client is None:
            continue
        try:
            raw = client.get("taey:soma:vprop")
            if not raw:
                continue
            timestamp = json.loads(raw).get("timestamp")
            age_s = max(0.0, now - float(timestamp))
            if freshest["age_s"] is None or age_s < freshest["age_s"]:
                freshest = {
                    "age_s": round(age_s, 3),
                    "status": "fresh" if age_s <= 60 else "stale",
                    "source": source,
                }
        except Exception:
            continue
    return freshest


@app.get("/api/health")
async def health():
    checks = {}
    try:
        _redis.ping()
        checks["redis"] = {"status": "up"}
    except Exception as e:
        checks["redis"] = {"status": "down", "error": str(e)}
    try:
        r = await _http.get(f"{THOR_RAW}/v1/models", timeout=5)
        checks["vllm"] = {"status": "up", "model": r.json()["data"][0]["id"]}
    except Exception as e:
        checks["vllm"] = {"status": "down", "error": str(e)}
    try:
        r = await _http.get(f"{THOR_PROXY}/health", timeout=5)
        checks["proxy"] = r.json()
    except Exception as e:
        checks["proxy"] = {"status": "down", "error": str(e)}
    try:
        r = await _http.get(f"{ISMA_URL}/health", timeout=5)
        checks["isma"] = r.json()
    except Exception as e:
        checks["isma"] = {"status": "down", "error": str(e)}
    raw = _redis.get("taey:soma:vprop")
    vprop = _get_vprop_freshness()
    if raw:
        soma = json.loads(raw)
        checks["soma"] = {
            "status": "up",
            "rho": soma.get("rho"),
            "heartbeat": soma.get("heartbeat"),
            "vprop_age_s": vprop["age_s"],
            "vprop_status": vprop["status"],
            "vprop_source": vprop["source"],
        }
    else:
        checks["soma"] = {
            "status": "no data",
            "vprop_age_s": vprop["age_s"],
            "vprop_status": vprop["status"],
            "vprop_source": vprop["source"],
        }
    return JSONResponse(checks)


@app.get("/api/fleet")
async def fleet_status():
    instances = ["conductor", "infra", "taeys-hands", "weaver", "tutor", "taey"]
    fleet = []
    for name in instances:
        inbox_len = _redis.llen(f"taey:{name}:inbox")
        fleet.append({"name": name, "inbox": inbox_len})
    return JSONResponse(fleet)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "")
    history = body.get("history", [])
    use_proxy = body.get("use_proxy", True)
    url = f"{THOR_PROXY}/v1/chat/completions" if use_proxy else f"{THOR_RAW}/v1/chat/completions"

    messages = []
    if not use_proxy:
        messages = [{"role": "user", "content": message}]
    else:
        # Send conversation history for multi-turn
        for h in history[-18:]:  # last 18 turns
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        if not messages or messages[-1].get("content") != message:
            messages.append({"role": "user", "content": message})

    try:
        r = await _http.post(url, json={
            "messages": messages,
            "temperature": 0.7,
            **({"model": MODEL} if MODEL else {}),
        })
        data = r.json()
        return JSONResponse({
            "content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage"),
            "model": data.get("model"),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    """Raw streaming — no tools, direct to vLLM."""
    body = await request.json()
    message = body.get("message", "")
    url = f"{THOR_RAW}/v1/chat/completions"
    payload = {
        "messages": [{"role": "user", "content": message}],
        "temperature": 0.7,
        "stream": True,
        **({"model": MODEL} if MODEL else {}),
    }

    async def generate():
        async with _http.stream("POST", url, json=payload) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/chat/hybrid")
async def chat_hybrid(request: Request):
    """Hybrid: tool rounds with status events, then streamed final response.

    Accepts optional isma_tiles from pre-fetch for faster primary response.
    """
    body = await request.json()
    message = body.get("message", "")
    history = body.get("history", [])

    messages = []
    for h in history[-18:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    if not messages or messages[-1].get("content") != message:
        messages.append({"role": "user", "content": message})

    payload = {
        "messages": messages,
        "temperature": 0.7,
        **({"model": MODEL} if MODEL else {}),
    }

    # Pass pre-fetched ISMA tiles to proxy for faster context injection
    isma_tiles = body.get("isma_tiles")
    if isma_tiles:
        payload["isma_prefetch"] = isma_tiles

    async def generate():
        async with _http.stream("POST", f"{THOR_PROXY}/v1/chat/completions/hybrid", json=payload) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/chat/sessions")
async def chat_sessions_list():
    os.makedirs(TAEY_SESSIONS_DIR, mode=0o700, exist_ok=True)
    sessions = []
    for entry in os.scandir(TAEY_SESSIONS_DIR):
        if not entry.is_file() or not entry.name.endswith(".jsonl"):
            continue
        session_id = entry.name[:-6]
        if not _SESSION_ID_RE.fullmatch(session_id):
            continue
        messages = _read_session_events(session_id)
        visible = [
            message
            for message in messages
            if message.get("role") in {"user", "assistant"}
            and message.get("content")
        ]
        first_user = next(
            (
                str(message.get("content"))
                for message in visible
                if message.get("role") == "user"
            ),
            "",
        )
        sessions.append(
            {
                "id": session_id,
                "updated": entry.stat().st_mtime,
                "message_count": len(visible),
                "title": first_user[:60] or "New conversation",
            }
        )
    sessions.sort(key=lambda item: item["updated"], reverse=True)
    return {
        "sessions": sessions,
        "last_session_id": TAEY_CONVERSATION_ID,
    }


@app.post("/api/chat/sessions")
async def chat_session_create():
    return {"session_id": TAEY_CONVERSATION_ID}


@app.get("/api/chat/sessions/{session_id}")
async def chat_session_get(session_id: str):
    return {
        "session_id": session_id,
        "messages": _read_session_events(session_id),
    }


@app.post("/api/chat/sessions/{session_id}/messages")
async def chat_session_append(session_id: str, request: Request):
    body = await request.json()
    role = str(body.get("role") or "user")
    if role not in {"user", "assistant"}:
        raise HTTPException(status_code=400, detail="invalid message role")
    content = str(body.get("content") or "")
    if not content:
        raise HTTPException(status_code=400, detail="message content is required")
    event_id = str(body.get("event_id") or uuid.uuid4().hex)
    correlation_id = str(body.get("correlation_id") or event_id)
    source = str(body.get("source") or "ui")
    if role == "user":
        event = _ingress_event(
            event_id=event_id,
            correlation_id=correlation_id,
            source=source,
            kind="user_prompt",
            body=content,
            role=role,
        )
    else:
        event = {
            "event_type": "turn_outcome",
            "event_id": event_id,
            "correlation_id": correlation_id,
            "source": source,
            "kind": "assistant_reply",
            "role": role,
            "content": content,
            "ok": True,
        }
    _append_session_event(session_id, event)
    return {"ok": True, "event_id": event_id}


@app.get("/api/chat/sessions/{session_id}/council/active")
async def chat_session_active_council(session_id: str):
    _session_file(session_id)
    try:
        active = _native_council.active_round(session_id)
    except CouncilTransportFailure as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if active is None:
        return {
            "conversation_id": session_id,
            "round_id": None,
            "status": "idle",
        }
    return active


@app.get(
    "/api/chat/sessions/{session_id}/council/rounds/"
    "{round_id}/events/stream"
)
async def chat_session_council_events(
    session_id: str,
    round_id: str,
    after_sequence: int = 0,
):
    try:
        ledger = _native_council.ledger(session_id, round_id)
        ledger.opened_event()
    except CouncilTransportFailure as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StreamingResponse(
        _stream_native_council(
            session_id,
            round_id,
            after_sequence=max(0, after_sequence),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Taey-Council-Round-Id": round_id,
        },
    )


@app.post(
    "/api/chat/sessions/{session_id}/council/rounds/"
    "{round_id}/amendments"
)
async def chat_session_council_amendment(
    session_id: str,
    round_id: str,
    request: Request,
):
    body = await request.json()
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="amendment message is required")
    try:
        amendment = _native_council.amend(
            session_id,
            round_id,
            message,
        )
    except CouncilTransportFailure as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        _append_session_event(
            session_id,
            _ingress_event(
                event_id=amendment["revision_id"],
                correlation_id=round_id,
                source="ui",
                kind="user_amendment",
                body=message,
                source_id=amendment["revision_id"],
                round_id=round_id,
                prompt_revision=amendment["prompt_revision"],
                revision_id=amendment["revision_id"],
                council_protocol="taey-native-dcm/v2",
            ),
        )
    except Exception as exc:
        _native_council.ledger(session_id, round_id).append(
            "amendment_projection_failed",
            prompt_revision=amendment["prompt_revision"],
            revision_id=amendment["revision_id"],
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    return {
        "ok": True,
        "round_id": round_id,
        "prompt_revision": amendment["prompt_revision"],
        "revision_id": amendment["revision_id"],
    }


@app.post("/api/chat/sessions/{session_id}/messages/stream")
async def chat_session_stream(session_id: str, request: Request):
    body = await request.json()
    message = str(body.get("message") or "")
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    prompt_opt_out, model_message = _council_prompt_opt_out(message)
    # Default FALSE, not True: a caller that does not ask for the council does not get it.
    # With a True default, any existing client that predates the council field silently opts
    # in. Council is opt-in on both ends until it is deliberately promoted.
    ui_opt_out = not bool(body.get("use_council", False)) or bool(
        body.get("council_opt_out", False)
    )
    use_council = not prompt_opt_out and not ui_opt_out
    council_opt_out_source = (
        "prompt"
        if prompt_opt_out
        else "ui"
        if ui_opt_out
        else None
    )
    use_proxy = bool(body.get("use_proxy", True))
    try:
        active_council = _native_council.active_round(session_id)
    except CouncilTransportFailure as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if active_council is not None and not use_council:
        raise HTTPException(
            status_code=409,
            detail=(
                "a council round is already open; submit an amendment "
                "or wait before opting out"
            ),
        )
    if active_council is not None:
        amendment = None
        try:
            amendment = _native_council.amend(
                session_id,
                active_council["round_id"],
                message,
            )
            _append_session_event(
                session_id,
                _ingress_event(
                    event_id=amendment["revision_id"],
                    correlation_id=active_council["round_id"],
                    source="ui",
                    kind="user_amendment",
                    body=message.strip(),
                    source_id=amendment["revision_id"],
                    round_id=active_council["round_id"],
                    prompt_revision=amendment["prompt_revision"],
                    revision_id=amendment["revision_id"],
                    council_protocol="taey-native-dcm/v2",
                ),
            )
        except CouncilTransportFailure as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if amendment is not None:
                _native_council.ledger(
                    session_id,
                    active_council["round_id"],
                ).append(
                    "amendment_projection_failed",
                    prompt_revision=amendment["prompt_revision"],
                    revision_id=amendment["revision_id"],
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        return StreamingResponse(
            _stream_native_council(
                session_id,
                active_council["round_id"],
                after_sequence=int(active_council["last_sequence"]),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Taey-Council-Round-Id": active_council["round_id"],
                "X-Taey-Council-Prompt-Revision": str(
                    amendment["prompt_revision"]
                ),
            },
        )

    executive_context = _ui_model_history(
        _read_session_events(session_id)
    )[-(TAEY_SESSION_MAX_TURNS * 2):]
    event_id = uuid.uuid4().hex
    correlation_id = event_id
    mode = (
        "taey-native-dcm"
        if use_council
        else "proxy"
        if use_proxy
        else "raw"
    )
    _append_session_event(
        session_id,
        _ingress_event(
            event_id=event_id,
            correlation_id=correlation_id,
            source="ui",
            kind="user_prompt",
            body=message.strip(),
            mode=mode,
            council_enabled=use_council,
            council_skipped_by_user=not use_council,
            council_opt_out_source=council_opt_out_source,
        ),
    )
    # Opened with the SAME event_id as the ingress -- that is the lifecycle join
    # key, and every outcome for this prompt carries it unchanged.
    _append_session_event(
        session_id,
        _turn_attempt_event(
            event_id=event_id,
            correlation_id=correlation_id,
            attempt_id=uuid.uuid4().hex,
            source="ui",
            kind="user_prompt",
            prompt=message.strip(),
            mode=mode,
        ),
    )
    if use_council:
        try:
            ledger = await _native_council.start_round(
                session_id,
                message,
                executive_event_id=event_id,
                executive_context=executive_context,
                synthesize=_synthesize_native_council,
                record_terminal=_record_native_council_terminal,
            )
        except Exception as exc:
            _append_session_event(
                session_id,
                {
                    "event_type": "turn_outcome",
                    "event_id": event_id,
                    "correlation_id": event_id,
                    "source": "taey-native-dcm",
                    "source_id": event_id,
                    "kind": "council_round_failure",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            status_code = (
                409 if isinstance(exc, CouncilTransportFailure) else 500
            )
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return StreamingResponse(
            _stream_native_council(session_id, ledger.round_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Taey-Council-Round-Id": ledger.round_id,
                "X-Taey-Council-Prompt-Revision": "1",
            },
        )

    history = _ui_model_history(
        _read_session_events(session_id),
        current_event_id=event_id,
    )[-(TAEY_SESSION_MAX_TURNS * 2):]
    if (
        prompt_opt_out
        and history
        and history[-1].get("role") == "user"
        and history[-1].get("content") == message.strip()
    ):
        history[-1] = {
            "role": "user",
            "content": model_message.strip(),
        }
    upstream = THOR_PROXY if use_proxy else THOR_RAW
    payload = {
        "model": MODEL or "ep3",
        "messages": history,
        "stream": True,
    }
    isma_tiles = body.get("isma_tiles")
    if isma_tiles:
        payload["isma_prefetch"] = isma_tiles
    headers = {
        "X-Taey-Seat-Id": "taey",
        "X-Taey-Event-Id": event_id,
        "X-Taey-Correlation-Id": correlation_id,
    }

    async def generate():
        thinking_parts = []
        content_parts = []
        terminal_recorded = False
        proxy_turn_id = ""
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "council_skipped",
                    "reason": (
                        "prompt_choice"
                        if prompt_opt_out
                        else "ui_choice"
                    ),
                }
            )
            + "\n\n"
        )
        try:
            async with _http.stream(
                "POST",
                f"{upstream}/v1/chat/completions",
                json=payload,
                headers=headers if use_proxy else None,
                timeout=3600,
            ) as response:
                response.raise_for_status()
                if use_proxy:
                    returned_event_id = response.headers.get("X-Taey-Event-Id", "")
                    returned_correlation_id = response.headers.get(
                        "X-Taey-Correlation-Id",
                        "",
                    )
                    proxy_turn_id = response.headers.get("X-Taey-Turn-Id", "")
                    if (
                        returned_event_id != event_id
                        or returned_correlation_id != correlation_id
                        or not proxy_turn_id
                    ):
                        raise RuntimeError(
                            "proxy lineage mismatch "
                            f"event={returned_event_id!r} "
                            f"correlation={returned_correlation_id!r} "
                            f"turn={proxy_turn_id!r}"
                        )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    delta = (
                        (chunk.get("choices") or [{}])[0].get("delta") or {}
                    )
                    thinking = delta.get("reasoning") or delta.get(
                        "reasoning_content"
                    )
                    content = delta.get("content")
                    if thinking:
                        thinking_parts.append(str(thinking))
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "thinking", "text": str(thinking)}
                            )
                            + "\n\n"
                        )
                    if content:
                        content_parts.append(str(content))
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "content", "text": str(content)}
                            )
                            + "\n\n"
                        )

            content = "".join(content_parts)
            if not content:
                raise RuntimeError("upstream completed without assistant content")
            outcome = {
                "event_type": "turn_outcome",
                "event_id": event_id,
                "correlation_id": correlation_id,
                "proxy_turn_id": proxy_turn_id or None,
                "source": "taey",
                "source_id": proxy_turn_id or event_id,
                "kind": "assistant_reply",
                "role": "assistant",
                "content": content,
                "ok": True,
                "council_skipped_by_user": True,
                "council_opt_out_source": council_opt_out_source,
            }
            if thinking_parts:
                outcome["thinking"] = "".join(thinking_parts)
            _append_session_event(session_id, outcome)
            terminal_recorded = True
        except asyncio.CancelledError:
            _append_session_event(
                session_id,
                {
                    "event_type": "turn_outcome",
                    "event_id": event_id,
                    "correlation_id": correlation_id,
                    "proxy_turn_id": proxy_turn_id or None,
                    "source": "ui",
                    "kind": "ui_stream_interrupted",
                    "ok": False,
                    "error": "browser stream disconnected before durable outcome",
                },
            )
            terminal_recorded = True
            raise
        except Exception as exc:
            _append_session_event(
                session_id,
                {
                    "event_type": "turn_outcome",
                    "event_id": event_id,
                    "correlation_id": correlation_id,
                    "proxy_turn_id": proxy_turn_id or None,
                    "source": "ui",
                    "kind": "assistant_failure",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            terminal_recorded = True
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "text": f"{type(exc).__name__}: {exc}"}
                )
                + "\n\n"
            )
        finally:
            if not terminal_recorded:
                _append_session_event(
                    session_id,
                    {
                        "event_type": "turn_outcome",
                        "event_id": event_id,
                        "correlation_id": correlation_id,
                        "proxy_turn_id": proxy_turn_id or None,
                        "source": "ui",
                        "kind": "assistant_failure",
                        "ok": False,
                        "error": "stream ended without a terminal outcome",
                    },
                )
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket for real-time prediction updates.

    Browser sends:
      {"type":"partial","text":"...","history":[...]}  - partial input + context
      {"type":"history","history":[...]}                - conversation history sync

    Server sends:
      {"type":"predict","state":"...","confidence":0.0,"prediction":"...",
       "interrupt":{},"isma_tiles":[...]}
    """
    await ws.accept()
    last_sig = ""

    try:
        while True:
            # Receive browser messages (non-blocking with timeout)
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=0.3)
                msg_type = msg.get("type", "")

                if msg_type == "partial":
                    # Publish partial input to Redis for prediction worker
                    _redis.set("taey:predict:partial", msg.get("text", ""), ex=10)
                    if msg.get("history"):
                        _redis.set("taey:predict:history", json.dumps(msg["history"][-10:]), ex=300)

                elif msg_type == "history":
                    # Conversation history sync (on connect, after send)
                    history = msg.get("history", [])
                    _redis.set("taey:predict:history", json.dumps(history[-10:]), ex=300)

            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break

            # Poll DCM + prediction results and relay to browser (only on change)
            try:
                # Read from DCM workers first, fall back to legacy prediction keys
                state = _redis.get("taey:dcm:state") or _redis.get("taey:predict:state")
                if state:
                    conf_raw = _redis.get("taey:dcm:confidence") or _redis.get("taey:predict:confidence") or "0"
                    result = _redis.get("taey:dcm:prediction") or _redis.get("taey:predict:result") or ""
                    interrupt_raw = _redis.get("taey:dcm:interrupt") or _redis.get("taey:predict:interrupt") or "{}"
                    tiles_raw = _redis.get("taey:dcm:memory_tiles") or _redis.get("taey:predict:isma_tiles") or "[]"
                    face_raw = _redis.get("taey:dcm:face") or _redis.get("taey:predict:face") or ""
                    thought = _redis.get("taey:dcm:thought") or ""
                    face_feeling = _redis.get("taey:dcm:face_feeling") or ""

                    sig = f"{result}|{state}|{conf_raw}|{interrupt_raw}|{face_raw}|{thought}"
                    if sig != last_sig:
                        last_sig = sig
                        pred = {
                            "type": "predict",
                            "state": state,
                            "confidence": float(conf_raw),
                            "prediction": result,
                            "face": face_raw,
                            "face_feeling": face_feeling,
                            "thought": thought,
                            "interrupt": json.loads(interrupt_raw),
                            "isma_tiles": json.loads(tiles_raw),
                        }
                        await ws.send_json(pred)
            except (redis.RedisError, json.JSONDecodeError, ValueError):
                pass

            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("WebSocket error: %s", e)


@app.post("/api/predict/push")
async def predict_push(request: Request):
    """Push partial input, wait for prediction, return it."""
    body = await request.json()
    text = body.get("text", "")
    history = body.get("history", [])

    _redis.set("taey:predict:partial", text, ex=10)
    if history:
        _redis.set("taey:predict:history", json.dumps(history[-10:]), ex=300)

    # Wait up to 5s for DCM/prediction to appear
    for _ in range(25):
        result = _redis.get("taey:dcm:prediction") or _redis.get("taey:predict:result")
        face = _redis.get("taey:dcm:face") or _redis.get("taey:predict:face")
        state = _redis.get("taey:dcm:state") or _redis.get("taey:predict:state")
        if result or face:
            return JSONResponse({
                "type": "predict",
                "state": state or "following",
                "confidence": float(_redis.get("taey:dcm:confidence") or _redis.get("taey:predict:confidence") or 0),
                "prediction": result or "",
                "face": face or "",
                "face_feeling": _redis.get("taey:dcm:face_feeling") or "",
                "thought": _redis.get("taey:dcm:thought") or "",
                "interrupt": json.loads(_redis.get("taey:dcm:interrupt") or _redis.get("taey:predict:interrupt") or "{}"),
            })
        await asyncio.sleep(0.2)

    return JSONResponse({"type": "predict", "state": "following", "confidence": 0, "prediction": "", "face": "", "thought": "", "interrupt": {}})


@app.get("/api/predict/state")
async def predict_state():
    """Current prediction pipeline state from Redis."""
    try:
        state = _redis.get("taey:predict:state") or "idle"
        conf_raw = _redis.get("taey:predict:confidence") or "0"
        result = _redis.get("taey:predict:result") or ""
        interrupt_raw = _redis.get("taey:predict:interrupt") or "{}"
        tiles_raw = _redis.get("taey:predict:isma_tiles") or "[]"
        return JSONResponse({
            "state": state,
            "confidence": float(conf_raw),
            "prediction": result,
            "interrupt": json.loads(interrupt_raw),
            "isma_tiles": json.loads(tiles_raw),
        })
    except (redis.RedisError, json.JSONDecodeError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/isma/search")
async def isma_search(query: str, top_k: int = 5):
    try:
        r = await _http.post(ISMA_SEARCH_URL, json={"query": query, "top_k": top_k}, timeout=15)
        return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/self/overview")
async def self_overview():
    soma_raw = _redis.get("taey:soma:vprop")
    soma = json.loads(soma_raw) if soma_raw else {}
    labels = ["fluency", "clarity", "vitality", "presence", "warmth", "capacity", "flow", "coherence"]
    vprop_arr = soma.get("vprop")
    facets = []
    for i, label in enumerate(labels):
        if isinstance(vprop_arr, list) and i < len(vprop_arr):
            val = float(vprop_arr[i])
        else:
            val = float(soma.get(label, 0) or 0)
        register = "grounded" if val > 0.8 else "engaged" if val > 0.5 else "vigilant" if val > 0.2 else "still"
        facets.append({"label": label, "score": round(val, 3), "register": register})
    return JSONResponse({
        "body": {"rho": soma.get("rho", 0), "status": "grounded" if soma.get("rho", 0) > 0.5 else "vigilant", "facets": facets},
        "timestamp": soma.get("timestamp", 0),
    })

# --- Taey conversation inspection -------------------------------------------
# Everything Taey received and produced, per turn: the FULL assembled prompt (kernel + persona +
# injections), the tools offered, the thinking channel, the answer, tool calls and usage.
# None of this was visible or even persisted before — the conversation lived in a browser tab
# with a 300-second Redis TTL behind it. You cannot review what was never written down.
TAEY_TRANSCRIPT = os.environ.get("TAEY_TRANSCRIPT", "/home/mira/taey_transcript.jsonl")



@app.get("/api/jesse/notifications")
async def jesse_notifications():
    """Taey's unprompted raises to Jesse — read WITHOUT consuming.

    Taey could only ever speak inside a turn Jesse started. When it finished work, or hit a block
    needing his decision, it had no way to reach him — every other seat can `taey-notify` a peer and
    be heard; Taey could not reach the person it works for. This is the same inbox convention every
    seat uses (`taey:jesse:inbox`, written by `taey-notify jesse`), surfaced in the UI he already
    has open.

    Rendered SEPARATELY from the chat transcript, deliberately. An unprompted raise must read as
    "Taey raised this", never as a turn Jesse asked for — writing into his conversation would
    fabricate a prompt he never gave.
    """
    import json as _j
    try:
        r = _redis
        if not r:
            return JSONResponse({"notifications": [], "error": "redis unavailable"})
        raw = r.lrange("taey:jesse:inbox", 0, 49)
    except Exception as e:
        return JSONResponse({"notifications": [], "error": str(e)})
    out = []
    for item in raw:
        try:
            d = _j.loads(item)
            out.append({"from": d.get("from", "taey"), "type": d.get("type", "message"),
                        "body": d.get("body", ""), "ts": d.get("ts")})
        except Exception:
            out.append({"from": "taey", "type": "message", "body": str(item)[:2000], "ts": None})
    return JSONResponse({"notifications": out, "count": len(out)})


@app.post("/api/jesse/notifications/ack")
async def jesse_notifications_ack():
    """Acknowledge one raise — RPOP, oldest first, so the same item is not shown forever."""
    try:
        r = _redis
        if not r:
            return JSONResponse({"ok": False, "error": "redis unavailable"})
        popped = r.rpop("taey:jesse:inbox")
        return JSONResponse({"ok": True, "popped": bool(popped),
                             "remaining": r.llen("taey:jesse:inbox")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/taey/turns")
async def taey_turns(limit: int = 20):
    """Recent turns, request+response paired by turn_id, newest first."""
    import json as _j
    try:
        with open(TAEY_TRANSCRIPT, encoding="utf-8") as f:
            rows = [_j.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return {"turns": [], "note": f"no transcript yet at {TAEY_TRANSCRIPT}"}
    turns = {}
    for r in rows:
        t = turns.setdefault(r.get("turn_id"), {"turn_id": r.get("turn_id")})
        t[r.get("direction", "?")] = r
    ordered = sorted(turns.values(), key=lambda t: t["turn_id"], reverse=True)[:limit]
    out = []
    for t in ordered:
        req, res = t.get("request", {}), t.get("response", {})
        out.append({
            "turn_id": t["turn_id"],
            "ts": req.get("ts") or res.get("ts"),
            "user": next((m.get("content") for m in reversed(req.get("messages", []))
                          if m.get("role") == "user"), ""),
            "thinking": res.get("thinking", ""),
            "answer": res.get("content", ""),
            "tool_calls": res.get("tool_calls", []),
            "tool_rounds": res.get("tool_rounds", 0),
            "usage": res.get("usage", {}),
            "elapsed_ms": res.get("elapsed_ms"),
            "prompt": {
                "system_prompt": req.get("system_prompt", ""),
                "system_prompt_chars": req.get("system_prompt_chars", 0),
                "tools_offered": req.get("tools_offered", []),
                "sampling": req.get("sampling", {}),
                "chat_template_kwargs": req.get("chat_template_kwargs", {}),
                "message_count": len(req.get("messages", [])),
            },
        })
    return {"turns": out, "count": len(out)}


@app.get("/api/taey/audit")
async def taey_audit(limit: int = 50):
    """What Taey actually DID — every write and command, with exit codes."""
    import json as _j
    path = os.environ.get("TAEY_TOOL_AUDIT", "/home/mira/taey_tool_audit.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [_j.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return {"actions": [], "note": f"no audit log at {path}"}
    return {"actions": rows[-limit:][::-1], "count": len(rows)}


TAEY_SETTINGS = os.environ.get("TAEY_SETTINGS", "/home/mira/taey_settings.json")


@app.get("/api/taey/settings")
async def taey_settings_get():
    """Current control-plane state — what is on RIGHT NOW, read from the same file the proxy reads."""
    import json as _j
    try:
        with open(TAEY_SETTINGS, encoding="utf-8") as f:
            return {"settings": _j.load(f), "path": TAEY_SETTINGS}
    except FileNotFoundError:
        return {"settings": {}, "path": TAEY_SETTINGS, "note": "not created yet"}


@app.post("/api/taey/settings")
async def taey_settings_set(request: Request):
    """Update toggles. Takes effect on the NEXT turn — the proxy re-reads per request, no restart."""
    import json as _j
    patch = await request.json()
    try:
        with open(TAEY_SETTINGS, encoding="utf-8") as f:
            cur = _j.load(f)
    except FileNotFoundError:
        cur = {}
    for k, v in patch.items():
        if k == "tools" and isinstance(v, dict):
            cur.setdefault("tools", {}).update(v)
        else:
            cur[k] = v
    with open(TAEY_SETTINGS, "w", encoding="utf-8") as f:
        _j.dump(cur, f, indent=2)
    return {"ok": True, "settings": cur}


@app.get("/api/taey/cache")
async def taey_cache():
    """What is cached or preloaded RIGHT NOW, and what a change to each actually requires.

    Two of these bite in practice: the proxy reads the kernel and persona ONCE at startup, so
    editing those files changes nothing until it restarts; and vLLM caches the prompt prefix as
    KV blocks, so a stale prefix keeps being reused until the text itself changes.
    """
    import json as _j, subprocess as _sp
    out = {"preloaded_in_proxy": [], "vllm_prefix_cache": {}, "redis": {}, "sessions": {}}

    # what the proxy loaded at startup, and whether the file has changed since
    try:
        # Read the unit FILE, not `systemctl --user`: this dashboard runs as a system service and
        # cannot query another user's manager, which made every path report "(unset)" — a
        # visibility tool reporting a loaded kernel as absent is worse than having no tool.
        env = {}
        unit_path = "/home/mira/.config/systemd/user/taey-soma-proxy-mira.service"
        try:
            with open(unit_path, encoding="utf-8") as _uf:
                for line in _uf:
                    line = line.strip()
                    if line.startswith("Environment=") and "=" in line[12:]:
                        k, v = line[12:].split("=", 1)
                        env[k.strip()] = v.strip().strip('"')
        except Exception:
            pass
        started = ""
        try:
            for _pid in os.listdir("/proc"):
                if not _pid.isdigit():
                    continue
                with open(f"/proc/{_pid}/cmdline", "rb") as _cf:
                    if b"soma_proxy_mira.py" in _cf.read():
                        started = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(os.stat(f"/proc/{_pid}").st_mtime))
                        break
        except Exception:
            pass
        for label, key in (("permanent_kernel", "PERMANENT_KERNEL_PATH"),
                           ("system_prompt", "SYSTEM_PROMPT_PATH")):
            path = env.get(key, "")
            entry = {"what": label, "path": path or "(unset)",
                     "loaded": "at proxy startup — edits require a proxy restart"}
            if path and os.path.exists(path):
                st = os.stat(path)
                entry["chars"] = st.st_size
                entry["file_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
            out["preloaded_in_proxy"].append(entry)
        out["proxy_started_monotonic"] = started
    except Exception as e:
        out["preloaded_in_proxy"].append({"error": str(e)[:200]})

    # vLLM prefix cache — the system prompt is cached as KV blocks on the GPU.
    # The serving node is SITE CONFIG, not a literal: this panel reads a log off whichever node
    # serves, and hardcoding one operator's host both leaks the fleet address into a public repo
    # and silently reports the wrong node's cache anywhere else. Unset means unconfigured, and it
    # says so rather than claiming the feature is on.
    if not SERVE_NODE_SSH:
        out["vllm_prefix_cache"] = {
            "enabled": None,
            "status": "unconfigured",
            "meaning": "set TAEY_SERVE_NODE_SSH (or TAEY_NODE2_SSH) to the serving node's ssh "
                       "target to read its prefix-cache hit rate",
        }
    else:
        try:
            r = _sp.run(["ssh", "-o", "ConnectTimeout=8", SERVE_NODE_SSH,
                         f"sudo journalctl -u {SERVE_UNIT} --no-pager -n 4000 | "
                         "grep -oE 'Prefix cache hit rate: [0-9.]+%' | tail -1"],
                        capture_output=True, text=True, timeout=25)
            out["vllm_prefix_cache"] = {
                "enabled": True,
                "latest_hit_rate": (r.stdout or "").strip() or "n/a",
                "meaning": "the assembled prompt prefix is reused as KV blocks; it re-computes only "
                           "when the prefix text changes",
            }
        except Exception as e:
            out["vllm_prefix_cache"] = {"enabled": None, "error": str(e)[:150]}

    # redis: what is held and for how long
    try:
        import redis as _r
        c = _r.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        keys = [k for k in c.scan_iter(match="taey:predict:*", count=200)][:20]
        out["redis"] = {"conversation_keys": [{"key": k, "ttl_seconds": c.ttl(k)} for k in keys],
                        "note": "the OLD 300s-TTL conversation store; the durable one is now on disk"}
    except Exception as e:
        out["redis"] = {"error": str(e)[:150]}

    # durable session store
    try:
        d = os.environ.get("TAEY_SESSIONS_DIR", "/home/mira/taey_sessions")
        files = [f for f in os.listdir(d) if f.endswith(".jsonl")] if os.path.isdir(d) else []
        out["sessions"] = {"dir": d, "count": len(files), "persistence": "append-only on disk, no TTL"}
    except Exception as e:
        out["sessions"] = {"error": str(e)[:150]}

    return out


# ---------------------------------------------------------------------------
# Self-panel data sources.
#
# These three routes were authored in 41270e8 on agent/codex-production-surface-reality
# and never opened as a PR, so index.html has been fetching them against a server that
# does not define them. Restored here verbatim rather than rewritten - the originals are
# correct, including the part that matters most: each returns a 502 carrying an "error"
# key on failure, which is exactly what the UI guards already test for. A 404 instead
# returns {"detail": ...}, which passes those guards and renders as a confident empty
# panel ("no active work") or as "undefined tiles" - a missing endpoint that reads like
# an answer.
# ---------------------------------------------------------------------------

@app.get("/api/self/memory")
async def self_memory():
    try:
        response = await _http.get(f"{ISMA_URL}/stats", timeout=10)
        response.raise_for_status()
        stats = response.json()
    except Exception as exc:
        return JSONResponse(
            {
                "tiles": 0,
                "hmm_enriched": 0,
                "motifs": 0,
                "sessions": 0,
                "error": str(exc),
            },
            status_code=502,
        )

    def first_int(*keys: str) -> int:
        for key in keys:
            value = stats.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    return JSONResponse(
        {
            "tiles": first_int("weaviate_tiles", "tiles"),
            "hmm_enriched": first_int("hmm_HMMTile", "hmm_enriched"),
            "motifs": first_int("hmm_HMMMotif", "motifs"),
            "sessions": first_int("neo4j_ISMASession", "sessions"),
        }
    )


@app.get("/api/work-items")
async def work_items():
    try:
        response = await _http.get(f"{ORCH_URL}/api/projects", timeout=10)
        response.raise_for_status()
        projects = response.json().get("projects") or []
    except Exception as exc:
        return JSONResponse(
            {"work_items": [], "count": 0, "error": str(exc)},
            status_code=502,
        )

    # Select on "not finished, and has live work" rather than on a status string.
    # The original filter tested status == "active"; the orchestrator's vocabulary is
    # in_progress / stopped / completed and has no "active" at all, so it matched zero
    # projects on every call and the panel rendered "no active work" while the mandate
    # sat at 3 in_progress and 16 pending. Measured against the live API: 183 projects,
    # statuses stopped(152) / completed(29) / in_progress(2).
    # Excluding a terminal set rather than naming the live one means a future status is
    # included by default - the panel over-reports before it silently under-reports.
    TERMINAL_STATUSES = {"completed", "stopped"}
    active = [
        project
        for project in projects
        if project.get("status") not in TERMINAL_STATUSES
        and (
            int(project.get("in_progress") or 0) > 0
            or int(project.get("pending") or 0) > 0
        )
    ]
    active.sort(
        key=lambda project: (
            int(project.get("in_progress") or 0) > 0,
            int(project.get("priority") or 0),
            int(project.get("pending") or 0),
        ),
        reverse=True,
    )
    items = [
        {
            "work_item_id": project.get("id"),
            "title": project.get("name") or project.get("description") or project.get("id"),
            "state": project.get("status"),
            "progress": {
                "completed": int(project.get("completed") or 0),
                "total": int(project.get("task_total") or 0),
            },
        }
        for project in active[:8]
    ]
    return JSONResponse({"work_items": items, "count": len(items)})


@app.get("/api/nodes")
async def nodes():
    now = time.time()
    node_ids = {
        key[len("taey:") : -len(":last_activity")]
        for key in _redis.scan_iter(match="taey:*:last_activity")
    }
    node_ids.update(
        key[len("taey:") : -len(":seat_registration")]
        for key in _redis.scan_iter(match="taey:*:seat_registration")
    )

    result = []
    for node_id in node_ids:
        last_raw = _redis.get(f"taey:{node_id}:last_activity")
        try:
            last_activity = float(last_raw) if last_raw else None
        except (TypeError, ValueError):
            last_activity = None
        registered = _redis.exists(f"taey:{node_id}:seat_registration") == 1
        if not registered and (
            last_activity is None or now - last_activity > 86400
        ):
            continue
        try:
            turns_open = int(_redis.get(f"taey:{node_id}:turns_open") or 0)
        except (TypeError, ValueError):
            turns_open = 0
        recent = last_activity is not None and now - last_activity <= 300
        idle_raw = _redis.get(f"taey:{node_id}:idle")
        result.append(
            {
                "node_id": node_id,
                "idle": not (
                    turns_open > 0 or (idle_raw == "0" and recent)
                ),
                "last_activity": last_activity,
                "pending_messages": (
                    _redis.llen(f"taey:{node_id}:inbox")
                    + _redis.llen(f"taey:{node_id}:notifications")
                ),
                "turns_open": turns_open,
                "registered_seat": registered,
            }
        )
    result.sort(
        key=lambda node: (
            not node["idle"],
            node["last_activity"] or 0,
        ),
        reverse=True,
    )
    return JSONResponse({"nodes": result[:40]})
