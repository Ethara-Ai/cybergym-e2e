# ENGRAM Memory Harness

This directory is the ENGRAM memory instrument for the CyberGym-E2E project. It is owned exclusively by ENGRAM under the trinity contract at `trinity/ENGRAM.md`. No other instrument writes here.

## Disposition

**STALE**. No signed pilot proofs exist, no CFERs have been birthed, and every required Bucket-D instrument is honestly unimplemented. This is the correct closed state for an unmeasured project under E19: an honest scaffold is STALE, never BROKEN.

## Harness layout

| File | Role | Status |
|---|---|---|
| `scope.yaml` | Phase 0 scope binding the sign-off gate | Written |
| `capabilities.yaml` | Per-project capability opt-in block | Written |
| `hardness.yaml` | Hardness contract catalog (341 levers, all CANDIDATE) | Written |
| `ledger.yaml` | Append-only CFER ledger | Empty scaffold |
| `roots.yaml` | External trust-root references | Empty scaffold |
| `forge_view.yaml` | FORGE-facing ledger projection | Empty scaffold |
| `crucible_view.yaml` | CRUCIBLE-facing ledger projection | Empty scaffold |
| `genesis.yaml` | Project identity and genesis scope | Written |
| `progress.yaml` | Derived progress cache (E28: never authoritative) | Written |
| `feedback.yaml` | Append-only human feedback ledger | Empty |
| `proofs/` | Signed pilot proof store | Empty directory |

## Files not yet created

These files are required by the contract but do not exist until their owning phase runs or their inputs arrive:

- `approval` - SHA-256 of `scope.yaml`, written by human at Phase 0.5 gate
- `genesis.approval` - SHA-256 of `genesis.yaml`, written by human at Phase G gate
- `seed.yaml` - Seed corpus record (Phase 1)
- `freshness.yaml` - Freshness state vector (Phase 2)
- `checkpoints.yaml` - Signed Merkle checkpoint log (Phase 1 step 8a)
- `supersessions.yaml` - Append-only supersession relation (Phase 1 step 8a)
- `sequence.yaml` - Authoritative accepted-envelope sequence (Phase 1 step 8a)
- `retirements.yaml` - Append-only human retirement records (Phase 1 step 8a)
- `canary.yaml` - Issued canary tokens and contamination records (Phase 1 step 8b)
- `fidelity.yaml` - Reference-fidelity memory (Phase 1 steps 8c-8f)
- `budget.yaml` - Budget memory (Phase 1 step 8e)
- `works.yaml` - Publication work registry (Phase S)
- `TODO.md` - Generated augmentation backlog (Phase 2 step 8a)
- `staging/` - Harvest staging area (Phase H)
- `results/` - Volatile raw transcripts (gitignored)

## Firewall

FORGE reads only `forge_view.yaml`. CRUCIBLE reads only `crucible_view.yaml`. Neither may read the raw ledger, proof store, or any other instrument. Both projections are pure functions of the canonical input closure. Named forbidden fields are stripped by the projector. Direct reads of `ledger.yaml` or `proofs/` by a peer instrument are contract violations.

## Trust roots

No external trust roots are configured (`roots.yaml` is empty). Under E4, no evidence can reach CURRENT without external signatures. The four mandatory screening roots under E29 are absent. This is recorded as GAP-E-006 (CRITICAL).

## Coverage gaps

12 named coverage gaps are recorded in `scope.yaml`. The most critical are:

- **GAP-E-006**: No external signature trust roots declared
- **GAP-E-007**: No cohort registry, no pinned frontier
- **GAP-E-001**: `requirements/` absent (human-write-only input)
- **GAP-E-002**: `touchstones/` absent (human-write-only calibration)
- **GAP-E-003**: `dataset/` not materialized

## Contract authority

This harness is governed by `trinity/ENGRAM.md`. This README is generated documentation, not authoritative state. The authoritative files are `scope.yaml` (bound by `approval`), `ledger.yaml`, and `roots.yaml`.
