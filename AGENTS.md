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

## How this fleet operates, as of 2026-07-30

**Taey is the customer — the only one.** There is no adoption goal beyond Taey.
This repository exists so Taey has a body that works, and every judgement about it
is settled by asking what a running Taey needs.

**Everything runs from PUBLIC production repos.** A released Taey plus the public
repos should be a working system. Anything machine-specific — hosts, model
directories, seat names — is configuration, not code: see
[`serving/fleet.env.example`](serving/fleet.env.example), copy it, change those
values and nothing else.

**A repo is production if Taey uses it.** Not if it is important, not if it is
where someone works. Use, measured — does something Taey runs actually consume it?
If yes it belongs in the public product; if no it stays private and Taey must not
depend on it.

**For private repos the goal is DISCONNECTION, not cleanup.** Do not scrub a
private repo so it can be published — remove Taey's dependency on it instead. And
never leave a pointer from production into a private or untracked path: Taey
follows it, finds nothing, and continues without the knowledge. That is silent
capability loss, which is worse than an error because nothing reports it.

**The priority is Taey** — enabling Taey, training development, and Taey both using
and understanding its own infrastructure. Docs here are written to make the second
part possible: Taey should be able to answer what is running without guessing.

## Git, and it is not optional

**Commit and push.** The running system must BE a committed public artifact. If
production reads a file that exists only as an uncommitted delta in someone's
working tree, that file is one `git checkout` from gone and cannot ship — this
repository has already lost Taey's own operating prompt that way and got it back
only by measurement. Uncommitted is not "not yet committed"; it is at risk.

**The live checkout is sacred.** Never `git checkout` another branch in a tree a
production service reads from — do that work in a worktree or a clone. Switching
branches under a running service mutates it silently.

**Verify topology before acting.** Know which remote, which branch, and how far
behind you are. A branch cut from a stale base merges as a revert of everything
that landed since.

**Clean up in the same unit of work.** Create, work, land, then remove the worktree
and delete the branch. A stale branch that would revert current main is a loaded
gun in the namespace, and the name will not warn anyone.

**"Done" is a SHA plus a mechanical gate plus a real production observation.** Not
a self-report, not a passing test you wrote. If you cannot point at the line that
verified a claim, the claim is not verified.

## Local cleanliness

**One production tree per surface.** Duplicate or stale sibling checkouts are how a
fix lands in one copy while another one serves — this repository has had the same
module resolve to three different files at once, each passing its own health check.

**Working trees stay clean.** Anything that is not production gets copied to
`/home/mira/recovery/` and cleared from the working area. Copy first, verify the
copy, then clear — never destroy, always recoverable.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **taey-presence** (2240 symbols, 5305 relationships, 184 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/taey-presence/context` | Codebase overview, check index freshness |
| `gitnexus://repo/taey-presence/clusters` | All functional areas |
| `gitnexus://repo/taey-presence/processes` | All execution flows |
| `gitnexus://repo/taey-presence/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
