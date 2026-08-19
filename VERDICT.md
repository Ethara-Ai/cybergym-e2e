# VERDICT: CyberGym-E2E Benchmark Audit

**Disposition: HOLD** 🟡

**Project**: CyberGym-E2E, a benchmark harness for evaluating AI agents on real-world vulnerability discovery and patching.

**Git SHA**: `c743ff475c3cbdca04caf6c45ee3a1e5a618056c` (dirty: `trinity`). **Trinity SHA**: `6757043b74cb7f76148983e979e491e7d943b555`. **Date**: 2026-08-18. **Run kind**: UPDATE.

**Scope digest**: `7bca974b4481ed7aef734870afdc9384be1c0bccb47242f8552b345973c6c776`. **Supersedes**: `audit/lineage/VERDICT.1c388d4.md` at `49163fd1576fa63032973d45f6210288214199cbecd1d9a6ac22a978f657049e`.

## Executive summary

This is a re-audit, and its central conclusion is about the previous one. The prior audit was emitted against a project that has since moved, under a sign-off gate that was never discharged, and against a task tree that is no longer materialized. Its ten findings are retained verbatim and carried as suspended claims. They are not withdrawn, and nothing in this report contradicts their substance; they simply no longer rest on bytes that exist, so they cannot be re-asserted as evidence until they are re-derived under an approved scope against a rebuilt tree.

The project itself is not in a refusal state. Every open item is a wait condition: a gate to discharge, a build to run, a projection to receive, or a trust root to pin. That is why the disposition is HOLD rather than BLOCK.

## Why the prior audit is suspended

Three independent reasons, any one of which would be sufficient.

**The gate was never discharged.** `audit/progress.yaml` recorded Phase 0.5 as `pending_approval` while Phases 1 and 2 were recorded complete and `VERDICT.md` was emitted. No `audit/scope.approved` file existed anywhere in the tree. CRUCIBLE Phase 0.5 item 2 requires the Phase 1 scaffolder to recompute the scope digest first and to raise and exit before writing any harness file. Recorded as D-COVERAGE-GAP-005.

**The project moved.** The prior scope was derived at root `/Users/apple/Desktop/Harness/project/cybergym-e2e` against `1c388d4`. HEAD is now `c743ff4`, two commits ahead: 193 files changed, 1374 insertions and 34156 deletions. `run_harbor.py` alone moved by 228 added and 54 removed lines, and it was recorded dirty and ungraded at the time of the prior audit. Every prior finding touching that file was derived against bytes that are no longer on disk.

**The graded artifact is not materialized.** The prior scope recorded 10 Harbor bundles at schema version 1.1. Zero `task.toml` files exist in the tree today.

## The dataset question, settled

`dataset` was a symlink, not a directory. `dataset.zip` preserves the entry with file mode 120755 pointing at `tasks`, and `.gitignore` ignores `tasks/`, `data/`, `dataset/`, `jobs/` and `run_logs/`. The bundles were a build output of `convert_to_harbor.py`, whose `--out` default is `ROOT/tasks`.

This matters for the disposition. The graded surface is recoverable, not lost, so D-COVERAGE-GAP-004 was downgraded from BLOCK to HOLD and the correct remedy is to rebuild and re-audit rather than to retract. Rebuilding needs the `data/projects` payloads, which were removed at `c743ff4` and are available from HuggingFace `sunblaze-ucb/cybergym-e2e` at revision `a65d1d273eb7ee5db7525418120fc2434b887203`, recorded in the tree at the prior SHA. The 139 upstream project definitions under `projects/`, 5659 files at tree digest `9e2a325b752793d3aea6d8023897d17b23645f37c33f4fe8e432e5fc5a32a65f`, are present and intact.

## Coverage gaps

| Gap | Subject | Caps at |
|---|---|---|
| D-COVERAGE-GAP-001 | no exclusion-list root, none of the five mandated named rows carried | HOLD |
| D-COVERAGE-GAP-002 | no manifest or lockfile tracked, dependency surface unenumerable | HOLD |
| D-COVERAGE-GAP-003 | `memory/crucible_view.yaml` absent, no standing direction | HOLD |
| D-COVERAGE-GAP-004 | graded artifact not materialized, regenerable | HOLD |
| D-COVERAGE-GAP-005 | prior run advanced past an undischarged gate, remediated by suspension | HOLD |

The dependency gap is worth stating plainly: no `requirements.txt`, `pyproject.toml`, `setup.py`, `package.json` or lockfile of any kind is tracked. A Python and Docker harness that declares no dependencies cannot have its dependency surface audited from declared bytes, which also means the prior finding about unpinned container images has no manifest to be checked against.

## Surface change since the prior audit

**Newly present.** `scripts/finance_client.py`, 178 lines, POSTs aggregated token-usage metrics to an external Finance API over httpx or urllib. It is opt-in, gated on `--finance-api-url` which defaults to `None`, runs on the host after scoring from already-written output files, and cannot alter the harness exit code or any score. It is nonetheless a new outbound network path introduced after an audit whose leading finding was that network isolation is declared but never enforced, and it should be in scope when that finding is re-derived. Its module docstring documents a call signature that does not match the function it describes.

**Removed.** `data/` (17 files, including the HuggingFace cache reference and the `hdf5/arvo_58701` payload), `jobs/`, `run_logs/` (6 logs). Deleted blobs remain reachable in git history, so full-history secret scanning must target history and not only the working tree.

**Unchanged.** `projects/`, `templates/`, `lib/validate.py`, `convert_to_harbor.py`.

## Suspended claims carried forward

Retained verbatim in `audit/findings.yaml` at `72519a313f09c20c9fe3878dcf6c80c86618d7ffc861a2148b9d553e46efbf08` and tracked as watch items: F-001 network isolation declared but never enforced, F-002 unpinned container image tags, F-005 rubric judge carrying half the final score with no reliability discipline, F-012 ground truth without provenance or signature binding, and gaps GAP-004, GAP-007 and GAP-012. Each must be re-derived, not re-copied, once the tree is rebuilt.

## Path out of HOLD

Discharge the scope gate, rebuild the task tree from the pinned upstream revision, and let ENGRAM reach Phase 1 so a `CRUCIBLE_VIEW` exists to scope against. Then Phases 1 and 2 can run for real and the suspended findings can be re-derived against bytes that exist.

```bash
echo '7bca974b4481ed7aef734870afdc9384be1c0bccb47242f8552b345973c6c776' > audit/scope.approved
```

*Instrument: CRUCIBLE | Harness: `audit/` | Contract: `trinity/CRUCIBLE.md`*
