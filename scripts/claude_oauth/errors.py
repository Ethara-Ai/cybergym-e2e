"""Classify Anthropic API errors so the bridge and pipeline can react sensibly.

Anthropic returns a small set of HTTP+body shapes; this module turns each
into an ``ErrorKind`` plus a recommended ``retry_after_seconds`` so callers
don't have to re-parse headers.

The hardest call is 429: it covers both a short transient throttle AND the
hard 5-hour / weekly subscription caps. The API does not distinguish, so we
heuristically classify based on the ``Retry-After`` value and the
``anthropic-ratelimit-tokens-remaining`` header.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

_LOG = logging.getLogger(__name__)

# Boundary between "wait inline at the bridge" and "this is a subscription cap,
# bubble up so the pipeline can pause-and-resume". 60s is the practical cutoff
# observed on real 429 responses -- transient throttles report Retry-After
# values of single-digit to ~30 seconds; subscription caps report values
# >= 60 seconds (typically thousands).
TRANSIENT_RETRY_AFTER_THRESHOLD = 60

# Subscription (Pro/Max OAuth) accounts are governed by two independent rolling
# windows -- 5-hour and 7-day -- reported through the "unified" header family.
# These are NOT the same as the per-minute token/request buckets below: a
# metered API key hits those, a subscription hits these. Reading only the
# token/request buckets meant a real 5-hour cap parsed as "no reset known" and
# fell back to a 300s cooldown, so the account was re-probed every 5 minutes
# for 5 hours instead of once at the real reset.
UNIFIED_STATUS_HEADER = "anthropic-ratelimit-unified-status"
UNIFIED_CLAIM_HEADER = "anthropic-ratelimit-unified-representative-claim"

# "representative-claim" names whichever window is currently binding.
_CLAIM_RESET_HEADERS = {
    "five_hour": "anthropic-ratelimit-unified-5h-reset",
    "seven_day": "anthropic-ratelimit-unified-7d-reset",
}

_UNIFIED_RESET_HEADERS = (
    "anthropic-ratelimit-unified-reset",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-reset",
)

_BUCKET_RESET_HEADERS = (
    "anthropic-ratelimit-unified-tokens-reset",
    "anthropic-ratelimit-unified-requests-reset",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-requests-reset",
)


def unified_status_rejects(headers: Mapping[str, str]) -> bool:
    """True when Anthropic says this account is out for the current window.

    Anything other than "allowed" is an account-level verdict that no amount of
    retrying or token refreshing clears -- only a different account will. It
    outranks a short retry-after, which would otherwise keep us hammering an
    account whose 5-hour quota is already spent.
    """
    # EXACT key only. Live responses also carry
    # "anthropic-ratelimit-unified-overage-status", which reads "rejected" on a
    # perfectly healthy account whose org has overage billing disabled. Any
    # loosening to a suffix/substring match here would cap every account on
    # every request -- see test_overage_status_rejected_is_not_a_cap.
    val = headers.get(UNIFIED_STATUS_HEADER) or headers.get(UNIFIED_STATUS_HEADER.lower())
    if isinstance(val, str) and val.strip().lower() not in ("", "allowed"):
        return True
    return any(_window_rejected(headers, w) for w in ("5h", "7d"))


def _window_rejected(headers: Mapping[str, str], window: str) -> bool:
    """True when the named window ("5h"/"7d") reports a non-allowed status."""
    name = f"anthropic-ratelimit-unified-{window}-status"
    val = headers.get(name) or headers.get(name.lower())
    return isinstance(val, str) and val.strip().lower() not in ("", "allowed")


def _resolve_unified_reset(headers: Mapping[str, str]) -> Optional[float]:
    """Absolute Unix time the binding subscription window resets.

    Order matters. A live response carries resets for BOTH windows at once
    (5h and 7d, ~6 days apart), so picking the wrong one either re-probes a
    capped account for days or parks a healthy one for a week:

      1. The window whose own status is not "allowed" -- it is the one that
         actually refused this request.
      2. Otherwise "representative-claim", which names the binding window.
      3. Otherwise the aggregate/first available reset.
    """
    for window in ("5h", "7d"):
        if _window_rejected(headers, window):
            val = _parse_iso_header(headers, f"anthropic-ratelimit-unified-{window}-reset")
            if val is not None:
                return val

    claim = headers.get(UNIFIED_CLAIM_HEADER) or headers.get(UNIFIED_CLAIM_HEADER.lower())
    if isinstance(claim, str):
        named = _CLAIM_RESET_HEADERS.get(claim.strip().lower())
        if named:
            val = _parse_iso_header(headers, named)
            if val is not None:
                return val

    for name in _UNIFIED_RESET_HEADERS:
        val = _parse_iso_header(headers, name)
        if val is not None:
            return val
    return None


class ErrorKind(str, Enum):
    """Coarse classification of an Anthropic API error."""

    TRANSIENT_THROTTLE = "transient_throttle"
    SUBSCRIPTION_CAP = "subscription_cap"
    OAUTH_TOKEN_INVALID = "oauth_token_invalid"
    ACCOUNT_RESTRICTED = "account_restricted"
    OVERLOADED = "overloaded"
    BILLING_ERROR = "billing_error"
    INVALID_REQUEST = "invalid_request"
    UPSTREAM_5XX = "upstream_5xx"
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        """Whether the bridge should retry this error class itself."""
        return self in {
            ErrorKind.TRANSIENT_THROTTLE,
            ErrorKind.OVERLOADED,
            ErrorKind.UPSTREAM_5XX,
        }

    @property
    def is_account_problem(self) -> bool:
        """Whether failing over to a different account would help."""
        return self in {
            ErrorKind.SUBSCRIPTION_CAP,
            ErrorKind.OAUTH_TOKEN_INVALID,
            ErrorKind.ACCOUNT_RESTRICTED,
            ErrorKind.BILLING_ERROR,
        }


@dataclass
class ClassifiedError:
    kind: ErrorKind
    status_code: int
    retry_after_seconds: Optional[int]
    reset_at_unix: Optional[float]
    message: str
    raw_error_type: Optional[str] = None
    request_id: Optional[str] = None


def _parse_int_header(headers: Mapping[str, str], name: str) -> Optional[int]:
    val = headers.get(name) or headers.get(name.lower())
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_iso_header(headers: Mapping[str, str], name: str) -> Optional[float]:
    """Parse an RFC3339 reset timestamp into Unix seconds. None on failure."""
    val = headers.get(name) or headers.get(name.lower())
    if not val:
        return None
    # Anthropic returns either RFC3339 (preferred) or seconds-from-epoch.
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime

        # Normalize trailing Z -> +00:00 so fromisoformat accepts it.
        norm = val.rstrip()
        if norm.endswith("Z"):
            norm = norm[:-1] + "+00:00"
        dt = datetime.fromisoformat(norm)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def extract_retry_after(headers: Mapping[str, str]) -> Optional[int]:
    """Best-effort seconds-to-retry, preferring Retry-After then ratelimit-reset."""
    explicit = _parse_int_header(headers, "Retry-After") or _parse_int_header(
        headers, "retry-after"
    )
    if explicit is not None and explicit >= 0:
        return explicit

    now = time.time()
    # Subscription windows first: on a Pro/Max account they are the binding
    # limit, and the per-bucket headers are often absent entirely.
    unified = _resolve_unified_reset(headers)
    if unified is not None:
        delta = int(unified - now)
        if delta > 0:
            return delta
    for key in _BUCKET_RESET_HEADERS:
        reset_at = _parse_iso_header(headers, key)
        if reset_at is not None:
            delta = int(reset_at - now)
            if delta > 0:
                return delta
    return None


def _extract_reset_at(headers: Mapping[str, str]) -> Optional[float]:
    """Absolute Unix-time when the most relevant rate-limit bucket resets."""
    unified = _resolve_unified_reset(headers)
    if unified is not None:
        return unified
    for key in _BUCKET_RESET_HEADERS:
        v = _parse_iso_header(headers, key)
        if v is not None:
            return v
    ra = extract_retry_after(headers)
    if ra is not None:
        return time.time() + ra
    return None


def _decode_body(body: bytes | str | None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull (error_type, message, request_id) out of an Anthropic error body."""
    if body is None:
        return None, None, None
    if isinstance(body, (bytes, bytearray)):
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None, None, None
    else:
        text = body
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None, text[:200] if text else None, None
    if not isinstance(obj, dict):
        return None, None, None
    err = obj.get("error") or {}
    if isinstance(err, dict):
        return (
            err.get("type"),
            err.get("message"),
            obj.get("request_id"),
        )
    return None, str(err)[:200], obj.get("request_id")


