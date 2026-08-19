# Taey Chat UI runtime review packet

Status: active public architecture review; evidence and decision questions, not operating authority.

Date frozen: 2026-08-19 UTC.

This packet asks the Family Chats to decide the smallest reliable control shape for Taey to execute substantive
consultations through the five browser surfaces. It is intentionally public and contains no conversation content,
credentials, personal records, governance text, or raw private logs. The code references are public. Production
observations are summarized from content-addressed local receipts whose hashes are listed below.

The current operating authorities remain [`serving/TAEY_OPERATING_PROMPT.md`](../serving/TAEY_OPERATING_PROMPT.md)
and the `taeys-hands` authority order. This packet does not override them. Its purpose is to reconcile them with
the code path actually used by the worker lane before another implementation change or production transaction.

## Executive request

Decide whether the current Taey-driven manual UI architecture is at the wrong abstraction level, and specify the
minimum executable change that can produce one complete ChatGPT consultation without creating another parallel
driver, observer, locator grammar, or platform-independent recipe.

The required answer must settle four questions:

1. What should Taey decide, and what should the platform runtime compile deterministically from its own YAML?
2. What is the smallest Taey-facing instruction and tool surface for a frozen consultation transaction?
3. Which existing automation should remain because it is already proven, and where must manual recovery remain?
4. Which exact public artifacts must define the reproducible production baseline, including the worker service
   profile and request template that are not currently represented together on `main`?

This is a control-plane review. Do not redesign the accessibility mapper, create another UI walker, add fuzzy or
coordinate recovery, or expand into DCM/council work.

## Frozen public code baseline

