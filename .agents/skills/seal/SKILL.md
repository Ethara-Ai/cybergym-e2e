---
name: seal
description: "ENGRAM scope-approval gate. Recomputes the bound SHA-256 of .memory/scope.yaml against .memory/approval and refuses every mutation until the two match. USE FOR: ENGRAM scope approval, .memory/ mutation checks, verifying approved scope bytes before memory work."
---

# seal

Seal is the scope-approval gate ENGRAM owns. It recomputes the bound SHA-256 of `.memory/scope.yaml` and compares it against the digest a human signed into `.memory/approval`. It refuses every mutation until the recomputed digest matches that approval. A missing, unreadable, or mismatched approval fails closed rather than passing.

`trinity/ENGRAM.md` is the authority for what this door binds and when it runs. Read it and follow it; this door adds nothing to it and restates none of it.

Run the parent gate at both moments, before any phase work and again before the root report, from the parent project root, the directory whose direct child is the `trinity/` submodule:

    just --justfile trinity/tools/justfile parent-gate instrument=ENGRAM moment=preflight
    just --justfile trinity/tools/justfile parent-gate instrument=ENGRAM moment=report
