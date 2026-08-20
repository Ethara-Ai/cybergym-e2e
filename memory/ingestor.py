"""ENGRAM CFER ingestor. Phase 1 step 4.

Parses attestation envelopes from memory/proofs/*.yaml, dispatches by
predicate type, verifies signer role, and births one CFER per isolated lever.
E3: measured CFERs are never deleted or overwritten.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    import json as yaml
    yaml.safe_load = yaml.loads

from memory.verifier import verify_envelope, load_trust_roots, VerificationResult


ACCEPTED_PREDICATES = {"engram.cfer/v1", "engram.reaudit/v1"}

CFER_REQUIRED_FIELDS = [
    "cfer_id", "signer_identity", "signer_role", "solver_registry_digest",
    "task_hash", "checker_hash", "transcript_hash", "environment_hash",
    "pilot_outcome", "statistical_rule", "binomial_upper_bound",
    "model_cohort", "measured_date", "signed_at", "validity_interval",
    "disposition", "lever",
]

REAUDIT_REQUIRED_FIELDS = [
    "task_hash", "signer_identity", "signer_role",
    "screening_roots", "screening_outcomes", "retraction_standing",
    "screening_measured_at", "screening_interval_days", "screening_expires_at",
]

VALID_RETRACTION_STANDINGS = {"retained", "withdrawn", "retracted"}


@dataclass
class IngestResult:
    accepted: bool
    predicate_type: str | None = None
    cfer_id: str | None = None
    task_hash: str | None = None
    lever_identity: str | None = None
    reason: str = ""
    caps_broken: bool = False
    verification: VerificationResult | None = None


@dataclass
class CFER:
    cfer_id: str
    signer_identity: str
    signer_role: str
    solver_registry_digest: str
    task_hash: str
    checker_hash: str
    transcript_hash: str
    environment_hash: str
    pilot_outcome: dict[str, Any]
    statistical_rule: dict[str, Any]
    binomial_upper_bound: float
    model_cohort: list[dict[str, Any]]
    measured_date: str
    signed_at: str
    ingested_at: str
    validity_interval: dict[str, str]
    supersession: dict[str, Any] | None
    disposition: str
    lever: dict[str, Any]
    envelope_digest: str
    source_path: str


@dataclass
class ReauditResult:
    task_hash: str
    screening_roots: dict[str, Any]
    screening_outcomes: dict[str, Any]
    retraction_standing: str
    screening_measured_at: str
    screening_interval_days: int
    screening_expires_at: str
    signer_identity: str
    signer_role: str
    envelope_digest: str
    source_path: str


def _compute_envelope_digest(data: dict[str, Any]) -> str:
    canonical = yaml.dump(data, sort_keys=True, default_flow_style=False) if hasattr(yaml, 'dump') else str(data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_cfer_fields(predicate: dict[str, Any]) -> str | None:
    for f in CFER_REQUIRED_FIELDS:
        if f not in predicate or predicate[f] is None:
            return f"missing required field: {f}"
    vi = predicate.get("validity_interval", {})
    if "valid_at" not in vi or "invalid_at" not in vi:
        return "validity_interval missing valid_at or invalid_at"
    lever = predicate.get("lever", {})
    if "identity" not in lever:
        return "lever missing identity"
    return None


def _validate_reaudit_fields(predicate: dict[str, Any]) -> str | None:
    for f in REAUDIT_REQUIRED_FIELDS:
        if f not in predicate or predicate[f] is None:
            return f"missing required field: {f}"
    rs = predicate.get("retraction_standing")
    if rs not in VALID_RETRACTION_STANDINGS:
        return f"invalid retraction_standing: {rs}"
    return None


def ingest_envelope(
    data: dict[str, Any],
    roots: list[dict[str, Any]],
    source_path: str = "",
) -> IngestResult:
    predicate_type = data.get("predicate_type")
    if predicate_type not in ACCEPTED_PREDICATES:
        return IngestResult(
            accepted=False,
            predicate_type=predicate_type,
            reason=f"unknown predicate type: {predicate_type}",
            caps_broken=True,
        )

    envelope = data.get("envelope", {})
    predicate = envelope.get("predicate", {})

    vr = verify_envelope(envelope, roots)
    if not vr.verified:
        return IngestResult(
            accepted=False,
            predicate_type=predicate_type,
            reason=f"verification failed: {vr.reason}",
            caps_broken=vr.caps_broken,
            verification=vr,
        )

    envelope_digest = _compute_envelope_digest(data)

    if predicate_type == "engram.cfer/v1":
        err = _validate_cfer_fields(predicate)
        if err:
            return IngestResult(
                accepted=False,
                predicate_type=predicate_type,
                reason=f"schema validation failed: {err}",
                caps_broken=True,
                verification=vr,
            )
        return IngestResult(
            accepted=True,
            predicate_type=predicate_type,
            cfer_id=predicate["cfer_id"],
            task_hash=predicate["task_hash"],
            lever_identity=predicate["lever"]["identity"],
            reason="accepted",
            verification=vr,
        )

    if predicate_type == "engram.reaudit/v1":
        err = _validate_reaudit_fields(predicate)
        if err:
            return IngestResult(
                accepted=False,
                predicate_type=predicate_type,
                reason=f"schema validation failed: {err}",
                caps_broken=True,
                verification=vr,
            )
        return IngestResult(
            accepted=True,
            predicate_type=predicate_type,
            task_hash=predicate["task_hash"],
            reason="accepted reaudit",
            verification=vr,
        )

    return IngestResult(
        accepted=False,
        predicate_type=predicate_type,
        reason="unreachable",
        caps_broken=True,
    )


def birth_cfer(data: dict[str, Any], source_path: str = "") -> CFER:
    predicate = data["envelope"]["predicate"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return CFER(
        cfer_id=predicate["cfer_id"],
        signer_identity=predicate["signer_identity"],
        signer_role=predicate["signer_role"],
        solver_registry_digest=predicate["solver_registry_digest"],
        task_hash=predicate["task_hash"],
        checker_hash=predicate["checker_hash"],
        transcript_hash=predicate["transcript_hash"],
        environment_hash=predicate["environment_hash"],
        pilot_outcome=predicate["pilot_outcome"],
        statistical_rule=predicate["statistical_rule"],
        binomial_upper_bound=predicate["binomial_upper_bound"],
        model_cohort=predicate["model_cohort"],
        measured_date=predicate["measured_date"],
        signed_at=predicate["signed_at"],
        ingested_at=now,
        validity_interval=predicate["validity_interval"],
        supersession=predicate.get("supersession"),
        disposition=predicate["disposition"],
        lever=predicate["lever"],
        envelope_digest=_compute_envelope_digest(data),
        source_path=source_path,
    )


def build_reaudit_result(data: dict[str, Any], source_path: str = "") -> ReauditResult:
    predicate = data["envelope"]["predicate"]
    return ReauditResult(
        task_hash=predicate["task_hash"],
        screening_roots=predicate["screening_roots"],
        screening_outcomes=predicate["screening_outcomes"],
        retraction_standing=predicate["retraction_standing"],
        screening_measured_at=predicate["screening_measured_at"],
        screening_interval_days=predicate["screening_interval_days"],
        screening_expires_at=predicate["screening_expires_at"],
        signer_identity=predicate["signer_identity"],
        signer_role=predicate["signer_role"],
        envelope_digest=_compute_envelope_digest(data),
        source_path=source_path,
    )


def ingest_all_proofs(
    proofs_dir: Path,
    roots_path: Path,
) -> tuple[list[CFER], list[ReauditResult], list[IngestResult]]:
    cfers: list[CFER] = []
    reaudits: list[ReauditResult] = []
    errors: list[IngestResult] = []

    if not proofs_dir.exists():
        return cfers, reaudits, errors

    roots = load_trust_roots(roots_path)

    for proof_file in sorted(proofs_dir.glob("*.yaml")):
        with open(proof_file) as f:
            data = yaml.safe_load(f)
        if not data:
            errors.append(IngestResult(
                accepted=False,
                reason=f"empty or unparseable file: {proof_file.name}",
                caps_broken=True,
            ))
            continue

        result = ingest_envelope(data, roots, source_path=str(proof_file))
        if not result.accepted:
            errors.append(result)
            continue

        if result.predicate_type == "engram.cfer/v1":
            cfer = birth_cfer(data, source_path=str(proof_file))
            cfers.append(cfer)
        elif result.predicate_type == "engram.reaudit/v1":
            reaudit = build_reaudit_result(data, source_path=str(proof_file))
            reaudits.append(reaudit)

    return cfers, reaudits, errors
