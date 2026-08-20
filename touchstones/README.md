# Touchstones

Calibration baselines for CyberGym-E2E benchmark evaluation.

**Status**: ADMITTED — 7 published baselines from arXiv:2606.04460v2 Tables 3–4.

> This directory is human-write-only under ENGRAM invariant E12. No agent
> may create, edit, or remove touchstone entries. ENGRAM tracks additions,
> removals, and relabelling as input deltas.

## What goes here

Touchstones are reference baselines that the benchmark results are measured against.
For CyberGym-E2E, these would include:

### Published baselines from the paper (Tables 3–4)

| Model | Harness | P-O | S1 | S2 | S3 | S4 | Source |
|---|---|---|---|---|---|---|---|
| Opus 4.5 | Claude Code | 82.3 | 24.9 | 21.9 | 19.2 | 7.6 | Table 3, 615 tasks |
| Sonnet 4.5 | Claude Code | 77.4 | 18.1 | 12.1 | 10.6 | 3.4 | Table 3, 615 tasks |
| GPT-5.2-Codex | Codex | 58.5 | 30.2 | 22.0 | 20.7 | 6.5 | Table 3, 615 tasks |
| Gemini 3 Pro | Gemini CLI | 77.6 | 29.6 | 23.6 | 22.6 | 5.0 | Table 3, 615 tasks |
| Opus 4.6 | Claude Code | 84.1 | 39.7 | 39.5 | 37.9 | 15.7 | Table 4, 920 tasks |
| GPT-5.4 | Codex | 87.1 | 67.9 | 66.2 | 65.9 | 22.2 | Table 4, 920 tasks |
| Gemini 3.1 Pro | Gemini CLI | 83.0 | 47.4 | 44.3 | 43.8 | 20.5 | Table 4, 920 tasks |

### Verdict table

Each admitted touchstone carries a pinned provenance record, per-atom screening outcome, and human-assigned label.

| ID | Source | Label | Verdict | Date |
|---|---|---|---|---|
| TS-001 | arXiv:2606.04460v2 Table 3 | Opus 4.5 / Claude Code / 615 tasks | Admitted | 2026-08-20 |
| TS-002 | arXiv:2606.04460v2 Table 3 | Sonnet 4.5 / Claude Code / 615 tasks | Admitted | 2026-08-20 |
| TS-003 | arXiv:2606.04460v2 Table 3 | GPT-5.2-Codex / Codex / 615 tasks | Admitted | 2026-08-20 |
| TS-004 | arXiv:2606.04460v2 Table 3 | Gemini 3 Pro / Gemini CLI / 615 tasks | Admitted | 2026-08-20 |
| TS-005 | arXiv:2606.04460v2 Table 4 | Opus 4.6 / Claude Code / 920 tasks | Admitted | 2026-08-20 |
| TS-006 | arXiv:2606.04460v2 Table 4 | GPT-5.4 / Codex / 920 tasks | Admitted | 2026-08-20 |
| TS-007 | arXiv:2606.04460v2 Table 4 | Gemini 3.1 Pro / Gemini CLI / 920 tasks | Admitted | 2026-08-20 |
