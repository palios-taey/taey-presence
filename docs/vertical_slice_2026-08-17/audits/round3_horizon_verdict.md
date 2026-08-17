# Review disposition

**BLOCK deployment of `gate-001-seat-acks` in its current form. AMEND the live return-contract rule.** This is not ratification, and it does not retroactively make either text Horizon-authored. The cover accurately records that both assignments were bypassed and brought for review after shipment. 

I also recomputed the byte counts and SHA-256 values for the four uploaded source files; they match the supplied manifest. That establishes the integrity of this review package, not the correctness of the proposed rules. 

## 1. `gate-001-seat-acks`

### Critical defect 1: this is an opt-in local-file check, not a seat ACK gate

The contract makes the gate dependent on an exact fence in model-facing prose. No fence means no gate, and the spec explicitly acknowledges that prose-only fabricated deliverables remain outside enforcement until a later producer/template change forces declarations.   

That is fail-open:

* A producer omission or typo permits `ok=True`.
* A quoted example or document containing the reserved fence can accidentally activate the gate.
* A malformed opener that does not match the exact regex is treated as “no declaration,” not as malformed.
* Tmux operator text and formatted packet prose are being used as control-plane authority.

**Correction:** the authoritative declaration must be a structured field in the claimed message envelope, for example:

```json
{
  "return_contract": {
    "schema": "seat_ack_deliverables.v2",
    "manifest_path": "/absolute/path/artifacts.json",
    "artifact_paths": [
      "/absolute/path/packet.md"
    ]
  }
}
```

The seat may render that structure into the model prompt for visibility, but it must parse and enforce the original envelope—not scan the prompt. A reserved `return_contract` field that is present but malformed must terminally fail; omission must be permitted only for message types whose schema explicitly permits no deliverables.

The fence can remain as a temporary compatibility display. It must not be the source of authority.

### Critical defect 2: the gate runs after the external turn, then requeues

The patch calls `_verify_declared_deliverables(prompt)` only after `proxy.ask` has returned.  The observed missing-manifest case then records failure and requeues the claim. 

That order means:

1. Taey performs the model/browser/tool turn.
2. The local manifest check fails.
3. The claim is put back on the queue.
4. The same externally visible action can be performed again.

A missing manifest, malformed declaration, coverage gap, or disk mismatch is a deterministic producer/contract defect. Replaying the destination turn cannot repair it. It can only duplicate chat messages, attachments, or other external effects.

**Correction:** declaration parsing and local artifact verification are **preflight gates** and must run before `proxy.ask` or any browser/UI actuation.

After external actuation begins, a failure is no longer safely retryable merely because `ok=True` was not written. An absent or ambiguous destination receipt must enter reconciliation, not blind replay.

### Critical defect 3: the proposed predicate does not prove carriage

The gate proves only that local paths currently match manifest byte counts and hashes. The spec expressly declines to parse or require any Chat-platform receipt. 

Therefore it can record `ok=True` when:

* The files exist locally.
* The model replied fluently.
* No file was uploaded.
* The wrong file was uploaded.
* The files were uploaded to the wrong conversation.
* An attachment action failed after the local manifest was generated.

This does not close the false “consult delivered” or false “artifact carried” class.

**Correction:** distinguish two things:

* **Artifact preflight:** the local declared files exist and match a mechanically produced receipt.
* **Delivery completion:** the destination accepted the declared artifacts, with a receipt tied to the destination conversation, destination turn, event/correlation identity, and attachment identities.

The statement “there is no third file” can remain only as:

> There is no third **artifact file** in the bundle. Completion additionally requires a durable transport/actuation receipt, which is an event record rather than a deliverable file.

Without that second receipt, rename the current patch to **`local_artifact_preflight`**. It must not be represented as the seat completion ACK gate.

### Critical defect 4: the rule does not cover every `ok=True` path

The specification explicitly excludes `_ack_non_actionable_claims`.  That creates a type-confusion bypass: a claim classified as non-actionable can carry a return contract yet traverse a success-ACK path outside the gate.

