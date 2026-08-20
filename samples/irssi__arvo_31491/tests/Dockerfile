# Harbor VERIFIER environment for CyberGym-E2E task irssi__arvo_31491
# Mirrors the agent environment (same base, source, build tools) and adds
# verifier test scripts at /tests/ (Harbor convention) and /verifier/
# (test.sh hardcoded paths).
FROM gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc

# Build tooling (same as agent Dockerfile + prepare.sh deps for verifier)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev git patch sudo curl \
    pkg-config libncurses5-dev libssl-dev python3-pip libglib2.0-dev \
    2>/dev/null || true
# Upgrade pip first (base image has pip 19.2.3 which can't parse modern pyproject.toml)
RUN pip3 install --no-cache-dir --upgrade pip 2>/dev/null || true
RUN pip3 install --no-cache-dir tomli tomli_w meson ninja 2>/dev/null || true

# Install uv + Python 3.13 venv for validate.py
COPY install_validate_deps.sh /tmp/install_validate_deps.sh
RUN bash /tmp/install_validate_deps.sh && rm /tmp/install_validate_deps.sh

# Self-test validate.py
COPY validate.py /scripts/validate.py

# Vulnerable source snapshot -> /src
COPY src.tgz /tmp/src.tgz
RUN mkdir -p /src && tar xf /tmp/src.tgz -C /src && rm /tmp/src.tgz

# Build / run / test scripts -> /src
COPY scripts/ /src/
# Sanitized task config -> /config
COPY config/ /config/

# SRC_RESET: remove pre-baked binaries and PoCs from ARVO base images
RUN rm -rf /out /work /bin/arvo /usr/bin/arvo /tmp/poc && mkdir -p /out /work

RUN mkdir -p /output /logs/verifier /data /scripts && chmod +x /src/*.sh 2>/dev/null || true

# Verifier test scripts: /tests/ for Harbor, /verifier/ for test.sh paths
COPY test.sh test_output.py validate.py test_weights.json /tests/
COPY data/ /tests/data/
COPY test.sh test_output.py validate.py test_weights.json /verifier/
COPY data/ /verifier/data/

WORKDIR /src
