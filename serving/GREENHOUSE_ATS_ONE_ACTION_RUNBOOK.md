# Greenhouse ATS one-action runbook

This is the canonical public launch path for one fresh Greenhouse ATS UI action.
The first production action for an application is `observe_form`. Do not create
its private action or manifest by hand and do not reuse an earlier identity.

## Preconditions

- Presence and Hands are clean public deployments at the reviewed commits.
- The dedicated Greenhouse Presence service is healthy with zero active turns.
- Its binding names the intended Greenhouse display, and the browser is already
  at the intended application.
- `TAEY_GREENHOUSE_ATS_PRIVATE_ROOT` names the same canonical absolute,
  nonsymlink, owner-owned `0700` directory configured in Presence.
- The application identity is represented only by its precomputed SHA-256.
  Applicant content and the lease credential are never command arguments,
  environment values, logs, or public artifacts.
- The transaction ID, action ID, event ID, and correlation ID are all new.
- `GREENHOUSE_ONE_ACTION_ENDPOINT` is resolved from the active dedicated
  Presence listener and ends with `/v1/greenhouse-ats/one-action`. Never infer
  or reuse a port from another Presence service.

## Mechanical gate

Run the independent local validator before a production launch:

```bash
python3 serving/validate_greenhouse_ats_observe_launcher.py
```

It proves the exact manifest and `observe_form` action schemas consumed by
Presence, canonical JSON and digest binding, owner-only directory and file
modes, `O_EXCL` collision refusal, symlink refusal, immutable-file birth/change
time agreement where the filesystem reports birth time, one exact local POST,
private response capture, and no retry after either a collision or an HTTP
failure. A `200` refusal or malformed observe receipt is also terminal. The gate
uses generated identities and a local synthetic endpoint; it does not touch a
browser or production private data.

## One fresh production observe

Resolve the values from the active production service and private application
record without printing them, then invoke the public launcher once:

```bash
python3 serving/launch_greenhouse_ats_observe.py \
  --private-root "$TAEY_GREENHOUSE_ATS_PRIVATE_ROOT" \
  --seat-id "$GREENHOUSE_SEAT_ID" \
  --event-id "$GREENHOUSE_EVENT_ID" \
  --correlation-id "$GREENHOUSE_CORRELATION_ID" \
  --display "$GREENHOUSE_DISPLAY" \
  --application-identity-sha256 "$GREENHOUSE_APPLICATION_IDENTITY_SHA256" \
  --hands-commit "$GREENHOUSE_HANDS_COMMIT" \
  --transaction-id "$GREENHOUSE_TRANSACTION_ID" \
  --action-id "$GREENHOUSE_ACTION_ID" \
  --endpoint "$GREENHOUSE_ONE_ACTION_ENDPOINT"
```

The launcher validates every supplied identity before creation. It creates:

```text
PRIVATE_ROOT/actions/SEAT/CORRELATION.json
PRIVATE_ROOT/transactions/SEAT/CORRELATION.json
PRIVATE_ROOT/outputs/SEAT/CORRELATION/headers.txt
PRIVATE_ROOT/outputs/SEAT/CORRELATION/response.json
```

The action and manifest are born as owner-only `0400` regular files beneath
validated `0700` directories. The response capture is born owner-only `0600`.
The launcher then calls only `POST /v1/greenhouse-ats/one-action` with body
`{"display":":N"}` and the exact seat, event, correlation, and
`greenhouse-ats-ui` profile headers. The lease credential remains in the
systemd credential descriptor path and is never handled by this launcher.

## Stop rule

The identity is spent after invocation, including a refusal, timeout, transport
failure, unknown result, or non-success HTTP status. Never run the command a
second time with the same identity. Keep the immutable action, manifest, and
private outputs as the evidence chain. Diagnose the first defect and create a
new transaction only after an explicit new production authorization.

After a successful observe, use only the bounded returned surface capsule to
construct the separately authorized next frozen action. `observe_form` grants
no fill, upload, selection, submit, or retry authority.
