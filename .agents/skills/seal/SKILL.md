# Seal

Recomputes the SHA-256 of `memory/scope.yaml` and compares it against `memory/approval`. Refuses any mutation to governed files until the digests match. Fails closed on mismatch or absence.

## Usage

Invoke before any phase that writes to governed memory instruments.

## Procedure

1. Compute `shasum -a 256 memory/scope.yaml`.
2. Read `memory/approval`.
3. If absent or mismatched, halt and report. No writes proceed.
4. If matched, report approval is current.

## Authority

Defers to `trinity/ENGRAM.md` Phase 0.5. This skill never restates the contract.
