# Assay

CRUCIBLE scope-approval gate. Recomputes the live SHA-256 of `audit/scope.yaml`
and refuses every mutation until it matches `audit/scope.approved`. Fails closed
on mismatch or absence.

This is the scope gate, not the verify gate. `crucible verify` is the command
whose success means the full audit gate passed.

## Usage

Invoke before any phase that writes audit harness files. The gate must pass
before Phase 1 scaffolding proceeds.

## Procedure

1. Compute `shasum -a 256 audit/scope.yaml`.
2. Read `audit/scope.approved`.
3. If `audit/scope.approved` is absent, halt and report. No writes proceed.
4. If the digest does not match, halt and report the mismatch. No writes proceed.
5. If matched, report approval is current and proceed.

## Approval instruction

To approve the current scope:

```bash
shasum -a 256 audit/scope.yaml | awk '{print $1}' > audit/scope.approved
```

Review `audit/scope.yaml` before approving. Re-scoping, widening ignore lists,
or flipping a capability invalidates prior approval and requires a new sign-off.

## Authority

Defers to `trinity/CRUCIBLE.md` Phase 0.5. This skill never restates the contract.
