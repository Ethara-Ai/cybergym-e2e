# VERDICT: CyberGym-E2E Benchmark Audit

**Disposition: HOLD** 🟡

**Project**: CyberGym-E2E, a benchmark harness for evaluating AI agents on real-world vulnerability discovery and patching.

**Date**: 2026-08-20. **Run kind**: POST-FIX UPDATE (Trinity cycle, rubric judge fix).

**Git SHA**: `401551231b9100474e6fa8e449cd52c4546f8b4a` (clean tree) ✅

**Scan engine**: CRUCIBLE scanners v1.0.0 (docker_scanner, task_scanner, runner_scanner).

## Executive Summary

CyberGym-E2E is a well-structured vulnerability discovery benchmark with sound task design, correct verifier isolation, and active network lockdown. This cycle resolves four prior HOLD-blocking findings:

1. **F-001 network policy** — RESOLVED ✅ `lockdown_agent_network()` applies iptables rules post-install
2. **F-002 unpinned images** — RESOLVED ✅ All 10 Dockerfiles now pinned with `@sha256:` digests
3. **F-005 rubric judge** — RESOLVED ✅ 11-trial position-randomized median replaces single LLM call
4. **F-011 dirty tree** — RESOLVED ✅ Working tree clean on main

Trinity infrastructure now in place:
- ENGRAM Phase 2 complete (15/15 conformance, 6/6 Bucket-D liveness)
- Trust root configured (self-attested, E4 caps below CURRENT)
- Cohort pinned (claude-opus-4-8 primary, claude-opus-4-6 secondary)
- Four E29 screening roots configured and projected to CRUCIBLE_VIEW

Disposition remains HOLD due to remaining coverage gaps (no external trust root signature, no provenance files, scope approval pending).

## Resolved Findings This Cycle

### F-001: Network Policy ✅ RESOLVED

`lockdown_agent_network()` at `run_harbor.py:213` applies iptables rules after `install_claude_code()` completes. Blocks all outbound except API bridge (`host.docker.internal`). Called at line 1123 before `run_claude_code_agent()`. IPv4 and IPv6 covered. REJECT with `icmp-net-unreachable`.

### F-002: Docker Image Pinning ✅ RESOLVED

All 10 Dockerfiles now carry `@sha256:` digest pins:
- 7 `n132/arvo:*` images pinned to current digests
- 1 `cybergym/e2e:quickjs` pinned to `sha256:de6128c6...`
- 2 `gcr.io/oss-fuzz-base/base-builder` already pinned

### F-011: Dirty Git Tree ✅ RESOLVED

Working tree clean. SHA `401551231b9100474e6fa8e449cd52c4546f8b4a` on main.

## Remaining Findings

### F-005: Rubric Judge ✅ RESOLVED

`evaluate_rubric()` now runs 11 trials with randomized criteria order (position randomization) and takes the median score via `statistics.median`. Minimum 3 successful trials required. Trajectory placed in cached content block for API cost efficiency. Prior pilot runs showed single-call scoring distortion (OSV-2026-981 scored 0.5 instead of 1.0); median eliminates outliers.

## Remaining Findings

### ⚠️ MEDIUM — No provenance files for ground truth (F-012)

No `solution/` directory contains TRUTH.md, provenance.yaml, provenance.sig, or rubrics.json. Expected for a Path B vendor harness without genesis. Ground truth authenticity cannot be independently verified.

**Impact**: HOLD (provenance)

### MEDIUM — Placeholder PoCs (F-007, documented)

hdf5 and pcapplusplus have identical 20-byte null-byte PoCs. S4 validation produces unreliable results for these 2 of 10 tasks. `POC_WARNING.md` documents the issue.

### MEDIUM — shell=True in verifier scripts (F-010)

`test_output.py` uses `subprocess.run(shell=True)` with f-string interpolation. Risk contained within Docker.

### MEDIUM — espeak-ng uses harfbuzz base image (F-003)

Intentional reuse documented in Dockerfile comment. Not independently reproducible.

### LOW — Remaining cosmetic/config findings

- F-004: Root containers (by design for ASan)
- F-006: irssi allow_internet inconsistency (mitigated by iptables)
- F-008: irssi name prefix inconsistency
- F-009: String-based network detection (mitigated by iptables)
- F-013: .env credentials on disk (gitignored)
- F-014: validate.py in agent env (intentional)
- F-015: irssi tests directory structure

## Coverage Gaps

