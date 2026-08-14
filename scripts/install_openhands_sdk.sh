#!/bin/bash
# Install the OpenHands Software Agent SDK (lighter alternative to full openhands-ai).
# Expects the SDK source to have been copied to /opt/software-agent-sdk/ by the caller.

set -eu

apt-get update -qq
apt-get install -y -qq curl git tmux build-essential >/dev/null 2>&1

# Install uv
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
source "$HOME/.local/bin/env" 2>/dev/null || true

uv python install 3.13

SDK_VENV="/opt/sdk-venv"
uv venv "$SDK_VENV" --python 3.13
source "$SDK_VENV/bin/activate"

# Install core SDK (all deps are lightweight)
uv pip install /opt/software-agent-sdk/openhands-sdk/

# Install tools without heavy optional deps (browser-use, tom-swe)
uv pip install --no-deps /opt/software-agent-sdk/openhands-tools/
uv pip install bashlex 'libtmux>=0.53.0' 'pydantic>=2.11.7' cachetools 'func-timeout>=4.3.5' 'binaryornot>=0.4.4' boto3

# Copy the runner script into the venv for easy invocation
cp /opt/software-agent-sdk/run_sdk_agent.py /opt/run_sdk_agent.py

echo "OpenHands SDK v$(python -c 'from openhands.sdk import __version__; print(__version__)' 2>/dev/null || echo '?') installed"
