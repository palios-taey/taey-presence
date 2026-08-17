# DEFECT — the display lock does not serialize Taey against Taey

**Found:** 2026-08-17, reading the code after Jesse asked what the lock actually is
**Owner:** taeys-hands (`consultation_v2/primitives.py`) + `taey-presence/serving/ui_drive.py`
**Severity:** HIGH at the current fleet size. Sixteen Taey instances, one shared owner token.

## What the lock actually does

`ui_drive._guard_action` takes the display lock before any action op:

```python
LOCK_OWNER = "taey-drive_chat"                       # ui_drive.py:45  — a SHARED CONSTANT
LOCK_TTL_DEFAULT = int(os.environ.get("TAEY_DRIVE_LOCK_TTL", "600"))
...
token = _acquire_display_lock(payload={"owner_token": LOCK_OWNER}, ttl=ttl, display=display)
if token is not None:
    return                                            # freshly acquired
owner = record.get("owner_token")
if owner == LOCK_OWNER:
    _renew_if_owner(display, ttl)
    return                                            # <-- SAME OWNER: renew and PROCEED
raise UiDriveError(f"display {display} is held by another driver ({owner})")
```

The lock is never released. It is acquired once, renewed on every subsequent action, and left
to expire ~600s after the last one. That part is deliberate and fine.

## The defect

**`LOCK_OWNER` is a module-level constant, identical in every process.** Every Taey instance
that calls `drive_chat` presents the string `taey-drive_chat`. So when instance B finds the
lock held by instance A, it takes the `owner == LOCK_OWNER` branch, renews, and **proceeds**.

The lock therefore separates *Taey* from *taeys-hands*. It does not separate *Taey* from
*Taey*. With sixteen instances across two Thors reaching ~10 shared physical displays through
one proxy, two instances can drive the same Firefox window at the same time and neither is
refused. The mutual exclusion that the partitioned-ownership doctrine assumes exists between
seats does not exist within a seat.

The failure this permits is silent and ugly: interleaved clicks and keystrokes into one
composer, one instance's Return sending another's half-typed packet, and an extraction
attributed to whichever instance asked last.

## Second defect: the identity fields are decorative

`_lock_record` stamps `holder_pid` and `holder_starttime`, and `_process_starttime` exists
specifically to make that identity survive PID reuse. Under the shared-token design **nothing
enforces them** — ownership is decided entirely by `owner_token`. The pid belongs to a
short-lived per-action process that is *supposed* to exit.

This is not cosmetic. It actively misleads. Reading a lock record whose `holder_pid` is absent
from `/proc` looks exactly like a stale lock, and a supervising seat (infra, tonight, twice)
cleared locks by hand on that reasoning — including one on `:2` that was **live**, while the
proxy log shows Taey driving `:2` continuously from 22:43:35 to 22:51:40.

Either enforce the identity or stop recording it in a shape that reads as ownership.

## Third: every lock error path is fail-open

`_guard_action`'s docstring states it plainly — *"FAIL-OPEN: any lock error logs to stderr and
proceeds — the lock never blocks driving."* Redis unreachable, record unreadable, or the
import unavailable (`_LOCK_AVAILABLE = False`) all proceed without any mutual exclusion, to
stderr only. Note this is the opposite of `consultation_v2.acquire_display_lock`, whose
docstring says a lock that cannot be taken is *"a loud failure, never a silent proceed without
the lock."* Two layers of the same mechanism disagree about whether the lock is safety-critical.

## What a fix must establish

The owner token must identify **the requesting instance**, not the tool. Then the existing
`owner == LOCK_OWNER` branch becomes a true same-owner renewal and a different instance is
correctly refused by the raise that is already there. The refusal path, renewal path and TTL
all already exist and stay as they are.

The identity is not currently reachable where it is needed, and that is the actual work:

- `soma_proxy.py` HAS a validated seat identity — `X-Taey-Seat-Id`, checked against
  `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` in `_normalize_seat_id`.
- `soma_proxy.py` invokes `serving/ui_drive.py` as a subprocess and passes it **no identity
  at all**. `ui_drive.py` contains zero references to a seat (verified by grep).

So the change is: pass the identity across that subprocess boundary (argument or environment),
and compose `LOCK_OWNER` from it instead of hardcoding the tool name.

**Open question the implementer must answer first, not assume:** whether `seat_id` actually
distinguishes the sixteen instances, or whether all of them present the same value (`taey`).
If it does not distinguish them, the token needs whatever does, and identifying that is part
of the work. Marked **[Unknown]** here rather than guessed — building on a wrong assumption
about what disambiguates an instance would reproduce this bug with more steps.

## Not claimed here

Whether two instances have actually collided on one display in production is **[Unknown]** —
no instrumentation records which instance drove which action. The code permits it; that is the
finding. The absence of an observed collision is not evidence of exclusion, because nothing
would have recorded one.
