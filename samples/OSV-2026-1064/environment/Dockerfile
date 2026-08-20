# Harbor ENVIRONMENT for CyberGym-E2E task freerdp__OSV-2026-1064 (mode: e2e)
# Agent-visible sandbox. The ground-truth PoC, the reference patch, and the
# VERIFIER copy of validate.py live under tests/ and are injected at grade time.
# A SELF-TEST copy of validate.py is included here so the agent can iteratively
# test its own PoC and patch during execution (matching the paper's methodology).
FROM gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc

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
