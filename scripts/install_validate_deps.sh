#!/bin/bash
# Creates /scripts/.venv (Python 3 venv with tomli) for the in-container
# self-test (validate.py).  This is the same form every shipped task uses and
# the only form the QC gate accepts (QC-06 rejects uv).  It fails loudly: the
# instruction hands the agent /scripts/.venv/bin/python, so a silent miss
# here would kill the self-test loop the methodology depends on.
set -euo pipefail

apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip

VENV="/scripts/.venv"
mkdir -p /scripts
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --no-cache-dir tomli
"$VENV/bin/python" -c "import tomli" || { echo "tomli not importable in $VENV" >&2; exit 1; }
