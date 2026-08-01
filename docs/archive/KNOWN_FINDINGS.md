# Known Findings

## 2026-07-30 - `/models/...` is a per-node container path, and the two Thors map it differently

Observed:
- Thor1 `10.0.0.8` mounts `/home/jetson/cpt-artifacts` as `/models`. That directory holds only
  `corpora`, `cpt_v7_eps1fix_servable`, `logs`, `tools`.
- Thor2 `10.0.0.197` mounts `/home/thor/serve-models` as `/models`. That directory holds both
  `cpt_v7_eps1fix_servable` and `module5_merged` (52G).
- `module5_merged` also exists on Thor1, at `/home/jetson/serve-models/module5_merged` — a
  directory that is **not** mounted into the serving container on that node.
- Therefore `/models/module5_merged` resolves on Thor2 and does not resolve on Thor1, while
  `/models/cpt_v7_eps1fix_servable` resolves on both. Both nodes serve alias `ep3`.

Impact:
- `/models/<name>` has no single host meaning. A check, script, or document that reasons about a
  `/models/...` path without first resolving that node's mount is measuring nothing.
- The failure is silent and reads as a real answer: running `ls /models/module5_merged` on a *host*
  reports "absent" on both nodes, because `/models` does not exist on either host — it exists only
  inside the container. That produced a confident, wrong "the artifact is gone" report on
  2026-07-30, while 52G of it sat on Thor2 the whole time.
- A single hardcoded `/models/...` string cannot be true of both nodes at once.

Corrective action:
- `serving/TAEY_OPERATING_PROMPT.md` no longer asserts a served artifact path; it directs reading
  `root` from `/v1/models` at the moment the question is asked.
- Correcting the record: the commit that made that change justified it as "asserts a path that does
  not exist." That evidence was wrong — the path resolves on Thor2. The change is still correct, for
  a stronger reason: the artifact behind `ep3` swaps at every promotion **and** the nodes do not
  share a host mapping.
- To resolve a `/models` path for a node, read the mount rather than assuming:
  `docker inspect taey-vllm --format '{{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}'`


## 2026-07-29 - Pre-deploy MAX_TOOL_ROUNDS Regression

Observed:
- Live production `/home/mira/taey-presence-validate/soma_proxy_mira.py` defaulted `MAX_TOOL_ROUNDS` to 60.
- Candidate commit `f01ee6f` defaulted `serving/soma_proxy.py` `MAX_TOOL_ROUNDS` to 8.
- The inspected systemd environment had no `MAX_TOOL_ROUNDS` override, so deploying the candidate as-is would have used the lower default.
- Production was not restarted during this finding.

Impact:
- The candidate would have regressed long tool workflows by forcing final prose after 8 rounds instead of preserving the live 60-round ceiling.

Corrective action:
- Candidate `serving/soma_proxy.py` now preserves the live default of 60 while retaining the `MAX_TOOL_ROUNDS` environment override.
