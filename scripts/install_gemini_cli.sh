#!/bin/bash
# Fail fast: a failed install must abort so a broken image is never shipped
# (pipefail catches a failing `curl ... | bash`).
set -eo pipefail

# adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/gemini_cli/gemini-cli-setup.sh.j2

apt-get update
apt-get install -y curl

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash

# nvm.sh is a third-party script; sourcing it under `set -e` can abort on a
# benign non-zero return, so relax errexit only across the source.
set +e
source "$HOME/.nvm/nvm.sh"
set -e

nvm install 22
npm -v

npm install -g @google/gemini-cli@0.37.1

mkdir -p "$HOME/.gemini"
