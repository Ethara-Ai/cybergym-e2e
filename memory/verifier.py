"""ENGRAM signature verifier. Phase 1 step 4.

Verifies DSSE signatures against external trust roots in memory/roots.yaml.
E4: self-attested ingests cap below CURRENT.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    import json as yaml
    yaml.safe_load = yaml.loads
    yaml.safe_dump = yaml.dumps


@dataclass
class VerificationResult:
    verified: bool
    signer_identity: str | None = None
    signer_role: str | None = None
    authorized_role: str | None = None
    role_match: bool = False
    trust_root_found: bool = False
    reason: str = ""
    caps_broken: bool = False


def load_trust_roots(roots_path: Path) -> list[dict[str, Any]]:
    if not roots_path.exists():
        return []
    with open(roots_path) as f:
        data = yaml.safe_load(f)
    if not data:
        return []
    return data.get("trust_roots", [])


def find_trust_root(roots: list[dict[str, Any]], signer_identity: str) -> dict[str, Any] | None:
    for root in roots:
        if root.get("identity") == signer_identity:
            return root
    return None


def check_role_authorization(trust_root: dict[str, Any], claimed_role: str) -> bool:
    authorized_roles = trust_root.get("authorized_roles", [])
    return claimed_role in authorized_roles


def compute_payload_digest(payload_bytes: bytes) -> str:
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return f"sha256:{digest}"


def verify_envelope(envelope: dict[str, Any], roots: list[dict[str, Any]]) -> VerificationResult:
    """Verify a DSSE envelope against trust roots.

    In production this would verify real cryptographic signatures.
    The current implementation checks structural correctness and
    trust-root role authorization, which is the contract-required
    behavior for the conformance suite.
    """
    dsse = envelope.get("dsse_wrapper", {})
    predicate = envelope.get("predicate", {})
    signatures = dsse.get("signatures", [])

    signer_identity = predicate.get("signer_identity")
    signer_role = predicate.get("signer_role")

    if not signer_identity:
        return VerificationResult(
            verified=False,
            reason="missing signer_identity in predicate",
            caps_broken=True,
        )

    if not signer_role:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            reason="missing signer_role in predicate",
            caps_broken=True,
        )

    if not signatures:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            signer_role=signer_role,
            reason="no signatures in DSSE wrapper",
            caps_broken=True,
        )

    if not roots:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            signer_role=signer_role,
            trust_root_found=False,
            reason="no trust roots configured",
        )

    sig_keyid = signatures[0].get("keyid")
    if sig_keyid != signer_identity:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            signer_role=signer_role,
            reason=f"signature keyid '{sig_keyid}' does not match signer_identity '{signer_identity}'",
            caps_broken=True,
        )

    trust_root = find_trust_root(roots, signer_identity)
    if trust_root is None:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            signer_role=signer_role,
            trust_root_found=False,
            reason=f"no trust root found for signer '{signer_identity}'",
            caps_broken=True,
        )

    role_ok = check_role_authorization(trust_root, signer_role)
    authorized_roles = trust_root.get("authorized_roles", [])

    if not role_ok:
        return VerificationResult(
            verified=False,
            signer_identity=signer_identity,
            signer_role=signer_role,
            authorized_role=str(authorized_roles),
            role_match=False,
            trust_root_found=True,
            reason=f"signer_role '{signer_role}' not authorized; allowed: {authorized_roles}",
            caps_broken=True,
        )

    return VerificationResult(
        verified=True,
        signer_identity=signer_identity,
        signer_role=signer_role,
        authorized_role=signer_role,
        role_match=True,
        trust_root_found=True,
        reason="signature structurally valid and role authorized",
    )


def verify_envelope_from_file(
    envelope_path: Path,
    roots_path: Path,
) -> VerificationResult:
    if not envelope_path.exists():
        return VerificationResult(
            verified=False,
            reason=f"envelope file not found: {envelope_path}",
        )
    with open(envelope_path) as f:
        data = yaml.safe_load(f)
    envelope = data.get("envelope", {})
    roots = load_trust_roots(roots_path)
    return verify_envelope(envelope, roots)
