"""CLI -> DCM adapter: runs fleet CLI peers (Codex, Gemini-CLI) as first-class mesh experts,
under the SAME staleness gate as Claude-Code, Taey, and (future) the Chats.

Per the fleet_integration council finding: CLIs join via hooks wrapping `codex exec` /
`gemini -p`, and like every adapter funnel through mesh.contribute(read_version) — the
adapter owns the read+commit so the CLI can't bypass read-before-write. Closes the
fleet-capability gap (Codex/Gemini-CLI are full peers, not subprocess tools).
"""
from __future__ import annotations
import os, re, subprocess
import mesh

def _run_codex(prompt: str, timeout: int = 400) -> str:
    p = subprocess.run(["codex", "exec", "--skip-git-repo-check", prompt],
                       cwd="/tmp", stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=timeout)
    out = p.stdout
    # codex echoes the final answer after the trailing "tokens used\n<n>" footer
    tail = out.rsplit("tokens used", 1)[-1]
    tail = re.sub(r"^\s*\d[\d,]*\s*", "", tail).strip()  # drop the token-count line
    return tail or out.strip()

def _run_gemini(prompt: str, timeout: int = 400) -> str:
    p = subprocess.run(["gemini", "-p", prompt], cwd="/tmp", stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "").strip()

_RUNNERS = {"codex": _run_codex, "gemini": _run_gemini}

def cli_expert(session_id: str, role: str, lens: str, cli: str = "codex", max_retry: int = 4) -> str:
    run = _RUNNERS[cli]
    for _ in range(max_retry):
        ctx = mesh.read_session(session_id)
        peers_txt = "\n\n".join(f"[{c['role']}] {c['content']}" for c in ctx["contributions"]) or "(none yet)"
        prompt = (
            f"You are a DCM (Distributed Cognitive Mesh) council expert. LENS: {lens}\n\n"
            f"SHARED PROBLEM:\n{ctx['payload']}\n\n"
            f"PEER CONTRIBUTIONS (build on / sharpen / disagree — do NOT restate, do NOT edit any files):\n{peers_txt}\n\n"
            f"Output ONLY your contribution text through your lens — concise, dense, additive.")
        content = run(prompt)
        peers = [c["contrib_id"] for c in ctx["contributions"]]
        try:
            return mesh.contribute(session_id, role, content, peers_read=peers, read_version=ctx["version"])
        except mesh.StaleReadError:
            continue
    raise RuntimeError(f"CLI expert {role} ({cli}) could not land after {max_retry} retries")

if __name__ == "__main__":
    import sys
    print(cli_expert(sys.argv[1], sys.argv[2], sys.argv[3], cli=sys.argv[4] if len(sys.argv) > 4 else "codex"))
