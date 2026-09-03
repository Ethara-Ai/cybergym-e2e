"""GLM (Z.ai / Zhipu) bridge for the Harbor runner.

Third host-side bridge next to ``claude_oauth`` (Claude subscription) and
``codex_oauth`` (ChatGPT subscription).  It exposes an Anthropic-compatible
``/v1/messages`` proxy on the host so the in-container Claude Code CLI can drive
a GLM model exactly as it drives Claude: the container only ever sees the
bridge address and a per-run stub key, the real ``ZAI_API_KEY`` stays on the
host, and the existing one-port network lockdown applies unchanged.

Upstream is Z.ai's Anthropic-compatible endpoint (``https://api.z.ai/api/anthropic``,
the one the GLM Coding Plan documents for Claude Code).  Model names the CLI
sends for Claude (``claude-*``) are rewritten to the configured GLM ids
(``GLM_MODEL_ID`` / ``GLM_SMALL_MODEL_ID``) before forwarding.
"""

from .credentials import CredentialProvider, CredentialsError, load_api_key
from .launcher import GLMBridge

__all__ = [
    "CredentialProvider",
    "CredentialsError",
    "GLMBridge",
    "load_api_key",
]
