# Read-scorer capture contract (design)

Status: **design only**. Not operating authority. Not implemented. Not deployed.

Task: `task-002b1694`. Baseline SHA: `520a517690277687475d185bdd4c2f011bcb4b6f`
(`palios-taey/taey-presence` `origin/main` at design time).

Do **not** resurrect closed PR #84 modules by name or copy. Do **not** add a
parallel non-UI seat. Reuse the live Hub `/v1/chat/completions` tool loop.

## 1. Live topology (Observed 2026-08-20)

| Path | Observation |
|---|---|
| Live checkout | `/home/mira/taey-presence-production` `main` = `520a517690277687475d185bdd4c2f011bcb4b6f`, dirty `AGENTS.md`+`CLAUDE.md` only. Not switched. |
| Hub chat/tools | `taey-soma-proxy-mira.service` ExecStart = production `serving/soma_proxy.py`. Listen health `http://127.0.0.1:8766/health` → `status=healthy`. Process env `VLLM_BASE_URL=http://10.0.0.8:8000`. Catalogue model id `taey`, root `/models/cpt_prod_v5_repos_1ep_servable`. |
| Presence engine | `taey-presence-engine.service` MainPID env: `MODEL_ENDPOINT=http://10.0.0.197:8000`, `MODEL_NAME=ep3`, `THOR_PROXY=http://127.0.0.1:8766`. `InferenceGateway` posts to `MODEL_ENDPOINT` + `/v1/chat/completions` with **no tools** (`engine.py:133-152`). |
| Thor `.197` catalogue | `GET http://10.0.0.197:8000/v1/models` → ids `taey` and `ep3`, root `/models/Qwen3.8-27B`. |
| Thor `.8` catalogue | `GET http://10.0.0.8:8000/v1/models` → id `taey`, root `/models/cpt_prod_v5_repos_1ep_servable`. |
| Port `8765` | connection refused. |
| Existing provenance | live files `TAEY_TOOL_AUDIT=/home/mira/taey_tool_audit.jsonl` and `TAEY_TRANSCRIPT=/home/mira/taey_transcript.jsonl` (process env). Append-only private jsonl, not in the public repo. |

**Split (Observed, not a slogan):** Git/orchestration scorers need the **8766 tool loop**
(`run_command`, `read_file`, …). The engine’s `ep3` path on `.197` is a no-tool
JSON complete used for prediction/interrupt. Do not treat those as one path.

## 2. Current Hub primitives to reuse

Request path in `serving/soma_proxy.py` at this SHA:

1. `POST /v1/chat/completions` → `chat_completions` (`:3166`)
2. `_start_turn` / `_request_context` / `X-Taey-Tool-Profile` (`:2723`)
3. `inject_preamble` then `_chat_completions_for_turn` (`:3200`)
4. non-stream tool rounds: `_http.post("/v1/chat/completions")` to vLLM, then
   `execute_tool_call_async` (`:538`) → `execute_tool_call` (`:1028`)
5. `run_command` → `_do_run_command` (`:1484`) `subprocess.run(..., shell=True)`
6. `_audit` (`:1451`) metadata/receipts to `TAEY_TOOL_AUDIT`

Existing profile table `_TOOL_PROFILE_ALLOWED` (`:165`): `full` (unrestricted) and
`manual-chat-ui` (`read_file`, `list_dir`, `drive_chat`). Unknown tools already
terminal-refuse without executing (`:1041-1056`).

## 3. GitNexus impact map (index `taey-presence-production`)

Observed CLI (this session):

| Symbol | Direction | Risk | Direct |
|---|---|---|---|
| `_do_run_command` | upstream | LOW | `execute_tool_call` (CALLS) |
| `execute_tool_call_async` | upstream | **HIGH** (3) | `_chat_completions_for_turn`, `nonstream_response`; process `chat_completions` |
| `_audit` | upstream | **CRITICAL** (14) | `execute_tool_call_async`, `execute_tool_call`, `_do_write_file`, `_do_run_command`, `_do_drive_chat`, `_start_turn`, `_end_turn`, `_chat_completions_for_turn` |
| `inject_preamble` | upstream | LOW (2) | `_chat_completions_for_turn` |
| `chat_completions` | upstream | LOW (0 HTTP handler) | — |

Future implementation must hook **before** `_do_run_command` subprocess and
**beside** existing `_audit`. Touching `_audit` is CRITICAL blast radius: keep
the capture record a new opt-in writer, do not change default `_audit` semantics
for `full` profile.

Unknown: whether that GitNexus index commit equals `520a5176`. Re-`gitnexus analyze`
the implementation worktree before editing symbols.

## 4. Capture contract (narrow)

