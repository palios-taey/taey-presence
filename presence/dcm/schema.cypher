// DCM inter-instance shared-state schema.
// Apply this to your OWN no-auth Neo4j to stand up the store:
//   cypher-shell -a bolt://localhost:7687 < dcm/schema.cypher
//
// This ships the SCHEMA ONLY (constraint + index). It contains no data —
// no instances, no state, nothing from anyone else's deployment.

CREATE CONSTRAINT presence_instance_id IF NOT EXISTS
  FOR (i:PresenceInstance) REQUIRE i.instance_id IS UNIQUE;

CREATE INDEX presence_instance_active IF NOT EXISTS
  FOR (i:PresenceInstance) ON (i.active);

// Node shape (written at runtime by inter_instance.py, shown here for reference):
//   (:PresenceInstance {
//      instance_id: string  // unique per instance/worker
//      state:       string  // JSON blob of the instance's current state
//      updated:     datetime
//      active:      boolean
//   })
