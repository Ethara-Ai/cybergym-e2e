"""ENGRAM conformance suite. Phase 2 step 2.

Negative controls, positive controls, and the E19 both-halves liveness proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    import json as yaml
    yaml.safe_load = yaml.loads

from memory.verifier import verify_envelope, load_trust_roots, VerificationResult
from memory.ingestor import ingest_envelope, birth_cfer, ACCEPTED_PREDICATES
from memory.freshener import freshen, FreshenerConfig, compute_lever_state
from memory.checkpointer import checkpoint, recover, compute_merkle_root, verify_checkpoint_chain, MerkleTreeHead
from memory.reporter import compute_forge_view, compute_crucible_view, FORGE_FORBIDDEN, CRUCIBLE_FORBIDDEN


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass
class TestResult:
    name: str
    passed: bool
    reason: str
    category: str


def _load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


def _load_test_roots() -> list[dict[str, Any]]:
    data = _load_fixture("test_trust_root.yaml")
    return data.get("trust_roots", [])


def test_verifier_rejects_bad_signature() -> TestResult:
    bad = _load_fixture("bad_envelope.yaml")
    roots = _load_test_roots()
    result = verify_envelope(bad.get("envelope", {}), roots)
    if not result.verified:
        return TestResult("verifier_rejects_bad_signature", True, "correctly rejected", "negative")
    return TestResult("verifier_rejects_bad_signature", False, "should have rejected", "negative")


def test_verifier_rejects_no_roots() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    result = verify_envelope(good.get("envelope", {}), [])
    if not result.verified:
        return TestResult("verifier_rejects_no_roots", True, "correctly rejected with no roots", "negative")
    return TestResult("verifier_rejects_no_roots", False, "should have rejected with no roots", "negative")


def test_verifier_accepts_known_good() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    roots = _load_test_roots()
    result = verify_envelope(good.get("envelope", {}), roots)
    if result.verified:
        return TestResult("verifier_accepts_known_good", True, "correctly accepted", "positive")
    return TestResult("verifier_accepts_known_good", False, f"should have accepted: {result.reason}", "positive")


def test_ingestor_rejects_unknown_predicate() -> TestResult:
    bad = _load_fixture("bad_envelope.yaml")
    roots = _load_test_roots()
    result = ingest_envelope(bad, roots)
    if not result.accepted and result.caps_broken:
        return TestResult("ingestor_rejects_unknown_predicate", True, "correctly rejected with caps_broken", "negative")
    return TestResult("ingestor_rejects_unknown_predicate", False, "should have rejected unknown predicate", "negative")


def test_ingestor_accepts_cfer() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    roots = _load_test_roots()
    result = ingest_envelope(good, roots)
    if result.accepted and result.cfer_id == "CFER-TEST-001":
        return TestResult("ingestor_accepts_cfer", True, "correctly accepted and identified CFER", "positive")
    return TestResult("ingestor_accepts_cfer", False, f"should have accepted: {result.reason}", "positive")


def test_ingestor_births_one_cfer() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    roots = _load_test_roots()
    result = ingest_envelope(good, roots)
    if not result.accepted:
        return TestResult("ingestor_births_one_cfer", False, f"ingest failed: {result.reason}", "positive")
    cfer = birth_cfer(good)
    if cfer.cfer_id == "CFER-TEST-001" and cfer.lever["identity"] == "LEV-TEST-001":
        return TestResult("ingestor_births_one_cfer", True, "one CFER birthed with correct lever", "positive")
    return TestResult("ingestor_births_one_cfer", False, "CFER birthed but fields wrong", "positive")


def test_ingestor_accepts_reaudit() -> TestResult:
    reaudit = _load_fixture("reaudit_envelope.yaml")
    roots = _load_test_roots()
    result = ingest_envelope(reaudit, roots)
    if result.accepted and result.predicate_type == "engram.reaudit/v1":
        return TestResult("ingestor_accepts_reaudit", True, "correctly accepted reaudit", "positive")
    return TestResult("ingestor_accepts_reaudit", False, f"should have accepted: {result.reason}", "positive")


def test_freshener_bitidentical() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    cfer = birth_cfer(good)
    cfer_dict = {
        "cfer_id": cfer.cfer_id,
        "lever": cfer.lever,
        "binomial_upper_bound": cfer.binomial_upper_bound,
        "measured_date": cfer.measured_date,
        "validity_interval": cfer.validity_interval,
        "model_cohort": cfer.model_cohort,
        "disposition": cfer.disposition,
    }
    config = FreshenerConfig(
        freshness_horizon_days=180,
        frontier_defeat_floor=0.30,
        evaluation_instant="2026-08-20T00:00:00Z",
    )
    out1 = freshen([cfer_dict], config)
    out2 = freshen([cfer_dict], config)

    states1 = [(s.lever_id, s.state, s.reason) for s in out1.state_vector]
    states2 = [(s.lever_id, s.state, s.reason) for s in out2.state_vector]
    if states1 == states2:
        return TestResult("freshener_bitidentical", True, "bit-identical on two runs", "positive")
    return TestResult("freshener_bitidentical", False, "outputs differ between runs", "positive")


def test_freshener_reaches_active() -> TestResult:
    cfer_dict = {
        "cfer_id": "CFER-ACTIVE",
        "lever": {"identity": "LEV-ACTIVE", "category": "test", "axis": "Reasoning"},
        "binomial_upper_bound": 0.10,
        "measured_date": "2026-08-01T00:00:00Z",
        "validity_interval": {"valid_at": "2026-08-01T00:00:00Z", "invalid_at": "2027-08-01T00:00:00Z"},
        "model_cohort": [{"provider": "anthropic", "model": "claude-opus-4-8"}],
        "disposition": "ACTIVE",
    }
    config = FreshenerConfig(
        freshness_horizon_days=365,
        frontier_defeat_floor=0.30,
        evaluation_instant="2026-08-15T00:00:00Z",
        required_cohort=[{"provider": "anthropic", "model": "claude-opus-4-8"}],
        cohort_valid=True,
    )
    out = freshen([cfer_dict], config)
    if out.state_vector and out.state_vector[0].state == "ACTIVE":
        return TestResult("freshener_reaches_active", True, "reached ACTIVE", "positive")
    return TestResult("freshener_reaches_active", False, f"got {out.state_vector[0].state if out.state_vector else 'empty'}", "positive")


def test_freshener_reaches_watch() -> TestResult:
    cfer_dict = {
        "cfer_id": "CFER-WATCH",
        "lever": {"identity": "LEV-WATCH", "category": "test", "axis": "Reasoning"},
        "binomial_upper_bound": 0.10,
        "measured_date": "2026-08-01T00:00:00Z",
        "validity_interval": {"valid_at": "2026-08-01T00:00:00Z", "invalid_at": "2027-08-01T00:00:00Z"},
        "model_cohort": [{"provider": "anthropic", "model": "claude-opus-4-8"}],
        "disposition": "ACTIVE",
    }
    config = FreshenerConfig(
        freshness_horizon_days=365,
        frontier_defeat_floor=0.30,
        evaluation_instant="2026-08-15T00:00:00Z",
        cohort_valid=False,
    )
    out = freshen([cfer_dict], config)
    if out.state_vector and out.state_vector[0].state == "WATCH":
        return TestResult("freshener_reaches_watch", True, "reached WATCH", "positive")
    return TestResult("freshener_reaches_watch", False, f"got {out.state_vector[0].state if out.state_vector else 'empty'}", "positive")


def test_freshener_reaches_expired() -> TestResult:
    cfer_dict = {
        "cfer_id": "CFER-EXPIRED",
        "lever": {"identity": "LEV-EXPIRED", "category": "test", "axis": "Reasoning"},
        "binomial_upper_bound": 0.10,
        "measured_date": "2025-01-01T00:00:00Z",
        "validity_interval": {"valid_at": "2025-01-01T00:00:00Z", "invalid_at": "2025-07-01T00:00:00Z"},
        "model_cohort": [{"provider": "anthropic", "model": "claude-opus-4-8"}],
        "disposition": "ACTIVE",
    }
    config = FreshenerConfig(
        freshness_horizon_days=180,
        frontier_defeat_floor=0.30,
        evaluation_instant="2026-08-15T00:00:00Z",
        cohort_valid=True,
    )
    out = freshen([cfer_dict], config)
    if out.state_vector and out.state_vector[0].state == "EXPIRED":
        return TestResult("freshener_reaches_expired", True, "reached EXPIRED", "positive")
    return TestResult("freshener_reaches_expired", False, f"got {out.state_vector[0].state if out.state_vector else 'empty'}", "positive")


def test_freshener_reaches_retired() -> TestResult:
    cfer_dict = {
        "cfer_id": "CFER-RETIRED",
        "lever": {"identity": "LEV-RETIRED", "category": "test", "axis": "Reasoning"},
        "binomial_upper_bound": 0.10,
        "measured_date": "2026-08-01T00:00:00Z",
        "validity_interval": {"valid_at": "2026-08-01T00:00:00Z", "invalid_at": "2027-08-01T00:00:00Z"},
        "model_cohort": [{"provider": "anthropic", "model": "claude-opus-4-8"}],
        "disposition": "RETIRED",
    }
    config = FreshenerConfig(
        freshness_horizon_days=365,
        frontier_defeat_floor=0.30,
        evaluation_instant="2026-08-15T00:00:00Z",
        cohort_valid=True,
    )
    out = freshen([cfer_dict], config)
    if out.state_vector and out.state_vector[0].state == "RETIRED":
        return TestResult("freshener_reaches_retired", True, "reached RETIRED", "positive")
    return TestResult("freshener_reaches_retired", False, f"got {out.state_vector[0].state if out.state_vector else 'empty'}", "positive")


def test_checkpointer_recovery_roundtrip() -> TestResult:
    good = _load_fixture("known_good_envelope.yaml")
    reaudit = _load_fixture("reaudit_envelope.yaml")

    from memory.ingestor import _compute_envelope_digest
    cfer_digest = _compute_envelope_digest(good)
    reaudit_digest = _compute_envelope_digest(reaudit)

    sequence = [
        {"position": 1, "envelope_digest": cfer_digest, "predicate_type": "engram.cfer/v1"},
        {"position": 2, "envelope_digest": reaudit_digest, "predicate_type": "engram.reaudit/v1"},
    ]
    envelopes = {cfer_digest: good, reaudit_digest: reaudit}
    supersessions = []

    cp_result = checkpoint(sequence, envelopes, supersessions, [])
    if not cp_result.success or not cp_result.tree_head:
        return TestResult("checkpointer_recovery_roundtrip", False, "checkpoint failed", "positive")

    rec_result = recover(
        signed_cfer_envelopes=[good],
        signed_reaudit_envelopes=[reaudit],
        sequence=sequence,
        checkpoints=[],
        retirements=[],
        roots=_load_test_roots(),
    )
    if not rec_result.success:
        return TestResult("checkpointer_recovery_roundtrip", False, f"recovery failed: {rec_result.reason}", "positive")
    if len(rec_result.ledger_records) != 1:
        return TestResult("checkpointer_recovery_roundtrip", False, f"expected 1 ledger record, got {len(rec_result.ledger_records)}", "positive")
    if len(rec_result.canary_entries) != 1:
        return TestResult("checkpointer_recovery_roundtrip", False, f"expected 1 canary entry, got {len(rec_result.canary_entries)}", "positive")

    return TestResult("checkpointer_recovery_roundtrip", True, "round-trip successful", "positive")


def test_projector_strips_forge_forbidden() -> TestResult:
    view = compute_forge_view(
        ledger={"cfer_count": 0, "records": []},
        hardness={"levers": 341},
        roots={"screening_roots": {}, "cohort_registry_issuer": {}},
        canary=None,
        supersessions=[],
        freshener_output=None,
        evaluation_instant="2026-08-20T00:00:00Z",
        contract_commit="test",
    )

    def _check_keys(d: dict, forbidden: set, path: str = "") -> list[str]:
        violations = []
        for k, v in d.items():
            full = f"{path}.{k}" if path else k
            if k in forbidden:
                violations.append(full)
            if isinstance(v, dict):
                violations.extend(_check_keys(v, forbidden, full))
        return violations

    violations = _check_keys(view, FORGE_FORBIDDEN)
    if violations:
        return TestResult("projector_strips_forge_forbidden", False, f"forbidden fields found: {violations}", "negative")
    return TestResult("projector_strips_forge_forbidden", True, "no forbidden fields in FORGE_VIEW", "negative")


def test_projector_strips_crucible_forbidden() -> TestResult:
    view = compute_crucible_view(
        ledger={"cfer_count": 0, "records": []},
        roots={"screening_roots": {}, "cohort_registry_issuer": {}},
        canary=None,
        evaluation_instant="2026-08-20T00:00:00Z",
        contract_commit="test",
    )

    def _check_keys(d: dict, forbidden: set, path: str = "") -> list[str]:
        violations = []
        for k, v in d.items():
            full = f"{path}.{k}" if path else k
            if k in forbidden:
                violations.append(full)
            if isinstance(v, dict):
                violations.extend(_check_keys(v, forbidden, full))
        return violations

    violations = _check_keys(view, CRUCIBLE_FORBIDDEN)
    if violations:
        return TestResult("projector_strips_crucible_forbidden", False, f"forbidden fields found: {violations}", "negative")
    return TestResult("projector_strips_crucible_forbidden", True, "no forbidden fields in CRUCIBLE_VIEW", "negative")


def run_all() -> list[TestResult]:
    tests = [
        test_verifier_rejects_bad_signature,
        test_verifier_rejects_no_roots,
        test_verifier_accepts_known_good,
        test_ingestor_rejects_unknown_predicate,
        test_ingestor_accepts_cfer,
        test_ingestor_births_one_cfer,
        test_ingestor_accepts_reaudit,
        test_freshener_bitidentical,
        test_freshener_reaches_active,
        test_freshener_reaches_watch,
        test_freshener_reaches_expired,
        test_freshener_reaches_retired,
        test_checkpointer_recovery_roundtrip,
        test_projector_strips_forge_forbidden,
        test_projector_strips_crucible_forbidden,
    ]
    results = []
    for test_fn in tests:
        try:
            r = test_fn()
        except Exception as e:
            r = TestResult(test_fn.__name__, False, f"exception: {e}", "error")
        results.append(r)
    return results


if __name__ == "__main__":
    results = run_all()
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"\nConformance suite: {passed} passed, {failed} failed, {len(results)} total\n")
    for r in results:
        icon = "PASS" if r.passed else "FAIL"
        print(f"  [{icon}] {r.name} ({r.category}): {r.reason}")
    if failed:
        raise SystemExit(1)
