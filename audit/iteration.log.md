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

## 2026-08-20, UPDATE run against 401551

Prior scope `7bca974b` derived 2026-08-18T00:00:00Z against `c743ff4` at root `/Users/apple/Desktop/mcp/cybergym-e2e`, preserved byte-identical at `audit/lineage/scope.c743ff4.yaml`. Prior scope was never signed; no `audit/scope.approved` exists anywhere in the tree.

**Binding input deltas.** Project root reverted to `/Users/apple/Desktop/Harness/project/cybergym-e2e`. Git SHA moved `c743ff` -> `401551`. Tracked file count `5717` -> `5792`. Committed task bundles `0` -> `13`. Any one re-fires Phase 0.5 on its own.

**Positive delta.** `memory/crucible_view.yaml` is now present with `projection.status=resolved` and its four screening roots (`exclusion_list`, `freeze_date_table`, `near_duplicate_index`, `source_attestation`) populated at fixed immutable digests. This closes prior `D-COVERAGE-GAP-003` and makes the contamination-screening surface runnable at build-time. `task_artifact_hashes` and `proof_digests` remain empty; `escalation_flags` empty.

**Newly present surfaces.** `tasks/` materialized (13 Harbor bundles: 10 pre-existing plus `OSV_2026_744`, `OSV-2026-981`, `OSV-2026-1064` unpacked 2026-08-19 from still-untracked upload zips). Each bundle carries `environment/`, `instruction.md`, `solution/`, `task.toml`, `tests/`.

**Untracked and unignored.** Three raw upload zip artifacts sit inside `tasks/` alongside their unpacked bundles: `OSV_2026_744-20260819T125442Z-1-001.zip`, `OSV-2026-1064-20260819T105558Z-1-001.zip`, `OSV-2026-981-20260819T092938Z-1-001.zip`. Neither committed nor gitignored. Recorded as `D-COVERAGE-GAP-007`.

**Trinity link.** `trinity` remains a committed symlink blob (mode 120000) whose target `../trinity` is unresolvable on stock filesystems; this shell can only reach the contract via the vendored working-tree link. Not converted to a git submodule. Recorded as `D-COVERAGE-GAP-006`.

**Raw scan signal.** `audit/results/scan.yaml` for git 401551: 10 HIGH image_pinning (all three new bundles unpinned plus seven previously pinned images now unpinned again), 48 MEDIUM shell_injection in `run_harbor.py`, 13 MEDIUM no_user_directive, 10 MEDIUM cross_task_image_mismatch (espeak-ng still borrowing harfbuzz image). Prior `findings.yaml` had marked most of these RESOLVED under c743ff4. All prior findings are re-suspended until Phase 2 re-derives them under the new approved scope.

**Coverage gaps this run.** Ten open: `GAP-001` (exclusion-list rows unverified), `GAP-002` (parent-project manifest still absent), `GAP-004` (dataset/ still absent), `GAP-005` (prior gate defect retained), `GAP-006` (trinity link), `GAP-007` (untracked delivery zips), `GAP-008` (`task_artifact_hashes` empty in view), `GAP-009` (neardup calibration null), `GAP-010` (fidelity_threshold not projected). One closed: `GAP-003` (CRUCIBLE_VIEW now readable).

**Outcome.** Re-scoped. Phases 0.5, 1, 2 invalidated. Prior findings retained SUSPENDED. `VERDICT.md` left byte-identical and marked STALE, preserved at `audit/lineage/VERDICT.c743ff4.md`. Stopped at the Phase 0.5 gate with new digest `aab0084c60f6984db51c8e79f3752eee9a4aa0c44f286557495806642e5ec748`.
