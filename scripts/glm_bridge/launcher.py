"""Host-side launcher for the GLM bridge (mirror of claude_oauth.launcher).

Starts ``python -m glm_bridge`` as a subprocess bound to ``127.0.0.1:<port>``,
waits for ``/healthz``, exposes ``container_base_url`` for the in-container CLI,
and tears the subprocess down on ``stop()`` / context exit::

    with GLMBridge() as bridge:
        os.environ["ANTHROPIC_BASE_URL"] = bridge.container_base_url
        os.environ["ANTHROPIC_API_KEY"] = bridge.stub_api_key
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Hostname a Docker Desktop / recent Docker Engine container uses to reach a
# service on the host loopback.  Same alias the Claude bridge uses.
DEFAULT_CONTAINER_HOST = os.environ.get("GLM_BRIDGE_HOST_ALIAS",
                                        os.environ.get("GOKU_CC_BRIDGE_HOST_ALIAS",
                                                       "host.docker.internal"))
SECRET_ENV = "KAKASHI_GLM_BRIDGE_SECRET"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scripts_dir() -> Path:
    """The dir containing the ``glm_bridge`` package (``scripts/``), so the
    ``-m glm_bridge`` subprocess resolves imports."""
    return Path(__file__).resolve().parents[1]


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
        return True
    except OSError:
        return False


class GLMBridge:
    """Context manager owning a GLM bridge subprocess for the lifetime of a run."""

    def __init__(self, *, port: int | None = None, host: str | None = None,
                 container_host: str = DEFAULT_CONTAINER_HOST,
                 startup_timeout: float = 45.0, log_level: str = "info",
                 bridge_secret: str | None = None) -> None:
        # Loopback by default (Docker Desktop proxies host.docker.internal to it).
        # Linux-native hosts reach the host via docker0 and need 0.0.0.0:
        # set GLM_BRIDGE_BIND (or the Claude bridge's GOKU_CC_BRIDGE_BIND).
        self.host = host or os.environ.get("GLM_BRIDGE_BIND") \
            or os.environ.get("GOKU_CC_BRIDGE_BIND", "127.0.0.1")
        self.port = port or _find_free_port()
        self.container_host = container_host
        self.startup_timeout = startup_timeout
        self.log_level = log_level
        self.bridge_secret = bridge_secret or os.environ.get(SECRET_ENV) \
            or secrets.token_urlsafe(24)
        self._proc: subprocess.Popen | None = None
        # Child output goes to a file, never a pipe nobody drains.
        log_dir = Path(os.environ.get("GLM_BRIDGE_LOG_DIR",
                                      os.environ.get("WCB_CC_BRIDGE_LOG_DIR",
                                                     Path.home() / ".cache" / "wildclawbench"
                                                     / "bridge-logs")))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"glm-bridge-{self.port}-{os.getpid()}.log"
        self._log_fh = None

    @property
    def stub_api_key(self) -> str:
        """What the in-container CLI presents as ANTHROPIC_API_KEY: the bridge
        secret.  The bridge authenticates it and substitutes the real key."""
        return self.bridge_secret

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def container_base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    def _subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env[SECRET_ENV] = self.bridge_secret
        return env

    def preflight(self) -> None:
        """Fail fast, with the bridge's own message, if no GLM key is available."""
        result = subprocess.run([sys.executable, "-m", "glm_bridge", "--check"],
                                cwd=str(_scripts_dir()), capture_output=True, text=True,
                                timeout=60, env=self._subprocess_env())
        if result.returncode != 0:
            raise RuntimeError(
                "GLM bridge preflight failed.\n"
                f"bridge --check output:\n{result.stderr.strip() or result.stdout.strip()}")
        logger.info("GLM bridge preflight OK: %s", result.stdout.strip())

    def _kill_stale_on_port(self) -> None:
        if not _port_open(self.host, self.port):
            return
        logger.warning("Port %d is already in use; killing stale process", self.port)
        try:
            result = subprocess.run(["lsof", "-ti", f"tcp:{self.port}"],
                                    capture_output=True, text=True, timeout=5)
            pids = [int(p) for p in result.stdout.split()]
        except (FileNotFoundError, subprocess.SubprocessError, ValueError) as e:
            raise RuntimeError(f"Port {self.port} is in use and the holder could not be "
                               f"identified (lsof unavailable: {e}). Pick another port.") from e
        for pid in pids:
            if pid != os.getpid():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, 15)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not _port_open(self.host, self.port, 0.5):
                return
            time.sleep(0.3)
        raise RuntimeError(f"Port {self.port} still in use after killing its holder")

    def start(self) -> "GLMBridge":
        self.preflight()
        self._kill_stale_on_port()
        logger.info("Starting GLM bridge on %s (container URL: %s)",
                    self.base_url, self.container_base_url)
        self._log_fh = open(self.log_path, "ab", buffering=0)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "glm_bridge", "--host", self.host, "--port", str(self.port),
             "--log-level", self.log_level, "--parent-pid", str(os.getpid())],
            cwd=str(_scripts_dir()), env=self._subprocess_env(),
            stdout=self._log_fh, stderr=subprocess.STDOUT, text=True)
        logger.info("Bridge log: %s", self.log_path)
        self._await_ready()
        return self

    def _log_tail(self, max_bytes: int = 4000) -> str:
        try:
            return self.log_path.read_bytes()[-max_bytes:].decode("utf-8", "replace")
        except OSError:
            return ""

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(f"GLM bridge exited during startup "
                                   f"(code {self._proc.returncode}):\n{self._log_tail()}")
            try:
                r = httpx.get(f"{self.base_url}/healthz", timeout=2.0)
                if r.status_code == 200:
                    time.sleep(0.2)
                    if self._proc and self._proc.poll() is not None:
                        raise RuntimeError("GLM bridge died after the health check passed "
                                           f"(stale bridge on port {self.port}?):\n{self._log_tail()}")
                    logger.info("GLM bridge ready at %s", self.base_url)
                    return
            except Exception as exc:  # noqa: BLE001 - connection refused while booting
                last_err = exc
            time.sleep(0.4)
        self.stop()
        raise RuntimeError(f"GLM bridge did not become healthy within "
                           f"{self.startup_timeout}s (last error: {last_err})")

    def stop(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=5)
                logger.info("GLM bridge stopped")
        finally:
            if self._log_fh is not None:
                with contextlib.suppress(Exception):
                    self._log_fh.close()
                self._log_fh = None

    def __enter__(self) -> "GLMBridge":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
