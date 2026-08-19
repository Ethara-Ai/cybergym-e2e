# EDICT: FORGE

**Disposition: HOLD:PILOT_REQUIRED** 🟡

**Project**: CyberGym-E2E. **Git SHA**: `c743ff475c3cbdca04caf6c45ee3a1e5a618056c` (dirty: `trinity`). **Trinity SHA**: `6757043b74cb7f76148983e979e491e7d943b555`. **Date**: 2026-08-18.

**Batch**: none opened. Slots requested 20, slots opened 0. Not terminal. **Reasons**: `forge-view-unresolvable`, `cohort-refresh-overdue`, `screening-unverified`.

## Correction to the previous revision

An earlier revision of this report recorded `BLOCK:INFEASIBLE_OR_UNVERIFIED`. That was wrong and is corrected here. The Dispositions section binds that value to a single sentence, that no pilot may be submitted until the feasibility bundle passes, which presupposes a scoped task whose feasibility was tested and did not pass. No task was ever scoped on this run, so the value asserted something untrue about a bundle that does not exist. `BLOCK:INVALID_TASK` fails the same way: it enumerates structural defects that make a bundle ungradable, and there is no bundle.

The contract assigns this exact condition, and does so by name. Under `HOLD:PILOT_REQUIRED` it states that an unresolvable `FORGE_VIEW` caps here, because a projection FORGE cannot read leaves the bundle unproven against standing direction rather than proven without it. That is precisely the situation. The same clause routes an unresolvable cohort-registry projection here with reason `cohort-refresh-overdue`, and an unresolvable screening root here with reason `screening-unverified`. Both apply. The contract adds that these are named coverage-gap reasons and never dispositions of their own, which is why they are carried as reasons beside a single disposition value.

The practical difference matters. BLOCK is a refusal and says the work is unsound. HOLD is a wait and says the work is unproven. Nothing about this project is unsound; FORGE simply has not been given what it needs to begin. `HOLD:PILOT_REQUIRED` is also the standing ceiling for all local verification, so a run that never scoped sits at that ceiling with everything below it unproven, which is the honest description.

## Why this run stopped before scoping

FORGE non-negotiable rule 1 requires standing direction to be read only through the read-only `FORGE_VIEW` at `memory/forge_view.yaml`, and forbids reading `memory/ledger.yaml`, the proof store, or any CRUCIBLE surface as a substitute. That file does not exist. ENGRAM computes the projections in its Phase 1, and ENGRAM is currently parked at its own Phase 0.5 sign-off gate. There is no lawful alternative source, so scoping never opened.

Three further preconditions are independently blocking. `requirements/` and `touchstones/` do not exist, and rule 4 requires both on every invocation; they are human-write-only under ENGRAM invariants E9 and E12, so no instrument may author them. Rule 11 mandates the Frontier-defeat tier for every slot, whose floors and projected thresholds are read from `FORGE_VIEW` under Hardness rule 2a and may only be raised, never lowered; with no view there is no floor, and with no `requirements/` there is no grant path to a lower tier either. Rule 9 routes cohort currency through the same projection, and no cohort is pinned anywhere in this project, which ENGRAM records as `ledger_shape.classification: UNPINNED` under GAP-E-007.

Rule 7 fails closed on missing sources, so a value is drawn from the closed set rather than deferred, and every precondition below caps the run rather than condemning a task.

## What caps this run

| Precondition | Rule | Caps at | Reason |
|---|---|---|---|
| `FORGE_VIEW` unresolvable | 1 | `HOLD:PILOT_REQUIRED` | `forge-view-unresolvable` |
| No pinned cohort registry | 9 | `HOLD:PILOT_REQUIRED` | `cohort-refresh-overdue` |
| No screening root, uncalibrated near-duplicate floor | 25 | `HOLD:PILOT_REQUIRED` | `screening-unverified` |
| `requirements/` and `touchstones/` absent | 4 | `HOLD:PILOT_REQUIRED` | named coverage gap |
| Frontier-defeat floor unreadable | 11 | `HOLD:PILOT_REQUIRED` | consequence of rule 1 |

## What was deliberately not written

`seed/contract.yaml` is the artifact the Phase 0.5 gate signs. Authoring it would mean asserting an archetype, a load-bearing lever set, a tier and a frontier floor with no lawful source for any of them. Rule 6 holds that difficulty is measured and never claimed, and a contract written against an absent projection is precisely a claimed difficulty. No `dataset/<uuid>/` bundle was written either; Phase 0 item 10 stops before any bundle file.

## Archetype vocabulary does not close

Recorded here because it binds FORGE directly. The ENGRAM hardness catalog at `memory/hardness.yaml` defines 47 archetypes, AR1 through AR47. Hardness rule 1 of this contract binds a closed primary-archetype selection set of ten, AR1 through AR10, and Phase 0 coverage requires every eligible archetype to be covered at least once with no archetype claiming more than one fifth of the slots. Thirty-seven catalogued archetypes are therefore unselectable by any lawful FORGE run, and the coverage rule is written against the smaller set. FORGE cannot resolve this: rule 1 is a closed set in its own contract and the catalog is ENGRAM-owned. Tracked as GAP-E-012 in `memory/scope.yaml` and surfaced in `HARDNESS.md`.

## This is a missing-input block, not a missing-material block

The material needed to author is present and intact: 139 upstream ARVO and OSS-Fuzz project definitions across 5659 files under `projects/` at tree digest `9e2a325b752793d3aea6d8023897d17b23645f37c33f4fe8e432e5fc5a32a65f`, the `convert_to_harbor.py` generator, and nine Harbor templates. The hardness catalog is live as well, with 341 levers across 45 categories on 3 axes recorded in `HARDNESS.md`, though every row is `CANDIDATE` and no lever is `ACTIVE`, so it carries design targets rather than anchors.

The previous tasks were a build output rather than tracked source. `dataset` was a symlink to the gitignored `tasks/` directory, which is the `--out` default of `convert_to_harbor.py`, so a rebuilt tree is a build step and not a recovery. What is missing is the standing direction that tells FORGE what hardness to author against, and the human inputs that bound it.

## Unblock path

The ordered backlog is generated at `seed/TODO.md`. In short: a human authors `requirements/` and `touchstones/`, discharges the ENGRAM Phase 0.5 gate so ENGRAM Phase 1 can compute the projections, and pins a cohort registry with an authorized issuer. Only then can FORGE open a batch.

*Instrument: FORGE | Harness: `seed/` | Contract: `trinity/FORGE.md`*
