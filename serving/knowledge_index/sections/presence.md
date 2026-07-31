# SECTION: presence
<!--
Source section for the Taey knowledge index, per TAEY_KNOWLEDGE_INDEX_SPEC.md v1 §3.
Owner: infra. Authored and pointer-verified 2026-07-31.

This file is the human-authoring form. It is COMPILED into index.json by build_index.py;
never hand-edit the compiled output — CI recompiles from SOURCE_MANIFEST and fails on any
mismatch (§3).

Every endpoint below is named by ENV VAR, never by host literal (§5 gate G2 class 2).
Every path is repo-relative to this repo (palios-taey/taey-presence).

WHAT THE COMPILER FILLS IN, and why it is not written here: pinned_sha,
generated_at_commit, artifact_commit_sha, artifact_manifest{path,sha256} and
receipts.liveness_sha256 are DERIVED FROM GIT AND FILE CONTENT at build time. Writing
them by hand would be a claim about a commit rather than a reading of one, and the whole
point of the receipt chain is that every hash is computed from the bytes it attests.

LIVENESS PREDICATES ARE EXECUTABLE, NOT PROSE (receipt spec §6). Two forms only: `jq`
(stdout parses as JSON and `jq -e` exits 0) and `text` (anchored POSIX ERE over stdout).
Status codes NEVER appear in a predicate — the probe-shape law is BODIES, NOT CODES: a
200 carrying an error object is not liveness. Every prose expectation this file used to
carry was non-conforming by definition and is recompiled here.
-->

## CAPABILITIES

```json
{
  "id": "presence-serve",
  "kind": "serve",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/SERVING.md",
  "artifact_paths": [
    "serving/SERVING.md",
    "serving/promote_model.sh",
    "serving/deploy_thor.sh",
    "serving/list_ep3_consumers.sh"
  ],
  "bootstrap": {
    "cmd": "bash serving/deploy_thor.sh --check",
    "requires": []
  },
  "liveness": {
    "probe_cmd": "curl -sf \"$TAEY_SERVE_URL/v1/models\"",
    "expect": {
      "lang": "jq",
      "predicate": ".data[0].root | type == \"string\" and (. | length) > 0"
    }
  },
  "endpoints": [
    {
      "name": "openai",
      "env": "TAEY_SERVE_URL",
      "health": "/v1/models"
    }
  ],
  "hardware_tier": "thor-inference",
  "receipts": {
    "liveness": "serving/receipts/presence-serve.liveness.json",
    "usage": "serving/receipts/presence-serve.usage.json"
  },
  "status": "production"
}
```

```json
{
  "id": "presence-proxy",
  "kind": "serve",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/DEPLOYMENT_TOPOLOGY.md",
  "artifact_paths": [
    "serving/soma_proxy.py",
    "serving/DEPLOYMENT_TOPOLOGY.md",
    "serving/TAEY_OPERATING_PROMPT.md"
  ],
  "bootstrap": {
    "cmd": "systemctl --user start taey-soma-proxy-mira.service",
    "requires": [
      "presence-serve"
    ]
  },
  "liveness": {
    "probe_cmd": "curl -sf \"$TAEY_PROXY_URL/v1/models\"",
    "expect": {
      "lang": "jq",
      "predicate": ".data | type == \"array\" and (. | length) > 0 and (.[0].id | type == \"string\")"
    }
  },
  "endpoints": [
    {
      "name": "chat",
      "env": "TAEY_PROXY_URL",
      "health": "/v1/models"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-proxy.liveness.json",
    "usage": "serving/receipts/presence-proxy.usage.json"
  },
  "status": "production"
}
```

```json
{
  "id": "presence-dashboard",
  "kind": "serve",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/DEPLOYMENT_TOPOLOGY.md",
  "artifact_paths": [
    "dashboard/app.py",
    "dashboard/__init__.py",
    "dashboard/static/index.html"
  ],
  "bootstrap": {
    "cmd": "systemctl start taey-dashboard.service",
    "requires": [
      "presence-proxy"
    ]
  },
  "liveness": {
    "probe_cmd": "curl -sf \"$TAEY_DASHBOARD_URL/api/self/overview\"",
    "expect": {
      "lang": "jq",
      "predicate": ".body.rho | type == \"number\""
    }
  },
  "endpoints": [
    {
      "name": "self",
      "env": "TAEY_DASHBOARD_URL",
      "health": "/api/self/overview"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-dashboard.liveness.json",
    "usage": "serving/receipts/presence-dashboard.usage.json"
  },
  "status": "production"
}
```

```json
{
  "id": "presence-seat",
  "kind": "orchestrate",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/SEAT.md",
  "artifact_paths": [
    "serving/taey_council_seat.py",
    "serving/seat_liveness.py",
    "serving/SEAT.md"
  ],
  "bootstrap": {
    "cmd": "python3 serving/taey_council_seat.py",
    "requires": [
      "presence-proxy"
    ]
  },
  "liveness": {
    "probe_cmd": "python3 serving/seat_liveness.py",
    "expect": {
      "lang": "jq",
      "predicate": ".ok == true and .seat_count > 0 and .namespace_declared == true"
    }
  },
  "endpoints": [
    {
      "name": "proxy",
      "env": "TAEY_SEAT_PROXY",
      "health": "/v1/models"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-seat.liveness.json",
    "usage": "serving/receipts/presence-seat.usage.json"
  },
  "status": "production"
}
```

## PROCESSES

PROCESS:  Find out which weights are actually answering as `ep3`
PLAN:     serving/SERVING.md
LAUNCH:   curl -sf "$TAEY_SERVE_URL/v1/models"
EXPECT:   data[0].root is the artifact path; data[0].id is only the alias
ON FAIL:  notify infra; serving/SERVING.md decides whether this is a deploy gap or a defect
NEVER:    never state a weights path from memory or from a document — the alias is permanent and the artifact behind it changes at every promotion

PROCESS:  Promote one checkpoint onto every serving node and prove they are identical
PLAN:     serving/SERVING.md
LAUNCH:   bash serving/promote_model.sh
EXPECT:   every node reports the same data[0].root and the drift gate passes
ON FAIL:  notify infra; do not serve a partially-promoted fleet
NEVER:    never promote by copying to one node and assuming the other; never gate a serving change on byte-identical generated output — greedy output is not byte-stable across a restart

PROCESS:  Check what a serving node is doing before changing anything on it
PLAN:     serving/DEPLOYMENT_TOPOLOGY.md
LAUNCH:   bash serving/list_ep3_consumers.sh
EXPECT:   each consumer classified PINNED-NO-FAILOVER, REDIRECTABLE or WEIGHTS-WATCHER, with RULED OUT / OPEN stated per run
ON FAIL:  notify infra; treat an unmapped consumer as PINNED until proven otherwise
NEVER:    never recall the consumer list from memory or from a document — run the script; a stale list reads exactly like a current one

PROCESS:  Restart a presence service without serving the wrong code
PLAN:     serving/DEPLOYMENT_TOPOLOGY.md
LAUNCH:   bash serving/deploy_thor.sh --check
EXPECT:   every node reports the managed committed artifact and no unmanaged local copy
ON FAIL:  notify infra; a node running an unmanaged copy is a full stop, not a warning
NEVER:    never branch-switch a tree a service runs from — make a worktree; never assume a service runs the file its command line names, because WorkingDirectory plus PYTHONPATH can resolve a different one
