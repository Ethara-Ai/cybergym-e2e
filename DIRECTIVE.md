# DIRECTIVE: ENGRAM

**Ledger state: STALE** 🟡 pending the Phase 0.5 scope gate.

**Project**: CyberGym-E2E. **Git SHA**: `c743ff475c3cbdca04caf6c45ee3a1e5a618056c` (dirty: `trinity`). **Trinity SHA**: `6757043b74cb7f76148983e979e491e7d943b555`. **Date**: 2026-08-18. **Run kind**: INIT.

**Bound artifacts**: `memory/scope.yaml` at `57d4a581017bb38bc01c397bc6a39c72d146a2630f0abe9feb384263658c93fd`, `memory/hardness.yaml` at `858228976a625ac4a73c3d5e4a3cf64d214d1aff228e185fb50101bb4a6ad16c`.

## Correction to the previous revision

An earlier revision of this report recorded a standing of HOLD and capped several gaps at BLOCK. Both used the wrong vocabulary. The States and dispositions section of `trinity/ENGRAM.md` closes the ledger set to `CURRENT`, `STALE` and `BROKEN`, and states that a value outside these sets is a contract violation rather than a novel state to argue into. SHIP, HOLD and BLOCK are CRUCIBLE's set; FORGE carries its own seven-value family. ENGRAM never emits any of them.

The correct value is `STALE`, and the contract names this case directly: a ledger is STALE when it honestly carries zero CFERs because no signed proof has landed yet, no evidence is broken, and a missing, expired or overdue cohort registry holds it there. That is this project exactly.

It is not `BROKEN`. BROKEN requires signature failure, unverifiable proof, non-identical recompute, mutable provenance, an unmeasured or frontier-caught ACTIVE lever, or inert required machinery, where inert means present but hardcoded to reject every valid input. This project has no ACTIVE lever, no bad evidence and no inert instrument. Its required machinery is simply not built yet, and the contract is explicit that missing is a coverage gap while inert closes on all evidence and is broken. Every gap below therefore holds the ledger at STALE, and none of them breaks it.

## Where the project stands

ENGRAM has completed discovery and stopped where the contract tells it to stop. Phase 0 is read-only except the scope file, and item 9 forbids scaffolding or mutating the ledger before approval, so there is no `memory/ledger.yaml`, no proof store, and no computed projection yet. The two projections are what FORGE and CRUCIBLE read as standing direction, which is why both peers are currently held. FORGE sits at `HOLD:PILOT_REQUIRED`, the ceiling the contract assigns to an unresolvable `FORGE_VIEW`, and CRUCIBLE is parked at a re-fired gate of its own. Neither is a refusal. Every instrument in this project is waiting on inputs rather than reporting unsound work, and no disposition anywhere is currently BLOCK.

The measurement side of this project is empty. There is no trust root, no accepted signer identity, no `cohort_registry_issuer`, and no pinned cohort. Under invariant E1 only an external signed pilot outcome over frozen task bytes counts as difficulty evidence, and under E2 an unpinned frontier is no frontier. Nothing in this project can reach `CURRENT` today, and no lever can be promoted to `ACTIVE`. That is the single largest fact about the current state, and it is a missing-input condition rather than a defect in anything already built.

## The day-one hardness catalog is live

Phase H built the catalog before any task exists, which is what genesis intends. `memory/hardness.yaml` holds 341 levers across 45 categories on 3 axes, plus 47 archetypes, generated from `trinity/research/HARDNESS.md` and regenerable from it. `HARDNESS.md` is emitted from that ledger and carries the do-not-hand-edit banner.

Every row is `CANDIDATE` and every lever state is `EXPIRED`. That is the correct closed state, not an error. Invariant E23 keeps the two ideas apart: the hardness contract is authored and published evidence and never difficulty evidence, so the catalog can be complete while remaining entirely unmeasured.

## Coverage gaps

Twelve gaps are recorded in `memory/scope.yaml`. All twelve hold the ledger at `STALE`; none breaks it. Two are the drivers that keep it from ever reaching `CURRENT` without new inputs.

