# VENDORED from kanao-harness commit 53f73dc
#   (harness/vendor/harbor/src/harbor/agents/installed/openhands_sdk.py), which is Harbor
#   0.23.0's stock file plus that project's patch: pinned uv bootstrap, LLM_*
#   knobs (timeout, token caps, provider, model_info, completion logging) and
#   reasoning capture in the in-container runner.  Loaded by `harbor run -a
#   harbor_agents.openhands_sdk:OpenHandsSDK` on top of the unpatched PyPI
#   Harbor release pinned in harbor.lock.  Keep byte-identical to the source
#   apart from this header and the one guard marked "kakashi:" in
#   populate_context_post_run; re-vendor rather than hand-edit.
"""OpenHands SDK agent adapter for Harbor.

This adapter allows running the OpenHands Software Agent SDK inside
Harbor-managed containers for benchmarking and evaluation.
"""

import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, ClassVar, override

from platformdirs import user_cache_dir
from pydantic import Field

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import Env, InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName
from harbor.models.trial.paths import EnvironmentPaths


class OpenHandsSDKOptions(InstalledAgentOptions):
    reasoning_effort: str | None = Field(
        default=None, description="Model reasoning effort."
    )
    load_skills: bool = Field(default=True, description="Load skills from skill paths.")
    skill_paths: list[str] | None = Field(
        default=None, description="Custom skill paths to load."
    )
    collect_token_ids: bool = Field(
        default=False, description="Request token IDs from the LLM backend."
    )
    max_iterations: int | None = Field(
        default=None, description="Maximum agent iterations per run."
    )
    temperature: float | None = Field(
        default=None, description="LLM sampling temperature."
    )
    python_version: str = Field(
        default="3.12", description="Python version for the SDK venv."
    )
    uv_version: str = Field(
        default="0.11.11",
        description=(
            "uv release provisioned into the container from the host (see "
            "install). Pinned so every trial bootstraps the same way."
        ),
    )
    # Endpoint and model knobs the runner reads as LLM_* environment variables
    # (see openhands_sdk_runner.py). They exist so a model served through a
    # local proxy — one LiteLLM has no metadata for — can still be driven with
    # the right provider, output cap and call timeout.
    timeout: Annotated[int | None, Env("LLM_TIMEOUT")] = Field(
        default=None,
        description="Per-call LLM timeout in seconds (LiteLLM `timeout`).",
    )
    num_retries: Annotated[int | None, Env("LLM_NUM_RETRIES")] = Field(
        default=None,
        description=(
            "Retries on transient LLM errors (LiteLLM `num_retries`). A single "
            "5xx or DNS blip otherwise raises ConversationRunError and loses "
            "the whole run."
        ),
    )
    max_input_tokens: Annotated[int | None, Env("LLM_MAX_INPUT_TOKENS")] = Field(
        default=None,
        description="Context window the SDK should assume for the model.",
    )
    max_output_tokens: Annotated[int | None, Env("LLM_MAX_OUTPUT_TOKENS")] = Field(
        default=None,
        description="Maximum output tokens per completion (sent to the LLM).",
    )
    provider: Annotated[str | None, Env("LLM_PROVIDER")] = Field(
        default=None,
        description=(
            "LiteLLM provider for a model id that carries no `provider/` prefix, "
            "e.g. `anthropic` for a proxy that speaks the Anthropic wire format. "
            "The runner prefixes the model with it for routing only; the "
            "trajectory keeps the bare id."
        ),
    )
    model_info: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Metadata for `litellm.register_model()` when LiteLLM does not know "
            "the model (context window, supports_reasoning, costs). Without it "
            "LiteLLM drops reasoning_effort for the id and caps max_tokens at 4096."
        ),
    )
    log_completions: Annotated[bool | None, Env("LLM_LOG_COMPLETIONS")] = Field(
        default=None,
        description=(
            "Write every raw LiteLLM request/response under "
            "/logs/agent/completions/ (the SDK's log_completions)."
        ),
    )