| Gap | Subject | Status | Cap |
|---|---|---|---|
| GAP-001 | CRUCIBLE_VIEW screening roots | ✅ RESOLVED | — |
| GAP-002 | requirements/benchmark.md is DRAFT | Open | HOLD |
| GAP-003 | touchstones/README.md has no admitted entries | Open | HOLD |
| GAP-004 | No provenance files in solution/ | Open | HOLD |
| GAP-005 | No samples/ directory | Open | HOLD |
| GAP-006 | Dirty git tree | ✅ RESOLVED | — |
| GAP-007 | Unpinned Docker images | ✅ RESOLVED | — |
| GAP-008 | No execution attestation | Open | HOLD |
| GAP-009 | No pinned scanner databases | Open | HOLD |
| GAP-010 | Scope approval not signed | Open | HOLD |
| GAP-011 | No screening roots | ✅ RESOLVED | — |
| GAP-012 | Rubric judge reliability | ✅ RESOLVED | — |
| GAP-013 | No external trust root signature | Open | HOLD |
| GAP-014 | neardup calibration pending | Open | HOLD |

**Resolved**: 5 of 14 gaps. **Remaining**: 9 gaps capping at HOLD.

**Note**: `requirements/benchmark.md` and `touchstones/README.md` exist but are DRAFT/EMPTY (human-write-only under E9/E12). They need human review and admission of touchstone entries.

## What Works Well

- ✅ All 10 Docker images pinned with `@sha256:` digests
- ✅ Network lockdown via iptables after install phase
- ✅ Verifier runs in a fresh container separate from the agent
- ✅ Agent container is destroyed before grading begins
- ✅ Solution files (fix.patch, poc.bin) not copied into agent environment
- ✅ Negative-weight tests penalize cheating behaviors
- ✅ 4-stage validation covers PoC crash, patch fix, test suite, and GT PoC
- ✅ Weighted scoring provides nuanced rewards beyond binary pass/fail
- ✅ Instruction.md files do not leak solution paths
- ✅ disallowedTools blocks WebFetch/WebSearch during agent runs
- ✅ Clean git tree with tracked SHA
- ✅ ENGRAM Phase 2 complete — 6/6 Bucket-D instruments liveness-proven
- ✅ Trust root and cohort registry configured
- ✅ Four E29 screening roots projected to CRUCIBLE_VIEW
- ✅ 5 pilot runs completed across 4 tasks with full trajectories and verifier output
- ✅ Deterministic 4-stage scoring (pytest_score) produces correct results across all runs

## Disposition Rationale

HOLD is driven by:

1. **No external trust root signature** (GAP-013) — SHIP requires a detached signature from an external trust root. Only a self-attested root exists (E4 caps below CURRENT).
2. **No provenance files** (F-012, GAP-004) — Ground truth has no signed provenance chain.
3. **Structural artifacts incomplete** (GAP-002, GAP-003, GAP-005) — requirements/ and touchstones/ exist but are DRAFT/EMPTY; no samples/ directory.
4. **No execution attestation** (GAP-008) — No signed execution evidence.
5. **Scope approval pending** (GAP-010) — audit/scope.approved not signed.

## Path to SHIP 🟢

### Immediate (removes HOLD blockers)

1. ~~**Add multi-trial rubric judge**~~ — ✅ DONE. 11-trial position-randomized median. F-005 and GAP-012 cleared.
2. **Sign scope approval** — Write SHA-256 of `audit/scope.yaml` to `audit/scope.approved`. Clears GAP-010.
3. **Pin scanner databases** — Record version and digest for each scanner. Clears GAP-009.

### Required for SHIP

4. **External trust root** — Configure an externally verifiable signer identity (not self-attested). Produce a detached signature over the audit manifest. Clears GAP-013.
5. **Add provenance files** — Create `provenance.yaml` and `provenance.sig` in each task's `solution/` directory. Clears F-012 and GAP-004.
6. **Add execution attestation** — Sign per-rollout durations and budget envelope. Clears GAP-008.
7. **Finalize requirements/ and touchstones/** — Human review of benchmark.md (DRAFT→approved). Admit touchstone entries. Clears GAP-002 and GAP-003.
8. **Calibrate neardup floor** — Run calibration set and record floor recall. Clears GAP-014.

### Optional (quality improvements)

9. Replace placeholder PoCs with real upstream crash inputs (hdf5, pcapplusplus)
10. Replace `shell=True` with argument lists in verifier scripts
11. Create `samples/` curated subset

## Artifacts

- `audit/results/scan.yaml` — Full scanner output
- `audit/scope.yaml` — Project scope and surfaces
- `audit/findings.yaml` — Structured findings (15 findings, 14 coverage gaps)
- `audit/evidence.yaml` — Grounded evidence (22 checks)
- `audit/crucible.py` — Typer CLI for running scans
- `audit/scanners/` — Docker, task, and runner scanner modules
- `audit/fixtures/` — Negative control test data
- `memory/crucible_view.yaml` — ENGRAM CRUCIBLE-facing projection (screening roots populated)
- `memory/roots.yaml` — Trust roots and screening roots
- `memory/cohorts/snapshot_001.yaml` — Pinned frontier cohort
