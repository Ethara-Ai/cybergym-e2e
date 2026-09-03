"""API-key lookup for the GLM bridge.

Z.ai keys are plain bearer tokens (no OAuth refresh dance), so this is small on
purpose: read the key once from the environment or a file, hand it to the
bridge, never write it anywhere.

Lookup order (first non-empty wins):

    ZAI_API_KEY, GLM_API_KEY, ZHIPU_API_KEY      environment
    GLM_API_KEY_FILE                             path to a file holding the key
    ~/.config/zai/api_key                        default key file
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

KEY_ENV_VARS = ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY")
KEY_FILE_ENV = "GLM_API_KEY_FILE"
DEFAULT_KEY_FILE = Path.home() / ".config" / "zai" / "api_key"

# Values that are obviously not a real key (the .env ships ``dummy`` for the
# Claude bridge's stub; make sure that can never be forwarded as a GLM key).
_PLACEHOLDERS = {"", "dummy", "stub", "changeme", "your-key", "your_api_key", "<key>"}


class CredentialsError(RuntimeError):
    """No usable GLM API key could be found."""


def _read_key_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def load_api_key() -> str:
    """Return the GLM API key or raise ``CredentialsError`` with a fix-it hint."""
    for name in KEY_ENV_VARS:
        val = os.environ.get(name, "").strip()
        if val and val.lower() not in _PLACEHOLDERS:
            return val
    file_env = os.environ.get(KEY_FILE_ENV, "").strip()
    candidates = [Path(file_env).expanduser()] if file_env else []
    candidates.append(DEFAULT_KEY_FILE)
    for path in candidates:
        val = _read_key_file(path)
        if val and val.lower() not in _PLACEHOLDERS:
            return val
    raise CredentialsError(
        "no GLM API key: set ZAI_API_KEY (or GLM_API_KEY / ZHIPU_API_KEY) in the "
        f"environment or .env, point {KEY_FILE_ENV} at a key file, or write the key "
        f"to {DEFAULT_KEY_FILE}"
    )


def key_prefix(key: str, n: int = 8) -> str:
    """Redacted form for logs: first ``n`` chars + '...'."""
    return key[:n] + "..." if key else ""


class CredentialProvider:
    """Caches the key so a missing file is reported once, at startup."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._key: Optional[str] = api_key.strip() if api_key else None

    def get_api_key(self) -> str:
        if not self._key:
            self._key = load_api_key()
        return self._key

    @property
    def prefix(self) -> str:
        return key_prefix(self.get_api_key())
