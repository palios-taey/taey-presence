"""DCM mesh — the substrate for fleet/Taey real-time cognitive coordination.

Design (2026-06-24, infra), informed by the prior DCM lessons (see dcm/reference/ +
infra-soul/research/dcm_rebuild_foundation.md):

  * The old DCM died on ADOPTION: agents WROTE to the graph but never READ, then
    reported success while working in silos. We make that failure STRUCTURALLY
    IMPOSSIBLE: a contribution records the peer-contributions it consumed, and the
    coordination layer can verify that every non-opening turn actually built on peers.
    "Done" requires evidence of reading — exactly the fleet's evidence-gated discipline,
    encoded into the substrate instead of relying on prompt goodwill.
  * Own graph namespace (:DCMSession / :DCMContribution) on the running fleet Neo4j
    (:7687) — isolated from the orchestrator's Project/Todo graph (respect it as a
    standalone project) and NOT in ISMA (the "sausage", in-progress reasoning, stays
    out of ISMA; only distilled finals flow there via publish_final()).
  * AI-speed / AI-native: no human-facing model. One-read context bundle (read_session)
    is the proven-good primitive — an instance gets the topic + all peer work in one call.

No auth (fleet Neo4j runs auth-disabled on the trusted internal net).
"""
from __future__ import annotations
import os, json, time, uuid
from neo4j import GraphDatabase

DCM_NEO4J_URI = os.environ.get("DCM_NEO4J_URI", "bolt://localhost:7687")
_driver = None


class StaleReadError(Exception):
    """Raised when a contribution is committed against a stale read — peers arrived since
    you read, so you'd be writing without incorporating them. The adoption contract,
    enforced structurally: re-read (read_session) and redo your turn on the fresh state.
    """
    def __init__(self, current_version: int, your_version: int, new_peer_ids: list[str]):
        self.current_version = current_version
        self.your_version = your_version
        self.new_peer_ids = new_peer_ids
        super().__init__(f"stale read: session at v{current_version}, you read v{your_version}; "
                         f"{len(new_peer_ids)} new peer(s) arrived — re-read and incorporate them")


def _db():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(DCM_NEO4J_URI)  # no auth
        _driver.verify_connectivity()
        with _driver.session() as s:
            s.run("CREATE CONSTRAINT dcm_session_id IF NOT EXISTS "
                  "FOR (x:DCMSession) REQUIRE x.session_id IS UNIQUE")
            s.run("CREATE CONSTRAINT dcm_contrib_id IF NOT EXISTS "
                  "FOR (c:DCMContribution) REQUIRE c.contrib_id IS UNIQUE")
    return _driver


def start_session(topic: str, payload: str, roles: list[str] | None = None) -> str:
    """Open a coordination session. payload = the artifact under work (e.g. a draft response)."""
    sid = f"dcm_{uuid.uuid4().hex[:12]}"
    with _db().session() as s:
        s.run("""CREATE (x:DCMSession {session_id:$sid, topic:$topic, payload:$payload,
                 roles:$roles, status:'open', created:$ts})""",
              sid=sid, topic=topic, payload=payload, roles=roles or [], ts=time.time())
    return sid


def read_session(session_id: str) -> dict:
    """One-read context bundle: topic + payload + ALL peer contributions so far.

    This is what an instance calls BEFORE contributing — it returns peer work so the
    instance can build on it. Each contribution carries contrib_id (cite these as peers_read).
    """
    with _db().session() as s:
        rec = s.run("MATCH (x:DCMSession {session_id:$sid}) RETURN x", sid=session_id).single()
        if not rec:
            raise ValueError(f"no DCM session {session_id}")
        x = rec["x"]
        contribs = s.run("""MATCH (c:DCMContribution)-[:IN]->(x:DCMSession {session_id:$sid})
                            RETURN c ORDER BY c.created""", sid=session_id)
        cs = [{"contrib_id": c["c"]["contrib_id"], "role": c["c"]["role"],
               "content": c["c"]["content"],
               "peers_read": c["c"]["peers_read"], "created": c["c"]["created"]}
              for c in contribs]
    return {"session_id": session_id, "topic": x["topic"], "payload": x["payload"],
            "status": x["status"], "contributions": cs, "version": len(cs)}


