---
name: assay
description: "CRUCIBLE scope-approval gate. Recomputes the live SHA-256 of .audit/scope.yaml against .audit/scope.approved and refuses every mutation until the two match. USE FOR: CRUCIBLE scope approval, .audit/ mutation checks, verifying approved audit scope bytes before audit work."
---

# assay

Assay is the scope-approval gate CRUCIBLE owns. It recomputes the live SHA-256 of `.audit/scope.yaml` and compares it against the digest a human signed into `.audit/scope.approved`. It refuses every mutation until the recomputed digest matches that approval. A missing, unreadable, or mismatched approval fails closed rather than passing. It is distinct from the `crucible verify` success gate.

`trinity/CRUCIBLE.md` is the authority for what this door binds and when it runs. Read it and follow it; this door adds nothing to it and restates none of it.

Run the parent gate at both moments, before any phase work and again before the root report, from the parent project root, the directory whose direct child is the `trinity/` submodule:

    just --justfile trinity/tools/justfile parent-gate instrument=CRUCIBLE moment=preflight
    just --justfile trinity/tools/justfile parent-gate instrument=CRUCIBLE moment=report
