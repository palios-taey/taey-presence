# Root-cause spec — taey-presence dashboard chat + .env (dogfood-found)

Repo: `/home/mira/staging/taey-presence-build`  •  Branch: `serving-stack`  •  Found by: fresh-agent
dogfood following CLAUDE.md PATH A against an Ollama endpoint with isolated Redis/ports.

The presence layer works (soma, face, ghost-text, dashboard all verified live). Three code-level
defects block a clean single-endpoint launch (the common public case). Fix on `serving-stack`.
**6SIGMA: root-cause shape, not patches. Production-verify by re-running the bring-up against a
real Ollama endpoint — NO synthetic unit tests.**

## Defect 1 — `.env` is documentation-only (never loaded)
- **Evidence:** `grep -rn dotenv` across the repo = nothing. Entrypoints read `os.environ` directly.
- **Impact:** README, `.env.example` ("Copy to `.env` and adjust"), and CLAUDE.md all tell the user
  to edit `.env`. It has zero effect — values must be `export`ed. A stranger edits `.env`, launches,
  and silently gets all-localhost defaults with no error.
- **Root-cause fix (makes the documented design true, simplest shape):** load `.env` at process
  start. Add `python-dotenv` to `requirements.txt`, and at the very top of each of the four
  entrypoints — `soma/mira_soma.py`, `presence/prediction_worker.py`, `presence/dcm_presence.py`,
  `dashboard/app.py` — before any `os.environ.get`, call:
  ```python
  from dotenv import load_dotenv; load_dotenv()
  ```
  `load_dotenv()` does NOT override already-exported vars, so it composes with the export path.
  Make it tolerant if python-dotenv is absent (try/except ImportError → no-op) so the dependency
  stays soft.

## Defect 2 — dashboard chat omits the `model` field
- **Evidence:** `dashboard/app.py:706` `r = await _http.post(url, json={"messages": messages, "temperature": 0.7})`.
  Same for the stream/hybrid handlers (~722, ~758). No `model` key. Ollama returns
  `{"error":{"message":"model is required"}}`, then `data["choices"]` KeyErrors → `{"error":"'choices'"}`.
- **Reference (the established pattern, already applied to the workers):** `presence/dcm_presence.py`
  and `presence/prediction_worker.py` build bodies with `**({"model": MODEL} if MODEL else {})`,
  where `MODEL = os.environ.get("MODEL", "")`.
- **Root-cause fix:** define `MODEL = os.environ.get("MODEL", "")` in `dashboard/app.py` and apply the
  same `**({"model": MODEL} if MODEL else {})` spread to ALL chat request bodies (plain, stream,
  hybrid). Consistent MODEL handling across every chat caller — empty MODEL stays omitted (vLLM
  single-model unaffected), set MODEL flows through (Ollama works).

## Defect 3 — dashboard chat ignores VLLM_URL (no single-endpoint default)
- **Evidence:** chat targets `THOR_PROXY` (`:8765`) / `THOR_RAW` (`:8000`); workers target `VLLM_URL`.
  The dashboard was built for the 2-endpoint production topology (fast local prediction model for the
  workers; the real Taey on a Thor for chat). A single-machine public user has ONE endpoint
  (`VLLM_URL`), so the chat reply hits dead `:8765`/`:8000` and fails.
- **Intent / desired shape:** single endpoint is the public default. `VLLM_URL` should be the one
  knob a basic user sets and have the dashboard chat use it; `THOR_PROXY`/`THOR_RAW` remain OPTIONAL
  overrides for the split topology. Recommended root-cause shape: derive the chat base URL from
  `VLLM_URL` (strip the trailing `/v1/chat/completions` to get the base) when `THOR_RAW`/`THOR_PROXY`
  are not explicitly set in the environment, instead of the current hardcoded `localhost:8000/8765`
  defaults. Keep `use_proxy` behavior intact when the proxy IS configured. Use judgment on the
  cleanest expression — the goal is: "set only VLLM_URL → chat works," "set THOR_* → split topology
  still works."

## Production verification (the oracle)
After the fix, re-run the PATH A bring-up from a clean clone against the local Ollama endpoint
(`http://localhost:11434/v1/chat/completions`, MODEL=qwen2.5:3b), isolated Redis, and confirm a
real chat round-trip returns content (not `{"error":"'choices'"}`). Paste that output as evidence.
Do not author unit tests — the live round-trip is the proof.
