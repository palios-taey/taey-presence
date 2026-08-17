# FIX C2 — fsync the containing directory after os.replace

Small, bounded, unblocks the #319 merge immediately. conductor-codex's round-2 audit
independently found it (directory_fsync_observed=False) and grok logged it as residual C2.

## The defect

_write_manifest_transaction fsyncs the TEMP FILE handle then os.replace()s it into place, and
never fsyncs the containing directory. A crash after a successful replace but before the
parent directory metadata is durable can lose the directory entry. Verified: the file has
exactly ONE fsync call, on the temp handle.

## Why fix rather than accept as LOW

The manifest now SELF-DESCRIBES its commit method in-band. A recorded commit_method implying
a durable atomic publish, backed by code that does not make the directory entry durable, is a
claim stronger than the code - which is precisely the defect class this whole tool exists to
eliminate. The in-band guarantee has to be true.

## The fix

After os.replace succeeds, open the containing directory O_RDONLY, fsync it, close it. The
same shape the pre-existing shared helper easy_setup.atomic_write_text already uses - read it
for the reference pattern, but do NOT modify it and do NOT import from it; keep this tool's
write transaction local as it is now.

Make sure the directory fd is closed on every path including failure, consistent with how the
temp file is already cleaned up.

If this changes what the in-band verification block should say about the commit method,
update that too so the recorded claim stays derived from what actually ran.

## Constraints

- Worktree /home/mira/.peer-worktrees/infra-codex-vslice-collect, branch
  agent/codex-taey-delegate-collect. Do NOT touch the live checkout or easy_setup.py.
- Do not weaken or remove any existing check. Do not change exit codes.
- Published suite must stay 15/15:
  bash /home/mira/taey-presence-production/docs/vertical_slice_2026-08-17/instruments/07_supervisor_acceptance_v2.sh \
       /home/mira/.peer-worktrees/infra-codex-vslice-collect
- AI-native coherence must stay PASS: python3 scripts/verify-ai-native-coherence.py
- PUSH the branch when done so PR #319 head updates and the gates re-run. Do not merge.

Report commit SHA, both gate outputs, and a demonstration that the directory fsync actually
happens on the success path.
