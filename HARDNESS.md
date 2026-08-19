# HARDNESS

GENERATED SECTION. DO NOT HAND-EDIT. Regenerated from `memory/hardness.yaml` by ENGRAM. Edit the ledger, never this file.

**Ledger digest**: `858228976a625ac4a73c3d5e4a3cf64d214d1aff228e185fb50101bb4a6ad16c`. **Source catalog**: `trinity/research/HARDNESS.md` at `24874eea076fe902970fa788b77cfad4094a8864ab9d671d659631b6a14d6874`. **Generated**: 2026-08-18. **Authority**: authored catalog. **Difficulty evidence**: none.

## Standing

The catalog holds 341 levers across 45 categories on 3 axes, plus 47 archetypes. Every row is `CANDIDATE` and every lever state is `EXPIRED`. That is the correct closed state for this project, not a defect: invariant E1 admits only an external signed pilot outcome over frozen task bytes as difficulty evidence, and no CFER exists here. Invariant E23 separates the two ideas cleanly, holding that the hardness contract is authored and published evidence and never difficulty evidence, so a lever may be catalogued in full while remaining unmeasured.

A row reaches `ANCHORED` only when a verified signed CFER binds it. A lever reaches `ACTIVE` only on fresh measured failure evidence against a pinned cohort. No cohort is pinned in this project, which `memory/scope.yaml` records as `ledger_shape.classification: UNPINNED` under GAP-E-007, so under invariant E2 there is no frontier to measure against and no lever can be promoted today.

| Quantity | Value |
|---|---|
| Levers catalogued | 341 |
| Categories | 45 |
| Axes | 3 |
| Archetypes catalogued | 47 |
| Archetypes selectable by FORGE | 10 |
| Rows `CANDIDATE` | 341 |
| Rows `ANCHORED` | 0 |
| Levers `ACTIVE` | 0 |

## Axes

At the Hard tier and above, at least one lever from each axis is required.

| Axis | Categories | Members |
|---|---|---|
| Perception | 12 | LV, FG, OCR, AUD, SG, CHT, MLT, SPF, TDS, DOC, DGM, HWR |
| Reasoning | 16 | CMC, ADV, NUM, TMP, MEM, DEC, RSN, CAU, TOM, SCI, PRF, PLN, CAL, HAL, SYN, MAD |
| Agentic | 17 | LH, FS, GUI, WEB, TOOL, INJ, FMT, COD, SQL, DSA, RAG, IFC, SLF, EMB, AGT, PRV, SEC |

## Tier floors

| Tier | Min levers | Min categories | Additional |
|---|---|---|---|
| Baseline | 6 | 5 | blocking on every task |
| Hard | 9 | 7 | at least one from each of ADV, INJ, CMC |
| Frontier-defeat | 12 | 9 | spans at least 3 modality types |

A maximum of 3 levers per category may contribute to the distinct-category count. Counts are floors and design targets only; they never raise a disposition, and a reader may raise a projected threshold but never lower it.

## Open contract defect

The catalog defines 47 archetypes. `trinity/FORGE.md` Hardness rule 1 binds a closed selection set of 10, namely AR1 through AR10. The remaining 37 archetypes, AR11 through AR47, are catalogued but cannot be chosen as a primary archetype by any lawful FORGE run. FORGE Phase 0 coverage also requires each eligible archetype to be covered at least once with no archetype claiming more than one fifth of slots, a rule written against the ten-member set. Recorded as GAP-E-012. Resolving it means either widening the FORGE vocabulary or marking the extra archetypes non-eligible in the catalog; ENGRAM cannot decide that alone because the selection set lives in a contract it does not own.

## Categories

| Category | Axis | Levers |
|---|---|---|
| ADV | reasoning | 9 |
| AGT | agentic | 7 |
| AUD | perception | 10 |
| CAL | reasoning | 7 |
| CAU | reasoning | 7 |
| CHT | perception | 7 |
| CMC | reasoning | 8 |
| COD | agentic | 6 |
| DEC | reasoning | 7 |
| DGM | perception | 7 |
| DOC | perception | 7 |
| DSA | agentic | 7 |
| EMB | agentic | 7 |
| FG | perception | 10 |
| FMT | agentic | 10 |
| FS | agentic | 10 |
| GUI | agentic | 7 |
| HAL | reasoning | 7 |
| HWR | perception | 7 |
| IFC | agentic | 7 |
| INJ | agentic | 10 |
| LH | agentic | 10 |
| LV | perception | 10 |
| MAD | reasoning | 7 |
| MEM | reasoning | 7 |
| MLT | perception | 6 |
| NUM | reasoning | 7 |
| OCR | perception | 10 |
| PLN | reasoning | 7 |
| PRF | reasoning | 7 |
| PRV | agentic | 7 |
| RAG | agentic | 7 |
| RSN | reasoning | 7 |
| SCI | reasoning | 7 |
| SEC | agentic | 7 |
| SG | perception | 10 |
| SLF | agentic | 7 |
| SPF | perception | 5 |
| SQL | agentic | 7 |
| SYN | reasoning | 7 |
| TDS | perception | 7 |
| TMP | reasoning | 7 |
| TOM | reasoning | 7 |
| TOOL | agentic | 7 |
| WEB | agentic | 7 |

Full row detail, one entry per lever with its statement, row state and CFER binding, lives in `memory/hardness.yaml`. It is not reproduced here because this report is a standing summary and the ledger is the authority.

*Instrument: ENGRAM | Harness: `memory/` | Contract: `trinity/ENGRAM.md`*
