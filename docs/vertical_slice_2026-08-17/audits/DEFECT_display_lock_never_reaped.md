# DEFECT — the display lock records its holder's liveness and never reads it

**Found:** 2026-08-17, production, three separate occurrences in one evening
**Owner:** taeys-hands (`consultation_v2/primitives.py`) — infra found it, infra does not patch it
**Severity:** HIGH. Takes a shared physical display out of service for one hour per dead turn.

## What the lock is

`acquire_display_lock` writes a Redis key `taey:plan_active:<display>` — for `:3` that renders
as `taey:plan_active::3`, the double colon being the display's own leading colon. It reserves
one X display so two drivers cannot type into the same Firefox window at once.

It is scoped to the DISPLAY, not to Taey. Sixteen Taey instances across two Thors reach the
same ~10 physical displays through `drive_chat`, so this key is the only thing standing
between them and a shared browser.

## The defect

`_lock_record` stamps every lock with the identity needed to tell whether its holder still
exists:

```python
record["holder_pid"]       = holder_pid
record["holder_starttime"] = holder_starttime   # from /proc/<pid>/stat, defeats PID reuse
```

`_process_starttime` exists specifically to make that identity forgery-proof across PID reuse.

**No code path ever reads those fields to decide whether an existing lock is stale.** The only
reads, at `primitives.py:169-170`, compare against `os.getpid()` — the same-process nested
acquisition case. `acquire_display_lock` does a bare `SET NX`: if the key exists it returns
`None` and the caller simply does not get the display. It never asks whether the holder is
alive. There is no reaper anywhere in `consultation_v2` or `serving/` (grepped).

`release_display_lock` requires the original `owner_token` AND the original process's
in-memory `_PROCESS_DISPLAY_LOCKS` entry. A process that dies before releasing cannot ever
release. The only cleanup is the Redis TTL, which defaults to **`ttl: int = 3600`**.

**So one driver process dying mid-drive removes a shared display from the fleet for an hour.**

## Production observations, same evening

| display | holder pid | locked_at | holder state when observed |
|---|---|---|---|
| `:3` | 1189853 | 22:29:24 | dead — no `/proc` entry |
| `:3` | 1229198 | 22:39:17 | dead |
| `:2` | 1250226 | 22:44:52 | dead |

Each was cleared by hand by infra after verifying `/proc/<pid>` was absent. A fourth instance
(pid 460534) was cleared earlier the same day. Taey misdiagnosed one of them as a live
`consult_monitor` process and filed a bug report with an invented start date — a fabrication
whose *root cause was this leak*, because a stale lock is indistinguishable from a live one
without the liveness check that is never performed.

## Why this is the right shape to fix, not to work around

The data is already written. The function to interpret it already exists. The gap is purely
that acquire treats "key present" as "display busy" instead of "display claimed by an
identity I can verify." A fix reads the record it already has:

- holder pid absent from `/proc`, or present with a different `starttime` → the claim is
  dead; break it and take the display.
- holder alive and matching → refuse, as today.
- Redis unreachable → raise, as today. A lock that cannot be evaluated is a loud failure.

That removes the hour-long outage without adding a background sweeper, a heartbeat, or a
second source of truth. It is smaller than the current code path, not larger.

The TTL should also stop being the only reaper. An hour is not a safety net at sixteen
instances contending for ten displays; it is an outage with a timer on it.

## Reproduction

```bash
redis-cli -h 127.0.0.1 GET taey:plan_active::3   # read holder_pid
ls -d /proc/<holder_pid>                          # absent => stale, yet the lock still refuses callers
```
