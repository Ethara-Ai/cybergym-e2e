"""ENGRAM deterministic freshener. Phase 1 step 6.

Pure function of CFER bytes, pinned config, and one evaluation instant.
E6: freshener output is pure and bit-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


LEVER_STATES = {"ACTIVE", "WATCH", "EXPIRED", "RETIRED"}


@dataclass(frozen=True)
class FreshenerConfig:
    freshness_horizon_days: int | None
    frontier_defeat_floor: float | None
    evaluation_instant: str
    required_cohort: list[dict[str, Any]] | None = None
    cohort_valid: bool = False
    cohort_refresh_deadline: str | None = None


@dataclass(frozen=True)
class LeverState:
    lever_id: str
    cfer_id: str
    state: str
    reason: str
    binomial_upper_bound: float
    measured_date: str
    valid_at: str
    invalid_at: str
    model_cohort_covers_required: bool
    category: str = ""
    axis: str = ""


@dataclass
class FreshenerOutput:
    state_vector: list[LeverState]
    active_count: int = 0
    watch_count: int = 0
    expired_count: int = 0
    retired_count: int = 0
    evaluation_instant: str = ""
    disposition: str = "STALE"
    disposition_reason: str = ""


def _parse_instant(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _cohort_covers(cfer_cohort: list[dict], required: list[dict] | None) -> bool:
    if not required:
        return False
    cfer_set = {(m.get("provider"), m.get("model")) for m in cfer_cohort}
    req_set = {(m.get("provider"), m.get("model")) for m in required}
    return req_set.issubset(cfer_set)


def compute_lever_state(
    cfer: dict[str, Any],
    config: FreshenerConfig,
) -> LeverState:
    lever = cfer.get("lever", {})
    lever_id = lever.get("identity", cfer.get("cfer_id", "unknown"))
    cfer_id = cfer.get("cfer_id", "unknown")
    vi = cfer.get("validity_interval", {})
    valid_at = vi.get("valid_at", "")
    invalid_at = vi.get("invalid_at", "")
    bound = cfer.get("binomial_upper_bound", 1.0)
    measured = cfer.get("measured_date", "")
    cohort = cfer.get("model_cohort", [])
    category = lever.get("category", "")
    axis = lever.get("axis", "")

    eval_dt = _parse_instant(config.evaluation_instant)

    if cfer.get("disposition") == "RETIRED":
        return LeverState(
            lever_id=lever_id, cfer_id=cfer_id, state="RETIRED",
            reason="human-set terminal state",
            binomial_upper_bound=bound, measured_date=measured,
            valid_at=valid_at, invalid_at=invalid_at,
            model_cohort_covers_required=False,
            category=category, axis=axis,
        )

    if invalid_at:
        inv_dt = _parse_instant(invalid_at)
        if eval_dt >= inv_dt:
            return LeverState(
                lever_id=lever_id, cfer_id=cfer_id, state="EXPIRED",
                reason="past validity interval",
                binomial_upper_bound=bound, measured_date=measured,
                valid_at=valid_at, invalid_at=invalid_at,
                model_cohort_covers_required=False,
                category=category, axis=axis,
            )

    if config.freshness_horizon_days is not None and measured:
        from datetime import timedelta
        meas_dt = _parse_instant(measured)
        horizon_end = meas_dt + timedelta(days=config.freshness_horizon_days)
        if eval_dt >= horizon_end:
            return LeverState(
                lever_id=lever_id, cfer_id=cfer_id, state="EXPIRED",
                reason="past freshness horizon",
                binomial_upper_bound=bound, measured_date=measured,
                valid_at=valid_at, invalid_at=invalid_at,
                model_cohort_covers_required=False,
                category=category, axis=axis,
            )

    covers = _cohort_covers(cohort, config.required_cohort)

    if not config.cohort_valid:
        return LeverState(
            lever_id=lever_id, cfer_id=cfer_id, state="WATCH",
            reason="cohort registry missing, expired, or overdue",
            binomial_upper_bound=bound, measured_date=measured,
            valid_at=valid_at, invalid_at=invalid_at,
            model_cohort_covers_required=covers,
            category=category, axis=axis,
        )

    if not covers:
        return LeverState(
            lever_id=lever_id, cfer_id=cfer_id, state="EXPIRED",
            reason="model_cohort does not cover required_cohort",
            binomial_upper_bound=bound, measured_date=measured,
            valid_at=valid_at, invalid_at=invalid_at,
            model_cohort_covers_required=False,
            category=category, axis=axis,
        )

    if config.frontier_defeat_floor is not None and bound >= config.frontier_defeat_floor:
        return LeverState(
            lever_id=lever_id, cfer_id=cfer_id, state="WATCH",
            reason=f"binomial upper bound {bound} >= floor {config.frontier_defeat_floor}",
            binomial_upper_bound=bound, measured_date=measured,
            valid_at=valid_at, invalid_at=invalid_at,
            model_cohort_covers_required=covers,
            category=category, axis=axis,
        )

    return LeverState(
        lever_id=lever_id, cfer_id=cfer_id, state="ACTIVE",
        reason="fresh, below floor, cohort valid",
        binomial_upper_bound=bound, measured_date=measured,
        valid_at=valid_at, invalid_at=invalid_at,
        model_cohort_covers_required=covers,
        category=category, axis=axis,
    )


def freshen(
    cfers: list[dict[str, Any]],
    config: FreshenerConfig,
) -> FreshenerOutput:
    if not cfers:
        return FreshenerOutput(
            state_vector=[],
            evaluation_instant=config.evaluation_instant,
            disposition="STALE",
            disposition_reason="zero CFERs, no signed proof has landed",
        )

    states = []
    for cfer in sorted(cfers, key=lambda c: c.get("cfer_id", "")):
        ls = compute_lever_state(cfer, config)
        states.append(ls)

    active = sum(1 for s in states if s.state == "ACTIVE")
    watch = sum(1 for s in states if s.state == "WATCH")
    expired = sum(1 for s in states if s.state == "EXPIRED")
    retired = sum(1 for s in states if s.state == "RETIRED")

    broken_reasons = []
    for s in states:
        if s.state == "ACTIVE" and not s.model_cohort_covers_required:
            broken_reasons.append(f"ACTIVE lever {s.lever_id} without cohort coverage")

    if broken_reasons:
        disp = "BROKEN"
        disp_reason = "; ".join(broken_reasons)
    elif active > 0 and not broken_reasons:
        disp = "CURRENT"
        disp_reason = f"{active} ACTIVE levers, all verified"
    else:
        disp = "STALE"
        disp_reason = f"no ACTIVE levers ({watch} WATCH, {expired} EXPIRED, {retired} RETIRED)"

    return FreshenerOutput(
        state_vector=states,
        active_count=active,
        watch_count=watch,
        expired_count=expired,
        retired_count=retired,
        evaluation_instant=config.evaluation_instant,
        disposition=disp,
        disposition_reason=disp_reason,
    )
