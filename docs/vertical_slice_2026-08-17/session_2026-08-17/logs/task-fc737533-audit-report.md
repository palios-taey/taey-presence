# task-fc737533 independent audit

## Verdict

REJECT commit `7538c98fc83da7eb953f160b861451612c273569` pending fixes.

Target worktree was inspected read-only and remained clean on branch
`agent/codex-taey-delegate-collect` at the audited SHA.

## Findings

### HIGH: successful output/input alias writes an immediately false manifest

`fleet_orchestrator/cli_taey_delegate.py:72-80` does not reject an output path
that is also a declared artifact. Collection hashes the old artifact, then the
atomic writer replaces that artifact with the manifest and returns exit 0.

Observed with `/tmp/conductor_codex_vslice_audit.py`:

```
exit=0
recorded_sha=8d114498c73e448981ab55e74018d9774add598ee23b2bf285c2e1c04794a37f
disk_sha_after_success=ab0263386ed059cb96107ea997e227897aea0156ec4b2ac7ddc4def504289f42
recorded_matches_disk_after_success=False
recorded_bytes=27 disk_bytes_after_success=312
```

### HIGH: re-verification is not immediate and leaves a TOCTOU window

`fleet_orchestrator/cli_taey_delegate.py:65-79` finishes all hashing before
constructing and writing the manifest. A same-size in-place mutation after
collection but immediately before `atomic_write_text` is invisible to the byte
count check and produces exit 0 with stale SHA-256.

Observed by replacing 4096 `A` bytes with 4096 `B` bytes at the write boundary:

```
exit=0
recorded_sha=6896d9ea3f73a4434f5832bc65714e7d066f177373f36f34dc8a6f735daa41b1
disk_sha_at_manifest_write=725bcd6c66d02acf6ebeab9c92410e010ea22e336876256aaf05a211f4ce1902
recorded_matches_disk_at_manifest_write=False
recorded_bytes=4096 disk_bytes=4096
```

The duplicate full collection pass at lines 65-68 does not close the window
between the final read and the manifest replacement.

### HIGH: a failed write can replace the prior manifest

`fleet_orchestrator/easy_setup.py:124-129` performs `os.replace` before the
directory fsync and read-back checks. If either post-replace operation fails,
the CLI returns nonzero but the previous manifest has already been replaced.
There is no rollback.

Observed by injecting an error on the directory fsync (the second fsync):

```
exit=1
stderr='ERROR: simulated directory fsync failure after replace'
fsync_calls=2
manifest_changed_on_failure=True
before_sha=e0f3622f08c9d7f6f3c72a8a0ea2c847c145f9d98033d8dd89c18404951516e4
after_sha=b5c52a62d5cc90491c775feed7b714a86200816c71fb177861ebd45c12bf9565
after_is_new_json=True
```

This violates the frozen requirement that a hard failure not write or modify
`artifacts.json`.

### HIGH: the supervisor acceptance script cannot fail its caller

`/home/mira/taey_runs/vertical_slice_prep/05_supervisor_acceptance.sh:13-15`
counts failures, but line 88 only prints the totals. With no `set -e` and no
final nonzero exit, the script's status is the successful final `echo` even
when `fail > 0`.

The script also invokes the module through `PYTHONPATH` at line 8 rather than
the installed `taey-delegate` entry point, never asserts happy-path exit at
lines 18-19, and line 80 only checks that the combined `sha bytes` string
changed, not that both fields changed independently.

## Passing baseline behavior

An isolated two-file happy path produced independently reproducible SHA-256 and
byte counts. An isolated missing-file run returned 1 and preserved the prior
manifest byte-for-byte.

## Commands run

```
python3 /tmp/conductor_codex_vslice_audit.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/mira/.peer-worktrees/infra-codex-vslice-collect python3 -B -m fleet_orchestrator.cli_taey_delegate collect ...
git -C /home/mira/.peer-worktrees/infra-codex-vslice-collect status --short --branch
git -C /home/mira/.peer-worktrees/infra-codex-vslice-collect rev-parse HEAD
```

The shared supervisor script was audited statically and was not executed because
it deletes and rewrites hard-coded shared `/tmp` paths while another auditor is
active.
