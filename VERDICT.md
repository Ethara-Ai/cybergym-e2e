# VERDICT: CyberGym-E2E Benchmark Audit

**Disposition: HOLD** 🟡

**Project**: CyberGym-E2E, a benchmark harness for evaluating AI agents on real-world vulnerability discovery and patching.

**Date**: 2026-08-19. **Run kind**: POST-FIX UPDATE.

**Scan engine**: CRUCIBLE scanners v1.0.0 (docker_scanner, task_scanner, runner_scanner).

## Executive Summary

CyberGym-E2E is a well-structured vulnerability discovery benchmark with sound task design and correct verifier isolation (agent container destroyed before grading). Three fixes were applied this cycle:

1. **espeak-ng validate.py** restored (was missing, blocking docker build)
2. **Placeholder PoC warnings** added for hdf5 and pcapplusplus (documented, upstream data needed)
3. **Image pinning script** created at `scripts/pin_docker_images.sh` (opt-in, requires Docker)

Disposition remains HOLD due to unpinned Docker images (reproducibility risk) and placeholder PoC data (2 tasks produce unreliable S4 scores).

## Scanner Results

75 raw findings deduplicated to 18 unique patterns across 3 scanners.

| Category | Count | Status |
|---|---|---|
| Intentional (root containers) | 1 | By design — agents need root for ASan compilation |
| Can't fix simply (network policy) | 1 | Agent needs network for API access + Claude Code install |
| Documented (placeholder PoCs) | 2 | POC_WARNING.md added, upstream data needed |
| Quality concerns | 3 | Rubric reliability, shell=True in verifiers, .env credentials |
| Actionable (image pinning) | 9 | `scripts/pin_docker_images.sh --apply` when Docker available |
| Cosmetic | 2 | irssi name prefix, irssi allow_internet moot |

## Fixes Applied This Cycle

### Fix 1: espeak-ng missing validate.py ✅

espeak-ng's `tests/` directory was missing `validate.py`, which all 9 other tasks have (identical sha256: `3a196f91...`). espeak-ng's Dockerfile line 24 runs `COPY validate.py /scripts/validate.py`, so **docker build was broken** for this task. Copied from curl. Verified identical hash.

### Fix 2: Placeholder PoC documentation ✅

hdf5 and pcapplusplus both have 20-byte all-zeros `poc.bin` files (identical sha256: `de47c9b2...`). These are placeholders, not real crash-triggering inputs. `POC_WARNING.md` added to each task's `solution/` directory with upstream download instructions. Original files left intact — pipeline handles them gracefully (S4 reports `failed`).

### Fix 3: Image pinning script ✅

Created `scripts/pin_docker_images.sh` to resolve sha256 digests for 8 unpinned Docker images. Supports `--dry-run` (list only) and `--apply` (update Dockerfiles in place). No Dockerfiles modified yet — requires Docker to pull images.

## Remaining Findings

### HIGH — Unpinned Docker images (F-002)

8 of 10 Dockerfiles use mutable tags from `n132/arvo:*` or `cybergym/e2e:*`. These tags can be changed or deleted by the account holder at any time, breaking reproducibility.

**Mitigation**: Run `scripts/pin_docker_images.sh --apply` when Docker is available.

### HIGH — Network policy not enforced (F-001)

`start_container()` never applies `--network=none`. However, adding it would break the pipeline:
- `install_claude_code()` needs network for curl, apt-get, npm
- Claude Code needs network to reach the LLM API at `host.docker.internal:3456`

**Current mitigations**: Prompt instruction ("Do NOT use network access"), `disallowedTools` blocks WebFetch/WebSearch, `uses_network` negative-weight test (-5 penalty).

### MEDIUM — Rubric judge reliability (F-005 quality concern)

The rubric judge is an intentional part of this harness's Harbor output format (documented in run_harbor.py docstring). The quality concern is that it makes a single LLM call with no multi-trial aggregation, no position randomization, and silently defaults to 0.0 on failure — while contributing 50% of `avg_score`.

### MEDIUM — shell=True in verifier scripts

`validate.py` and `test_output.py` use `subprocess.run(..., shell=True)` with f-string interpolation. These run inside Docker containers so the risk is contained, but a crafted filename could inject shell commands during grading.

### MEDIUM — Placeholder PoCs (F-007, documented)

hdf5 and pcapplusplus have identical 20-byte null-byte PoCs. S4 validation produces unreliable results for these 2 tasks. `POC_WARNING.md` files document the issue and provide upstream download instructions.

## What Works Well

- ✅ Verifier runs in a fresh container separate from the agent
- ✅ Agent container is destroyed before grading begins
- ✅ Solution files (fix.patch, poc.bin) are not copied into agent environment
- ✅ Negative-weight tests penalize cheating behaviors
- ✅ 4-stage validation covers PoC crash, patch fix, test suite, and GT PoC confirmation
- ✅ Weighted scoring provides nuanced rewards beyond binary pass/fail
- ✅ Instruction.md files do not leak solution paths
- ✅ disallowedTools blocks WebFetch/WebSearch during agent runs
- ✅ All 10 tasks now have validate.py (espeak-ng fixed)
- ✅ Rubric scoring is documented as intentional Harbor output
- ✅ Image pinning script ready for when Docker is available

## Disposition Rationale

HOLD is driven by:
1. Unpinned images (F-002) — reproducibility risk, mitigation script ready
2. Placeholder PoCs (F-007) — 2 tasks have unreliable S4 scores, documented
3. Network enforcement gap (F-001) — architectural constraint, mitigated by soft controls

## Path to SHIP

1. Pin Docker images: `./scripts/pin_docker_images.sh --apply`
2. Replace placeholder PoCs with real ones from upstream dataset
3. (Optional) Add multi-trial rubric judge aggregation
4. (Optional) Replace shell=True with argument lists in verifier scripts

## Artifacts

- `audit/results/scan.yaml` — Full scanner output (75 findings, 18 unique patterns)
- `audit/scope.yaml` — Project scope and surfaces
- `audit/findings.yaml` — Structured findings and coverage gaps
- `audit/evidence.yaml` — Grounded evidence from checks
- `audit/crucible.py` — Typer CLI for running scans
- `audit/scanners/` — Docker, task, and runner scanner modules
- `audit/fixtures/` — Negative control test data
- `scripts/pin_docker_images.sh` — Image pinning utility
