"""Claude Code OAuth credentials: read from system stores + refresh.

The official ``claude`` CLI stores credentials under the keychain service
``Claude Code-credentials`` on macOS (and ``~/.claude/.credentials.json`` on
Linux). The payload shape is::

    {"claudeAiOauth": {
        "accessToken":     "sk-ant-oat01-...",
        "refreshToken":    "sk-ant-ort01-...",
        "expiresAt":       1782402066667,    # Unix ms
        "scopes":          ["user:inference", ...],
        "subscriptionType": "max"
    }}

Sources are tried in this priority order:

  1. ``CLAUDE_CODE_CREDENTIALS`` env var (inline JSON, for tests/CI).
  2. ``WCB_CC_CREDS_PATH`` env var (path to JSON file, for overrides).
  3. ``~/.claude/.credentials.json`` (the primary source on Linux, where the
     ``claude`` CLI writes the token as a plaintext file).
  4. macOS Keychain (``security find-generic-password -s ...``; no-op off Darwin).
  5. Linux Secret Service (``secret-tool lookup ...``; optional, desktop only,
     no-op off Linux or when no keyring is unlocked).
  6. ``~/.cache/wildclawbench/claude_creds.json`` (bridge refresh cache).

Precedence (see ``load_credentials``): (1) wins outright; (2, WCB_CC_CREDS_PATH)
is used exclusively when set; otherwise the FRESHEST credentials among the
file, keychain, secret-service and cache sources are used (latest
``expiresAt``), because Anthropic rotates the refresh token on every refresh
and only the most recent copy still works.  After a refresh the new tokens
are written back to the cache, the credentials file and (macOS) the keychain
item, so the ``claude`` CLI and the bridge stay in sync.

On Linux no OS keychain is required: the ``claude`` CLI writes the file at (3),
which is read directly. The Secret Service source at (5) only matters for
desktop-Linux setups whose CLI stored the token in GNOME Keyring / KWallet.

When a refresh happens, we write to (5) only -- never back to Keychain --
because the ``claude`` CLI also manages Keychain and we don't want write
races. Next bridge start re-reads Keychain (canonical source) and falls back
to the cache if Keychain has somehow gone stale.
"""

from __future__ import annotations

import json
import logging
import contextlib
import os
import re
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

_LOG = logging.getLogger(__name__)

# Public Claude Code client identifier (same value ships in every release of
# the `claude` CLI; required for the OAuth refresh-token grant).
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
REFRESH_LEEWAY_SECONDS = 60

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CACHE_PATH = Path.home() / ".cache" / "wildclawbench" / "claude_creds.json"


class CredentialsError(RuntimeError):
    """Raised when credentials cannot be loaded or refreshed."""


class TransientCredentialsError(CredentialsError):
    """A refresh failed for a reason that may clear on its own (network,
    5xx).  A pool must NOT mark the slot permanently invalid for this."""


@dataclass
class OAuthCredentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    subscription_type: Optional[str] = None

    @classmethod
    def from_claude_payload(cls, payload: dict) -> "OAuthCredentials":
        cc = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        cc = cc or payload
        try:
            return cls(
                access_token=cc["accessToken"],
                refresh_token=cc["refreshToken"],
                expires_at_ms=int(cc["expiresAt"]),
                scopes=list(cc.get("scopes") or []),
                subscription_type=cc.get("subscriptionType"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise CredentialsError(f"Malformed Claude Code credentials: {e}") from e

    def to_claude_payload(self) -> dict:
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at_ms,
                "scopes": self.scopes,
                "subscriptionType": self.subscription_type,
            }
        }

    def is_expired(self, leeway_seconds: int = REFRESH_LEEWAY_SECONDS) -> bool:
        return time.time() >= (self.expires_at_ms / 1000.0) - leeway_seconds


def _read_inline_env() -> Optional[str]:
    raw = os.environ.get("CLAUDE_CODE_CREDENTIALS")
    return raw if raw else None


