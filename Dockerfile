# Harness runner image source.
#
# trinity/FORGE.md:184 requires "Bind its image digest into the Phase 4.5
# release record". This Dockerfile is the source; the release-bound digest is
# produced with:
#
#     docker build -t kakashi-harness:latest .
#     docker inspect kakashi-harness:latest --format '{{ index .RepoDigests 0 }}'
#
# and recorded in harness-config.json under harness_image_digest, then mirrored
# into the parent-root <parent>/.seed/harness-config.json and folded into the
# pilot block's harness_config_digest per trinity/FORGE.md:298.
#
# Retained-finding closures embedded below:
#   - hadolint DL3013: uv is version-pinned rather than installed unpinned.
#   - hadolint SC2015: the uv.lock branch is an explicit if/then/fi block
#     rather than the A && B || C form that silently swallows B's failure.
#   - trivy DS-0002: the entrypoint runs as a dedicated non-root user
#     ``harness`` (uid/gid 1000). Callers that need Docker socket access must
#     pass ``--group-add <host-docker-gid>`` at ``docker run`` time; the
#     runner never chowns or mounts the socket itself.
#   - trivy DS-0026: a HEALTHCHECK probe verifies the runner module is
#     importable without touching the network.

FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

RUN groupadd --system --gid 1000 harness \
 && useradd --system --uid 1000 --gid harness --home /harness --shell /usr/sbin/nologin harness

WORKDIR /harness

COPY pyproject.toml ./
COPY uv.lock* ./
RUN pip install --no-cache-dir uv==0.5.11 \
 && if [ -f uv.lock ]; then \
        uv pip install --system --no-cache -r uv.lock; \
    else \
        uv pip install --system --no-cache -r pyproject.toml; \
    fi

COPY --chown=harness:harness . /harness

USER harness

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('run_harbor') else 1)" || exit 1

ENTRYPOINT ["python3", "run_harbor.py"]
