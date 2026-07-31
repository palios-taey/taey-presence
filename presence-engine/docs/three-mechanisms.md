# The three mechanisms, in depth

They share one input signal (your partial, not-yet-submitted text) and one
model path. They are otherwise distinct features with distinct triggers,
distinct outputs, and distinct UI. Conflating them is the mistake this doc
exists to prevent.

| | Trigger | Question it answers | Output | UI |
|---|---|---|---|---|
| **Prediction** | every debounced keystroke | "what will you say next?" | a predicted continuation + confidence | ghost text + accept ("OMG") button; blurs on pivot |
| **Interrupt** | confusion or urgency, above a floor | "do I need to speak before you finish?" | a clarifying question or urgent flag | an interrupt bubble that appears mid-typing |
| **DCM** | each loop tick | (publishes "here is my state") | state WRITTEN to Neo4j; peers not yet read back | (none; read-path unwired) |

## Prediction (anticipatory)

`prediction/predictor.py`. Runs an LLM completion on your partial input asking
for the most likely continuation. Renders dim until confidence clears a floor,
then solid. The **pivot** behavior — blurring the ghost when your trajectory
diverges (`state == "diverging"`) rather than abruptly swapping it — is what
keeps it from feeling like a slot machine. It's about *your words*.

## Interrupt (reactive)

`interrupt/interrupter.py`. Runs an LLM completion asking a different question:
am I confused, or is something urgent? Most of the time the answer is no, and
nothing happens. When the answer is yes (a non-empty clarification, or urgency
above the confidence floor) the engine **interrupts** — it asserts that
speaking now beats letting you finish. This is a real semantic act, not
autocomplete. It's about *the engine's own state* (confusion/urgency), not
your next word.

Why they're separate: a system can be highly confident about what you'll say
(strong prediction) while being entirely un-confused (no interrupt). And it can
be confused (interrupt) while having no idea what you'll say next (no
prediction). The two signals are orthogonal — one model pass each, different
prompts, different thresholds.

## DCM (inter-instance) — publish-only as wired

`dcm/inter_instance.py`. Each instance WRITES its current state
(`PresenceInstance` node) to a shared no-auth Neo4j via `write_state()`.

**Honest status:** `read_peer_states()` is implemented but the engine loop does
NOT call it. So peer state is *published* but not *consumed* — the documented
goal ("an instance factors a peer's context into its own decisions") is NOT yet
wired. This is inter-instance telemetry today, not coordination. Wiring the
read path into prediction/interrupt is a future enhancement.

Optional: if Neo4j is unreachable, each engine runs standalone and the other
two mechanisms are unaffected. Ships `dcm/schema.cypher` (constraint + index
only) so you stand up your own store. No data crosses.