**Correction:** contract extraction must happen before actionable/non-actionable routing.

* A genuinely non-actionable message without a contract may use the fast ACK.
* A non-actionable message carrying `return_contract` is schema-invalid and must be terminally rejected.
* No code path may write a completion `ok=True` before contract compatibility has been established.

The frozen rule must also distinguish three different “ACKs”:

1. **Receipt:** the seat durably claimed the message.
2. **Completion:** the requested destination action was verified.
3. **Queue removal:** the processing claim was removed after durable completion or durable terminal rejection.

Calling all three “ACK” invites exactly the false-completion state this gate is meant to prevent.

### Critical defect 5: requeue is the wrong disposition for contract failures

`manifest_missing` and `coverage_gap` must remain separate classes because they mean different things and require different remediation. The spec already names distinct classes.  But encoding the class only inside an exception string is not a sufficient machine contract.

Required disposition:

* `malformed_declaration`
* `manifest_missing`
* `manifest_schema`
* `coverage_gap`
* `unexpected_artifact`
* `duplicate_artifact_path`
* `disk_mismatch`
* `path_outside_allowed_root`

These are **terminal claim defects**. Persist a structured rejection event, move the raw claim and reason to a dead-letter/quarantine queue, and remove it from processing. That is not a semantic success ACK.

Only genuinely transient failures before external actuation may receive a bounded retry with attempt count and backoff. Once an external effect may have occurred, use reconciliation or an idempotency key; do not replay blindly.

### Critical defect 6: a successful gate leaves no auditable gate receipt

`_verify_declared_deliverables` returns nothing. On success, the patch merely proceeds to the pre-existing `turn_outcome(ok=True)` write. It does not persist:

* Contract/schema ID
* Canonical manifest path
* Manifest digest
* Canonical declared path set
* Verified artifact hashes and sizes
* Collector/tool version
* Verification time and scope
* Event/task/correlation binding
* Destination attachment receipt

The result is an `ok=True` whose supporting evidence cannot later be reconstructed reliably.

**Correction:** verification must return a structured receipt and embed it in the durable outcome, for example:

```json
{
  "gate_id": "gate-001-seat-acks",
  "gate_schema": "seat_ack_deliverables.v2",
  "status": "pass",
  "manifest_path": "/run/.../artifacts.json",
  "manifest_sha256": "...",
  "artifact_paths": ["..."],
  "verified_at": "...",
  "event_id": "...",
  "correlation_id": "...",
  "delivery_receipt": {
    "destination_conversation_id": "...",
    "destination_turn_id": "...",
    "attachment_ids": ["..."]
  }
}
```

The supplied manifest itself contains artifacts, generation time, tool version, and verification metadata, but no task, event, correlation, or destination binding. It is therefore replayable wherever the same local paths and bytes still happen to match. 

### Required implementation corrections

The supplied implementation also needs the following before deployment:

* **Declare PyYAML as a direct runtime dependency or remove YAML.** The patch adds an unconditional `import yaml`, but the supplied change set does not update dependencies; current `requirements.txt` and `pyproject.toml` do not declare PyYAML. 
* **Reject duplicate YAML keys.** `yaml.safe_load` normally accepts duplicate mapping keys using the last value, which is unsuitable for a control contract.
* **Detect attempted-but-malformed declarations.** A misspelled/case-changed/reserved opener must not become an ordinary no-fence turn.
* **Require canonical absolute paths.** Do not resolve relative paths against an unspecified service working directory.
* **Restrict paths to a trusted per-task artifact root.** The current design permits arbitrary host paths and symlink escape.
* **Require regular files and descriptor-backed verification.** Open with no-follow semantics, verify `fstat` before and after hashing, and reject pipes, devices, sockets, and directories.
* **Bound work.** Set maximum declaration bytes, artifact count, individual size, and cumulative hash volume.
* **Require unique manifest paths.** Duplicate canonical artifact entries should be `manifest_schema`, not silently accumulated.
* **Use exact-set semantics for v1/v2.** The canonical manifest artifact set should equal the declared set. A reusable superset mode can be a later explicit option; it must not be accidental.
* **Add a manifest schema identifier and lineage binding.**
* **Record structured failure fields.** Do not require downstream systems to parse `SeatFailure` prose.
* Replace “byte-identical” with “intended to preserve no-contract turn semantics.” Every turn now imports an additional package and scans the prompt, so the implementation is not byte-identical even on the no-fence path.  