def contribute(session_id: str, role: str, content: str, peers_read: list[str],
               read_version: int | None = None) -> str:
    """Write a contribution. peers_read = contrib_ids of peers this turn actually consumed.

    Adoption contract, ENFORCED STRUCTURALLY (optimistic concurrency): pass read_version
    (the `version` you got from read_session). If new contributions arrived since — i.e.
    the live version != your read_version — the write is REJECTED with StaleReadError, so
    you cannot commit while ignoring peers who showed up. Re-read and redo. This makes the
    old DCM's fatal failure (write-into-the-void while siloed) impossible by construction.
    The check + create are one atomic Cypher (no check-then-write race). read_version=None
    bypasses the gate (use only for a deliberate opener).
    """
    cid = f"contrib_{uuid.uuid4().hex[:12]}"
    with _db().session() as s:
        if read_version is None:
            s.run("""MATCH (x:DCMSession {session_id:$sid})
                     CREATE (c:DCMContribution {contrib_id:$cid, role:$role, content:$content,
                             peers_read:$peers, created:$ts})-[:IN]->(x)""",
                  sid=session_id, cid=cid, role=role, content=content, peers=peers_read, ts=time.time())
            return cid
        # peers_read is SERVER-DERIVED (the actual peer set present at commit), NOT the
        # caller's claim — agents confabulate their reads (observed live in the N=6 council),
        # so the authoritative honesty record is what the substrate sees, unfakeable. The
        # staleness gate (cur == rv) already guarantees the agent read a version containing
        # exactly these peers, so derived == truly-read. claimed_peers kept for audit only.
        rec = s.run("""MATCH (x:DCMSession {session_id:$sid})
                       OPTIONAL MATCH (c:DCMContribution)-[:IN]->(x)
                       WITH x, collect(c.contrib_id) AS peer_ids, count(c) AS cur
                       WHERE cur = $rv
                       CREATE (n:DCMContribution {contrib_id:$cid, role:$role, content:$content,
                               peers_read: peer_ids, claimed_peers:$claimed, created:$ts})-[:IN]->(x)
                       RETURN n.contrib_id AS cid""",
                    sid=session_id, rv=read_version, cid=cid, role=role,
                    content=content, claimed=peers_read, ts=time.time()).single()
        if rec is None:
            fresh = read_session(session_id)
            known = set(peers_read)
            new_ids = [c["contrib_id"] for c in fresh["contributions"] if c["contrib_id"] not in known]
            raise StaleReadError(fresh["version"], read_version, new_ids)
        return rec["cid"]


def verify_coordination(session_id: str) -> dict:
    """Proof that this was coordination, not silos: did later contributors read peers?

    Returns per-contribution evidence + a verdict. This is the cannot-lie check the old
    DCM lacked — a session where everyone wrote but no one read peers is exposed here.
    """
    sess = read_session(session_id)
    cs = sess["contributions"]
    # A contribution is a silo only if, at the moment it committed, EARLIER peers existed
    # that it did not read. With the staleness gate, such a commit is rejected — so a clean
    # session has none. (Genuine parallel openers that re-read on rejection cite the peer.)
    silos = []
    for i, c in enumerate(cs):
        earlier = {p["contrib_id"] for p in cs[:i]}
        if earlier and not (earlier & set(c["peers_read"] or [])):
            silos.append(c["role"])
    return {"contributions": len(cs),
            "opening": cs[0]["role"] if cs else None,
            "built_on_peers": [c["role"] for i, c in enumerate(cs) if i > 0 and (set(c["peers_read"] or []) & {p["contrib_id"] for p in cs[:i]})],
            "silo_violations": silos,
            "coordinated": len(cs) > 1 and not silos}


def publish_final(session_id: str, final: str) -> None:
    """Close the session; the DISTILLED final is what's eligible to flow to ISMA (not the sausage)."""
    with _db().session() as s:
        s.run("MATCH (x:DCMSession {session_id:$sid}) SET x.status='closed', x.final=$final, x.closed=$ts",
              sid=session_id, final=final, ts=time.time())


if __name__ == "__main__":
    import sys
    print(json.dumps(verify_coordination(sys.argv[1]), indent=2))
