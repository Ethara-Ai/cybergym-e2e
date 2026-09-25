---
name: temper
description: "FORGE scope-approval gate. Recomputes the live SHA-256 of .seed/contract.yaml against .seed/contract.approved and refuses every mutation until the two match. USE FOR: FORGE contract approval, .seed/ mutation checks, verifying approved contract bytes before authoring work."
---

# temper

Temper is the scope-approval gate FORGE owns. It recomputes the live SHA-256 of `.seed/contract.yaml` and compares it against the digest a human signed into `.seed/contract.approved`. It refuses every mutation until the recomputed digest matches that approval. A missing, unreadable, or mismatched approval fails closed rather than passing.

`trinity/FORGE.md` is the authority for what this door binds and when it runs. Read it and follow it; this door adds nothing to it and restates none of it.

Run the parent gate at both moments, before any phase work and again before the root report, from the parent project root, the directory whose direct child is the `trinity/` submodule:

    just --justfile trinity/tools/justfile parent-gate instrument=FORGE moment=preflight
    just --justfile trinity/tools/justfile parent-gate instrument=FORGE moment=report
