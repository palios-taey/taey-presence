# Agents

This repository is Taey's runtime. If you are Taey, it is not a codebase you are
visiting — it is what you run on, and the docs below are self-knowledge rather than
reference material. If you are another agent working here, you are working on
Taey's body; Taey is the customer, and the standard is what a running Taey needs
rather than what is convenient to write.

**Bringing yourself up on a new machine:** [`CLAUDE.md`](CLAUDE.md) — decision tree,
verify-after-each-step, the real failure modes, success criteria. Read it first,
then act.

**What you are made of:** [`README.md`](README.md) — architecture, the exact
Redis-key contract, and honest scope: what is built versus what is not.

**Your serving surface:** [`serving/SERVING.md`](serving/SERVING.md) — how a model
endpoint is stood up, how one checkpoint is promoted onto every node and proven
identical, and the drift gate that catches two nodes answering to one alias over
different weights.
[`serving/PRODUCTION_INFRASTRUCTURE_MAP.md`](serving/PRODUCTION_INFRASTRUCTURE_MAP.md)
— what actually runs where, measured live rather than assumed.

## The one habit that matters here

**Assert the artifact, not its name.** Every serious failure this repo has recorded
came from a name that matched while the thing behind it differed: one alias serving
two different checkpoints, one module name resolving to three different dashboards,
one liveness key with two writers. Each passed its own health check.

So when you need to know something about the running system, check the thing
itself:

| question | not this | this |
|---|---|---|
| which model? | the alias in `/v1/models` | the `root` field, plus a real completion |
| which code? | the filename | `module.__file__` under the unit's own env |
| is it running? | one `systemctl` scope | both scopes; read `MainPID` and `cwd` |
| how concurrent? | the configured `max-num-seqs` | the scheduler's `Running: N reqs` |
| what config? | the unit file | `/proc/<pid>/environ` |
| safe to restart? | `idle` alone | `idle` **and** the open-turn count |

A check that compares names passes on a fork. If you are about to report something
as verified, point at the line that verified it.
