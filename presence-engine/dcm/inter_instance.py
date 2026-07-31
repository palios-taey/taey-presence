"""DCM — inter-instance state over a shared Neo4j.

STATUS: publish-only as wired. Each instance WRITES its current state to a
shared Neo4j graph (`PresenceInstance` nodes) via `write_state()`. The
`read_peer_states()` method is implemented and works, but the engine loop does
NOT currently call it — peer state is published but not yet read back into
prediction/interrupt decisions. So this provides inter-instance *telemetry*,
not yet inter-instance *coordination*. Wiring `read_peer_states()` into the
loop is a documented future enhancement, not a present capability.

NO AUTH. Connects to a Neo4j running auth-disabled (server-side
`NEO4J_AUTH=none`) on your own trusted network — no credential in this code by
design. To stand up your own store, apply `dcm/schema.cypher` (constraint +
index only; no data).

If Neo4j is unreachable the engine runs fine — DCM simply does nothing. It is
optional, never a hard dependency.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("dcm")


class InterInstanceState:
    """Shared-state read/write over a no-auth Neo4j.

    `bolt_url` points at your own Neo4j (e.g. bolt://localhost:7687). No auth
    is passed — the server is expected to run with auth disabled on a trusted
    network. Construct with bolt_url=None to disable DCM entirely (no-op).
    """

    PEER_FRESH_SECONDS = 60  # peers stale after this are ignored

    def __init__(self, bolt_url: Optional[str] = None):
        self._driver = None
        self._bolt = bolt_url
        if bolt_url:
            self._connect(bolt_url)

    def _connect(self, bolt_url: str) -> None:
        try:
            from neo4j import GraphDatabase
            # NO AUTH — auth=None connects to a Neo4j running with auth
            # disabled. No credential by design.
            self._driver = GraphDatabase.driver(bolt_url)
            self._driver.verify_connectivity()
            self._ensure_schema()
        except Exception as e:
            log.warning("Neo4j unavailable (%s) — DCM disabled, running standalone", e)
            self._driver = None

    def _ensure_schema(self) -> None:
        # Idempotent — same DDL as dcm/schema.cypher. Ships schema, not data.
        with self._driver.session() as s:
            s.run("""CREATE CONSTRAINT presence_instance_id IF NOT EXISTS
                     FOR (i:PresenceInstance) REQUIRE i.instance_id IS UNIQUE""")
            s.run("""CREATE INDEX presence_instance_active IF NOT EXISTS
                     FOR (i:PresenceInstance) ON (i.active)""")

    def write_state(self, instance_id: str, state: dict) -> None:
        """Publish this instance's current state for peers to read."""
        if not self._driver:
            return
        try:
            with self._driver.session() as s:
                s.run("""
                    MERGE (i:PresenceInstance {instance_id: $id})
                    SET i.state = $state, i.updated = datetime(), i.active = true
                """, id=instance_id, state=json.dumps(state))
        except Exception as e:
            log.debug("DCM write failed: %s", e)

    def read_peer_states(self, exclude_id: Optional[str] = None) -> list:
        """Read all fresh peer instance states."""
        if not self._driver:
            return []
        try:
            with self._driver.session() as s:
                result = s.run("""
                    MATCH (i:PresenceInstance)
                    WHERE i.active = true
                      AND i.updated > datetime() - duration({seconds: $fresh})
                      AND ($exclude IS NULL OR i.instance_id <> $exclude)
                    RETURN i.instance_id AS id, i.state AS state
                """, fresh=self.PEER_FRESH_SECONDS, exclude=exclude_id)
                return [{"id": r["id"], "state": json.loads(r["state"])} for r in result]
        except Exception:
            return []

    def close(self) -> None:
        if self._driver:
            self._driver.close()