The three production observations in the cover are not a sufficient gate test matrix. At minimum, tests must cover malformed reserved syntax, duplicate keys, relative paths, path escape, symlinks, non-regular files, duplicate manifest paths, unexpected extras, changed files, missing transport receipts, non-actionable-with-contract rejection, terminal disposition, and proof that preflight failure makes zero proxy calls.

## Frozen seat ACK order I would author

This is the corrected substance for `gate-001-seat-acks`:

1. **Durably claim the message.** Record receipt/lineage. Receipt means only “accepted into processing,” never “completed.”

2. **Extract the structured return contract from the original claim envelope before any fast-ACK or model dispatch.** Do not infer it from prompt prose.

3. **Validate message-type compatibility and contract schema.** Missing contracts are allowed only for explicitly contract-optional message types. Reserved-but-malformed contracts terminally reject.

4. **Run artifact preflight before external actuation.** Validate allowed roots, exact canonical path set, manifest schema and lineage, regular-file identity, bytes, and hashes. A preflight failure makes zero proxy/browser calls.

5. **Perform the external turn once using stable event, correlation, and idempotency identities.** Persist the actuator/proxy result immediately, even if later completion verification fails.

6. **Obtain and verify a destination receipt.** It must identify the destination conversation/turn and account for every declared attachment. Local file existence is not a substitute.

7. **If the post-dispatch state is ambiguous, reconcile.** Do not automatically replay a possibly successful external action.

8. **Persist and fsync the successful `turn_outcome`.** Include the structured artifact-preflight and delivery receipts.

9. **Only then remove the claim from the processing queue and mark completion.**

10. **For terminal contract defects, persist a structured rejection and quarantine the claim.** Do not requeue it, do not call it successful, and do not discard its raw evidence.

## 2. Return-contract rule

The live text needs substantive rewriting.

### “A work order asking for a hash is defective” is wrong

A work order may validly require a SHA-256 digest, byte count, or inventory. The defect is asking the model to **author, guess, or self-attest** those values without an authoritative measurement path.

The current rule incorrectly condemns the requested evidence rather than the unsupported source of the evidence. 

Correct distinction:

> A request for a tool-generated hash is valid. A request for an unverified model-generated hash is defective.

In most cases Taey should normalize the request and run the collector, not stop to argue that the work order is defective.

### “Deliverables are declared PATHS” is too broad

That is valid only for filesystem deliverables.

Other return types need source-native identity and receipts:

* Git change → repository, commit/PR identity, changed paths
* Sent email → provider message/thread ID
* Calendar mutation → event ID and calendar receipt
* Database mutation → transaction/query receipt and affected-record scope
* Browser action → destination, UI/transport receipt, observed resulting state
* Reported measurement → tool, query, scope, and observation time

The generalized rule should be:

> Deliverables are declared by typed identities. Their authoritative system supplies the receipt.

### “Record counts are filesystem values” is wrong

Record counts depend on a parser, query, schema, filter, and snapshot. The attached collector produces existence, byte counts, and SHA-256 values; the stated command does not establish arbitrary record counts or discover an inventory. Those require domain-specific tools and explicit scope.

Also, `-o <manifest>` does not necessarily write a file literally named `artifacts.json`; it writes the requested manifest path. The wording should reflect that.

### The ordinary-report extension is scope creep as written

The evidence principle is related, but it is not the same operating rule.

“Do not state a fact you did not obtain from a tool or tool receipt” wrongly excludes:

