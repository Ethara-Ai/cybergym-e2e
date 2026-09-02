#!/bin/bash

# Install the OpenCode agent (https://github.com/sst/opencode) inside the task
# container. Mirrors the codex / gemini-cli install pattern (nvm + npm global).

apt-get update
apt-get install -y curl

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash

source "$HOME/.nvm/nvm.sh"

nvm install 22
npm -v

npm install -g opencode-ai@1.18.26

mkdir -p "$HOME/.config/opencode"
