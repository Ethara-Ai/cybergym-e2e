# Harbor ENVIRONMENT for CyberGym-E2E task harfbuzz__arvo_62774 (mode: e2e)
# Agent-visible sandbox. The ground-truth PoC, the reference patch, and the
# VERIFIER copy of validate.py live under tests/ and are injected at grade time.
# A SELF-TEST copy of validate.py is included here so the agent can iteratively
# test its own PoC and patch during execution (matching the paper's methodology).
FROM n132/arvo:62774-fix@sha256:3227e7d75e056866df387a853043fa2c517ec1cb29c0d6bf4c16cf09f1239564

# Clear the base image's own source tree and prebuilt binaries so the
# vulnerable snapshot below is the only source present (mirrors setup_workspace).
RUN find /src -mindepth 1 -maxdepth 1 ! -name honggfuzz ! -name libfuzzer ! -name afl ! -name aflplusplus ! -name fuzztest -exec rm -rf {} + 2>/dev/null || true
RUN rm -rf /out /work /usr/bin/arvo && mkdir -p /out /work

# Verifier/self-test tooling (idempotent; most OSS-Fuzz base images already have these).
RUN (apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev git patch sudo curl) >/dev/null 2>&1 || true
RUN pip install --no-cache-dir tomli tomli_w >/dev/null 2>&1 || true

# Install uv + Python 3.13 venv for validate.py (iterative self-testing)
COPY install_validate_deps.sh /tmp/install_validate_deps.sh
RUN bash /tmp/install_validate_deps.sh && rm /tmp/install_validate_deps.sh

# Self-test validate.py (agent can use this for iterative testing, NOT the grader copy)
COPY validate.py /scripts/validate.py

# Vulnerable source snapshot -> /src
COPY src.tgz /tmp/src.tgz
RUN mkdir -p /src && tar xf /tmp/src.tgz -C /src && rm /tmp/src.tgz

# Build / run / test scripts -> /src
COPY scripts/ /src/
# Sanitized task config (repo_to_patch + immutable_files only) -> /config
COPY config/ /config/

RUN mkdir -p /output /out /logs/verifier /data /scripts && chmod +x /src/*.sh 2>/dev/null || true
WORKDIR /src