One new **tool profile** string, header-selected. Suggested name:
`read-scorer` (not a new Python module named after closed PR #84).

### 4.1 What a scorer turn must preserve

Private append-only record (outside git), one JSON object per tool round plus one
turn envelope:

```json
{
  "schema": "palios.read_scorer_capture.v1",
  "turn_id": "<uuid>",
  "recorded_at": "<iso8601>",
  "proxy_git_head": "520a517690277687475d185bdd4c2f011bcb4b6f",
  "proxy_argv": "serving/soma_proxy.py",
  "vllm_base_url": "http://10.0.0.8:8000",
  "vllm_model_id": "taey",
  "tool_profile": "read-scorer",
  "upstream_request_sha256": "<sha256 of exact JSON body after inject_preamble>",
  "proposal": {
    "tool": "run_command",
    "arguments": {"command": "git status --short"},
    "refused": false
  },
  "execution": {
    "executed": true,
    "exit_status": 0,
    "result_sha256": "<sha256 of tool result string>",
    "result_chars": 0
  },
  "validation": {
    "kind": "post_read",
    "command": "git rev-parse HEAD",
    "result_sha256": "<sha256>"
  }
}
```

Public receipt (committable): only hashes, counts, profile name, proxy SHA, vLLM
model id. No argv bodies, no command output, no prompts.

Private store: new env `TAEY_READ_SCORER_RAW` defaulting to a mode-0600 jsonl
under `/home/mira/recovery/read-scorer-capture/` (or equivalent non-repo path).
Do not write this file into `palios-taey/taey-presence`.

### 4.2 Allowed vs refused

Allowed (execute, then digest): `search_isma`, `retrieve_document`, `read_file`,
`list_dir`, `check_body_state`, `compute`, `fetch_url`.

`run_command` execute **only** if the argv is a read-only git or orch inspect
after a mechanical parser (not a regex on the whole line):

- git: `status`, `log`, `show`, `diff`, `rev-parse`, `cat-file`, `ls-tree`,
  `blame`, `merge-base`, `worktree list`
- orch: `taey-task status`, `taey-task list`, `taey-plan next` (read)

Everything else in `run_command` (including `git commit`, `git push`,
`taey-task update`, `taey-task outcome`, `taey-notify`, pipelines with `;` `&&`
unparsed) → **safe refusal**: `executed=false`, `exit_status=79`, no
`subprocess`. Same envelope as a real tool result so scorers see argv + refusal.

Not in the profile (existing terminal refusal, no execution): `write_file`,
`send_message`, `drive_chat`, and any future mutation tool.

Default profile remains `full`. Production operator seats are unchanged.

### 4.3 Fresh validation

After every allowed `run_command`, the proxy issues one bounded follow-up inspect
chosen from the allow-list (e.g. `git rev-parse HEAD` in the same cwd) and stores
its digest on `validation`. If that inspect cannot run, the turn is fail-closed
(no partial success).

### 4.4 Identity

Each envelope records: proxy git HEAD (read `git rev-parse HEAD` of the serving
tree at process start, already knowable), `VLLM_BASE_URL`, catalogue model id
from `/v1/models`, `PROCESS_GENERATION`, seat/`TAEY_SESSION_NAME`.

## 5. Executable schema / CLI plan (not implemented here)

1. Add `"read-scorer"` to `_TOOL_PROFILE_ALLOWED` with the allow-set above.
2. In `execute_tool_call`, **before** `_do_run_command`, if profile is
   `read-scorer` and argv is not allow-listed: return refusal string containing
   `exit_status=79` and `executed=false`; `_audit` as today.
3. If `X-Taey-Capture: read-scorer` (or profile itself): append the v1 envelope
   to `TAEY_READ_SCORER_RAW` after `_audit("tool_end")`. Do not persist command
   stdout (hash only).
4. Scorer CLI (caller, not a new daemon):

```bash
curl -sS http://127.0.0.1:8766/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'X-Taey-Tool-Profile: read-scorer' \
  -H 'X-Taey-Capture: read-scorer' \
  -d '{"model":"taey","stream":false,"messages":[{"role":"user","content":"<scorer prompt>"}]}'
```

5. Public receipt publisher (separate, later): sha256 of each private envelope
   line → a jsonl under a private training store, never the serving repo.

## 6. Migration from current topology

| Now | After (when implemented) |
|---|---|
| Scorers cannot get a fail-closed Git/orch trace without `full` `run_command` (live mutations). | Scorers set `read-scorer` profile; mutations never reach subprocess. |
| Engine `ep3` on `.197` has no tools. | Unchanged. Do not route scorers there. |
| `TAEY_TOOL_AUDIT` metadata-only. | Keep. Add a second private capture file for scorer envelopes. |
| `manual-chat-ui` profile. | Keep. Do not overload it for Git scorers (`drive_chat` is UI). |

Closed PR #84 remains closed. No new files named after that experiment.

## 7. Failure-closed boundaries

- Unknown `X-Taey-Tool-Profile` → existing 4xx from `_turn_context` (`:2729`).
- `read-scorer` + mutation argv → 79, `executed=false`, no subprocess.
- Capture env path missing/unwritable → fail the turn (do not silently drop receipts).
- Do not run scorers against `MODEL_ENDPOINT` `.197` (no tool loop).
- Do not claim `8766` vLLM id is `ep3`; Observed id is `taey` on `.8`.
- `full` profile stays mutation-capable; this contract does not remove Taey’s
  production `run_command`. It adds a **narrow scorer profile**.
- No implementation in this commit.

## 8. Verify (design SHA)

```bash
git -C /home/mira/taey-presence-production rev-parse HEAD
curl -sS http://127.0.0.1:8766/health
curl -sS http://10.0.0.8:8000/v1/models
curl -sS http://10.0.0.197:8000/v1/models
tr '\\0' '\\n' < /proc/$(systemctl --user show taey-soma-proxy-mira.service -p MainPID --value)/environ | rg 'VLLM_BASE_URL|TAEY_TOOL_AUDIT'
```