def classify_anthropic_error(
    status_code: int,
    body: bytes | str | None,
    headers: Mapping[str, str] | None = None,
) -> ClassifiedError:
    """Map an Anthropic upstream response into a ``ClassifiedError``.

    Heuristics:
      - 429 with retry-after < 60s and tokens-remaining > 0  -> TRANSIENT_THROTTLE
      - 429 with retry-after >= 60s OR tokens-remaining == 0 -> SUBSCRIPTION_CAP
      - 401                                                  -> OAUTH_TOKEN_INVALID
      - 403                                                  -> ACCOUNT_RESTRICTED
      - 402                                                  -> BILLING_ERROR
      - 529                                                  -> OVERLOADED
      - 5xx                                                  -> UPSTREAM_5XX
      - 400                                                  -> INVALID_REQUEST
    """
    headers = headers or {}
    err_type, message, request_id = _decode_body(body)
    retry_after = extract_retry_after(headers)
    reset_at = _extract_reset_at(headers)
    message = message or err_type or f"HTTP {status_code}"

    if status_code == 429:
        tokens_remaining = _parse_int_header(
            headers, "anthropic-ratelimit-unified-tokens-remaining"
        )
        if tokens_remaining is None:
            tokens_remaining = _parse_int_header(
                headers, "anthropic-ratelimit-tokens-remaining"
            )
        is_cap = False
        if retry_after is not None and retry_after >= TRANSIENT_RETRY_AFTER_THRESHOLD:
            is_cap = True
        if tokens_remaining == 0:
            is_cap = True
        if retry_after is None and tokens_remaining is None:
            # No hint at all: assume the 5-hour subscription cap rather than
            # burning retries against it (the conservative reading).
            is_cap = True
        if unified_status_rejects(headers):
            # Outranks everything above: the server has said this account is
            # out for the window, so a short retry-after is not an invitation
            # to retry the same account.
            is_cap = True
        kind = ErrorKind.SUBSCRIPTION_CAP if is_cap else ErrorKind.TRANSIENT_THROTTLE
        return ClassifiedError(
            kind=kind,
            status_code=429,
            retry_after_seconds=retry_after,
            reset_at_unix=reset_at,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if status_code == 401:
        return ClassifiedError(
            kind=ErrorKind.OAUTH_TOKEN_INVALID,
            status_code=401,
            retry_after_seconds=None,
            reset_at_unix=None,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if status_code == 403:
        return ClassifiedError(
            kind=ErrorKind.ACCOUNT_RESTRICTED,
            status_code=403,
            retry_after_seconds=None,
            reset_at_unix=None,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if status_code == 402:
        return ClassifiedError(
            kind=ErrorKind.BILLING_ERROR,
            status_code=402,
            retry_after_seconds=None,
            reset_at_unix=None,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if status_code == 529:
        return ClassifiedError(
            kind=ErrorKind.OVERLOADED,
            status_code=529,
            retry_after_seconds=retry_after,
            reset_at_unix=reset_at,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if status_code == 400:
        return ClassifiedError(
            kind=ErrorKind.INVALID_REQUEST,
            status_code=400,
            retry_after_seconds=None,
            reset_at_unix=None,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    if 500 <= status_code < 600:
        return ClassifiedError(
            kind=ErrorKind.UPSTREAM_5XX,
            status_code=status_code,
            retry_after_seconds=retry_after,
            reset_at_unix=reset_at,
            message=message,
            raw_error_type=err_type,
            request_id=request_id,
        )

    return ClassifiedError(
        kind=ErrorKind.UNKNOWN,
        status_code=status_code,
        retry_after_seconds=None,
        reset_at_unix=None,
        message=message,
        raw_error_type=err_type,
        request_id=request_id,
    )
