# crucible

Run the CRUCIBLE audit instrument. Reads `trinity/CRUCIBLE.md` and executes
the main branch (Phase 0 through Phase 2) against the current project state.
Resamples requirements, touchstones, and dataset on every invocation.

## Quick commands

```bash
# Status check
python -m audit.crucible status

# Full scan
python -m audit.crucible scan --verbose

# Verify gate
python -m audit.crucible verify --findings audit/findings.yaml --context audit/evidence.yaml

# Generate verdict
python -m audit.crucible report
```

## Authority

Defers to `trinity/CRUCIBLE.md`. This command never restates the contract.
