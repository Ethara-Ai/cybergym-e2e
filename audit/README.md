# CRUCIBLE Audit Harness — CyberGym-E2E

Adversarial audit harness for the CyberGym-E2E benchmark, built under the
[CRUCIBLE contract](../trinity/CRUCIBLE.md) from the ENGRAM/FORGE/CRUCIBLE
trinity.

## Disposition

**HOLD** — 15 findings (3 HIGH, 8 MEDIUM, 4 LOW) with 12 coverage gaps.

## Structure

```
audit/
├── crucible.py            # Typer CLI entry point (scan, verify, report, status)
├── scope.yaml             # Phase 0 scope with project surface inventory
├── findings.yaml          # F-001 through F-015 with severity and evidence refs
├── evidence.yaml          # CHK-001 through CHK-018 raw check evidence
├── capabilities.yaml      # Optional capability opt-in block
├── provenance.yaml        # Provenance manifest
├── budget.yaml            # Budget ledger view
├── review.md              # Phase 2 model review instructions
├── TODO.md                # Generated augmentation backlog (DO NOT HAND-EDIT)
├── progress.yaml          # Derived per-phase progress cache
├── iteration.log.md       # Phase 0 differential iteration log
├── feedback.yaml          # Append-only human feedback ledger
├── README.md              # This file
├── scanners/              # Scanner modules
│   ├── __init__.py
│   ├── docker_scanner.py  # Docker image pinning, base image consistency
│   ├── task_scanner.py    # task.toml schema, PoC files, patch files
│   └── runner_scanner.py  # run_harbor.py shell injection, network policy
├── fixtures/              # Negative-control test data
│   ├── README.md
│   ├── null_poc.bin       # 1-byte null PoC for null detection test
│   ├── empty_patch.diff   # 0-byte patch for empty patch test
│   └── sample_task.toml   # Well-formed task.toml for schema validation
├── lineage/               # Preserved prior-run scope and verdict snapshots
│   ├── README.md
│   ├── scope.1c388d4.yaml
│   └── VERDICT.1c388d4.md
└── results/               # Volatile raw scan transcripts (gitignored)
```

## CLI Usage

```bash
# Show current audit status
python -m audit.crucible status

# Run all scanners against the project
python -m audit.crucible scan
python -m audit.crucible scan --verbose

# Verify findings against evidence (exit 0 = gate passed)
python -m audit.crucible verify \
    --findings audit/findings.yaml \
    --context audit/evidence.yaml

# Generate VERDICT.md from findings
python -m audit.crucible report
```

## Scanners

### DockerScanner (`scanners/docker_scanner.py`)
- **Image pinning**: checks every FROM line for `@sha256:` digest pins
- **Namespace trust**: flags third-party Docker Hub namespaces (e.g. `n132/`)
- **USER directive**: verifies containers don't run as root
- **Cross-task consistency**: detects wrong base image tags (e.g. espeak using harfbuzz)

### TaskScanner (`scanners/task_scanner.py`)
- **Schema validation**: checks required fields in task.toml v1.1
- **PoC integrity**: detects null-byte placeholders, empty files, GT mismatches
- **Patch integrity**: detects empty patches
- **Name consistency**: flags prefixed or mismatched task names
- **Cross-task duplicates**: finds identical PoC files across tasks

### RunnerScanner (`scanners/runner_scanner.py`)
- **Network policy**: verifies `start_container()` enforces `allow_internet`
- **Shell injection**: detects `subprocess` with `shell=True` + f-string interpolation
- **Rubric judge**: checks for multi-trial aggregation and reliability discipline
- **Credentials**: detects sensitive keys in `.env`
- **Weak detection**: flags string-only network usage detection in test_output.py

## Key Findings

| ID | Severity | Title |
|----|----------|-------|
| F-001 | HIGH | Network policy not enforced by runner |
| F-002 | HIGH | 8/10 Docker images use unpinned mutable tags |
| F-005 | HIGH | Rubric judge lacks reliability discipline |
| F-003 | MEDIUM | espeak-ng uses wrong base image tag |
| F-004 | MEDIUM | Containers run as root with no user isolation |
| F-007 | MEDIUM | Duplicate placeholder PoC across tasks |
| F-012 | MEDIUM | No provenance files for ground truth |

## Front Doors

- **Skill**: `.agents/skills/assay/SKILL.md` — CRUCIBLE gate skill for Claude Code
- **Command**: `.opencode/commands/crucible.md` — CRUCIBLE command for OpenCode

## Authority

This harness defers to `trinity/CRUCIBLE.md`. It never restates the contract.
