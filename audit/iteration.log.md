# CRUCIBLE iteration log

Dated work differential, appended once per invocation under Phase 0 item 3. Append-only.

## 2026-08-18, UPDATE run against c743ff4

Prior scope `8fe3aad5` derived 2026-08-18T16:00:00Z against `1c388d4` at root `/Users/apple/Desktop/Harness/project/cybergym-e2e`, preserved at `audit/lineage/scope.1c388d4.yaml`.

**Binding input deltas.** Project root moved. Git SHA moved two commits, 193 files changed, 1374 insertions, 34156 deletions. Both independently re-fire the Phase 0.5 sign-off.

**Removed surfaces.** `dataset/` (the graded artifact of the prior audit; zero `task.toml` files remain anywhere in the tree), `data/` (17 files), `jobs/`, `run_logs/` (6 logs).

**Changed surfaces.** `run_harbor.py` +228/-54, now committed; it was recorded dirty and ungraded at audit time.

**Newly present surfaces.** `scripts/finance_client.py` (178 lines, host-side opt-in egress to an external Finance API), `VERDICT.md`, `audit/`, `trinity` gitlink, `dataset.zip` placeholder.

**Gate finding.** `audit/progress.yaml` recorded Phase 0.5 as `pending_approval` while Phases 1 and 2 were recorded complete and `VERDICT.md` was emitted. No `audit/scope.approved` exists. Phase 0.5 item 2 requires the Phase 1 scaffolder to recompute the digest first and to exit before writing any harness file. Recorded as D-COVERAGE-GAP-005, capping at BLOCK.

**Outcome.** Re-scoped. Phases 0.5, 1 and 2 invalidated. Prior findings suspended as watch items, retained verbatim. `VERDICT.md` left byte-identical and marked STALE. Stopped at the Phase 0.5 gate with new digest `84d738de3b1353807f096921ab179483e994f05105b3a558c31d7ac8f5d22018`.
