# CRUCIBLE Audit Fixtures

Negative-control test data for the CRUCIBLE audit harness. Each fixture
is designed to trigger (or not trigger) a specific scanner check under
known conditions.

## Files

| File | Purpose | Expected scanner behavior |
|------|---------|--------------------------|
| `null_poc.bin` | 1 null byte | TaskScanner `null_poc` check fires |
| `empty_patch.diff` | 0-byte patch | TaskScanner `empty_patch` check fires |
| `sample_task.toml` | Well-formed task.toml v1.1 | TaskScanner schema validation passes cleanly |

## Usage

These fixtures are consumed by the audit harness's negative-control tests
(Phase 1 step 12, Phase 2 step 1). They verify that scanners correctly
detect known defects and do not false-positive on clean inputs.

```python
from pathlib import Path
from audit.scanners.task_scanner import TaskScanner

fixtures = Path("audit/fixtures")
null_poc = fixtures / "null_poc.bin"
assert null_poc.read_bytes() == b"\x00"
```

## Integrity

These fixtures must not be modified without re-running the full negative-control
suite. Changes to fixtures re-trigger the Phase 0.5 sign-off gate because they
are bound into the scope digest through `audit/scope.yaml`.
