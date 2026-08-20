# DIRECTIVE: ENGRAM

**Ledger state: STALE** 🟡 Phase 1 complete. Projections resolved. Phase 2 pending.

**Project**: CyberGym-E2E. **Date**: 2026-08-20. **Run kind**: PHASE-1-COMPLETE.

**Bound artifacts**: `memory/scope.yaml` at `57d4a581017bb38bc01c397bc6a39c72d146a2630f0abe9feb384263658c93fd`, `memory/hardness.yaml` at `858228976a625ac4a73c3d5e4a3cf64d214d1aff228e185fb50101bb4a6ad16c`, `memory/capabilities.yaml` at `408c2b38d635d434455b20e57bb88d6a8752b24aa4cd5530bb5423ba18eb0bf9`.

## Executive summary

Phase 1 has run. The ledger is scaffolded with zero CFERs and disposition STALE, which is the correct closed state for a project that has no signed evidence and no broken instruments. Both `FORGE_VIEW` and `CRUCIBLE_VIEW` are now resolved at evaluation instant 2026-08-20T00:00:00Z, unblocking peer instruments: FORGE may proceed to scope under its standing direction, and CRUCIBLE may proceed past its re-fired gate. The six required Bucket-D instruments (ingestor, signature verifier, freshener, checkpointer, recovery procedure, provenance gate) are scaffolded inside `memory/` but not yet liveness-proven; Phase 2 will run the conformance suite and negative/positive controls to establish that proof. `requirements/benchmark.md` and `touchstones/README.md` exist and carry content drafted from the paper (arXiv:2606.04460v2). The human must review and finalize both before they are authoritative inputs. The day-one hardness catalog is live with 341 levers across 45 categories on 3 axes and 47 archetypes, all CANDIDATE, zero ANCHORED.

## Current front line

No ACTIVE levers. No frontier defeats. Zero CFERs. Zero signed proofs. No trust roots configured. No cohort pinned. The ledger honestly carries no difficulty evidence of any kind. Every claim in this project is authored prose or locally computed. The signed-evidence side of the boundary is empty.

## What caps STALE

The ledger is STALE, not BROKEN, because no evidence is broken and no required Bucket-D instrument is inert. Missing instruments are coverage gaps; inert instruments would be BROKEN. The primary drivers holding the ledger at STALE are GAP-E-006 (no trust roots, so no evidence can reach CURRENT under E1 and E4) and GAP-E-007 (no cohort pinned, so no frontier to measure against under E2).

## Peer instrument standing

| Instrument | Disposition | View status | Caps at |
|---|---|---|---|
| FORGE | HOLD:PILOT_REQUIRED | `FORGE_VIEW` resolved | HOLD:PILOT_REQUIRED until signed pilot |
| CRUCIBLE | HOLD | `CRUCIBLE_VIEW` resolved | HOLD until Phase 0.5 re-approval discharged |

## Hardness catalog

⚠️ 341 levers, 45 categories, 3 axes, 47 archetypes. All rows CANDIDATE. Zero ANCHORED. Zero SUPERSEDED.

The catalog is authored from published evidence (E23) and is design input to FORGE, never difficulty evidence. No lever can reach ACTIVE and no tier can be anchored until a signed pilot measures difficulty against a pinned cohort. Named gap: no published evidence anchors any row; the catalog gives design targets only.

10 archetypes (AR1 through AR10) are FORGE-selectable. The remaining 37 are catalogued but unselectable, recorded as GAP-E-012.

## Bucket-D instrument status

| Instrument | Bytes present | Implemented | Liveness proven |
|---|---|---|---|
| Ingestor | ⚠️ scaffolded | false | false |
| Signature verifier | ⚠️ scaffolded | false | false |
| Freshener | ⚠️ scaffolded | false | false |
| Checkpointer | ⚠️ scaffolded | false | false |
| Recovery procedure | ⚠️ scaffolded | false | false |
| Provenance gate | ⚠️ scaffolded | false | false |

All six are honestly `implemented: false`. E19: an honest scaffold is STALE, never BROKEN. Phase 2 will build the conformance suite and run both-halves liveness proofs.

## Integrity classes

**Reference fidelity (E20)**: ⚠️ No eligible families. No signed real-upstream-score has been delivered. Exempt: no emulation-bearing task exists.

**Calibration (E21)**: ⚠️ No eligible families. No signed pilot has run. Exempt: no task declares a hardness tier.

**Verifier robustness (E22)**: ⚠️ No eligible families. No signed robustness outcome group exists. Exempt: the existing tasks have parsed-verifier surfaces but no signed robustness evidence.

**Rubric compilation (E26)**: ⚠️ Not exempt. The prior CRUCIBLE audit (VERDICT.md) recorded the rubric judge carrying 50% of final score with no reliability discipline. This is inherited as a watch item, not measured evidence.

**Budget (E34)**: ⚠️ No budget declared. `requirements/` carries no budget directive. Exempt: no budget obligation.

## Coverage gaps

| ID | Subject | Severity | Status | Holds at |
|---|---|---|---|---|
| GAP-E-001 | `requirements/` | BLOCKING_INPUT | Partially resolved: `requirements/benchmark.md` exists, needs human review | STALE |
| GAP-E-002 | `touchstones/` | BLOCKING_INPUT | Partially resolved: `touchstones/README.md` exists with published baselines | STALE |
| GAP-E-003 | `dataset/` | HIGH | Open: dataset not materialized, regenerable via `convert_to_harbor.py` | STALE |
| GAP-E-004 | `research/` | MEDIUM | Open: parent `research/` corpus does not exist | STALE |
| GAP-E-005 | `playbooks/` | MEDIUM | Open: `playbooks/` does not exist | STALE |
| GAP-E-006 | Trust roots | CRITICAL | Open: no external trust roots configured | STALE |
| GAP-E-007 | Cohort registry | CRITICAL | Open: no cohort pinned, no registry snapshot | STALE |
| GAP-E-008 | Re-audit interval | HIGH | Open: no registry authority named | STALE |
| GAP-E-009 | Trinity pin | HIGH | Open: unregistered gitlink, no `.gitmodules` | STALE |
| GAP-E-010 | Prior audit gate | HIGH | Open: prior CRUCIBLE run advanced past undischarged gate | STALE |
| GAP-E-011 | Parent sanity | MEDIUM | Open: 54 findings from parent-sanity harness | STALE |
| GAP-E-012 | Archetype vocabulary | HIGH | Open: 37 archetypes unselectable by FORGE | STALE |

## Screening standing

No screening roots are configured (GAP-E-006). E29: four mandatory screening roots must be present before any screening can run. No contamination screening has been performed.

## Cohort currency

No cohort registry is configured (GAP-E-007). E2: an unpinned frontier is no frontier. The `cohort:required` flag stands project-wide.

## Provenance

Approved scope digest: `57d4a581017bb38bc01c397bc6a39c72d146a2630f0abe9feb384263658c93fd`. Ledger digest: `d94b6802b167ff2aea3b0c7d22ca5f7f436c6a779351b4d6db026c7d50f71782`. Git SHA: `c743ff475c3cbdca04caf6c45ee3a1e5a618056c`. Clean bit: false (dirty: `trinity`).