class OpenHandsSDK(BaseInstalledAgent):
    """
    The OpenHands SDK agent uses the OpenHands Software Agent SDK to solve tasks.

    Unlike the full OpenHands (openhands-ai) which includes a Docker runtime,
    this adapter uses the lightweight SDK that runs directly in the container.
    """

    capabilities = AgentCapabilities(atif=True)
    options_model = OpenHandsSDKOptions
    options: OpenHandsSDKOptions

    _OUTPUT_FILENAME = "openhands_sdk.txt"
    _TRAJECTORY_FILENAME = "trajectory.json"
    _INSTRUCTION_FILENAME = "instruction.txt"

    # Where the bootstrap lands inside the container. uv's binary, the Python
    # it downloads and the SDK venv all live under /opt so a non-root agent
    # user can own them without touching $HOME.
    _UV_DIR = "/opt/uv"
    _UV_PYTHON_DIR = "/opt/uv-python"
    _VENV_DIR = "/opt/openhands-sdk-venv"
    _UV_RELEASE_URL = (
        "https://github.com/astral-sh/uv/releases/download/{version}/uv-{target}.tar.gz"
    )
    # `uname -m` → uv's target architecture.
    _UV_ARCHES: ClassVar[dict[str, str]] = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }

    DEFAULT_SKILL_PATHS = [
        "~/.openhands-sdk/skills",
        "~/.claude/skills",
        "~/.codex/skills",
        "~/.agents/skills",
        "~/.goose/skills",
        "~/.gemini/skills",
        "~/.factory/skills",
        "~/.opencode/skill",
    ]

    def __init__(
        self,
        reasoning_effort: str | None = None,
        load_skills: bool = True,
        skill_paths: list[str] | None = None,
        collect_token_ids: bool = False,
        max_iterations: int | None = None,
        temperature: float | None = None,
        python_version: str = "3.12",
        *args,
        **kwargs,
    ):
        """
        Initialize OpenHands SDK agent.

        Args:
            reasoning_effort: Reasoning effort level (low, medium, high).
            load_skills: Whether to load skills from skill paths.
            skill_paths: Custom skill paths to load from. If None, uses default paths.
            collect_token_ids: When True, request token IDs from the LLM backend
                (requires SGLang/vLLM; third-party APIs will ignore this).
            max_iterations: Maximum number of agent iterations per run.
                Maps to the SDK's max_iteration_per_run parameter.
            temperature: LLM sampling temperature (0.0 to 2.0).
            python_version: Python version for the SDK venv (openhands-sdk
                requires >=3.12). Installed via uv regardless of the system
                Python in the base image.
        """
        super().__init__(
            *args,
            reasoning_effort=reasoning_effort,
            load_skills=load_skills,
            skill_paths=skill_paths,
            collect_token_ids=collect_token_ids,
            max_iterations=max_iterations,
            temperature=temperature,
            python_version=python_version,
            **kwargs,
        )

    @staticmethod
    @override
    def name() -> str:
        return AgentName.OPENHANDS_SDK.value

    @override
    def get_version_command(self) -> str | None:
        return f"{self._VENV_DIR}/bin/python -c 'import openhands.sdk; print(openhands.sdk.__version__)' 2>/dev/null"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip()

    @property
    def _trajectory_path(self) -> PurePosixPath:
        return PurePosixPath(EnvironmentPaths.agent_dir / self._TRAJECTORY_FILENAME)

    async def _uv_target(self, environment: BaseEnvironment) -> str:
        """uv's release target for this container: arch plus gnu/musl libc."""
        result = await environment.exec(
            command=(
                "uname -m; "
                "if [ -f /etc/alpine-release ] || (ldd --version 2>&1 | grep -qi musl); "
                "then echo musl; else echo gnu; fi"
            ),
        )
        lines = [
            line.strip() for line in (result.stdout or "").splitlines() if line.strip()
        ]
        if len(lines) < 2:
            raise RuntimeError(
                f"could not identify the container platform (uname -m gave {result.stdout!r})"
            )
        machine, libc = lines[0], lines[1]
        arch = self._UV_ARCHES.get(machine)
        if arch is None:
            raise RuntimeError(
                f"no uv release for container architecture {machine!r}; "
                f"supported: {', '.join(sorted(self._UV_ARCHES))}"
            )
        return f"{arch}-unknown-linux-{libc}"

    def _uv_tarball(self, target: str) -> Path:
        """The uv release tarball for `target`, downloaded once and cached on the host."""
        version = self.options.uv_version
        cache_dir = Path(user_cache_dir("harbor")) / "uv"
        cache_dir.mkdir(parents=True, exist_ok=True)
        tarball = cache_dir / f"uv-{version}-{target}.tar.gz"
        if tarball.is_file() and tarball.stat().st_size > 0:
            return tarball
        url = self._UV_RELEASE_URL.format(version=version, target=target)
        self.logger.info("Downloading uv %s for %s from %s", version, target, url)
        partial = tarball.with_suffix(".part")
        # GitHub's release CDN occasionally resets a connection mid-handshake;
        # one such blip must not fail a trial that has not started yet.
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    partial.write_bytes(response.read())
                break
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                self.logger.warning("uv download attempt %d/3 failed: %s", attempt, exc)
                time.sleep(2 * attempt)
        else:
            raise RuntimeError(f"could not download uv from {url}: {last_error}")
        partial.replace(tarball)
        return tarball

    async def _provision_uv(self, environment: BaseEnvironment) -> None:
        """Put uv into the container without apt or curl.

        The stock recipe (`curl … astral.sh/uv/install.sh | sh`) assumes the
        image has curl or lets apt install it. Benchmark images often
        guarantee neither — some empty their apt sources on purpose so the
        agent cannot install packages — so the binary is fetched on the host
        and copied in. From there uv brings its own downloader and TLS roots:
        the Python build and the SDK wheels need nothing else from the image.
        """
        target = await self._uv_target(environment)
        tarball = await asyncio.to_thread(self._uv_tarball, target)
        remote_tarball = "/tmp/harbor-uv.tar.gz"
        await environment.upload_file(source_path=tarball, target_path=remote_tarball)
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {self._UV_DIR} && "
                f"tar -xzf {remote_tarball} -C {self._UV_DIR} --strip-components=1 && "
                f"chmod 0755 {self._UV_DIR}/uv && rm -f {remote_tarball} && "
                f"{self._UV_DIR}/uv --version"
            ),
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Check if already installed
        check_result = await environment.exec(
            command=f'[ -f {self._VENV_DIR}/bin/python ] && {self._VENV_DIR}/bin/python -c "import openhands.sdk" 2>/dev/null',
        )
        already_installed = check_result.return_code == 0

        # Best effort, never fatal. tmux backs the SDK's terminal tool (without
        # it the tool falls back to a subprocess session it calls less
        # stable), git is what the workspace helpers and most tasks expect, and
        # coreutils supplies the `stdbuf` run() prefers. Images that empty
        # their apt sources cannot install any of them; the SDK still runs.
        try:
            await self.ensure_system_dependencies(
                environment, ("coreutils", "git", "tmux")
            )
        except Exception as exc:  # noqa: BLE001 - the image is what it is
            self.logger.warning(
                "Could not install optional system packages (git, tmux); "
                "continuing with what the image provides: %s",
                str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
            )

        if not already_installed:
            # Create the bootstrap dirs owned by the default user (uv runs as
            # the agent user, so they must be writable by them; /opt itself is
            # typically root-owned).
            agent_user = environment.default_user or "root"
            dirs = f"{self._VENV_DIR} {self._UV_DIR} {self._UV_PYTHON_DIR}"
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {dirs} && chown {agent_user}:{agent_user} {dirs}",
            )
            await self._provision_uv(environment)
            # Install SDK via uv with an explicit Python version so the venv
            # does not depend on the (possibly too old, or absent) system Python.
            version_spec = f"=={self._version}" if self._version else ""
            python_version = str(self.options.python_version)
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    f"export PATH={self._UV_DIR}:$PATH "
                    f"UV_PYTHON_INSTALL_DIR={self._UV_PYTHON_DIR} "
                    "UV_CACHE_DIR=/tmp/uv-cache; "
                    f"uv python install {python_version} && "
                    f"uv venv {self._VENV_DIR} --python {python_version} --clear && "
                    f"uv pip install --python {self._VENV_DIR}/bin/python "
                    f"openhands-sdk{version_spec} openhands-tools{version_spec} fastapi"
                ),
            )

        # Upload runner script
        runner_script_path = Path(__file__).parent / "openhands_sdk_runner.py"
        local_copy = self.logs_dir / "run_agent.py"
        local_copy.write_text(runner_script_path.read_text())
        await environment.upload_file(
            source_path=local_copy,
            target_path="/installed-agent/run_agent.py",
        )
        await environment.exec(
            command="chmod +x /installed-agent/run_agent.py",
            user="root",
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """
        Populate context with results from agent trajectory.
        """
        trajectory_file = self.logs_dir / self._TRAJECTORY_FILENAME
        if not trajectory_file.exists():
            self.logger.debug(f"No trajectory file found at {trajectory_file}")
            return

        try:
            with open(trajectory_file) as f:
                trajectory_data = json.load(f)

            # Extract metrics from trajectory
            final_metrics = trajectory_data.get("final_metrics", {})
            context.cost_usd = final_metrics.get("total_cost_usd")
            context.n_input_tokens = final_metrics.get("total_prompt_tokens", 0)
            context.n_output_tokens = final_metrics.get("total_completion_tokens", 0)
            context.n_cache_tokens = final_metrics.get("total_cached_tokens", 0)
            # The runner keeps the cache-write side under `extra`; surface it
            # so result.json's n_cache_creation_tokens is not null.
            cache_write = (final_metrics.get("extra") or {}).get(
                "total_cache_write_tokens"
            )
            # kakashi: the field exists only in kanao's patched Harbor
            # (models/agent/context.py); stock 0.23.0's AgentContext is a
            # pydantic model that raises on unknown attributes, which aborted
            # the trial before verification.  The count still travels in the
            # trajectory's final_metrics.extra, which the harness reads.
            if cache_write and "n_cache_creation_tokens" in type(context).model_fields:
                context.n_cache_creation_tokens = int(cache_write)

        except (json.JSONDecodeError, OSError) as e:
            self.logger.error(f"Failed to parse trajectory file: {e}")

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Run the OpenHands SDK agent."""
        # Instruction goes in a file, not argv: an instruction that names a
        # path or keyword can otherwise match the agent's own
        # `ps | grep <keyword> | kill` and SIGTERM the agent process.
        instruction_local = self.logs_dir / self._INSTRUCTION_FILENAME
        instruction_local.write_text(instruction, encoding="utf-8")
        instruction_container = f"/logs/agent/{self._INSTRUCTION_FILENAME}"
        await environment.upload_file(
            source_path=instruction_local,
            target_path=instruction_container,
        )

        env: dict[str, str] = {}

        # Pass through LLM configuration from extra_env or environment
        llm_api_key = self._get_env("LLM_API_KEY")
        if llm_api_key is None:
            raise ValueError("LLM_API_KEY environment variable must be set")
        env["LLM_API_KEY"] = llm_api_key

        llm_base_url = self._get_env("LLM_BASE_URL")
        if llm_base_url is not None:
            env["LLM_BASE_URL"] = llm_base_url

        # Set model name
        if self.model_name:
            env["LLM_MODEL"] = self.model_name
        elif (llm_model := self._get_env("LLM_MODEL")) is not None:
            env["LLM_MODEL"] = llm_model
        else:
            raise ValueError("No LLM model specified")

        # Set up paths
        env["AGENT_LOGS_DIR"] = "/logs/agent"
        env["TRAJECTORY_PATH"] = f"/logs/agent/{self._TRAJECTORY_FILENAME}"
        env["LOAD_SKILLS"] = "1" if self.options.load_skills else "0"
        env["SKILL_PATHS"] = ":".join(
            self.options.skill_paths or self.DEFAULT_SKILL_PATHS
        )

        # Pass MCP server config so run_agent.py can register them with the SDK
        if self.mcp_servers:
            mcp_list: list[dict[str, str | list[str]]] = []
            for server in self.mcp_servers:
                entry: dict[str, str | list[str]] = {
                    "name": server.name,
                    "transport": server.transport,
                }
                if server.transport == "stdio":
                    if server.command:
                        entry["command"] = server.command
                    if server.args:
                        entry["args"] = server.args
                else:
                    if server.url:
                        entry["url"] = server.url
                mcp_list.append(entry)
            env["MCP_SERVERS_JSON"] = json.dumps(mcp_list)

        # Let the SDK pass reasoning effort as LiteLLM's provider-aware
        # top-level parameter. Keep extra_body for provider-specific fields.
        if self.options.reasoning_effort is not None:
            env["LLM_REASONING_EFFORT"] = self.options.reasoning_effort
        if self.options.collect_token_ids:
            env["LITELLM_EXTRA_BODY"] = json.dumps({"return_token_ids": True})

        # Declarative LLM_* knobs (timeout, token caps, provider, completion
        # logging) plus the model registration the runner applies before it
        # builds the LLM.
        env.update(self._resolved_env_vars)
        if self.options.model_info:
            env["LLM_MODEL_INFO_JSON"] = json.dumps(self.options.model_info)

        if self.options.max_iterations is not None:
            env["MAX_ITERATIONS"] = str(self.options.max_iterations)

        if self.options.temperature is not None:
            env["LLM_TEMPERATURE"] = str(self.options.temperature)

        # `stdbuf -oL` keeps the log line-buffered while the agent runs; images
        # without coreutils (busybox) still get the plain tee.
        command = f"""
{self._VENV_DIR}/bin/python /installed-agent/run_agent.py \
    --instruction-file="$AGENT_LOGS_DIR/{self._INSTRUCTION_FILENAME}" \
    --logs-dir="$AGENT_LOGS_DIR" \
    --trajectory-path="$TRAJECTORY_PATH" \
    2>&1 | {{ command -v stdbuf >/dev/null 2>&1 && exec stdbuf -oL tee /logs/agent/{self._OUTPUT_FILENAME} || exec tee /logs/agent/{self._OUTPUT_FILENAME}; }}
"""

        await self.exec_as_agent(environment, command=command.strip(), env=env)