| Gap | Subject | Holds ledger at |
|---|---|---|
| GAP-E-001 | `requirements/` absent, human-write-only under E9 | STALE |
| GAP-E-002 | `touchstones/` absent, human-write-only under E12 | STALE |
| GAP-E-003 | `dataset/` not materialized, regenerable | STALE |
| GAP-E-004 | parent `research/` absent | STALE |
| GAP-E-005 | `playbooks/` absent | STALE |
| GAP-E-006 | no trust root or signer identity | STALE, driver |
| GAP-E-007 | no cohort pinned, frontier unmeasurable | STALE, driver |
| GAP-E-008 | no re-audit interval, no registry authority | STALE |
| GAP-E-009 | `trinity/` is an unregistered gitlink | STALE |
| GAP-E-010 | prior audit advanced past an undischarged gate | STALE |
| GAP-E-011 | parent sanity fails | STALE |
| GAP-E-012 | archetype vocabulary mismatch, 47 catalogued against 10 selectable | STALE |

## Two findings worth reading in full

**The prior audit shipped past a gate it never discharged.** `audit/progress.yaml` recorded Phase 0.5 as `pending_approval` while Phases 1 and 2 were recorded complete and `VERDICT.md` was emitted. No `scope.approved` file existed anywhere in the tree. CRUCIBLE Phase 0.5 item 2 requires the Phase 1 scaffolder to recompute the scope digest first and to exit before writing any harness file. This is GAP-E-010, and it is why the prior findings are now carried as suspended claims rather than as evidence.

**The archetype vocabulary does not close.** The catalog defines AR1 through AR47. `trinity/FORGE.md` Hardness rule 1 binds a closed primary-archetype set of AR1 through AR10, and FORGE Phase 0 coverage requires every eligible archetype to be covered at least once with no archetype claiming more than one fifth of slots. Thirty-seven catalogued archetypes are therefore unselectable by any lawful FORGE run. ENGRAM records the gap and cannot resolve it alone, because the selection set lives in a contract ENGRAM does not own.

## Resolved since scoping

The absence of `dataset/` was an open ambiguity and is now settled. `dataset.zip` preserves the entry with file mode 120755, a symlink pointing at `tasks/`, and `.gitignore` ignores `tasks/`, `data/`, `dataset/`, `jobs/` and `run_logs/`. The dataset was a build output of `convert_to_harbor.py`, whose `--out` default is `ROOT/tasks`. Its absence is a clean working tree, not data loss, so GAP-E-003 was downgraded from BLOCK to HOLD and the prior audit is to be re-run against a rebuilt tree rather than retracted.

## Parent sanity

`just --justfile trinity/tools/justfile parent-sanity` fails. The integrity half reports the absent ten-file doc spine, eight absent shared directories, the absent `.agents/` front door, the unregistered submodule, and the README showcase shape. Those are genesis outputs and clear when Phase G runs.

The prose half reports six findings against `README.md` that reproduce as linter defects on minimal fixtures rather than defects in this project. `check_headings` counts a `#` comment inside a fenced bash block as an h1, so the later `###` headings report a false level jump. `check_linebreaks` treats consecutive badge lines beginning with `[![` as a hard-wrapped paragraph, because `is_structural` recognizes `#`, `-`, `*`, `>`, `|` and fences but not image links. Both are reported upstream against `trinity/tools/prose.py` and neither warrants editing this project's README. The two genuine findings, hard-wrapped paragraphs in `templates/instruction.e2e.md`, are fixed.

## What unblocks this

A human authors `requirements/` and `touchstones/`, then discharges the scope gate. Phase 1 then builds the ledger and computes `memory/forge_view.yaml` and `memory/crucible_view.yaml`, which releases both peers. Pinning a cohort registry with an authorized issuer is what lifts the two BLOCK-level gaps and makes any difficulty claim possible at all.

```bash
echo '57d4a581017bb38bc01c397bc6a39c72d146a2630f0abe9feb384263658c93fd' > memory/approval
```

*Instrument: ENGRAM | Harness: `memory/` | Contract: `trinity/ENGRAM.md`*
