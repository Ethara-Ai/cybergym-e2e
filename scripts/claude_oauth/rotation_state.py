"""Durable, cross-process rotation state for the multi-account pool.

WHY THIS EXISTS
---------------
``batch_run.sh`` fans out with ``xargs -P $MAX_PARALLEL`` (its own examples use
50). Each task runs ``run_agent.py``, which starts its *own* bridge subprocess
on its *own* ephemeral port. Before this module, every one of those processes
kept its cooldown bookkeeping in a process-local float on ``_AccountSlot``, so:

  * a subscription cap discovered by worker 3 was invisible to workers 1, 2,
    4 ... N, which kept hammering the capped account until each rediscovered
    the cap the expensive way; and
  * restarting the bridge forgot every cooldown.

This module is the shared, durable view: which account is cold, until when, and
why. It holds NO secret material -- credentials stay in the keychain / creds
files that ``credentials.py`` owns. That split is deliberate: cooldowns change
on every 429, tokens change only on rotation, and mixing them would rewrite
token material constantly and widen the window in which a rotating refresh
token can be lost.

CONCURRENCY
-----------
Two separate guarantees, both built on ``fcntl.flock``:

  * ``_locked()`` guards this file's read-modify-write cycle.
  * ``account_lock(label)`` is a named mutex the credential providers take
    around a token refresh, so exactly one process machine-wide refreshes a
    given account at a time. Anthropic rotates the refresh token and
    invalidates its predecessor with no grace window, so two concurrent
    refreshes of one account permanently brick it.

Writes go through ``credentials._atomic_write_private`` (temp file, 0600 from
the first byte, fsync, rename) rather than a second implementation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

_LOG = logging.getLogger(__name__)

try:
    import fcntl

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows; the bridge is POSIX-only anyway
    fcntl = None  # type: ignore[assignment]
    _HAVE_FLOCK = False

STATE_VERSION = 1

# Sits beside the existing refresh cache (credentials._CACHE_PATH) so the
# bridge keeps all its mutable host state under one directory.
_STATE_PATH = Path.home() / ".cache" / "wildclawbench" / "rotation-state.json"

# Bound the wait for a contended lock so a wedged holder cannot hang a run
# forever; on timeout we proceed unlocked rather than fail.
#
# Not arbitrary: the guarded section can be a token refresh, and
# ``refresh_credentials`` defaults to max_attempts=3 at timeout=30.0 with
# exponential backoff (credentials.py), so a healthy worst-case refresh runs
# ~100s. A shorter bound would abandon the lock while a legitimate refresh is
# still in flight and reintroduce the double-refresh race this module prevents.
#
# There is deliberately no "stale lock" reclaim. flock is released by the
# kernel when a process dies, so a dead holder's lock is already gone and the
# non-blocking acquire below simply succeeds. A lock that is still held is held
# by a LIVE process, and the only correct response is to wait for it.
LOCK_TIMEOUT_SECONDS = 200.0

_LOCK_POLL_SECONDS = 0.05


def state_path() -> Path:
    """Location of the shared state file (override with WCB_CC_ROTATION_STATE)."""
    override = os.environ.get("WCB_CC_ROTATION_STATE", "").strip()
    return Path(override).expanduser() if override else _STATE_PATH


def default_state() -> dict:
    return {"version": STATE_VERSION, "accounts": {}}


def default_account() -> dict:
    return {
        "cooldown_until": 0.0,
        "invalid": False,
        "failure_count": 0,
        "last_reason": None,
        "updated_at": 0.0,
    }


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _flock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[bool]:
    """Hold an exclusive flock on ``lock_path``. Yields True if actually held.

    Degrades to an unlocked pass (yielding False, with a warning) rather than
    failing the run: a bridge that cannot lock is worse than one that races,
    because the race is rare and a hard failure is certain.
    """
    if not _HAVE_FLOCK:
        _LOG.warning("fcntl unavailable; proceeding without a cross-process lock")
        yield False
        return

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _LOG.warning("could not create lock dir for %s: %s; proceeding unlocked", lock_path, e)
        yield False
        return

    deadline = time.time() + timeout
    try:
        # Not a plain `with open(...)`: a failure to open must degrade to an
        # unlocked pass rather than propagate, so the handle is acquired first
        # and handed to a `with` below.
        fh = open(lock_path, "a+")  # noqa: SIM115
    except OSError as e:
        _LOG.warning("could not open lock %s: %s; proceeding unlocked", lock_path, e)
        yield False
        return

    with fh:
        held = False
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                pass

            if time.time() >= deadline:
                _LOG.warning(
                    "timed out after %.0fs waiting for %s; proceeding unlocked",
                    timeout, lock_path,
                )
                break
            time.sleep(_LOCK_POLL_SECONDS)

        try:
            yield held
        finally:
            if held:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def account_lock(label: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[bool]:
    """Machine-wide mutex for refreshing one account's OAuth token.

    Callers MUST re-read the credential store after acquiring this and skip the
    refresh if another process already rotated the token -- otherwise the loser
    of the race spends a refresh token it now knows is dead. See the callers in
    ``credentials.py``.
    """
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in label) or "default"
    with _flock(state_path().with_name(f"refresh-{safe}.lock"), timeout) as held:
        yield held


# --------------------------------------------------------------------------- #
# State I/O
# --------------------------------------------------------------------------- #


def _read_unlocked() -> dict:
    path = state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        # Absent simply means a fresh pool, not an error.
        return default_state()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.warning("rotation state at %s is malformed JSON; using defaults", path)
        return default_state()

    if not isinstance(parsed, dict) or parsed.get("version") != STATE_VERSION:
        _LOG.warning("rotation state at %s has unexpected shape/version; using defaults", path)
        return default_state()

    accounts = parsed.get("accounts")
    if not isinstance(accounts, dict):
        return default_state()

    clean: dict[str, dict] = {}
    for label, entry in accounts.items():
        if not isinstance(label, str) or not isinstance(entry, dict):
            continue
        merged = default_account()
        for key in merged:
            if key in entry:
                merged[key] = entry[key]
        try:
            merged["cooldown_until"] = float(merged["cooldown_until"] or 0.0)
            merged["failure_count"] = int(merged["failure_count"] or 0)
            merged["updated_at"] = float(merged["updated_at"] or 0.0)
            merged["invalid"] = bool(merged["invalid"])
        except (TypeError, ValueError):
            merged = default_account()
        clean[label] = merged

    return {"version": STATE_VERSION, "accounts": clean}


def _write_unlocked(state: dict) -> None:
    # Imported lazily: credentials.py imports this module for account_lock, so
    # a module-level import here would be circular.
    from .credentials import _atomic_write_private

    try:
        _atomic_write_private(state_path(), json.dumps(state, indent=2))
    except OSError as e:
        _LOG.warning("could not persist rotation state to %s: %s", state_path(), e)


@contextlib.contextmanager
def _locked() -> Iterator[dict]:
    """Read-modify-write the state file under an exclusive lock.

    The dict yielded is mutated in place by the caller and written back on a
    clean exit. An exception inside the block leaves the file untouched.
    """
    with _flock(state_path().with_suffix(".lock")):
        state = _read_unlocked()
        yield state
        _write_unlocked(state)


def _entry(state: dict, label: str) -> dict:
    return state["accounts"].setdefault(label, default_account())


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load() -> dict:
    """Snapshot of shared state. Read-only; no lock needed for a single read."""
    return _read_unlocked()


def get_account(label: str) -> dict:
    return load()["accounts"].get(label) or default_account()


def is_available(label: str, now: Optional[float] = None) -> bool:
    entry = get_account(label)
    if entry["invalid"]:
        return False
    return (now if now is not None else time.time()) >= entry["cooldown_until"]


def mark_cooldown(label: str, until_unix: float, reason: str = "") -> None:
    """Cool an account until ``until_unix``. Never shortens an existing cooldown."""
    with _locked() as state:
        entry = _entry(state, label)
        entry["cooldown_until"] = max(float(entry["cooldown_until"]), float(until_unix))
        entry["failure_count"] = int(entry["failure_count"]) + 1
        entry["last_reason"] = reason or entry["last_reason"]
        entry["updated_at"] = time.time()


def mark_invalid(label: str, reason: str = "") -> None:
    """Exclude an account permanently. Cleared only by ``clear_cooldowns``."""
    with _locked() as state:
        entry = _entry(state, label)
        entry["invalid"] = True
        entry["failure_count"] = int(entry["failure_count"]) + 1
        entry["last_reason"] = reason or entry["last_reason"]
        entry["updated_at"] = time.time()


def mark_success(label: str) -> None:
    """Record a good response: clears the failure streak, keeps any cooldown.

    Called on EVERY 2xx, so the healthy case must not touch the lock -- taking
    it per-request would serialize every bridge process on a single file. A
    lock-free read decides whether there is anything to clear, and only a
    genuine state change pays for the lock.
    """
    entry = get_account(label)
    if entry["failure_count"] == 0 and entry["last_reason"] is None:
        return
    with _locked() as state:
        entry = _entry(state, label)
        entry["failure_count"] = 0
        entry["last_reason"] = None
        entry["updated_at"] = time.time()


def ensure_account(label: str) -> None:
    ensure_accounts([label])


def ensure_accounts(labels: Iterable[str]) -> None:
    """Register several accounts under ONE lock acquisition.

    Called at provider construction, and batch_run.sh starts up to MAX_PARALLEL
    bridges at once -- taking the lock per account would make startup contend
    N times per process for no reason. Skips the write entirely when every
    label is already present, which is the steady state.
    """
    labels = list(labels)
    existing = load()["accounts"]
    if all(label in existing for label in labels):
        return
    with _locked() as state:
        for label in labels:
            _entry(state, label)


def clear_cooldowns() -> int:
    """Re-enable every account after a rate-limit storm. Returns count cleared."""
    with _locked() as state:
        cleared = 0
        for entry in state["accounts"].values():
            if entry["invalid"] or entry["cooldown_until"] > 0 or entry["failure_count"]:
                cleared += 1
            entry.update(default_account())
            entry["updated_at"] = time.time()
        return cleared


def next_reset_at() -> Optional[float]:
    """Soonest time a cold account frees up, or None if one is available now."""
    state = load()
    now = time.time()
    entries = list(state["accounts"].values())
    if not entries:
        return None
    if any(not e["invalid"] and now >= e["cooldown_until"] for e in entries):
        return None
    future = [e["cooldown_until"] for e in entries if not e["invalid"]]
    return min(future) if future else None


def snapshot() -> list[dict]:
    state = load()
    now = time.time()
    return [
        {
            "label": label,
            "invalid": e["invalid"],
            "cooldown_until": e["cooldown_until"],
            "failure_count": e["failure_count"],
            "last_reason": e["last_reason"],
            "available": (not e["invalid"]) and now >= e["cooldown_until"],
        }
        for label, e in sorted(state["accounts"].items())
    ]
