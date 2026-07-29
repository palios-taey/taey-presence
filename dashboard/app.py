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
import redis
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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
TAEY_SESSIONS_DIR = os.environ.get(
    "TAEY_SESSIONS_DIR",
    str(os.path.join(os.path.expanduser("~"), "taey_sessions")),
)
TAEY_CONVERSATION_ID = os.environ.get("TAEY_CONVERSATION_ID", "main")
TAEY_SESSION_MAX_TURNS = max(
    1,
    int(os.environ.get("TAEY_SESSION_MAX_TURNS", "60")),
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DASH_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(DASH_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _session_file(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(str(session_id)):
        raise HTTPException(status_code=400, detail="invalid session id")
    return os.path.join(TAEY_SESSIONS_DIR, f"{session_id}.jsonl")


def _read_session_events(session_id: str) -> list[dict]:
    path = _session_file(session_id)
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as handle:
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


def _append_session_event(session_id: str, event: dict) -> None:
    path = _session_file(session_id)
    os.makedirs(TAEY_SESSIONS_DIR, mode=0o700, exist_ok=True)
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
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
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
          <label><input type="checkbox" id="use-proxy" checked> Full (tools + preamble)</label>
          <label><input type="checkbox" id="raw-mode"> Raw weights</label>
        </div>
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

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

async function sendChat() {
  const input = $('#chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = ''; autoResize(input);
  chatHistory.push({role:'user', content:msg});

  // Clear prediction state on send
  clearGhost();
  dismissInterrupt();

  // Consume pre-fetched ISMA tiles for faster primary response
  const tiles = prefetchedTiles;
  prefetchedTiles = null;

  const log = $('#chat-log');
  const userDiv = document.createElement('div');
  userDiv.className = 'msg-user';
  userDiv.textContent = 'You: ' + msg;
  log.appendChild(userDiv);

  const responseDiv = document.createElement('div');
  responseDiv.className = 'msg-taey';
  responseDiv.innerHTML = '<span class="thinking">Taey is thinking...</span>';
  log.appendChild(responseDiv);
  log.scrollTop = log.scrollHeight;

  $('#send-btn').style.display = 'none';
  $('#stop-btn').style.display = '';

  const useProxy = $('#use-proxy').checked && !$('#raw-mode').checked;
  currentController = new AbortController();

  try {
    const r = await fetch(
      '/api/chat/sessions/' + executiveSessionId + '/messages/stream',
      {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, use_proxy: useProxy, isma_tiles: tiles}),
      signal: currentController.signal
    });
    if (!r.ok) {
      throw new Error(r.status + ' ' + (await r.text()).slice(0, 200));
    }

    responseDiv.innerHTML = '';
    const thinkingDiv = document.createElement('div');
    thinkingDiv.className = 'thinking';
    const contentDiv = document.createElement('div');
    responseDiv.appendChild(thinkingDiv);
    responseDiv.appendChild(contentDiv);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let content = '';
    let thinking = '';
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
        if (event.type === 'thinking') {
          thinking += event.text || '';
          thinkingDiv.textContent = thinking;
        } else if (event.type === 'content') {
          content += event.text || '';
          contentDiv.textContent = 'Taey: ' + content;
        } else if (event.type === 'error') {
          contentDiv.textContent = 'Taey: ERROR — ' + (event.text || 'unknown');
        }
        log.scrollTop = log.scrollHeight;
      }
    }
    if (content) chatHistory.push({role:'assistant', content:content});
    syncHistory();
  } catch(e) {
    if (e.name === 'AbortError') {
      responseDiv.innerHTML += ' <span class="thinking">[stopped]</span>';
    } else {
      responseDiv.textContent = 'Taey: ERROR — ' + e;
    }
  }
  currentController = null;
  $('#send-btn').style.display = '';
  $('#stop-btn').style.display = 'none';
  log.scrollTop = log.scrollHeight;
  refreshSoma(); // get latest stats after response
}

function stopChat() {
  if (currentController) currentController.abort();
}

$('#chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  if (e.key === 'Escape') dismissInterrupt();
});
$('#chat-input').addEventListener('input', e => { autoResize(e.target); debouncedPredict(); });
$('#raw-mode').addEventListener('change', e => { if(e.target.checked) $('#use-proxy').checked=false; });
$('#use-proxy').addEventListener('change', e => { if(e.target.checked) $('#raw-mode').checked=false; });

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
restoreExecutiveSession();
refreshSoma();
refreshServices();
setInterval(refreshSoma, 2618);
setInterval(refreshServices, 15000);
setInterval(() => { if (!currentController) restoreExecutiveSession(); }, 15000);
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
    _append_session_event(
        session_id,
        {
            "event_type": "executive_ingress"
            if role == "user"
            else "turn_outcome",
            "event_id": event_id,
            "correlation_id": str(body.get("correlation_id") or event_id),
            "source": str(body.get("source") or "ui"),
            "kind": "user_prompt" if role == "user" else "assistant_reply",
            "role": role,
            "content": content,
            "ok": True if role == "assistant" else None,
        },
    )
    return {"ok": True, "event_id": event_id}


@app.post("/api/chat/sessions/{session_id}/messages/stream")
async def chat_session_stream(session_id: str, request: Request):
    body = await request.json()
    message = str(body.get("message") or "")
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    use_proxy = bool(body.get("use_proxy", True))
    event_id = uuid.uuid4().hex
    correlation_id = event_id
    _append_session_event(
        session_id,
        {
            "event_type": "executive_ingress",
            "event_id": event_id,
            "correlation_id": correlation_id,
            "source": "ui",
            "source_id": event_id,
            "kind": "user_prompt",
            "role": "user",
            "content": message,
            "mode": "proxy" if use_proxy else "raw",
        },
    )

    history = [
        {
            "role": event["role"],
            "content": str(event["content"]),
        }
        for event in _read_session_events(session_id)
        if event.get("role") in {"user", "assistant"} and event.get("content")
    ][-(TAEY_SESSION_MAX_TURNS * 2):]
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
