#!/usr/bin/env bash
# Host-side deps for the Claude Code subscription bridge (scripts/claude_oauth).
#
# The bridge runs on the HOST (not in the task container) using the same Python
# that runs run_agent.py, so install these into that interpreter. httpx is
# usually already present; fastapi + uvicorn power the Anthropic-compatible
# proxy server (scripts/claude_oauth/bridge.py + __main__.py).
set -eux

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m pip install --upgrade "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27"

echo "Bridge deps installed. Verify Claude Code subscription creds with:"
echo "  $PYTHON -m claude_oauth --check   # run from the scripts/ dir"
