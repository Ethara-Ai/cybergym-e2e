# VERDICT: CyberGym-E2E Benchmark Audit

**Disposition: HOLD** 🟡

**Project**: CyberGym-E2E, a benchmark harness for evaluating AI agents on real-world vulnerability discovery and patching.

**Scope**: 10 Harbor-format task bundles, 1 standalone runner (run_harbor.py), verifier test suites, LLM rubric judge, Docker environments, ground-truth solutions, agent trajectories.

**Git SHA**: `1c388d465739ed3d5665bd8e3d0ef1f5f21acf6b` (dirty: run_harbor.py has uncommitted changes)

**Trinity SHA**: `6757043b74cb7f76148983e979e491e7d943b555`

**Date**: 2026-08-18

## Executive Summary

CyberGym-E2E is a well-structured vulnerability discovery benchmark with sound task design and a correct verifier isolation model (agent container destroyed before grading). However, several critical-capable gaps prevent a SHIP disposition. The most significant is that network isolation is declared but never enforced, meaning agents can look up known fixes online. Additionally, 80% of Docker images use unpinned third-party tags, the rubric judge has no reliability discipline despite contributing 50% of the final score, and ground truth lacks any provenance or signature binding.

## Critical Findings

### F-001: Network policy not enforced ⛔

The runner never reads `allow_internet` from task.toml and never applies `--network=none`. All 10 task containers have unrestricted network access. For a benchmark measuring vulnerability discovery capability, this means an agent could trivially look up the answer for any of these known OSS-Fuzz/arvo vulnerabilities. This is the most significant scoring integrity issue.

**Evidence**: run_harbor.py `start_container()` (lines 148-159) does not reference allow_internet or apply any network restriction. Confirmed by grep returning zero matches for "allow_internet", "network.*none", or "--net" in run_harbor.py.

### F-002: 8 of 10 Docker images use unpinned mutable tags ⚠️

Only curl and irssi use `@sha256:` pinned images. The remaining 8 tasks use `n132/arvo:*-fix` or `cybergym/e2e:quickjs` tags from Docker Hub, which can be mutated at any time. The `n132/` namespace is an unverified third-party account.

### F-005: Rubric judge lacks reliability discipline ⚠️

`evaluate_rubric()` makes a single LLM judge call (Claude Sonnet 4) with no position randomization, no perturbation suite, no conformal prediction sets, and no multi-trial aggregation. `rubric_score` contributes 50% of the final reward. On judge failure, rubric_score silently defaults to 0.0.

### F-012: No provenance or signatures for ground truth ⚠️

No solution/ directory contains TRUTH.md, provenance.yaml, provenance.sig, or any binding record. The ground-truth PoC and patch files have no signed chain linking them to the source vulnerability.

## Additional Findings

| ID | Title | Severity | Class |
|---|---|---|---|
| F-003 | espeak-ng uses harfbuzz base image | MEDIUM | Delivery integrity |
| F-004 | All containers run as root | MEDIUM | Container security |
| F-006 | irssi allows internet (inconsistency) | MEDIUM | Task consistency |
| F-007 | pcapplusplus and hdf5 share identical null-byte PoC | MEDIUM | Ground truth |
| F-008 | irssi task name has cybergym-e2e/ prefix | LOW | Delivery conformance |
| F-009 | Negative-weight network check is string-only | MEDIUM | Scoring integrity |
| F-010 | Shell injection via subprocess with shell=True | MEDIUM | Code injection |
| F-011 | Git tree dirty with uncommitted changes | LOW | Provenance |
| F-013 | AWS Bearer Token on disk in .env | MEDIUM | Secret exposure |
| F-014 | validate.py reveals grading logic to agent | LOW | Information disclosure |
| F-015 | irssi tests/ has different structure than others | LOW | Delivery conformance |

## Coverage Gaps (12 total, each caps at HOLD)

GAP-001 through GAP-012 are detailed in `audit/findings.yaml`. Key gaps include: absent crucible_view.yaml, absent requirements, absent touchstones, absent provenance, dirty git tree, unpinned images, absent execution attestation, absent scanner databases, unsigned scope, absent contamination screening roots, and absent rubric judge reliability.

## What Works Well

- ✅ Verifier runs in a fresh container separate from the agent (run_harbor.py line 1049-1051)
- ✅ Agent container is destroyed before grading begins
- ✅ Solution files (fix.patch, poc.bin) are not copied into agent environment
- ✅ Negative-weight tests penalize cheating behaviors (network use, empty patches, GT copying)
- ✅ 4-stage validation covers PoC crash, patch fix, test suite, and GT PoC confirmation
- ✅ Weighted scoring provides nuanced rewards beyond binary pass/fail
- ✅ Instruction.md files do not leak solution paths or ground truth locations
- ✅ disallowedTools setting in Claude Code blocks WebFetch, WebSearch during agent runs
- ✅ Two tasks (curl, irssi) use properly pinned Docker images

## Disposition Rationale

HOLD is driven by:
1. Network isolation gap (F-001) - agents may have access to answers
2. Unpinned images (F-002, GAP-007) - builds are not reproducible
3. Rubric judge unreliability (F-005, GAP-012) - 50% of score is from a single unvalidated judge call
4. Absent provenance (F-012, GAP-004) - ground truth has no signed binding
5. 12 named coverage gaps, each independently capping at HOLD

## Path to SHIP

1. Enforce network policy: read `allow_internet` from task.toml and apply `--network=none` to Docker containers when false
2. Pin all Docker images with `@sha256:` digests
3. Apply rubric judge reliability discipline (multi-trial, randomization, perturbation)
4. Add provenance.yaml with signed bindings to all solution/ directories
5. Commit all harness changes and clean the git tree
6. Populate requirements/ and touchstones/ for the audit framework
7. Create crucible_view.yaml for projected values
8. Replace null-byte placeholder PoCs with genuine exploit inputs
9. Add USER directives to Dockerfiles for least-privilege agent execution
10. Replace shell=True subprocess calls with argument lists

## Approval Gate

Scope approval is pending. To approve:

```bash
shasum -a 256 audit/scope.yaml
# Expected: 8fe3aad51feba33a607430efc6b0bec78735b66f0d906e15e4fbf9e56e146529
echo '8fe3aad51feba33a607430efc6b0bec78735b66f0d906e15e4fbf9e56e146529' > audit/scope.approved
```

## Artifacts

- `audit/scope.yaml` - project scope and surfaces
- `audit/capabilities.yaml` - capability opt-in block
- `audit/evidence.yaml` - grounded evidence from checks
- `audit/findings.yaml` - structured findings and coverage gaps
- `audit/review.md` - Phase 2 model prompt
- `audit/TODO.md` - generated augmentation backlog
