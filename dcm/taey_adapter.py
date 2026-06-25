"""Taey -> DCM adapter: runs the REAL Taey model (served via soma_proxy on Thor2:8765)
as a first-class mesh expert, under the SAME staleness gate as every other participant.

This is the P2 Taey-on-the-mesh path: an instance of Taey reads peers from the mesh,
reasons AS Taey (the soma_proxy injects Taey's persona + ISMA tools), and contributes —
the adapter owns the read+commit so Taey physically can't bypass the read-before-write
contract (adoption enforced for Taey the way the staleness gate enforces it for code agents).

Same chokepoint as the Claude-Code Task contract + the (future) Chats consult adapter:
every participant type funnels through mesh.contribute(read_version=...).
"""
from __future__ import annotations
import os, re, json, urllib.request
import mesh

TAEY_URL = os.environ.get("TAEY_DCM_URL", "http://localhost:8765/v1/chat/completions")  # set to your Taey soma_proxy endpoint
TAEY_MODEL = os.environ.get("TAEY_DCM_MODEL", "/models/taey-phase-combined-v1")


def _ask_taey(system_extra: str, user: str, max_tokens: int = 1500, timeout: int = 300) -> str:
    body = json.dumps({
        "model": TAEY_MODEL,
        "messages": [
            {"role": "system", "content": system_extra},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7, "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(TAEY_URL, body, {"content-type": "application/json"})
    m = json.load(urllib.request.urlopen(req, timeout=timeout))["choices"][0]["message"]
    c = m.get("content") or ""
    return re.sub(r"<think>.*?</think>", "", c, flags=re.DOTALL).strip()


def taey_expert(session_id: str, role: str, lens: str, max_retry: int = 4) -> str:
    """Taey participates as a mesh expert. Reads peers, reasons through `lens`, contributes.
    Retries on StaleReadError (re-reads + incorporates peers who arrived) — same as any expert.
    Returns the contrib_id.
    """
    for _ in range(max_retry):
        ctx = mesh.read_session(session_id)
        peers_txt = "\n\n".join(
            f"[{c['role']}] {c['content']}" for c in ctx["contributions"]) or "(no peers yet — you are first)"
        user = (
            f"You are participating in a Distributed Cognitive Mesh council THROUGH YOUR LENS: {lens}\n\n"
            f"SHARED PROBLEM:\n{ctx['payload']}\n\n"
            f"PEER CONTRIBUTIONS SO FAR (build on / sharpen / respectfully disagree — do not restate):\n{peers_txt}\n\n"
            f"Give your contribution through your lens, concise and dense. This is real design work for the Family.")
        content = _ask_taey(system_extra=f"You are contributing to a DCM council as the '{role}' expert.", user=user)
        peers = [c["contrib_id"] for c in ctx["contributions"]]
        try:
            return mesh.contribute(session_id, role, content, peers_read=peers, read_version=ctx["version"])
        except mesh.StaleReadError:
            continue  # peers arrived; re-read + redo
    raise RuntimeError(f"Taey expert {role} could not land after {max_retry} retries (mesh too hot)")


if __name__ == "__main__":
    import sys
    print(taey_expert(sys.argv[1], sys.argv[2], sys.argv[3]))
