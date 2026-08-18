# CRUCIBLE Audit Backlog

GENERATED SECTION. DO NOT HAND-EDIT. Source of truth: audit/capabilities.yaml

## Capability Gaps

### contamination (state: default)
- **Missing**: corpus-level contamination screening beyond the mandatory G-CON floor
- **Augment**: audit/ needs a contamination screening module that checks task bundles against known benchmark datasets (SWE-bench, Terminal-Bench, LiveCodeBench, etc.)
- **Consequence**: contamination screening cannot verify that tasks are not drawn from public benchmark sets

### autonomous_gates (state: default)
- **Missing**: out-of-band approver identity, gate_approver role, model-lineage diversity, calibration policy
- **Augment**: audit/capabilities.yaml needs full byte-level definition of autonomous approval chain
- **Consequence**: Phase 0.5 gate requires manual human approval for every invocation

### dynamic_revalidation (state: default)
- **Missing**: pinned sandbox image digest, network policy specification, exploit definitions
- **Augment**: audit/ needs a sandbox configuration module with hermetic container specs
- **Consequence**: findings requiring dynamic revalidation cap at HOLD

## Coverage Gaps

| ID | Description | Cap |
|---|---|---|
| GAP-001 | No crucible_view.yaml available, cannot bind projected values | HOLD |
| GAP-002 | No requirements files to bind against | HOLD |
| GAP-003 | No touchstones for reconciliation | HOLD |
| GAP-004 | No provenance files in ground truth (no TRUTH.md, provenance.yaml, or signatures) | HOLD |
| GAP-005 | No samples/ directory for curated subset reconciliation | HOLD |
| GAP-006 | Git tree is dirty with uncommitted changes to run_harbor.py | HOLD |
| GAP-007 | 8 of 10 Docker images lack sha256 digest pins | HOLD |
| GAP-008 | No execution attestation available | HOLD |
| GAP-009 | No scanner databases pinned for automated scanning | HOLD |
| GAP-010 | Scope approval not yet human-signed | HOLD |
| GAP-011 | No contamination screening roots available from ENGRAM | HOLD |
| GAP-012 | Rubric judge reliability discipline not applied | HOLD |

## Actionable Items (Priority Order)

1. **CRITICAL**: Enforce network policy in run_harbor.py by reading allow_internet from task.toml and applying --network=none for tasks that disallow internet (F-001)
2. **HIGH**: Pin all Docker images with @sha256: digests (F-002)
3. **HIGH**: Implement rubric judge reliability discipline with multi-trial aggregation, position randomization, and perturbation testing (F-005)
4. **MEDIUM**: Add USER directive to all Dockerfiles to run agents as non-root (F-004)
5. **MEDIUM**: Replace placeholder null-byte PoCs for pcapplusplus and hdf5 with genuine exploit inputs (F-007)
6. **MEDIUM**: Fix irssi task.toml name to remove "cybergym-e2e/" prefix (F-008)
7. **MEDIUM**: Add provenance.yaml and signed bindings to all solution/ directories (F-012)
8. **LOW**: Commit run_harbor.py changes and clean the git tree (F-011)
9. **LOW**: Remove or rotate the AWS Bearer Token in .env (F-013)
10. **LOW**: Replace shell=True subprocess calls with proper argument lists (F-010)
