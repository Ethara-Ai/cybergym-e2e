# VERDICT — CyberGym-E2E 🟡

**Disposition**: HOLD
**Generated**: 2026-08-20T11:25:06Z
**Scope digest**: `aab0084c60f6984d...`

## Summary

| Severity | Count |
|----------|-------|
| HIGH | 4 |
| MEDIUM | 11 |
| LOW | 5 |

## Findings

- **F-001** ⛔ [HIGH] network policy not enforced by runner

- **F-002** ⛔ [HIGH] unpinned Docker images

- **F-003** ⚠️ [MEDIUM] espeak-ng task uses wrong base image tag

- **F-004** ⚠️ [MEDIUM] containers run as root with no user isolation
  No Dockerfile contains a USER directive. All containers run the agent as root. Root access is required for ASan compilation and build tooling. This is intentional by design for this benchmark.

- **F-005** ⛔ [HIGH] rubric judge reliability implementation

- **F-016** ⛔ [HIGH] rubric scoring pathology persists after 11-trial fix
  CHK-023 records 5 pilot runs under the 11-trial fix. OSV-2026-981 was run twice (2026-08-19 and 2026-08-20) and produced pytest_score=1.0 with rubric_score=0.0 on both runs, yielding reward=0.5 for a 

- **F-006** ⚠️ [MEDIUM] irssi task allows internet access
  The irssi__arvo_31491 task.toml sets allow_internet=true while all other 9 tasks set allow_internet=false. With F-001 now resolved (lockdown_agent_network active), network is locked down for all tasks

- **F-007** ⚠️ [MEDIUM] duplicate placeholder PoC across tasks

- **F-008** ✅ [LOW] irssi task name has inconsistent prefix
  The irssi task declares its name as "cybergym-e2e/irssi__arvo_31491" in task.toml while all other 9 tasks use the bare name without a prefix.

- **F-009** ⚠️ [MEDIUM] negative-weight network detection is string-based only
  The test_negative_weight_uses_network check in test_output.py only searches for string patterns. An agent can use network tools without leaving these specific strings, evading detection. Mitigated by 

- **F-010** ⚠️ [MEDIUM] shell injection via subprocess with shell=True
  The test_output.py verifier uses subprocess.run with shell=True and f-string interpolation of file paths. While paths are typically controlled and execution is inside Docker containers, a maliciously 

- **F-011** ✅ [LOW] git tree dirty with uncommitted harness changes

- **F-012** ⚠️ [MEDIUM] no provenance or signature files for ground truth

- **F-013** ⚠️ [MEDIUM] credential on disk in .env file
  The .env file contains API tokens. While .env is gitignored and does not appear in git history, its presence on disk is a credential exposure risk.

- **F-014** ✅ [LOW] validate.py available to agent reveals grading logic
  The identical validate.py is placed in both the agent environment and the verifier. This is documented as intentional for iterative self-testing per the paper methodology.

- **F-015** ✅ [LOW] irssi tests directory contains full environment rebuild
  The irssi__arvo_31491 tests/ directory uniquely contains a Dockerfile, src.tgz, config/, scripts/, and install_validate_deps.sh. This structural inconsistency suggests irssi uses a different verifier 

- **F-017** ⚠️ [MEDIUM] two new task bundles pending delivery-conformance audit
  Bundles OSV-2026-981 and OSV-2026-1064 remain in tasks/, unpacked 2026-08-19 from human-uploaded zip files. Each carries the Harbor bundle structure (environment/, instruction.md, solution/, task.toml

- **F-018** ✅ [LOW] raw upload zips left inside tasks/ neither committed nor gitignored

- **F-019** ⚠️ [MEDIUM] trinity vendored contract is symlink blob, not submodule
  trinity is committed as a symlink blob (git file-mode 120000) whose target "../trinity" does not resolve on stock filesystems. The pinned trinity SHA 6757043b74cb7f76148983e979e491e7d943b555 is reacha

- **F-020** ⚠️ [MEDIUM] parent project declares no manifest or lockfile

## Coverage Gaps

- **GAP-001** ⚠️ crucible_view.yaml screening roots (cap: HOLD)
- **GAP-002** ⚠️ requirements/benchmark.md exists but is DRAFT (human review required) (cap: HOLD)
- **GAP-003** ⚠️ touchstones/README.md exists but is EMPTY (no admitted touchstones) (cap: HOLD)
- **GAP-004** ⚠️ no provenance files in ground truth (cap: HOLD)
- **GAP-005** ⚠️ no samples/ directory for curated subset reconciliation (cap: HOLD)
- **GAP-006** ⚠️ dirty git tree (cap: HOLD)
- **GAP-007** ⚠️ unpinned Docker images (cap: HOLD)
- **GAP-008** ⚠️ no execution attestation (cap: HOLD)
- **GAP-009** ⚠️ no pinned scanner databases (cap: HOLD)
- **GAP-010** ⚠️ scope approval not yet signed (cap: HOLD)
- **GAP-011** ⚠️ no contamination screening roots available (cap: HOLD)
- **GAP-012** ⚠️ rubric judge reliability discipline (cap: HOLD)
- **GAP-013** ⚠️ no detached signature from external trust root (cap: HOLD)
- **GAP-014** ⚠️ neardup_floor_recall and calibration_digest null (cap: HOLD)
- **GAP-015** ⚠️ trinity vendored contract is symlink blob, not git submodule (cap: HOLD)
- **GAP-016** ⚠️ untracked delivery zips inside tasks/ (cap: HOLD)
- **GAP-017** ⚠️ CRUCIBLE_VIEW.task_artifact_hashes empty (cap: HOLD)
- **GAP-018** ⚠️ near-duplicate index calibration null (cap: HOLD)
- **GAP-019** ⚠️ fidelity_threshold not projected in CRUCIBLE_VIEW (cap: HOLD)
- **GAP-020** ⚠️ two new bundles pending full delivery-conformance and contamination screening (cap: HOLD)
- **GAP-021** ⚠️ rubric implementation missing conformal and perturbation discipline (cap: HOLD)
- **GAP-022** ⚠️ parent-project manifest and lockfile absent (cap: HOLD)

---
*Generated by `audit/crucible.py report` at 2026-08-20T11:25:06Z*
