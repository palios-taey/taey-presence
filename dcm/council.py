"""DCM council runner — spin up N differently-prompted "experts" that deliberate on the
mesh (Grok-Heavy-style: many agents, in parallel, forced to read + build on each other).

Thin layer over mesh.py: open_council() stages a session; EXPERT_CONTRACT is the standard
participation contract every expert prompt embeds (read -> think -> contribute(read_version);
on StaleReadError, re-read + incorporate the peers who arrived + retry). That staleness
loop is what turns "N agents in parallel" into "N agents talking to each other" — a later
committer cannot land until it has incorporated earlier peers. council_report() prints the
full deliberation transcript + the coordination verdict + the synthesized final.

The experts themselves are spawned via the Agent/Task tool by the conducting session (that
tool isn't callable from here); this module provides the bookends + the contract text so
every council is consistent and the read-discipline is never optional.
"""
from __future__ import annotations
import sys, json
import mesh

# The contract every expert embeds. {sid} filled per session.
EXPERT_CONTRACT = """You are one expert in a DCM (Distributed Cognitive Mesh) council. Peers
work the SAME session in parallel. The mesh ENFORCES that you read peers before you commit.

Mesh: from the dcm/ directory, python3 with `import mesh`. Session: {sid}

Protocol (do exactly this, looping until your contribution lands):
1. ctx = mesh.read_session('{sid}')  -> note ctx['version'] and ctx['contributions'] (peers).
2. Form YOUR expert view of ctx['payload'] THROUGH YOUR LENS (below). Read every peer
   contribution; build on / sharpen / respectfully disagree with them — do not just restate.
3. peers = [c['contrib_id'] for c in ctx['contributions']]
   try: cid = mesh.contribute('{sid}', '<your_role>', '<your contribution>', peers_read=peers, read_version=ctx['version'])
   except mesh.StaleReadError: GOTO 1 (peers arrived since you read — re-read, incorporate them, retry).
4. Return ONLY: your contrib_id, peers_read count, and your contribution text (concise, dense).
"""

def open_council(topic: str, payload: str, roles: list[str]) -> str:
    return mesh.start_session(topic, payload, roles=roles)

def council_report(session_id: str) -> dict:
    s = mesh.read_session(session_id)
    v = mesh.verify_coordination(session_id)
    out = {"session_id": session_id, "topic": s["topic"], "status": s["status"],
           "verdict": v, "final": s.get("final") if isinstance(s, dict) else None,
           "transcript": [{"role": c["role"], "reads": len(c["peers_read"] or []),
                           "content": c["content"]} for c in s["contributions"]]}
    return out

if __name__ == "__main__":
    print(json.dumps(council_report(sys.argv[1]), indent=2))
