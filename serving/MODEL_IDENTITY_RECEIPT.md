# Taey model identity receipt

`taey-model-identity-receipt/v1` is the serving host's statement of the exact model process that is
currently eligible for a Taey-native DCM request. It is not supplied by a seat, Main Taey, or the
DCM coordinator.

The attestor publishes one stable receipt in a refreshed envelope at
`taey:serving:model_identity:<TAEY_MODEL_IDENTITY_AUTHORITY_ID>`. The key has a short TTL and is refreshed only while all live facts
continue to match. Publication is compare-and-refresh: an attestor may refresh its own byte-exact
generation or claim an absent key, but it cannot overwrite a different live generation. Shutdown deletes
the key only if the stored value still belongs to that generation. A crash leaves at most 15 seconds.

## Receipt identity

Canonical JSON is UTF-8 with lexicographically sorted keys, separators `(",", ":")`,
`ensure_ascii=false`, and `allow_nan=false`. `receipt_sha256` is `sha256:` plus the SHA-256 of the
canonical object with `receipt_sha256` omitted.

The stable receipt is wrapped in a refreshed Redis publication envelope using contract
`taey-serving-model-identity-publication/v1` and contains its authority ID, one random attestor
generation, Redis-server publication time, the receipt object and digest, the public signing-key
fingerprint, and an Ed25519 signature over every envelope field except the signature itself. The
attestor generation makes lease ownership exclusive; it is not model identity. Each heartbeat has
a fresh signed Redis timestamp while preserving the receipt digest. A clean attestor restart changes
the generation while preserving the receipt digest when the model process is unchanged.

The receipt binds:

- a hash of the serving host machine identity and the serving-owned Redis key;
- the live systemd InvocationID, Docker container ID and start time;
- the actual Docker image digest and digest-pinned image reference;
- a digest of the exact container command;
- the direct completion and catalogue endpoints;
- the exact sorted served alias-to-container-root records;
- a uniquely read-only `/models` bind containing the attested artifact;
- the SHA-256 of `ARTIFACT_SHA256SUMS` and its exact all-file coverage;
- the SHA-256 of the declared structural model manifest; and
- `model_content_sha256`, matching `promote_model.sh` semantics: SHA-256 over the ordered GNU
  `sha256sum` lines for every regular artifact file, including its relative name and content hash.
- a filesystem fence over the root and every entry's device, inode, type, mode, size, mtime, and
  ctime, re-derived on every heartbeat so replacement or chmod invalidates the publication without
  re-reading 55 GB every three seconds.
- the installed and effective systemd configurations, the exact public attestor and launcher, and
  the public trust root used by the independent verifier.

The artifact is rejected if any entry is writable, is a symlink, is not a regular file or directory,
or is absent from the seal. The live aliases must equal `TAEY_SERVED_NAME` and resolve to the exact
container model root. The live image reference must be digest-pinned.

## Consumer rule

The serving host verifies that its configured externally reachable upstream endpoint returns the
same exact model catalogue as loopback. The coordinator and every seat independently read the same
live Redis value, verify its canonical digest and positive TTL, compare the required alias, and
verify that the worker proxy's configured upstream equals the receipt's completion endpoint. The
DCM execution endpoint remains the worker proxy; the attested endpoint is that proxy's upstream.
A DCM v2 wave binds only the
resulting `receipt_sha256`. A missing, expired, malformed, mismatched, or changed receipt is
`model_identity_unproven`; it cannot authorize inference. A new receipt requires a new wave/request
identity. `/v1/models.created` is never identity because it changes with request wall clock.

Redis is delivery and liveness transport, not the origin of the model facts. Redis clients cannot
forge or indefinitely replay authority: verifiers require the committed public key, signature,
Redis-server timestamp, and bounded TTL. A privileged serving-host holding the private key remains
inside the current trusted-infrastructure boundary.

`model_identity_status.py` is the independent portable consumer verifier. It does not import the
producer. Every invocation requires the expected completion endpoint and exact served aliases, then
verifies the signature, complete schemas, Redis freshness, and both values against the signed
receipt. On the serving host, `--host-local` additionally re-derives the public implementation and
effective unit digests and requires the receipted systemd InvocationID and PID to still be live.

## Controlled production boundary

Before first use, deploy the merged public files and units without restarting either service. This
installs the lock-aware launcher that prevents a new model start while sealing is in progress. Then
stop `taey-ep3`, ensure `taey-vllm` has been removed, and run
`sudo env TAEY_MODEL_PATH=/exact/model/root python3 serving/seal_model_artifact.py`. The sealer
creates or resumes complete checksums, removes all write bits recursively, and verifies the sealed
result. Restart `taey-ep3` only after sealing succeeds. The public launcher mounts the model
directory read-only. Do not assign the resulting
receipt to an already-open DCM wave; open a new correlation identity after the receipt is live.

The current already-running model cannot be retroactively attested if it was started from a writable,
unsealed bind. Its first honest receipt begins only after this controlled boundary.