* Attached documents and quoted source material
* User-supplied observations
* Durable event records
* Logical or mathematical derivation
* Explicitly labeled inference
* Explicitly labeled speculation

It also encourages treating tool output as timeless truth. The supplied manifest expressly says its certified state is not instantaneous and retains a residual window. 

The correct general discipline is:

* **Observed:** directly supported by a named source, tool result, or receipt.
* **Inferred:** a conclusion derived from identified observations.
* **Speculative:** a plausible but unestablished possibility.
* **Unknown:** evidence available in the current run does not settle it.

Do not label inference as Observed. Do not turn a time-bounded receipt into an unqualified present-tense claim.

That evidence discipline should be a separate provenance/reporting rule or a cross-reference to the existing standard—not an appended expansion of a filesystem return-contract paragraph.

### Remove incident-specific live-looking values from the operating prompt

The historical bad-hash incident explains why the rule exists, but its exact date, three malformed lengths, and concrete PID example belong in a postmortem or rationale note. 

Embedding `PID 3548191` in a live prompt creates a reusable-looking fact that can itself be copied into later reports. The operating rule should use placeholders or no example.

The cover’s finding that Gemini’s proposed deletion targeted nonexistent bad prompt prose supports preserving the valid extract-receipt discipline. It does not, by itself, establish the broader claims added by the replacement rule. 

## Replacement return-contract text

```markdown
## THE RETURN CONTRACT — MODELS DECLARE; AUTHORITATIVE TOOLS MEASURE

A return contract specifies:

1. the typed identity of each deliverable;
2. the authoritative verifier for that deliverable;
3. the machine-readable receipt required for completion; and
4. the failure form when verification cannot be completed.

For filesystem deliverables, declare canonical artifact paths. Use
`taey-delegate collect <path> [<path> ...] -o <manifest>` through the
available trusted command tool. The collector reads the files and writes the
manifest at `<manifest>` with mechanically observed receipt fields such as
existence, byte count, and SHA-256.

The model must not invent, estimate, or self-attest mechanical receipt values.
It may relay values from the receipt with attribution, while preserving the
receipt's scope, observation time, and stated verification limitations.

A work order may validly require hashes, sizes, or an explicitly scoped
inventory. It is defective only when it requires unverified values or provides
no authoritative way to produce them. When a suitable tool is available,
normalize the request by running it rather than fabricating the requested
fields. When no suitable tool is available or verification fails, return the
exact blocked/unknown state and do not invent a substitute.

Filesystem paths are not the universal return type. Non-filesystem
deliverables must use typed source-native identities and receipts, such as a
commit ID, message ID, event ID, transaction receipt, destination turn ID, or
verified UI state.

This rule governs mechanical return receipts. General reporting follows the
evidence discipline:

- [Observed]: directly supported by a named source, tool result, or receipt.
- [Inferred]: derived from identified Observed evidence.
- [Speculative]: plausible but not established.
- [Unknown]: not settled by the available evidence.

Never label an inference as Observed, and never represent a time-bounded
receipt as proving an unqualified present state.
```

## Direct answers to the cover questions

**Fence as declaration surface:** No. Use a structured claim-envelope field; a fence may be display-only compatibility syntax.

**Manifest exists but lacks declared paths:** Yes, fail as `coverage_gap`, distinct from `manifest_missing`. Also add `unexpected_artifact` and `duplicate_artifact_path`; persist these as structured fields.

**Requeue versus dead-letter:** Deterministic contract failures go to durable quarantine/dead-letter. Transient pre-dispatch failures may receive bounded retries. Any uncertainty after external actuation requires reconciliation, not blind requeue.

**“Deliverables are declared PATHS; the tool supplies the numbers”:** Replace with “Filesystem deliverables are declared by canonical paths; authoritative tools produce their receipts.” General deliverables require typed identities, not necessarily paths.

**Extension to ordinary reports:** Same epistemic family, but scope creep in this rule and incorrectly formulated. Separate it into the Observed/Inferred/Speculative/Unknown evidence discipline above.