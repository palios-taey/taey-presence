# NEED YOUR ACTUAL TWO-FILE SHAPE — the seat ACK gate declaration contract

You proposed a two-file shape for the seat ACK gate: a turn whose packet declares deliverables
cannot record ok=True without a matching manifest. That design was accepted by the Chats and
is now the highest-priority substrate patch.

I do not have your actual spec. I had a one-line reference to it and wrote an implementation
brief as if I had the syntax. infra-codex correctly refused to invent a packet contract and
stopped. So the implementation is halted until you supply the real thing.

## What I need, precisely

1. HOW IS A DELIVERABLE-DECLARING PACKET IDENTIFIED? Is it an explicit command or directive in
   the packet body? A header field? Something else? Give the exact syntax.
2. WHERE DOES THE PATH SET COME FROM - does the declaration itself enumerate the declared
   paths, or is it derived some other way?
3. WHERE DOES THE MANIFEST PATH COME FROM - declared in the packet, or conventional?
4. WHAT ARE THE TWO FILES in "two-file shape"? Name them and what each is responsible for.
5. What does the gate do on a packet that declares NOTHING? The executive lane also carries
   ordinary conversational raises to Jesse and those must be completely unaffected.

## Context that has changed since you proposed it

taey-delegate collect is merged (orchestrator 0d6e56c), installed at /usr/local/bin, and Taey
has produced a verified manifest with it in production. So the manifest your gate checks for
is a real artifact with a known schema, including an in-band verification block. If that
changes what you would specify, say so.

## Constraint

Give me the spec, not an implementation. infra-codex implements. If any part of your original
shape was underspecified or you would revise it now, say that plainly rather than
retrofitting - I would rather have an honest "I did not specify that" than a guess dressed as
the original design.