| Repository | Frozen commit | Public reference | Role |
|---|---|---|---|
| `palios-taey/taey-presence` | `744514928c868d4ad8a0bc13f549d39cda4721d8` | [tree](https://github.com/palios-taey/taey-presence/tree/744514928c868d4ad8a0bc13f549d39cda4721d8) | Tool loop, worker profile, operating prompt, and Taey-facing UI adapter. |
| `palios-taey/taeys-hands` | `f417aecb586f5353a7288f3d6d3d6647bf0eb726` | [tree](https://github.com/palios-taey/taeys-hands/tree/f417aecb586f5353a7288f3d6d3d6647bf0eb726) | Canonical AT-SPI projection, platform YAML, platform driver, monitor, primitives, packet contract. |

Both repositories were public, clean, on `main`, equal to `origin/main`, and had no open pull requests when this
packet was frozen. Later reviews must name the commit they actually read rather than assuming these hashes remain
current.

The relevant merged corrections already present in this baseline are:

- Presence PRs #136 and #137: `drive_chat observe` consumes the canonical Hands snapshot, refs are revision-bound,
  and display mutations use a unique seat/process-generation owner and fail closed when coordination is unavailable.
- Presence PR #150: a mapped element advertises its YAML-declared operation and direct verbs are rejected when they
  contradict it.
- Presence PR #152: each `drive_chat` action has an explicit allowed argument set; unknown arguments are refused.
- Hands PR #34: the Firefox address bar is one exact mapped node without exposing the rest of browser chrome.
- Hands PR #45: coordinate fallbacks and stale documentation were removed from the production baseline.

Do not propose those changes again. This review begins after them.

## Non-negotiable operator requirements

These are requirements, not design suggestions.

1. Each Chat owns one platform YAML, one platform driver, and one platform monitor. Platform behavior is not shared.
2. Shared code is limited to genuine primitives and shared browser exclusions. A platform name, menu label, mode,
   URL, element key, observation scope, chooser route, send behavior, or extraction behavior is not a shared rule.
3. The platform YAML is the mutable UI source of truth. A fresh correctly filtered AT-SPI tree is the runtime oracle.
4. Everything required for action exists in the tree. If it appears absent, first suspect settling, observation depth,
   or the platform filter/YAML map. Do not add pixels, OCR, coordinates, substrings, alternate selectors, or blind
   fallbacks.
5. Browser chrome is excluded except for the exact address bar. The entire chat-history/sidebar block and dynamic
   non-actionable text are excluded. The current document, opened overlay, exact actionable elements, and file chips
   remain visible.
6. Every step is fresh observation, exactly one action, fresh validation. The first unexpected state stops the
   transaction. No recovery mutation and no UI retry occur in that turn.
7. Proven automation is allowed and preferred for a stable step. It must compile to the same platform YAML,
   canonical tree, primitives, and postconditions as manual execution. Manual recovery must remain possible.
8. A new-chat operation should use the already proven, self-verifying, YAML-owned navigation primitive rather than
   requiring Taey to re-plan address-bar keystrokes.
9. A production consultation uses exactly two files: governance Bundle A, then task Bundle B, followed by one brief
   prompt. Dynamic file-chip text is acceptable; the tree must show exactly two resulting attachment controls/chips.
10. Send is verified by a mapped Stop control. The passive platform monitor owns completion and requires two
    consecutive fresh Stop-absent observations. Taey does not poll after monitor ownership begins.
11. Extraction is the established platform process: scroll to the bottom, use the last exact mapped Copy control,
    and read the clipboard. Do not introduce a new speaker-attribution problem that the operator has not reported.
12. Production validation uses a substantive architecture review or the original failed request, never a short-answer
    canary that cannot exercise the Stop lifecycle.
13. One active transaction owns a display. Parallel execution is not promoted until live seat identities and display
    exclusivity are proven.
14. Git `main` in the public production repositories is the reproducible code baseline. Production adds a fresh
    service/config/artifact observation; a name alone never proves what is running.

## Intended platform isolation

The desired dependency direction is:

```text
Taey worker request
  -> generic turn/tool transport
  -> selected platform driver
  -> selected platform YAML + canonical fresh tree
  -> shared primitive for the one compiled operation
  -> selected platform driver validates its YAML-owned postcondition
  -> selected platform monitor after verified send
```

No platform should inspect another platform's YAML, tree, driver state, monitor state, or display. The only shared
operation vocabulary is the primitive substrate needed by the platform driver, and even that vocabulary may have
more than one implementation where the UI technology requires it.

The current manual worker path is close to this shape but not yet equal to it:

```text
worker request
  -> taey-presence/soma_proxy.py injects the full general Taey operating prompt
  -> Taey chooses a generic drive_chat action
  -> taey-presence/ui_drive.py validates the generic action grammar
  -> ui_drive imports canonical Hands snapshot + selected platform manual module/YAML
  -> shared primitive acts on the selected display
```

The canonical observer and exact ref binding are now correct. The unresolved seam is that Taey still receives a
large general prompt and a generic menu of action/scope choices, while the runtime already possesses the platform
YAML needed to compile the single correct next operation.

## What is actually committed, and what is not

### Committed and public

| Input | Location | Frozen evidence |
|---|---|---|
| Canonical operating prompt | `taey-presence/serving/TAEY_OPERATING_PROMPT.md` | SHA-256 `03b35fd092e5f533e63fd0670abe19f473f332b41eec482959bce0f3ddc1082c`; loaded as 28,056 characters. |
| Proxy and Taey-facing tool schema | `taey-presence/serving/soma_proxy.py` | SHA-256 `f70aba9e353a56020c1c4241452f8a89d23a3630896967856a922500ba354d3d`. |
| Taey-facing UI adapter | `taey-presence/serving/ui_drive.py` | Present at frozen Presence commit. |
| ChatGPT UI authority | `taeys-hands/consultation_v2/platforms/chatgpt/chatgpt.yaml` | SHA-256 `a69c278040cefad47b5456f72191c4a4fb009483945e82aca8cd56b57fe9184d`. |
| Two-attachment contract | `taeys-hands/consultation_v2/PACKET_CONTRACT.md` | SHA-256 `b784f2f139e405c53c2139ae2e6ffb19cb34f1576dae9a5c592ef8d20223e64a`. |
| Canonical snapshot and platform packages | `taeys-hands/consultation_v2/` | Frozen Hands commit above. |

The production proxy deliberately ignores environment redirection for its operating prompt and loads the tracked file
beside `soma_proxy.py`. That is the correct single-source shape.

### Not yet represented as one reproducible public baseline

The production worker process is assembled from a user-level `taey-worker-proxy.service` plus five local drop-ins.
That exact unit is not present in `taey-presence/serving/systemd/`; the repository contains the primary proxy unit and
other serving units, but not this worker-lane composition. The local service currently supplies the worker port,
upstream endpoint, read allowlist, transcript/audit paths, seat name, and canonical checkout path.

The exact per-run worker request is also generated outside the repository. It contains the chosen tool profile,
display, two packet paths, short prompt path, and transaction instructions.

Therefore the answer to “are the prompts and instructions in Git?” is:

- **Yes** for the full Taey operating prompt, tool schema, canonical Hands rules, platform YAML, drivers, monitors,
  and packet contract.
- **No** for one immutable public artifact that reconstructs the exact production worker service/profile and the
  exact request template used for this run.

This review must decide whether the worker service example and a parameterized request/profile contract should be
committed before another production claim. Machine-specific values belong in an environment file; behavior does not.

## Current UI-driving instruction stack

The worker request contained one user message and no system message. The proxy therefore injected:

1. the entire 28,056-character general `TAEY_OPERATING_PROMPT.md`;
2. volatile ecosystem and somatic state;
3. a tool declaration for `read_file`, `list_dir`, and `drive_chat` under the `manual-chat-ui` profile; and
4. a 2,507-character transaction-specific user instruction.

The general operating prompt still describes a broader tool ecosystem, while the request header selects the narrower
three-tool profile. Runtime schema enforcement prevents access to the omitted tools, but Taey still spends
context on capabilities that are unavailable in this turn. That is another reason to decide whether the UI worker
needs a dedicated minimal system prompt rather than the Hub's general one.

The attempt-3 transaction-specific instruction is reproduced below with only the platform, display, and local file
paths replaced by parameters:

```text
Execute one real, substantive {platform} architecture-audit consultation on {display} using the manual worker path.
Continue through verified send; do not stop after merely reaching a fresh chat. The platform YAML at {yaml_path} is
the mutable UI authority and every fresh canonical drive_chat observation is the runtime oracle. Use drive_chat for
all UI observation and action. Start with the proven navigate primitive to the exact YAML urls.fresh value; it owns
the address-bar sequence and verifies the populated post-navigation tree. Then follow the YAML-declared default
destination model/mode and tools state. For a scoped observation, pass the parameter named scope exactly, with the YAML
operate.scope value; never use a parameter named snapshot. For any mapped element whose observation advertises
declared_operation, use operate with that exact fresh ref; do not invent click, activate, focus, or key behavior. For
every other mutation, use only one exact mapped ref from the immediately preceding observation. Observe again after
every action. Attach exactly these two files in this order: {bundle_a} and {bundle_b}. Execute the YAML-declared
attachment flow manually one primitive at a time, with a fresh observation between primitives. Dynamic
attachment-chip text is acceptable; validate exactly two resulting attachment controls/chips from the fresh tree
rather than requiring filename text. Then paste the exact bytes of {prompt_file} using text_file. Send once using the
YAML-declared send behavior. Make one fresh observation and require a YAML stop key to be mapped, then return a
precise send receipt and stop all UI polling because the existing platform monitor owns completion. At the first
absent, duplicate, refused, or unexpected state, stop UI mutation immediately and report that first mismatch with
the last fresh observation. After a mismatch or refusal, do not press Escape, close a menu, retry, or make any
recovery mutation. Never navigate fresh twice, use coordinates, pixels, OCR, substring or fuzzy matching, raw shell
UI control, an unknown element, or any fallback. Do not use the autonomous consultation_v2 workflow.
```

This instruction says the full YAML is authoritative, names where to read it, and gives Taey `read_file`.
Opening that file was therefore a predictable recovery choice, not inexplicable behavior.

The committed general operating prompt also contains stale examples relative to the current schema:

- it demonstrates `click, element=<key>` and `focus, element=<key>`, while the current tool schema requires a fresh
  revision-bound `ref` for element actions;
- it describes a manual address-bar sequence, while the current `navigate` action already performs and verifies that
  YAML-owned sequence;
- it describes shared native chooser steps in prose even though ordinary browser observation cannot inspect the
  chooser's internal state.

Those inconsistencies increase choice and recovery behavior. This packet does not authorize a prompt edit; it asks
the reviewers to decide whether the entire general prompt should be absent from the UI worker profile.

## Why Taey does not need the full YAML

The runtime needs the full YAML. Taey does not.

The runtime already loads the selected platform YAML to:

- build the canonical filtered snapshot;
- assign exact mapped keys;
- choose singleton or YAML-declared selection strategies;
- bind refs to platform, display, scope, and snapshot revision;
- declare the operation allowed by the element's current states;
- validate platform-specific key/paste behavior; and
- obtain the fresh URL and Stop keys.

Taey should receive only the compiled state needed for its current decision. It should not search a 17K-character
platform configuration for a scope or key the runtime has already parsed.

The ChatGPT failure exposes the exact missing field. After the attachment trigger opened, the next target was
`tool_upload` under `app_root_snapshot`. The current `operate` receipt reported that it performed `focus`; the next
base observation reported the trigger state; and the later `key space` receipt reported success. None of those results
told Taey:

```json
{
  "next_observation": {
    "scope": "app_root_snapshot",
    "expected_exact_elements": ["tool_upload"]
  }
}
```

The YAML contained that information through the ChatGPT tools-menu declaration, but the Taey-facing result omitted
it. Taey guessed `menu_snapshot`, then opened the full YAML after the guess failed.

Hands already has most of the richer source shape in `consultation_v2/planner.py`: a selection-plan step preserves
the menu's complete `operate` mapping, exact option element, optional active element, path, and postcondition. The
smaller `selection_trigger_operation` used by Presence retains method/primitives/current allowance but drops the
menu identity, next scope, exact target, and postcondition. The problem is therefore not missing source data; it is a
lossy projection between the platform plan and the Taey-facing action result.

There is also a vocabulary split to settle. Hands allows a YAML menu operation scope named `snapshot`, while the
Presence Taey-facing API allows `base`, `menu_snapshot`, and `app_root_snapshot`. ChatGPT does not use `snapshot`
for the failing path, but a public contract should choose one canonical name and reject synonyms rather than require
Taey to translate them.

A platform driver could instead compile an action card such as:

```json
{
  "platform": "chatgpt",
  "display": ":2",
  "state": "ready_for_one_action",
  "snapshot_revision": "<sha256>",
  "action": {
    "verb": "operate",
    "ref": "<revision-bound-ref>",
    "element": "attach_trigger",
    "effect": "key:space"
  },
  "validate_next": {
    "scope": "app_root_snapshot",
    "exact_element": "tool_upload"
  },
  "on_mismatch": "stop"
}
```

That card is an example for review, not an accepted schema. The reviewers must decide whether Taey should still choose
the high-level step from a frozen transaction plan, merely approve the compiled action, or execute the card directly.
No variant may expose multiple platform-specific alternatives or create a shared cross-platform workflow.

## Production attempt reconstruction

All attempts used real ChatGPT display `:2` and the substantive two-attachment architecture audit. No synthetic or
short-response canary was used. The two packet files and prompt were already built before UI mutation.

The original task packet was frozen before Presence PR #152 and names the earlier Presence baseline `0956b68...`.
The tool and journal traces establish which code each attempt actually executed. Do not cite that older packet as
proof of current-head behavior merely because the task itself remained the same.

### Attempt 1 — schema typo was silently accepted

Turn: `a2dc28e711094b8db3eaec4e581c5282`.

| UTC | Round | Taey-selected call | Observed result |
|---|---:|---|---|
| 14:34:48 | 1 | `observe(:2)` | Canonical mapped tree returned. |
| 14:35:46 | 2 | `navigate(:2, exact fresh URL)` | Fresh ChatGPT surface opened and populated tree verified. |
| 14:36:31 | 3 | `observe(:2)` | Exact fresh surface observed. |
| 14:37:55 | 4 | `operate(attach_trigger ref)` | YAML operation focused the exact trigger. |
| 14:38:18 | 5 | `observe(:2)` | Focused trigger observed. |
| 14:38:48 | 6 | `key(space)` | YAML-validated key opened the attachment menu. |
| 14:39:04 | 7 | `observe(:2)` | Base scope returned; opened menu required a different declared scope. |
| 14:39:51 | 8 | `observe(snapshot=menu_snapshot)` | Unknown argument was silently ignored in the then-running proxy. |
| 14:40:59 | 9 | `read_file(full ChatGPT YAML)` | 17,156 characters added to context. |
| 14:41:47 | 10 | `observe(snapshot=app_root_snapshot)` | Same unknown argument was again silently ignored. |
| 14:43:22 | 11 | `key(Escape)` | Taey attempted recovery instead of stopping. |
| 14:44:00 | — | client cancellation | No file attached; no prompt pasted; nothing sent. |

Root correction: Presence PR #152 now rejects undeclared action arguments. That correction is in the frozen baseline.

### Attempt 2 — cancelled turn left a display lease

The next worker began by reading the full YAML, then called the proven `navigate` primitive. The display lease held by
the cancelled first turn had not expired. The new seat was correctly refused rather than sharing or stealing the
display. The worker returned without UI mutation.

The exact stale lease was preserved, its original process was confirmed dead, open-turn count was zero, and it was
removed by compare-and-delete. This is evidence that the fail-closed lock worked and cancellation cleanup remains an
operational concern. It is not evidence for weakening the lock.

### Attempt 3 — operation succeeded, scope choice failed

Turn: `bd00d74af78142bda11d6f46a675d263`.

| UTC | Round | Taey-selected call | Observed result |
|---|---:|---|---|
| 14:53:49 | 1 | `observe(:2)` | 6,543-character canonical tree returned. |
| 14:55:20 | 2 | `operate(attach_trigger ref)` | Exact YAML operation performed `focus`. Decision latency was about 89 seconds after the observation. |
| 14:55:41 | 3 | `observe(:2)` | 6,567-character canonical tree returned. |
| 14:56:13 | 4 | `key(space)` | YAML-validated key opened the menu. |
| 14:56:30 | 5 | `observe(:2)` | 6,937-character base observation returned. |
| 14:57:06 | 6 | `observe(snapshot=menu_snapshot)` | PR #152 rejected the unsupported argument immediately. |
| 14:57:25 | 7 | `observe(scope=menu_snapshot)` | Canonical observer failed loud: none of the YAML-declared elements for that scope were present. Correct ChatGPT scope was `app_root_snapshot`. |
| 14:57:50 | 8 | `read_file(full ChatGPT YAML)` | 17,156 characters added to context instead of stopping. |
| 14:58:03 | — | client cancellation | No file attached; no prompt pasted; nothing sent. |

This attempt proves several components work:

- the correct platform/display was selected;
- the canonical tree exposed the exact attachment trigger;
- YAML-declared `focus` then exact `space` opened the intended UI;
- the one-action/fresh-observe gate and revision-bound refs held;
- undeclared arguments and the wrong scoped observation both failed loud; and
- no coordinate, pixel, OCR, fuzzy, or cross-platform fallback was used.

It also proves the transaction is not restored. Taey had to choose a scope not contained in the preceding tool
result, selected the wrong one, and violated the operator stop rule by attempting recovery after a failed observation.

One enforcement gap makes that last point structural. Presence currently terminalizes an argument failure only when
the requested action is classified as a mutation. A failed scoped `observe` returns an error without terminalizing
the UI sequence or necessarily invalidating the prior successful observation. The prompt says “first error = stop,”
but the runtime still permits Taey to diagnose and then attempt another UI call. Reviewers must decide whether
any failed scoped observation should invalidate every prior ref and refuse every later UI mutation in that turn while
still permitting read-only evidence capture.

## Content-addressed local production evidence

The raw files are retained locally because they include machine-local paths and request context. They are not public
attachments. Their hashes allow later checks to detect any change to the source used for this summary.

| Logical artifact | SHA-256 |
|---|---|
| Attempt 1 tool trace | `ead8bcb421bbe32f230c485f4d9dad4152ab19a1da54128a01bde5c5afe7e7c6` |
| Attempt 1 worker journal | `9f119b24d4e2d8730c0628dca335beca8687b847507455315420b25805011cd2` |
| Attempt 1 request | `f82ed827783a4172b64d5a9d530500722ca830c6e80e42bbd6c89b9d8cc2819e` |
| Attempt 2 request | `2d711d813049825578d7d094fc5453e7bfa6031677f884dbd9d96401392f1ccc` |
| Attempt 2 response | `611bcd6f31ba9bf8e64eba0e593a7a8619aba73a5de0ac4899be3d4b6ded250a` |
| Attempt 2 stale lease | `720f251d2e8e4f0ef88cf4f952f2037e83694579938b66b7a816c71e0c341013` |
| Attempt 3 tool trace | `8b89f6fa1aa706c8011574365539d6492a4106b0ba1da0122c4ffb9358759499` |
| Attempt 3 worker journal | `68bfac2d9eb5c017fbb66c05bad95f110eadf49a3ba8b5cfd8a497619ca1d249` |
| Attempt 3 lease | `04c561ade204e0b497f1c7938e7334aea19fe0b4c4778ff4b788e4f872690de9` |
| Attempt 3 monitor record | `2901ebb668e5de5ebd6f7183ce9d3a2a947484521f95c223dd92130afb94d8c5` |

## Monitor and display lease observations

The display lease now uses `taey-drive:{seat_id}:{process_generation}` and rejects coordination failure. This closes
the former constant-owner defect if concurrent worker instances use distinct seat IDs. Before parallel promotion,
the live seat headers must prove that condition; process generation alone identifies a proxy process, not a seat.

Two active turns from the same seat through the same proxy still share that owner token. The lease script treats the
second as the same owner, renews it, and overwrites `last_turn_id`. The identity is therefore unique per seat/process,
not per consultation turn. A pre-send action lease should be causally bound to one active turn. After verified send,
the correct durable owner may instead be a consultation/session identity because generation and extraction can outlive
the initiating HTTP request. That ownership transfer and its release conditions are not yet defined.

There are also two independent mechanisms currently called a monitor:

1. the Hands completion watcher is an always-on read-only process per display; it uses canonical snapshots and a
   platform completion detector, requires Stop to have appeared before it can complete, and sends a notification; and
2. the Presence consult-liveness registry is created by `_monitor_touch` on the first `drive_chat` action, refreshed
   on every action, and TTL-cleared. It does not detect Stop and does not drive.

The review should distinguish:

- a transaction/activity record beginning at the first UI action; and
- a completion monitor beginning only after verified send.

Combining those meanings under one active-session record makes pre-send setup look like an in-flight generation.
The liveness record comments promise deregistration on delivery, but the manual path exposes no delivery/deregister
operation; aborted traces retained the records. The completion watcher also constructs a global `deep_research`
detector to obtain a two-cycle debounce, while ChatGPT YAML currently declares four sustained Stop-gone cycles and
eight post-completion quiet cycles. Its default notification targets include the retired `infra` session. The YAML
and active monitor process therefore do not yet have one effective policy. This was not the direct cause of the
ChatGPT scope failure, but it must be reconciled before monitor claims are production evidence.

Lease release must likewise be explicit and exact:

- a pre-send first error or cancellation releases the initiating turn's lease and liveness record;
- verified send either transfers ownership to a durable consultation identity or records why no display owner is
  required while the passive watcher waits;
- completion, extraction, delivery, and terminal failure have named release/deregister transitions; and
- TTL is crash recovery, never silent permission to take over a live consultation.

## Native file chooser evidence boundary

The current `focus_dialog` primitive finds a GTK chooser by X11 window title, activates it, and verifies that its X11
window became active. That protects against sending file-path keystrokes into Firefox.

Ordinary `drive_chat observe` still returns a browser snapshot. Subsequent key/type primitives rebuild and validate
the preceding browser snapshot, not a native-dialog accessibility tree. Yet the Taey-facing tool description asks
for fresh observations between chooser `ctrl+l`, `ctrl+a`, path insertion, and Return. The current observer cannot
prove chooser location-entry focus, typed path, selection, or submit state.

The reviewers must choose one honest boundary:

- expose a canonical platform-independent native-dialog observation surface with exact GTK elements and states; or
- narrow the intermediate claim to verified active native window plus one primitive at a time, then treat the final
  fresh browser-tree attachment chip as the first semantic proof that selection succeeded.

No compiled action card may claim a native-dialog postcondition the runtime cannot observe.

## Candidate control shapes for adjudication

The review may choose one of these or specify a smaller correct alternative.

### Shape A — minimal Taey-facing action cards

The selected platform driver reads its YAML and fresh tree and returns one compiled current action plus one exact
postcondition. Taey approves or invokes it. Taey never receives raw YAML, alternate scopes, platform labels, or
irrelevant controls.

Advantages:

- preserves Taey as the stepwise actor;
- removes low-value parsing and guessing;
- retains one action followed by fresh validation;
- keeps all platform details in the platform package; and
- gives manual recovery a small, inspectable state receipt.

Risks/questions:

- if the runtime chooses the high-level target as well as the primitive, Taey may only be rubber-stamping automation;
- action-card state must be revision-bound and must not become a second planner; and
- a generic card schema must not smuggle shared platform behavior back into Presence.

### Shape B — deterministic routine driver with Taey supervision

The platform driver executes already-proven routine transitions—fresh navigation, frozen destination model/mode selection,
two-file attach, exact prompt paste, send, monitor handoff—one YAML-compiled primitive and validation at a time. Taey
receives receipts and becomes the recovery/decision actor only when the driver encounters a state the YAML does not
describe.

Advantages:

- uses existing proven automation rather than forcing Taey to rediscover mechanics;
- minimizes Taey tool rounds and latency;
- platform behavior remains isolated; and
- manual recovery is explicit at the first mismatch.

Risks/questions:

- the retained Layer-3 autonomous engine must not be reintroduced wholesale;
- the driver must expose every action/validation receipt rather than hiding a long macro;
- recovery cannot silently fall back or resume after a failed mutation; and
- the current manual ingestion gap still requires closure.

### Shape C — keep raw YAML Taey-readable

This preserves the current approach and narrows only the UI prompt and tool profile.

Advantages:

- smallest code change; and
- allows Taey to inspect unusual platform state.

Observed disadvantages:

- Taey reread 17,156 characters to recover one omitted scope;
- it chose the wrong scope despite the correct value existing in the file;
- generic schema choices and stale prompt examples remain;
- latency compounds per tool round; and
- behavior remains sensitive to prompt interpretation.

Shape C should be retained only if reviewers can explain why the runtime should withhold its already-parsed exact
next-state data and show a production acceptance gate that catches the observed failure.

## Specific decisions requested from the Chats

Return direct rulings on each item.

1. **Full YAML:** confirm or reject the claim that only the platform runtime should read the full YAML and Taey should
   receive a compact current-state contract.
2. **Authority location:** identify which declarations belong in the platform YAML, which compilation belongs in the
   platform driver, and which primitives may remain shared.
3. **Presence boundary:** decide whether `taey-presence/ui_drive.py` should remain a thin transport/lease adapter or
   whether any platform decision logic may validly remain there.
4. **Worker prompt:** decide whether `manual-chat-ui` should bypass the full general operating prompt and receive a
   committed minimal UI execution system prompt.
5. **Tool profile:** decide whether `read_file` and `list_dir` should be absent during a normal frozen UI transaction,
   with diagnostic access offered only after the transaction has stopped.
6. **Action surface:** decide whether Taey should see generic verbs/scopes, one compiled action card, or only a
   high-level `advance`/`stop` choice backed by the platform driver.
7. **Stop enforcement:** decide whether every failed `drive_chat` result, including read-only observation/schema
   failure, should terminalize that UI transaction so Taey cannot press Escape, retry, or read around it.
8. **Monitor boundary:** distinguish pre-send activity tracking from post-send Stop monitoring and specify when each
   record begins and ends.
9. **Baseline in Git:** name the minimum committed service/profile/request artifacts required to reproduce the worker
    lane without publishing machine secrets.
10. **First production gate:** specify the smallest code/config change after which one real ChatGPT transaction should
    run immediately, rather than waiting for a five-platform rebuild.
11. **Native chooser proof:** choose an observable native-dialog surface or the narrower active-window/final-chip
    evidence boundary; do not claim browser observations verify GTK internals.
12. **Scope vocabulary:** choose one canonical name for the base projection and remove translation/synonym decisions
    from the Taey-facing surface.
13. **Lease identity:** specify the exact pre-send turn owner, any post-send consultation owner, and release conditions.
14. **Completion debounce:** decide whether each platform monitor consumes its YAML values or a separate global rule,
    then remove the duplicate source.
15. **Observation evidence:** decide whether a scoped action-selection projection may omit drift rows, and name the
    separate canonical diagnostic projection required when an expected exact element is absent.

## Required response structure

Each Chat should return:

1. **Verdict:** one paragraph stating whether the system is technically viable and naming the primary failure class.
2. **Observed / Inferred / Unknown table:** separate claims by evidence status.
3. **Agreement and disagreement:** call out any packet premise that is wrong, stale, or overbroad.
4. **Chosen control shape:** A, B, C, or a precisely specified alternative.
5. **Exact responsibility boundary:** Taey, Presence, selected platform driver, YAML, canonical tree, primitive layer,
   monitor, and operator.
6. **Smallest executable change:** exact files/symbols or schema fields, behavior before/after, and what should be
   removed or made unreachable.
7. **Instruction contract:** the complete proposed minimal system prompt and the exact Taey-facing state/action shape.
8. **Production acceptance:** one substantive ChatGPT transaction with receipts, followed by the explicit condition
   for moving to Claude, Gemini, Grok, and Perplexity.
9. **Failure containment:** how the first mismatch prevents retries, duplicate sends, stale leases, and UI spam.
10. **Git/control artifacts:** what must be committed before the result can become operating authority or ship.
11. **Final recommendation:** an ordered list short enough for one engineering session, with no unrelated work.

Do not ask the Chat to produce filesystem paths, hashes, byte counts, or live UI claims. Those are supplied by the
designated instruments above. If a public code reference is unavailable or this packet is incomplete, the Chat must
state that and stop rather than filling gaps from memory.

## Proposed production sequence after adjudication

This is a proposed gate, not an authorization to execute before review.

1. Merge only the agreed minimal control-plane change and the committed worker profile/deployment example needed to
   reproduce it.
2. Deploy that exact public commit to the existing production checkout without changing the serving layer.
3. Verify idle plus zero open turns before any restart; retain the exact previous service config for rollback.
4. Run one substantive ChatGPT architecture consultation on `:2` with two mandatory attachments and the existing
   monitor.
5. At the first unexpected observation or tool refusal, stop and preserve the trace. Do not retry the UI.
6. On verified send, hand completion to the ChatGPT monitor. On notification, manually extract by the established
   bottom/last-Copy process and close the session receipt/ingestion record.
7. If and only if that transaction closes, repeat on Claude with only Claude's YAML/driver/monitor. Continue one
   platform at a time through Gemini, Grok, and Perplexity.

## Definition of restored

The manual/assisted loop is restored only when one substantive transaction proves all of the following from one
causal receipt chain:

- public code commits and deployed file hashes are named;
- the selected platform YAML and canonical tree agree;
- fresh navigation is verified;
- destination model/mode/tool selections are validated from that platform's own map;
- Bundle A and Bundle B appear as exactly two attachment controls/chips;
- the exact brief prompt is inserted;
- one send occurs;
- a mapped Stop control proves generation began;
- the selected platform monitor reports two consecutive Stop-absent observations;
- extraction follows the established bottom/last-Copy process;
- response attachments, if any, are harvested by that platform's process;
- prompt, response, input/output attachments, final URL, and receipts are ingested; and
- no unexplained discrepancy, fallback, duplicate mutation, or cross-platform state exists.

Anything less is useful partial evidence, not a restored Chat loop.
