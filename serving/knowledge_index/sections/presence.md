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

LIVENESS PREDICATES ARE NOT AUTHORED HERE AT ALL. They live in serving/validate_presence.sh's
LIVENESS ORACLE block — the single authored source — and are COMPILED in by build_index.py.
A predicate written in two places is two predicates, and they drift silently because nothing
compares them. The suite runs them; the index binds them; same bytes, one author.

The grammar itself (receipt spec §6) is two forms only: `jq`
(stdout parses as JSON and `jq -e` exits 0) and `text` (anchored POSIX ERE over stdout).
Status codes NEVER appear in a predicate — the probe-shape law is BODIES, NOT CODES: a
200 carrying an error object is not liveness. Every prose expectation this file used to
carry was non-conforming by definition and is recompiled here.
-->

<!--
WHY serving/gates_manifest.json IS IN EVERY artifact_paths:

The checker reads `gates_manifest_ref` AT the entry's artifact_commit_sha. If the manifest
does not exist at that commit it is unreadable, and an unreadable manifest is a REFUSE —
so a receipt whose artifact predates the manifest can never be accepted, however healthy
the surface is.

Including it here is not a workaround for that; it is the honest statement of what the
artifact is. An entry's deployed artifact includes the GATE CONTRACT it was accepted
under. Change which gates must be green and you have changed the terms of that surface's
production status, which is exactly a new artifact commit.
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
    "serving/deploy_thor.sh",
    "serving/gates_manifest.json",
    "serving/list_ep3_consumers.sh",
    "serving/promote_model.sh"
  ],
  "bootstrap": {
    "cmd": "bash serving/deploy_thor.sh --check",
    "requires": []
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
    "serving/DEPLOYMENT_TOPOLOGY.md",
    "serving/TAEY_CHAT_UI_SEND_SYSTEM.md",
    "serving/TAEY_LINKEDIN_APPLICATION_CLASSIFICATION_SYSTEM.md",
    "serving/TAEY_LINKEDIN_ENGAGERS_SYSTEM.md",
    "serving/TAEY_LINKEDIN_JOBS_RESTORE_SYSTEM.md",
    "serving/TAEY_LINKEDIN_JOBS_SYSTEM.md",
    "serving/TAEY_OPERATING_PROMPT.md",
    "serving/gates_manifest.json",
    "serving/soma_proxy.py",
    "serving/ui_drive.py"
  ],
  "bootstrap": {
    "cmd": "systemctl --user start taey-soma-proxy-mira.service",
    "requires": [
      "presence-serve"
    ]
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
    "dashboard/__init__.py",
    "dashboard/app.py",
    "dashboard/static/index.html",
    "serving/gates_manifest.json"
  ],
  "bootstrap": {
    "cmd": "systemctl start taey-dashboard.service",
    "requires": [
      "presence-proxy"
    ]
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
    "serving/council_prompt_receipt.py",
    "serving/council_prompts/adversarial-failure.md",
    "serving/council_prompts/context-memory.md",
    "serving/council_prompts/control-acceptance.md",
    "serving/council_prompts/evidence-reality.md",
    "serving/council_prompts/options-alternatives.md",
    "serving/council_prompts/scope-intent.md",
    "serving/council_prompts/shared.md",
    "serving/council_prompts/systems-dependencies.md",
    "serving/council_seats.json",
    "serving/SEAT.md",
    "serving/gates_manifest.json",
    "serving/manage_council_seats.py",
    "serving/seat_liveness.py",
    "serving/taey_council_seat.py",
    "serving/taey_seat.py",
    "serving/validate_council_prompt_receipt_producer.py",
    "serving/validate_outbound_request_receipt_bytes.py",
    "serving/validate_repo_root_imports.py",
    "serving/outbound_request_codec.py"
  ],
  "bootstrap": {
    "cmd": "python3 serving/taey_council_seat.py",
    "requires": [
      "presence-proxy"
    ]
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

```json
{
  "id": "presence-soma",
  "kind": "serve",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/DEPLOYMENT_TOPOLOGY.md",
  "artifact_paths": [
    "serving/gates_manifest.json",
    "soma/mira_soma.py"
  ],
  "bootstrap": {
    "cmd": "systemctl start taey-soma.service",
    "requires": []
  },
  "endpoints": [
    {
      "name": "soma",
      "env": "TAEY_DASHBOARD_URL",
      "health": "/api/soma"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-soma.liveness.json",
    "usage": "serving/receipts/presence-soma.usage.json"
  },
  "status": "production"
}
```

```json
{
  "id": "presence-prediction",
  "kind": "serve",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/DEPLOYMENT_TOPOLOGY.md",
  "artifact_paths": [
    "presence/prediction_worker.py",
    "serving/gates_manifest.json"
  ],
  "bootstrap": {
    "cmd": "systemctl start taey-prediction-worker.service",
    "requires": [
      "presence-proxy"
    ]
  },
  "endpoints": [
    {
      "name": "predict",
      "env": "TAEY_DASHBOARD_URL",
      "health": "/api/predict/state"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-prediction.liveness.json",
    "usage": "serving/receipts/presence-prediction.usage.json"
  },
  "status": "production"
}
```

```json
{
  "id": "presence-workers",
  "kind": "orchestrate",
  "repo": {
    "name": "palios-taey/taey-presence",
    "public_url": "https://github.com/palios-taey/taey-presence"
  },
  "entry_doc": "serving/DEPLOYMENT_TOPOLOGY.md",
  "artifact_paths": [
    "presence-engine/engine.py",
    "presence/dcm_presence.py",
    "serving/gates_manifest.json",
    "serving/presence_liveness.py"
  ],
  "bootstrap": {
    "cmd": "systemctl start taey-dcm-presence.service",
    "requires": [
      "presence-proxy"
    ]
  },
  "endpoints": [
    {
      "name": "proxy",
      "env": "TAEY_PROXY_URL",
      "health": "/v1/models"
    }
  ],
  "hardware_tier": "any",
  "receipts": {
    "liveness": "serving/receipts/presence-workers.liveness.json",
    "usage": "serving/receipts/presence-workers.usage.json"
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
