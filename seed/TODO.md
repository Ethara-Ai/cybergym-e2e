# FORGE backlog

Generated. Everything declared but unimplemented, in the order it unblocks. Do not hand-edit.

## Blocking, in dependency order

1. Human authors `requirements/` at the parent project root. It is human-write-only under ENGRAM invariant E9 and no instrument may create it. Without it there is no design library, no budget declaration, and no authority to grant a slot a tier below Frontier-defeat.
2. Human authors `touchstones/` with a `touchstones/README.md` verdict table. Human-write-only under E12.
3. ENGRAM Phase 0.5 gate is discharged, then ENGRAM Phase 1 computes the two projections. FORGE needs `memory/forge_view.yaml` to exist and carry a pinned cohort registry, a frontier-defeat floor, projected tier thresholds, category axes, orthogonality groups, and lever states.
4. A cohort registry is pinned and a `cohort_registry_issuer` is authorized in `memory/roots.yaml`. Until then `ledger_shape.classification` stays UNPINNED and no lever can be ACTIVE, so no slot can anchor Hard or Frontier-defeat.
5. ENGRAM scaffolds `playbooks/` and a playbook relevant to vulnerability-discovery task authoring lands there.

## Then, and only then

6. FORGE Phase 0 opens one batch of at least twenty slots and writes `seed/contract.yaml`.
7. Human signs the design gate: `echo '<sha256 of seed/contract.yaml>' > seed/contract.approved`.
8. Phase 1 writes one runnable Harbor bundle per slot under `dataset/<uuid>/`.
9. Phases 2 and 3 run local validity and the adversarial pass. Local verification caps at `HOLD:PILOT_REQUIRED`.
10. An external signer, authorized for the role by a pinned trust policy controlled by neither agent, runs the frozen tasks against a frozen solver registry and signs one attestation. No such signer is configured in this project today.
11. Human signs the release gate at `seed/release.approved`. Only then can any slot reach `SHIP`.

## Standing note on scale

Rule 10 makes the batch the unit of work: one invocation is at least twenty slots, each a full runnable bundle with a pinned container environment, verifier, reference solution, negative controls and per-model rollout evidence. Steps 6 through 11 are not a single session of work.
