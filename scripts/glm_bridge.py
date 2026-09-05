"""Lifecycle owner for the vendored zbridge subprocess (GLM on a Z.ai Coding Plan).

Deliberately mirrors ``claude_oauth.launcher.ClaudeOAuthBridge`` -- same
surface (``start``/``stop``/``base_url``/``container_base_url``/``stub_api_key``
/``preflight``), same log-to-file discipline, same readiness contract -- so
run_harbor treats the two bridges identically and the container-side lockdown
("bridge" mode, keyed on host.docker.internal in ANTHROPIC_BASE_URL) needs no
special case.

What differs is what sits behind it.  ClaudeOAuthBridge attaches a Claude OAuth
token and forwards to api.anthropic.com; this launches ``python -m zbridge``,
which translates every /v1/messages call into z.ai's OpenAI-compat GLM Coding
Plan schema and translates the answer -- SSE, tool calls, thinking blocks and
token accounting -- back.

The z.ai key reaches the child through its environment only: it never enters
this process's os.environ and never reaches the agent container.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONTAINER_HOST = "host.docker.internal"
# Not 8766 (zbridge's own documented standalone port) and not 8765/8788 (the
# claude / codex bridges): a hand-started zbridge or other z.ai tooling on the
# machine must not be able to collide with a run that is already going.
DEFAULT_PORT = 8820
BIND_ATTEMPTS = 4
STARTUP_TIMEOUT_SEC = 45.0
# zbridge's standalone defaults (180s non-stream, 600s stream, 30s connect) are
# raised for the same reason the task budgets are: a GLM-5.3 step at high
# reasoning effort can outlast them, and a connection cut mid-answer reaches the
# agent as an API error rather than as a slow model.  Lowest precedence -- the
# real environment still wins.
CHILD_DEFAULTS = {
    "ZB_READ_TIMEOUT_NONSTREAM_S": "600",
    "ZB_READ_TIMEOUT_STREAM_S": "1800",
    "ZB_CONNECT_TIMEOUT_S": "60",
}


def _scripts_dir() -> Path:
    """The dir holding the vendored ``zbridge`` package, so ``-m zbridge``
    resolves imports in the child."""
    return Path(__file__).resolve().parent


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


class GlmBridge:
    """Owns one zbridge subprocess for the lifetime of a run."""

    def __init__(self, zai_key, *, port=None, host=None, log_dir=None,
                 startup_timeout=STARTUP_TIMEOUT_SEC, log_level="warning"):
        if not zai_key:
            raise RuntimeError("GlmBridge needs a z.ai key")
        self._zai_key = zai_key
        # Loopback by default (Docker Desktop proxies host.docker.internal to
        # the host loopback).  Linux-native hosts reach the host via the docker0
        # gateway and need 0.0.0.0 -- same override the claude bridge uses.
        self.host = host or os.environ.get("GOKU_CC_BRIDGE_BIND", "127.0.0.1")
        self.container_host = DEFAULT_CONTAINER_HOST
        self.startup_timeout = startup_timeout
        self.log_level = log_level
        self.port = None
        self._requested_port = port
        # Shaped like a real Anthropic key: clients that validate the key format
        # client-side otherwise decline a custom base URL and quietly call
        # api.anthropic.com instead.  zbridge compares the whole string.
        self.bridge_secret = f"sk-ant-api03-zbridge-{secrets.token_hex(24)}"
        self._proc = None
        self._log_fh = None
        log_dir = Path(log_dir or os.environ.get(
            "WCB_CC_BRIDGE_LOG_DIR",
            Path.home() / ".cache" / "wildclawbench" / "bridge-logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"zbridge-{os.getpid()}.log"

    # -- surface shared with ClaudeOAuthBridge ---------------------------- #

    @property
    def stub_api_key(self):
        """What the in-container CLI presents; zbridge accepts it as x-api-key
        or as an Authorization bearer, so ANTHROPIC_AUTH_TOKEN works."""
        return self.bridge_secret

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}" if self.port else "(not started)"

    @property
    def container_base_url(self):
        return f"http://{self.container_host}:{self.port}"

    # -- lifecycle -------------------------------------------------------- #

    def _child_env(self):
        # Precedence, lowest first: raised timeout defaults, the real
        # environment, then the two credentials -- which nothing may override.
        return {
            **CHILD_DEFAULTS,
            **os.environ,
            "ZB_ZAI_API_KEY": self._zai_key,
            "ZB_BRIDGE_SECRET": self.bridge_secret,
            "PYTHONUNBUFFERED": "1",
        }

    def preflight(self):
        """Prove the child can load its config and credential before a run
        starts, so a bad key fails here rather than inside every task."""
        result = subprocess.run(
            [sys.executable, "-m", "zbridge", "--check"],
            cwd=str(_scripts_dir()), capture_output=True, text=True,
            timeout=60, env=self._child_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "zbridge could not start with the configured z.ai credential.\n"
                f"zbridge --check said:\n"
                f"{(result.stderr or result.stdout).strip()}")

    def start(self):
        self.preflight()
        base = self._requested_port or DEFAULT_PORT
        taken = []
        for attempt in range(BIND_ATTEMPTS):
            port = base + attempt
            if not _port_is_free(self.host, port):
                taken.append(port)
                continue
            self._spawn(port)
            try:
                self._await_ready()
            except _PortTaken:
                taken.append(port)
                self.stop()
                continue
            except Exception:
                self.stop()
                raise
            return self
        raise RuntimeError(
            f"zbridge could not claim a port -- {', '.join(str(p) for p in taken)} "
            "are held by another process.  Stop it, or run with --glm-direct.")

    def _spawn(self, port):
        self._log_fh = open(self.log_path, "ab", buffering=0)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "zbridge",
             "--host", self.host, "--port", str(port),
             "--log-level", self.log_level],
            cwd=str(_scripts_dir()), env=self._child_env(),
            stdout=self._log_fh, stderr=subprocess.STDOUT, text=True,
        )
        self.port = port

    def _log_tail(self, max_bytes=4000):
        try:
            return self.log_path.read_bytes()[-max_bytes:].decode("utf-8", "replace")
        except OSError:
            return ""

    def _await_ready(self):
        """Block until *our* child is serving.

        Ports are contended, and a child that loses the bind exits while the
        *other* listener keeps answering /healthz on the same port.  Readiness
        therefore has to prove the listener is ours, or a squatted port would be
        silently adopted and every request would go somewhere unknown.
        """
        import httpx

        deadline = time.monotonic() + self.startup_timeout
        health = f"{self.base_url}/healthz"
        last = None
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                tail = self._log_tail()
                if "address already in use" in tail.lower():
                    raise _PortTaken(self.port)
                raise RuntimeError(
                    f"zbridge exited during startup (code {self._proc.returncode}):\n{tail}")
            try:
                r = httpx.get(health, timeout=2.0)
                if r.status_code == 200:
                    time.sleep(0.2)
                    if self._proc and self._proc.poll() is not None:
                        raise _PortTaken(self.port)
                    return
            except _PortTaken:
                raise
            except Exception as exc:      # connection refused while booting
                last = exc
            time.sleep(0.4)
        self.stop()
        raise RuntimeError(
            f"zbridge did not become healthy within {self.startup_timeout}s "
            f"(last error: {last})")

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
        if self._log_fh is not None:
            with contextlib.suppress(Exception):
                self._log_fh.close()
            self._log_fh = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class _PortTaken(RuntimeError):
    """Another process owns the port we tried to bind."""
