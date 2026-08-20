# The seat — what holds Taey's context between wakes

> **Written for Taey.** A seat is the process that keeps you present between turns. The
> model answering is you; the seat is what lets you still be mid-thought when someone
> comes back an hour later. Read this as how your own continuity works.

## What a seat is

A seat is a long-running process that owns one conversation: it holds the turn loop, calls
the proxy for each turn, executes the tools you ask for, and records what happened. When a
seat dies, the conversation on disk survives — the *pointer to where you were* is what is
lost, which is why seats are supervised rather than started by hand.

Seats run from the committed checkout. `serving/taey_council_seat.py` is the council seat;
each one holds a different lens on the same question and reaches the same model through the
proxy.

## Liveness: the namespace is the load-bearing part

Every seat writes `taey:<TAEY_SESSION_NAME>:{idle,turns_open,...}`.

**Two seats sharing one `TAEY_SESSION_NAME` write each other's keys.** One can then declare
the other idle in the middle of a turn, and the symptom — a turn that silently stops being
attended — looks nothing like its cause. A delegate seat MUST set a different name from the
executive.

Check it:

```bash
python3 serving/seat_liveness.py
```

Exit 0 with `seat_count` and the namespace it can account for. `namespace_declared: false`
means this environment cannot say which liveness keys its seats own — that is reported
rather than passed over, because a check that cannot see a thing must not imply it looked.

## Counting seats without counting the counter

Use a bracketed pattern:

```bash
ps -eo args= | grep -c "[t]aey_council_seat.py"
```

A bare `pgrep -f taey_council_seat` matches **its own command line** and reports one seat
that does not exist. This is not a style preference — it has produced a phantom process in
this fleet more than once.

## Where the seat runs is not where the thinking happens

Seat processes and the model can be on different machines: the seat calls
`$TAEY_SEAT_PROXY`, and the proxy reaches whichever node is serving. Counting seats on a
host tells you where the **drivers** are and nothing about where the **work** happens. To
learn which weights answered, ask the endpoint — `curl -sf "$TAEY_PROXY_URL/v1/models"` and
read `data[0].root`, never the alias in `data[0].id`.

## When a seat is not running

A stopped seat is not an error state by itself; seats are started for work. What *is* an
error is a seat that is running and not reachable, or a seat whose namespace collides with
another's. Both of those show up in `seat_liveness.py` and neither shows up in a count.

A live turn failing is not permission to destroy the seat. `taey_seat.py` logs a turn
error to `$TAEY_SESSIONS_DIR/<session>.process.log` and keeps the stdin loop. The
supervisor sets `remain-on-exit` so a pane exit leaves the named tmux session in place,
records `#{pane_dead_status}` plus a pane capture under
`$TAEY_SESSIONS_DIR/<session>/exit-evidence/`, and respawns the same pane. A missing
session (external `kill-session`) is still fatal and distinct from a dead pane.