def _read_credentials_file() -> Optional[str]:
    candidates: list[str] = []
    env_path = os.environ.get("WCB_CC_CREDS_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(str(Path.home() / ".claude" / ".credentials.json"))
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                _LOG.debug("credentials file %s read failed: %s", p, e)
    return None


def _read_keychain_macos() -> Optional[str]:
    if platform.system() != "Darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.debug("keychain read failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def _read_secretservice_linux() -> Optional[str]:
    """Read the token from the Linux Secret Service (GNOME Keyring / KWallet).

    Optional desktop-Linux source: newer `claude` CLI builds may store the
    credential blob in the freedesktop Secret Service instead of the plaintext
    ``~/.claude/.credentials.json`` file. Requires ``secret-tool`` (libsecret)
    on PATH and an *unlocked* keyring — so it silently no-ops on headless
    servers / EC2 (no D-Bus session), where the file source is used instead.
    """
    if platform.system() != "Linux":
        return None
    try:
        r = subprocess.run(
            ["secret-tool", "lookup", "service", _KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.debug("secret-service read failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def _read_cache_file() -> Optional[str]:
    if _CACHE_PATH.is_file():
        try:
            return _CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _no_credentials_hint() -> str:
    """OS-appropriate remediation hint for a missing-credentials error."""
    system = platform.system()
    if system == "Darwin":
        return (
            "No Claude Code credentials found. Sign in via the `claude` CLI "
            "first, then verify with:\n"
            "  security find-generic-password -s 'Claude Code-credentials' -w"
        )
    if system == "Linux":
        return (
            "No Claude Code credentials found. Sign in via the `claude` CLI "
            "first (it writes ~/.claude/.credentials.json on Linux), then verify with:\n"
            "  test -f ~/.claude/.credentials.json && echo OK\n"
            "Alternatively set WCB_CC_CREDS_PATH to a credentials JSON file, or "
            "CLAUDE_CODE_CREDENTIALS to inline JSON."
        )
    return (
        "No Claude Code credentials found. Set WCB_CC_CREDS_PATH to a "
        "credentials JSON file, or CLAUDE_CODE_CREDENTIALS to inline JSON."
    )


def _credentials_file_path() -> Path:
    env_path = os.environ.get("WCB_CC_CREDS_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".claude" / ".credentials.json"


def load_credentials() -> OAuthCredentials:
    """Load the freshest credentials available.

    ``CLAUDE_CODE_CREDENTIALS`` (inline JSON) wins outright.  Otherwise every
    on-disk / keychain source is read and the one with the LATEST
    ``expiresAt`` is used.  Anthropic rotates the refresh token on every
    refresh, so "newest expiry" is the same thing as "the only refresh token
    that still works"; a fixed source order used to pick the stale file over
    the bridge's own refresh cache and 401 the next run.
    """
    inline = _read_inline_env()
    if inline:
        try:
            return OAuthCredentials.from_claude_payload(json.loads(inline))
        except json.JSONDecodeError as e:
            raise CredentialsError(f"CLAUDE_CODE_CREDENTIALS is not valid JSON: {e}") from e

    # An explicit WCB_CC_CREDS_PATH is an operator decision (which account to
    # spend): it is used exclusively, and refreshes are written back to it.
    override = os.environ.get("WCB_CC_CREDS_PATH")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise CredentialsError(f"WCB_CC_CREDS_PATH={override} does not exist")
        try:
            return OAuthCredentials.from_claude_payload(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise CredentialsError(f"WCB_CC_CREDS_PATH={override} unreadable: {e}") from e

    candidates: list[tuple[str, OAuthCredentials]] = []
    for name, reader in (
        ("file", _read_credentials_file),
        ("keychain", _read_keychain_macos),
        ("secret-service", _read_secretservice_linux),
        ("cache", _read_cache_file),
    ):
        raw = reader()
        if not raw:
            continue
        try:
            candidates.append((name, OAuthCredentials.from_claude_payload(json.loads(raw))))
        except (json.JSONDecodeError, CredentialsError) as e:
            _LOG.warning("credential source %s unusable: %s", name, e)
    if not candidates:
        raise CredentialsError(_no_credentials_hint())
    name, creds = max(candidates, key=lambda nc: nc[1].expires_at_ms)
    _LOG.info("credentials loaded from %s (expires in %ds)", name,
              int(creds.expires_at_ms / 1000 - time.time()))
    return creds


def refresh_credentials(
    creds: OAuthCredentials,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
) -> OAuthCredentials:
    """Exchange ``refresh_token`` for a new ``access_token`` (and rotated refresh).

    Anthropic returns ``{access_token, refresh_token, expires_in, ...}`` --
    ``refresh_token`` is rotated on every call, so if we don't write the new
    one back somewhere durable the next refresh will 401.

    Retries up to ``max_attempts`` on transient network errors and on 5xx
    responses. A 4xx response (typically 401 = refresh_token revoked) is
    raised immediately -- retrying won't help.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(
                    REFRESH_ENDPOINT,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": creds.refresh_token,
                        "client_id": CLAUDE_CODE_CLIENT_ID,
                    },
                    headers={"content-type": "application/json"},
                )
        except (httpx.HTTPError, OSError) as e:
            last_error = e
            if attempt >= max_attempts:
                raise TransientCredentialsError(
                    f"OAuth refresh network error after {attempt} attempts: {e}"
                ) from e
            sleep_s = backoff_base * (2 ** (attempt - 1))
            _LOG.warning(
                "OAuth refresh attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, max_attempts, e, sleep_s,
            )
            time.sleep(sleep_s)
            continue

        if r.status_code == 200:
            break
        if 400 <= r.status_code < 500:
            raise CredentialsError(
                f"OAuth refresh failed (non-retryable): HTTP {r.status_code} {r.text[:200]}"
            )
        last_error = TransientCredentialsError(
            f"OAuth refresh failed: HTTP {r.status_code} {r.text[:200]}"
        )
        if attempt >= max_attempts:
            raise last_error
        sleep_s = backoff_base * (2 ** (attempt - 1))
        _LOG.warning(
            "OAuth refresh attempt %d/%d got HTTP %d; retrying in %.1fs",
            attempt, max_attempts, r.status_code, sleep_s,
        )
        time.sleep(sleep_s)
    else:  # pragma: no cover - exhausted loop with no break
        raise CredentialsError(
            f"OAuth refresh failed after {max_attempts} attempts: {last_error}"
        )

    try:
        body = r.json()
    except ValueError as e:
        raise CredentialsError(f"OAuth refresh returned non-JSON: {e}") from e

    access_token = body.get("access_token")
    if not access_token:
        raise CredentialsError(f"OAuth refresh missing access_token: {body}")
    refresh_token = body.get("refresh_token") or creds.refresh_token
    expires_in = int(body.get("expires_in", 3600))
    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=int(time.time() * 1000) + expires_in * 1000,
        scopes=creds.scopes,
        subscription_type=creds.subscription_type,
    )


def _atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600 from the first byte.

    A temp file is created 0600 in the same directory and renamed over the
    target, so there is never a window where the token is world-readable or
    the file is half-written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_cache(creds: OAuthCredentials) -> None:
    _atomic_write_private(_CACHE_PATH, json.dumps(creds.to_claude_payload()))


def persist_refreshed(creds: OAuthCredentials) -> None:
    """Write refreshed credentials everywhere the CLI and the bridge read.

    The refresh token rotates on every refresh, so whichever copy is not
    updated is dead.  Cache first (always), then the credentials file if it
    exists (preserving unrelated top-level keys), then the macOS keychain
    entry if present.  Each step is best-effort and logged.
    """
    try:
        write_cache(creds)
    except OSError as e:
        _LOG.warning("Could not persist refreshed creds to cache: %s", e)

    path = _credentials_file_path()
    if path.is_file():
        try:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, json.JSONDecodeError):
                existing = {}
            existing.update(creds.to_claude_payload())
            _atomic_write_private(path, json.dumps(existing))
            _LOG.info("refreshed credentials written back to %s", path)
        except OSError as e:
            _LOG.warning("Could not write refreshed creds to %s: %s", path, e)

    if platform.system() == "Darwin":
        _write_keychain_macos(creds)


def _write_keychain_macos(creds: OAuthCredentials) -> None:
    """Update the CLI's keychain item in place (best-effort)."""
    try:
        probe = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0:
            return  # no keychain item: nothing to keep in sync
        m = re.search(r'"acct"<blob>="([^"]*)"', probe.stdout)
        account = m.group(1) if m else os.environ.get("USER", "")
        # Preserve any other keys the CLI keeps in the same blob.
        existing = {}
        try:
            raw = _read_keychain_macos()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    existing = parsed
        except (json.JSONDecodeError, TypeError):
            existing = {}
        existing.update(creds.to_claude_payload())
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account,
             "-s", _KEYCHAIN_SERVICE, "-w", json.dumps(existing)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            _LOG.info("refreshed credentials written back to macOS keychain")
        else:
            _LOG.warning("keychain write-back failed: %s", r.stderr.strip()[:200])
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.warning("keychain write-back failed: %s", e)


class CredentialProvider:
    """Thread-safe lazy credential cache with auto-refresh.

    The bridge keeps one of these per process. Callers ask for an access
    token via ``get_access_token()``; the provider reads from disk/keychain
    on first call, then refreshes proactively whenever the token is within
    ``REFRESH_LEEWAY_SECONDS`` of expiry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: Optional[OAuthCredentials] = None

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = load_credentials()
            if self._creds.is_expired():
                _LOG.info("Refreshing Claude Code OAuth token")
                self._creds = refresh_credentials(self._creds)
                persist_refreshed(self._creds)
            return self._creds.access_token

    def peek(self) -> OAuthCredentials:
        """Load credentials WITHOUT refreshing (for --check / preflight)."""
        with self._lock:
            if self._creds is None:
                self._creds = load_credentials()
            return self._creds

    def force_reload(self, token_used: Optional[str] = None) -> None:
        """Drop the in-memory credentials so the next call re-reads disk.

        When ``token_used`` is given, only reload if it is still the current
        token: a 401 for a token that has already been replaced by a refresh
        must not throw the fresh token away.
        """
        with self._lock:
            if token_used is not None and self._creds is not None \
                    and self._creds.access_token != token_used:
                _LOG.info("ignoring stale 401: token already rotated")
                return
            self._creds = None



# ----------------------------------------------------------------------------
# Multi-account pool support
# ----------------------------------------------------------------------------


class _FileCredentialProvider(CredentialProvider):
    """CredentialProvider that always loads from a specific file path."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path).expanduser()

    def _load(self) -> OAuthCredentials:
        if not self._path.is_file():
            raise CredentialsError(f"credentials file not found: {self._path}")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise CredentialsError(f"could not read {self._path}: {e}") from e
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in {self._path}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()
            if not self._creds.is_expired():
                return self._creds.access_token
            # Cross-process serialization: only one bridge process should hit
            # the refresh endpoint; others wait, then re-read the rotated
            # token. Without this, concurrent harness runs sharing the same
            # pool file race on refresh and lose tokens (last-writer-wins).
            import fcntl
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_fh:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                except OSError as e:
                    _LOG.warning("flock failed on %s: %s; proceeding unlocked", lock_path, e)
                # Re-load -- another process may have refreshed while we waited.
                try:
                    fresh = self._load()
                    if not fresh.is_expired():
                        self._creds = fresh
                        return self._creds.access_token
                except CredentialsError:
                    pass
                _LOG.info("Refreshing OAuth token from %s", self._path)
                self._creds = refresh_credentials(self._creds)
                try:
                    _atomic_write_private(self._path, json.dumps(self._creds.to_claude_payload()))
                except OSError as e:
                    _LOG.warning("Could not persist refreshed creds to %s: %s", self._path, e)
            return self._creds.access_token

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None


class _KeychainCredentialProvider(CredentialProvider):
    """CredentialProvider that loads from a specific macOS Keychain service."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def _load(self) -> OAuthCredentials:
        if platform.system() != "Darwin":
            raise CredentialsError("Keychain accounts only supported on macOS")
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", self._service, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise CredentialsError(f"keychain read failed for {self._service}: {e}") from e
        if r.returncode != 0 or not r.stdout.strip():
            raise CredentialsError(
                f"no keychain entry for service {self._service!r}: {r.stderr[:200]}"
            )
        try:
            payload = json.loads(r.stdout.strip())
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in keychain {self._service}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()
            if self._creds.is_expired():
                _LOG.info("Refreshing OAuth token from keychain %s", self._service)
                self._creds = refresh_credentials(self._creds)
                persist_refreshed(self._creds)
            return self._creds.access_token

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None


@dataclass
class _AccountSlot:
    provider: CredentialProvider
    label: str
    exhausted_until: float = 0.0
    invalid: bool = False

    def is_available(self, now: Optional[float] = None) -> bool:
        if self.invalid:
            return False
        now = now if now is not None else time.time()
        return now >= self.exhausted_until


class MultiAccountCredentialProvider:
    """Pool of ``CredentialProvider``s with rotation, exhaustion tracking, and failover.

    Drop-in replacement for ``CredentialProvider`` at the bridge layer: exposes
    ``get_access_token() -> str`` and ``force_reload()``. The bridge calls
    ``mark_account_exhausted`` / ``mark_account_invalid`` to record state from
    upstream classification (see ``src.utils.claude_oauth.errors``).

    Selection policy: first available slot in insertion order. This makes the
    behavior predictable and lets a user put their "primary" account first.
    """

    def __init__(self, slots: list[_AccountSlot]) -> None:
        if not slots:
            raise CredentialsError("MultiAccountCredentialProvider needs >= 1 slot")
        self._slots = slots
        self._lock = threading.Lock()
        self._last_used_index: int = 0

    def get_access_token(self) -> str:
        with self._lock:
            slot, idx = self._select_slot_locked()
            self._last_used_index = idx
        try:
            return slot.provider.get_access_token()
        except TransientCredentialsError:
            # Network blip / upstream 5xx: the slot is still good; surface the
            # error instead of retiring the account for the rest of the run.
            raise
        except CredentialsError:
            with self._lock:
                slot.invalid = True
            return self.get_access_token()

    def _select_slot_locked(self) -> tuple[_AccountSlot, int]:
        now = time.time()
        for idx, slot in enumerate(self._slots):
            if slot.is_available(now):
                return slot, idx
        # All accounts exhausted/invalid -- raise with earliest reset hint.
        soonest = min(
            (s.exhausted_until for s in self._slots if not s.invalid),
            default=0.0,
        )
        delta = max(0.0, soonest - now)
        raise CredentialsError(
            f"all {len(self._slots)} accounts exhausted; soonest reset in {delta:.0f}s"
        )

    def force_reload(self) -> None:
        with self._lock:
            for slot in self._slots:
                slot.provider.force_reload()
                slot.exhausted_until = 0.0
                slot.invalid = False

    def mark_account_exhausted(self, token_prefix: str, until_unix: float) -> None:
        with self._lock:
            slot = self._find_slot_by_prefix_locked(token_prefix)
            if slot is None:
                return
            slot.exhausted_until = max(slot.exhausted_until, until_unix)
            _LOG.info(
                "account %s marked exhausted until %s (in %.0fs)",
                slot.label, until_unix, max(0.0, until_unix - time.time()),
            )

    def mark_account_invalid(self, token_prefix: str) -> None:
        with self._lock:
            slot = self._find_slot_by_prefix_locked(token_prefix)
            if slot is None:
                return
            slot.invalid = True
            _LOG.warning("account %s marked invalid (will not be retried)", slot.label)

    def mark_current_exhausted(self, until_unix: float) -> None:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                slot = self._slots[self._last_used_index]
                slot.exhausted_until = max(slot.exhausted_until, until_unix)
                _LOG.info(
                    "account %s marked exhausted until %s (in %.0fs)",
                    slot.label, until_unix, max(0.0, until_unix - time.time()),
                )

    def mark_current_invalid(self) -> None:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                slot = self._slots[self._last_used_index]
                slot.invalid = True
                _LOG.warning("account %s marked invalid", slot.label)

    def next_reset_at(self) -> Optional[float]:
        """Soonest Unix-time at which any exhausted account becomes available.

        Returns ``None`` if at least one account is currently available.
        """
        with self._lock:
            now = time.time()
            if any(s.is_available(now) for s in self._slots):
                return None
            future = [s.exhausted_until for s in self._slots if not s.invalid]
            return min(future) if future else None

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "label": s.label,
                    "token_prefix": getattr(s.provider, "token_prefix", lambda: None)(),
                    "invalid": s.invalid,
                    "exhausted_until": s.exhausted_until,
                    "available": s.is_available(),
                }
                for s in self._slots
            ]

    def _find_slot_by_prefix_locked(self, token_prefix: str) -> Optional[_AccountSlot]:
        for slot in self._slots:
            if not hasattr(slot.provider, "token_prefix"):
                continue
            sp = getattr(slot.provider, "token_prefix", lambda: None)()
            if sp and (sp.startswith(token_prefix) or token_prefix.startswith(sp)):
                return slot
        return None


def _add_token_prefix_to_provider(p: CredentialProvider) -> CredentialProvider:
    """Monkey-patch a single-account ``CredentialProvider`` so it exposes ``token_prefix()``."""
    if hasattr(p, "token_prefix"):
        return p

    def _tp(self: CredentialProvider) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None

    p.token_prefix = _tp.__get__(p, CredentialProvider)  # type: ignore[attr-defined]
    return p


def load_account_pool(spec: str) -> Optional[MultiAccountCredentialProvider]:
    """Parse a ``WCB_CC_ACCOUNT_POOL`` spec into a multi-account provider.

    Spec format: colon-separated entries, each one of:
      - A file path (absolute or ``~``-relative) -> ``_FileCredentialProvider``
      - ``keychain:<service-name>``               -> ``_KeychainCredentialProvider``
      - ``default``                               -> default ``CredentialProvider``
                                                     (Keychain -> ~/.claude/.credentials.json -> cache)

    Empty entries are skipped. Returns ``None`` if the spec yields no slots.
    """
    if not spec:
        return None
    slots: list[_AccountSlot] = []
    # "," is the unambiguous separator; ":" is still accepted for old specs,
    # re-joining the "keychain:<service>" form that a bare split used to break
    # into two bogus file slots.
    if "," in spec:
        entries = spec.split(",")
    else:
        entries = []
        toks = spec.split(":")
        i = 0
        while i < len(toks):
            if toks[i].strip() == "keychain" and i + 1 < len(toks):
                entries.append(f"keychain:{toks[i + 1]}")
                i += 2
            else:
                entries.append(toks[i])
                i += 1
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if entry == "default":
            slots.append(_AccountSlot(
                provider=_add_token_prefix_to_provider(CredentialProvider()),
                label="default",
            ))
            continue
        if entry.startswith("keychain:"):
            service = entry[len("keychain:"):]
            slots.append(_AccountSlot(
                provider=_KeychainCredentialProvider(service),
                label=f"keychain:{service}",
            ))
            continue
        # Treat as file path.
        slots.append(_AccountSlot(
            provider=_FileCredentialProvider(Path(entry)),
            label=f"file:{entry}",
        ))
    if not slots:
        return None
    return MultiAccountCredentialProvider(slots)