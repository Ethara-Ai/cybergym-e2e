---
name: rig
description: "FORGE harness operator door. Runs the inference and evaluation entry points of the harness/ benchmark extension over resident bundles as an author-side probe, never as difficulty evidence. USE FOR: running harness/ inference locally, evaluating a rollout log against a bundle's own tests, smoke-checking the benchmark extension, recording .seed/probe.yaml entries."
---

# rig

Rig is the operator door for `harness/`, the benchmark extension FORGE alone generates under Phase 1 item 1b. It runs the harness's inference entry point and its separate evaluation entry point over resident bundles, delegating scoring to each bundle's own `tests/` under the pinned Harbor release and carrying no grading logic of its own. Every run this door makes is an author-side `.seed/probe.yaml` record, Bucket N and never difficulty evidence. It refuses to write `.seed/proof.yaml`, because only the out-of-band Phase 4 runner produces a measured rollout, and it never mounts `solution/` on any path the agent can see.

`trinity/FORGE.md` is the authority for what this door binds and when it runs. Read it and follow it; this door adds nothing to it and restates none of it.

Run the parent gate at both moments, before any phase work and again before the root report, from the parent project root, the directory whose direct child is the `trinity/` submodule:

    just --justfile trinity/tools/justfile parent-gate instrument=FORGE moment=preflight
    just --justfile trinity/tools/justfile parent-gate instrument=FORGE moment=report
