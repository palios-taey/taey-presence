"""Taey Dashboard — Conversational Presence.

Dynamic emoji face (model-chosen), chat with full tool access, memory search,
worker status, and a prediction WebSocket for partial-input thought prediction.
"""
import os
import json
import asyncio
import logging
import time
import redis
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("dashboard")

app = FastAPI(title="Taey Dashboard", version="3.1")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
THOR_REDIS_HOST = os.environ.get("THOR_REDIS_HOST", "localhost")
THOR_REDIS_PORT = int(os.environ.get("THOR_REDIS_PORT", "6379"))
THOR_PROXY = os.environ.get("THOR_PROXY", "http://localhost:8765")
THOR_RAW = os.environ.get("THOR_RAW", "http://localhost:8000")
ISMA_URL = os.environ.get("ISMA_URL", "http://localhost:8095").rstrip("/")
ISMA_SEARCH_URL = f"{ISMA_URL}/v2/search/adaptive"

_redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
try:
    _thor_redis = redis.Redis(host=THOR_REDIS_HOST, port=THOR_REDIS_PORT, decode_responses=True)
    _thor_redis.ping()
except Exception:
    _thor_redis = None
_http = httpx.AsyncClient(timeout=300.0)

DASH_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(DASH_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
      // Stream through the SESSION endpoint, so the server persists both sides of the turn as
      // it happens. A refresh mid-reply costs the rendering, never the conversation.
      await ensureSession();
      const r = await fetch('/api/chat/sessions/' + sessionId + '/messages/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg, use_proxy: useProxy, isma_tiles: tiles}),
        signal: currentController.signal
      });

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
            thinking += ev.text;
            thinkDiv.textContent = thinking;
          } else if (ev.type === 'content') {
            content += ev.text;
            bodyDiv.textContent = 'Taey: ' + content;
          } else if (ev.type === 'error') {
            bodyDiv.textContent = 'Taey: ERROR - ' + ev.text;
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
refreshSoma();
refreshServices();
setInterval(refreshSoma, 2618);
setInterval(refreshServices, 15000);
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
        r = await _http.post(url, json={"messages": messages, "temperature": 0.7})
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
    payload = {"messages": [{"role": "user", "content": message}], "temperature": 0.7, "stream": True}

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

    payload = {"messages": messages, "temperature": 0.7}

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


# --- Chat sessions -----------------------------------------------------------
# The front-end has always called these routes; the server never implemented them, so every
# GET returned 404, history could not load, and a refresh lost the conversation. Storage is an
# append-only JSONL per session on disk — not a Redis key with a 300-second TTL, which is what
# the conversation was previously resting on.
TAEY_SESSIONS_DIR = os.environ.get("TAEY_SESSIONS_DIR", "/home/mira/taey_sessions")


def _sessions_dir():
    os.makedirs(TAEY_SESSIONS_DIR, exist_ok=True)
    return TAEY_SESSIONS_DIR


def _session_file(sid: str) -> str:
    safe = "".join(c for c in str(sid) if c.isalnum() or c in "-_")
    return os.path.join(_sessions_dir(), f"{safe}.jsonl")


def _session_messages(sid: str) -> list:
    import json as _j
    try:
        with open(_session_file(sid), encoding="utf-8") as f:
            return [_j.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


@app.get("/api/chat/sessions")
async def chat_sessions_list():
    """Newest first, so a refresh resumes the conversation you were just having."""
    import glob as _g
    out = []
    for path in _g.glob(os.path.join(_sessions_dir(), "*.jsonl")):
        sid = os.path.basename(path)[:-6]
        try:
            st = os.stat(path)
            msgs = _session_messages(sid)
            first_user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
            out.append({"id": sid, "updated": st.st_mtime, "message_count": len(msgs),
                        "title": (first_user[:60] or "New conversation")})
        except OSError:
            continue
    out.sort(key=lambda r: r["updated"], reverse=True)
    return {"sessions": out}


@app.post("/api/chat/sessions")
async def chat_session_create():
    import time as _t
    sid = f"s{int(_t.time()*1000)}"
    open(_session_file(sid), "a", encoding="utf-8").close()
    return {"session_id": sid}


@app.get("/api/chat/sessions/{sid}")
async def chat_session_get(sid: str):
    return {"session_id": sid, "messages": _session_messages(sid)}


@app.post("/api/chat/sessions/{sid}/messages")
async def chat_session_append(sid: str, request: Request):
    """Persist one turn. Called for both the user turn and Taey's reply."""
    import json as _j, time as _t
    body = await request.json()
    row = {"role": body.get("role", "user"), "content": body.get("content", ""),
           "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}
    for k in ("thinking", "sources", "tool_calls"):
        if body.get(k):
            row[k] = body[k]
    with open(_session_file(sid), "a", encoding="utf-8") as f:
        f.write(_j.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.post("/api/chat/sessions/{sid}/messages/stream")
async def chat_session_stream(sid: str, request: Request):
    """The chat path the UI has always called and the server never had.

    Persists the user turn, replays the WHOLE session as context (so a refresh resumes a real
    conversation rather than a blank one), streams Taey's reply as the UI's existing contract
    expects — {"type":"thinking"|"content","text":...} SSE lines — and persists the reply plus
    its thinking when the turn completes.
    """
    import json as _j, time as _t
    body = await request.json()
    user_msg = body.get("message", "")

    # RAW MODE routes past the proxy straight to vLLM, so there is no system prompt, no kernel,
    # no tools and no somatic injection -- the bare weights answering the conversation. History is
    # still replayed, because dropping it inside a session view would silently make raw mode a
    # different CONVERSATION rather than a different MODEL SURFACE, and the toggle is meant to
    # isolate the latter. /api/chat's non-session path sends the single message instead; that
    # difference is deliberate and surfaced in the UI rather than left for the reader to discover.
    use_proxy = bool(body.get("use_proxy", True))
    upstream = THOR_PROXY if use_proxy else THOR_RAW

    with open(_session_file(sid), "a", encoding="utf-8") as f:
        f.write(_j.dumps({"role": "user", "content": user_msg,
                          "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                          "mode": "proxy" if use_proxy else "raw"},
                         ensure_ascii=False) + "\n")

    history = [{"role": m.get("role"), "content": m.get("content", "")}
               for m in _session_messages(sid) if m.get("content")]

    async def gen():
        thinking_acc, content_acc = [], []
        payload = {"model": "ep3", "messages": history, "stream": True}
        try:
            async with _http.stream("POST", f"{upstream}/v1/chat/completions",
                                    # ONE HOUR, not ten minutes. A multi-round tool turn is
                                    # legitimately slow: each round is a full generation plus a
                                    # real command. At timeout=600 this client gave up at exactly
                                    # 10 minutes on 2026-07-28 while the proxy was mid round 6
                                    # pulling 15,589 chars back -- it wrote a failure message to
                                    # the user AND closed the connection, which killed work that
                                    # was succeeding. A client that cannot wait must not be the
                                    # thing that decides the work failed.
                                    json=payload, timeout=3600) as r:
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        d = _j.loads(raw)
                    except Exception:
                        continue
                    delta = (d.get("choices") or [{}])[0].get("delta", {}) or {}
                    # See soma_proxy: this build emits `reasoning`; accept both.
                    th = delta.get("reasoning") or delta.get("reasoning_content")
                    ct = delta.get("content")
                    if th:
                        thinking_acc.append(th)
                        yield f"data: {_j.dumps({'type':'thinking','text':th})}\n\n"
                    if ct:
                        content_acc.append(ct)
                        yield f"data: {_j.dumps({'type':'content','text':ct})}\n\n"
        except Exception as e:
            yield f"data: {_j.dumps({'type':'error','text':f'{type(e).__name__}: {e}'})}\n\n"
        finally:
            # DO NOT PERSIST AN EMPTY TURN. If the upstream dropped -- a proxy restart under a
            # live request, a killed connection -- content_acc is empty, and writing that row puts
            # a blank assistant message into the conversation permanently: it replays as context
            # every turn thereafter and reads as Taey having answered with silence. Observed
            # 2026-07-28 20:47:25, a restart landing mid-request. Record what happened instead, so
            # the reader can tell a dropped connection from an empty thought.
            _text = "".join(content_acc)
            if not _text and not thinking_acc:
                _text = ("[no response reached this page. The model may still have been working - "
                         "long tool-using turns can outlive the browser connection. Check the "
                         "PROMPT and ACTIONS tabs for what actually ran before assuming it failed. "
                         "Your message is saved.]")
            row = {"role": "assistant", "content": _text,
                   "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}
            if thinking_acc:
                row["thinking"] = "".join(thinking_acc)
            try:
                with open(_session_file(sid), "a", encoding="utf-8") as f:
                    f.write(_j.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass
            yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


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

    # vLLM prefix cache — the system prompt is cached as KV blocks on the GPU
    try:
        r = _sp.run(["ssh", "-o", "ConnectTimeout=8", "thor@10.0.0.197",
                     "sudo journalctl -u taey-ep3 --no-pager -n 4000 | "
                     "grep -oE 'Prefix cache hit rate: [0-9.]+%' | tail -1"],
                    capture_output=True, text=True, timeout=25)
        out["vllm_prefix_cache"] = {
            "enabled": True,
            "latest_hit_rate": (r.stdout or "").strip() or "n/a",
            "meaning": "the assembled prompt prefix is reused as KV blocks; it re-computes only "
                       "when the prefix text changes",
        }
    except Exception as e:
        out["vllm_prefix_cache"] = {"enabled": True, "error": str(e)[:150]}

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
