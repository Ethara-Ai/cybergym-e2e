"""ENGRAM checkpointer and recovery. Phase 1 step 8a.

Maintains signed Merkle checkpoint log over the ledger.
E17: the ledger is checkpointed and recoverable.
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
    yaml.safe_dump = yaml.dumps


@dataclass
class MerkleTreeHead:
    prior_root: str | None
    new_root: str
    leaf_count: int
    ingest_timestamp: str
    signature: str


@dataclass
class CheckpointResult:
    success: bool
    tree_head: MerkleTreeHead | None = None
    reason: str = ""


@dataclass
class RecoveryResult:
    success: bool
    ledger_records: list[dict[str, Any]] = field(default_factory=list)
    supersessions: list[dict[str, Any]] = field(default_factory=list)
    canary_entries: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    caps_broken: bool = False


def _hash_leaf(canonical_bytes: str) -> str:
    return hashlib.sha256(canonical_bytes.encode("utf-8")).hexdigest()


def _combine_hashes(left: str, right: str) -> str:
    combined = left + right
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def compute_merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"empty").hexdigest()

    layer = [_hash_leaf(leaf) for leaf in leaves]

    while len(layer) > 1:
        next_layer = []
        for i in range(0, len(layer), 2):
            if i + 1 < len(layer):
                next_layer.append(_combine_hashes(layer[i], layer[i + 1]))
            else:
                next_layer.append(layer[i])
        layer = next_layer

    return layer[0]


def _canonicalize(record: dict[str, Any]) -> str:
    if hasattr(yaml, 'dump'):
        return yaml.dump(record, sort_keys=True, default_flow_style=False)
    return str(sorted(record.items()))


def build_leaf_sequence(
    sequence: list[dict[str, Any]],
    envelopes: dict[str, dict[str, Any]],
    supersessions: list[dict[str, Any]],
) -> list[str]:
    leaves = []
    sup_by_authorizing = {}
    for s in supersessions:
        auth_cfer = s.get("superseding_cfer_id")
        if auth_cfer:
            sup_by_authorizing.setdefault(auth_cfer, []).append(s)

    for entry in sorted(sequence, key=lambda e: e.get("position", 0)):
        digest = entry.get("envelope_digest", "")
        env = envelopes.get(digest, {})
        leaves.append(_canonicalize(env) if env else digest)
        cfer_id = env.get("envelope", {}).get("predicate", {}).get("cfer_id")
        if cfer_id and cfer_id in sup_by_authorizing:
            for sup_rec in sup_by_authorizing[cfer_id]:
                leaves.append(_canonicalize(sup_rec))

    return leaves


def checkpoint(
    sequence: list[dict[str, Any]],
    envelopes: dict[str, dict[str, Any]],
    supersessions: list[dict[str, Any]],
    prior_checkpoints: list[MerkleTreeHead],
    signer_identity: str = "self",
) -> CheckpointResult:
    if not sequence:
        return CheckpointResult(
            success=True,
            tree_head=None,
            reason="empty ledger, no checkpoint needed",
        )

    leaves = build_leaf_sequence(sequence, envelopes, supersessions)
    new_root = compute_merkle_root(leaves)

    prior_root = prior_checkpoints[-1].new_root if prior_checkpoints else None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = f"self-signed:{hashlib.sha256(new_root.encode()).hexdigest()[:16]}"

    head = MerkleTreeHead(
        prior_root=prior_root,
        new_root=new_root,
        leaf_count=len(leaves),
        ingest_timestamp=now,
        signature=sig,
    )

    return CheckpointResult(success=True, tree_head=head)


def verify_checkpoint_chain(checkpoints: list[MerkleTreeHead]) -> tuple[bool, str]:
    if not checkpoints:
        return True, "empty chain"

    for i, head in enumerate(checkpoints):
        if i == 0:
            if head.prior_root is not None:
                return False, f"first checkpoint has non-null prior_root"
        else:
            if head.prior_root != checkpoints[i - 1].new_root:
                return False, f"checkpoint {i} prior_root does not chain to predecessor"
    return True, "chain valid"


def recover(
    signed_cfer_envelopes: list[dict[str, Any]],
    signed_reaudit_envelopes: list[dict[str, Any]],
    sequence: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    retirements: list[dict[str, Any]],
    roots: list[dict[str, Any]],
) -> RecoveryResult:
    if not signed_cfer_envelopes and not signed_reaudit_envelopes:
        return RecoveryResult(
            success=True,
            reason="empty recovery: no envelopes to replay",
        )

    ledger_records = []
    supersessions = []
    canary_entries = []

    for env_data in signed_cfer_envelopes:
        predicate = env_data.get("envelope", {}).get("predicate", {})
        cfer_id = predicate.get("cfer_id")
        if not cfer_id:
            return RecoveryResult(
                success=False,
                reason="CFER envelope missing cfer_id",
                caps_broken=True,
            )

        record = {
            "cfer_id": cfer_id,
            "task_hash": predicate.get("task_hash"),
            "lever_identity": predicate.get("lever", {}).get("identity"),
            "disposition": predicate.get("disposition"),
            "measured_date": predicate.get("measured_date"),
            "binomial_upper_bound": predicate.get("binomial_upper_bound"),
            "model_cohort": predicate.get("model_cohort"),
            "validity_interval": predicate.get("validity_interval"),
            "envelope_digest": _canonicalize(env_data),
        }
        ledger_records.append(record)

        sup = predicate.get("supersession")
        if sup:
            supersessions.append({
                "cfer_id": sup.get("prior_cfer_id", sup.get("cfer_id")),
                "superseding_cfer_id": cfer_id,
                "invalid_at": sup.get("invalid_at"),
                "envelope_digest": hashlib.sha256(
                    _canonicalize(env_data).encode()
                ).hexdigest(),
            })

    for env_data in signed_reaudit_envelopes:
        predicate = env_data.get("envelope", {}).get("predicate", {})
        task_hash = predicate.get("task_hash")
        canary_entries.append({
            "task_hash": task_hash,
            "retraction_standing": predicate.get("retraction_standing"),
            "screening_measured_at": predicate.get("screening_measured_at"),
            "screening_expires_at": predicate.get("screening_expires_at"),
            "screening_roots": predicate.get("screening_roots"),
        })

    for ret in sorted(retirements, key=lambda r: r.get("retired_at", "")):
        lever_id = ret.get("lever_id")
        for rec in ledger_records:
            if rec.get("lever_identity") == lever_id:
                rec["disposition"] = "RETIRED"

    return RecoveryResult(
        success=True,
        ledger_records=ledger_records,
        supersessions=supersessions,
        canary_entries=canary_entries,
        reason="recovery complete",
    )
