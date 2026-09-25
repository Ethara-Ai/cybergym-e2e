# VENDORED from kanao-harness commit 53f73dc
#   (harness/vendor/harbor/src/harbor/agents/installed/openhands_sdk_runner.py), which is Harbor
#   0.23.0's stock file plus that project's patch: pinned uv bootstrap, LLM_*
#   knobs (timeout, token caps, provider, model_info, completion logging) and
#   reasoning capture in the in-container runner.  Loaded by `harbor run -a
#   harbor_agents.openhands_sdk:OpenHandsSDK` on top of the unpatched PyPI
#   Harbor release pinned in harbor.lock.  Keep byte-identical to the source
#   apart from this header; re-vendor rather than hand-edit.
#!/usr/bin/env python3
"""Harbor runner script for OpenHands SDK agent.

Runs one OpenHands SDK conversation inside the task container and writes an
ATIF trajectory. The conversion helpers (``records_from_events``,
``build_trajectory``) import nothing from the SDK so they can be unit-tested
on a host that has no ``openhands`` install; only ``main()`` needs the SDK.

Environment knobs (all optional unless noted):
    LLM_MODEL             model id (required) — a ``provider/`` prefix routes it
    LLM_API_KEY           credential (required)
    LLM_BASE_URL          endpoint override (a local proxy, say)
    LLM_PROVIDER          LiteLLM provider for a prefix-less model id; the
                          runner prefixes the model with it for routing only
    LLM_MODEL_INFO_JSON   ``litellm.register_model()`` metadata for a model
                          LiteLLM does not know
    LLM_REASONING_EFFORT  provider-neutral reasoning effort
    LLM_TIMEOUT           per-call timeout in seconds
    LLM_MAX_INPUT_TOKENS / LLM_MAX_OUTPUT_TOKENS   token caps
    LLM_LOG_COMPLETIONS   true|false — raw LiteLLM traffic to completions/
    LLM_TEMPERATURE, MAX_ITERATIONS, LITELLM_EXTRA_BODY, LOAD_SKILLS,
    SKILL_PATHS, MCP_SERVERS_JSON — as before
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        print(f"Warning: ignoring non-integer {name}={raw!r}", file=sys.stderr)
        return None


def _utc_timestamp(value: Any) -> Any:
    """An ISO 8601 timestamp with a UTC offset.

    SDK events are stamped with ``datetime.now().isoformat()`` — the
    container's local clock with no offset. Read it as local time, which is
    what it is, and express it in UTC so every trajectory carries the same
    unambiguous form whatever the image's TZ.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # local → aware
    return parsed.astimezone(UTC).isoformat()


def _text_of(parts: Any) -> str:
    """Join the text of a content list (TextContent objects or dicts)."""
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    texts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text")
        if text:
            texts.append(str(text))
    return "\n".join(texts)


def _reasoning_of(event: Any) -> str | None:
    """The model's reasoning for an LLM-backed event, or None when there is none.

    LiteLLM normalises provider reasoning into ``reasoning_content``; Anthropic
    additionally returns ``thinking_blocks`` whose text is the same reasoning.
    Prefer the normalised field and fall back to the blocks, so a provider
    that fills only one of the two still lands in the trajectory.
    """
    reasoning = getattr(event, "reasoning_content", None)
    if reasoning:
        return str(reasoning)
    blocks = getattr(event, "thinking_blocks", None) or []
    texts: list[str] = []
    for block in blocks:
        text = getattr(block, "thinking", None)
        if text is None and isinstance(block, dict):
            text = block.get("thinking")
        if text:
            texts.append(str(text))
    return "\n".join(texts) if texts else None


def _event_kind(event: Any) -> str:
    """Classify an SDK event by class, tolerating a host without the SDK."""
    try:
        from openhands.sdk.event import (
            ActionEvent,
            AgentErrorEvent,
            MessageEvent,
            ObservationEvent,
            TokenEvent,
        )
    except ImportError:  # host-side tests pass duck-typed fakes
        return type(event).__name__
    if isinstance(event, MessageEvent):
        return "MessageEvent"
    if isinstance(event, ActionEvent):
        return "ActionEvent"
    if isinstance(event, ObservationEvent):
        return "ObservationEvent"
    if isinstance(event, AgentErrorEvent):
        return "AgentErrorEvent"
    if isinstance(event, TokenEvent):
        return "TokenEvent"
    return type(event).__name__


def _tool_call_arguments(event: Any) -> dict[str, Any]:
    tool_call_args: dict[str, Any] = {}
    # Try tool_call.function.arguments (OpenAI format), then the SDK's own
    # MessageToolCall.arguments (a JSON string).
    tool_call = getattr(event, "tool_call", None)
    raw_args: Any = None
    if tool_call is not None:
        function = getattr(tool_call, "function", None)
        if function is not None:
            raw_args = getattr(function, "arguments", None)
        if raw_args is None:
            raw_args = getattr(tool_call, "arguments", None)
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            tool_call_args = parsed if isinstance(parsed, dict) else {"raw": parsed}
        except json.JSONDecodeError:
            tool_call_args = {"raw": raw_args}
    elif isinstance(raw_args, dict):
        tool_call_args = raw_args
    # Fallback: extract from the parsed action's dict representation
    action = getattr(event, "action", None)
    if not tool_call_args and action is not None:
        try:
            action_dict = (
                action.model_dump() if hasattr(action, "model_dump") else vars(action)
            )
            # Remove internal fields
            tool_call_args = {
                k: v for k, v in action_dict.items() if k != "kind" and v is not None
            }
        except Exception:
            pass
    return tool_call_args


def _usage_metrics(usage: Any) -> dict[str, Any] | None:
    """One TokenUsage (object or dict) as ATIF step metrics.

    A ``cost_usd`` attribute, when the caller attached one (see
    ``_usage_by_response``), rides along as the step's cost.
    """
    if usage is None:
        return None

    def _get(name: str) -> int:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        return int(value or 0)

    prompt = _get("prompt_tokens")
    completion = _get("completion_tokens")
    cached = _get("cache_read_tokens")
    cache_write = _get("cache_write_tokens")
    reasoning = _get("reasoning_tokens")
    metrics: dict[str, Any] = {}
    if prompt > 0:
        metrics["prompt_tokens"] = prompt
    if completion > 0:
        metrics["completion_tokens"] = completion
    if cached > 0:
        metrics["cached_tokens"] = cached
    cost = getattr(usage, "cost_usd", None)
    if cost is None and isinstance(usage, dict):
        cost = usage.get("cost_usd")
    if cost:
        metrics["cost_usd"] = float(cost)
    extra: dict[str, Any] = {}
    if cache_write > 0:
        extra["cache_write_tokens"] = cache_write
    if reasoning > 0:
        extra["reasoning_tokens"] = reasoning
    if extra:
        metrics["extra"] = extra
    return metrics or None


def records_from_events(
    events: list[Any],
    usage_by_response: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flatten SDK conversation events into plain records for build_trajectory.

    One record per LLM response: the SDK emits one ActionEvent per tool call,
    so a response with parallel tool calls arrives as several events sharing
    ``llm_response_id`` — they are folded into one record with several
    ``tool_calls``. Each record carries the agent's prose (``thought``), its
    reasoning (``reasoning_content`` / thinking blocks) and, when the LLM's
    metrics carry a usage entry for that response id, this call's token usage.
    """
    usage_by_response = usage_by_response or {}
    records: list[dict[str, Any]] = []
    last_agent_timestamp: str | None = None

    for event in events:
        kind = _event_kind(event)
        timestamp = _utc_timestamp(getattr(event, "timestamp", None))

        if kind == "MessageEvent":
            content = _text_of(
                getattr(getattr(event, "llm_message", None), "content", None)
            )
            source = getattr(event, "source", None)
            if source == "user":
                records.append(
                    {"type": "user_message", "content": content, "timestamp": timestamp}
                )
            elif source == "agent":
                response_id = getattr(event, "llm_response_id", None)
                records.append(
                    {
                        "type": "assistant_message",
                        "content": content,
                        "timestamp": timestamp,
                        "reasoning_content": _reasoning_of(event),
                        "response_id": response_id,
                        "metrics": _usage_metrics(usage_by_response.get(response_id))
                        if response_id
                        else None,
                    }
                )
                last_agent_timestamp = timestamp

        elif kind == "ActionEvent":
            response_id = getattr(event, "llm_response_id", None)
            tool_call = {
                "id": getattr(event, "tool_call_id", "") or "",
                "name": getattr(event, "tool_name", "") or "",
                "arguments": _tool_call_arguments(event),
            }
            thought = _text_of(getattr(event, "thought", None))
            # The SDK's `finish` tool carries the agent's final answer as its
            # `message` argument rather than as prose, so surface it as the
            # step's message — that is what a reader (or the judge) treats as
            # the final assistant message.
            if not thought and tool_call["name"] == "finish":
                final = tool_call["arguments"].get("message")
                if isinstance(final, str):
                    thought = final
            previous = records[-1] if records else None
            if (
                previous is not None
                and previous["type"] == "assistant_message"
                and previous.get("tool_calls")
                and response_id
                and previous.get("response_id") == response_id
            ):
                # Parallel tool call from the same LLM response.
                previous["tool_calls"].append(tool_call)
                if not previous["content"]:
                    previous["content"] = thought
                if not previous.get("reasoning_content"):
                    previous["reasoning_content"] = _reasoning_of(event)
                continue
            records.append(
                {
                    "type": "assistant_message",
                    "content": thought,
                    "timestamp": timestamp,
                    "reasoning_content": _reasoning_of(event),
                    "response_id": response_id,
                    "tool_calls": [tool_call],
                    "metrics": _usage_metrics(usage_by_response.get(response_id))
                    if response_id
                    else None,
                }
            )
            last_agent_timestamp = timestamp

        elif kind == "ObservationEvent":
            observation = getattr(event, "observation", None)
            obs_content = ""
            if observation is not None:
                obs_raw = getattr(observation, "content", None)
                obs_content = (
                    _text_of(obs_raw)
                    if isinstance(obs_raw, list)
                    else (str(obs_raw) if obs_raw else str(observation))
                )
            records.append(
                {
                    "type": "tool_result",
                    "tool_call_id": getattr(event, "tool_call_id", None),
                    "content": obs_content,
                    "timestamp": timestamp,
                }
            )

        elif kind == "AgentErrorEvent":
            # A tool call the scaffold could not execute (bad arguments, a
            # tool that raised). It answers a tool call like any observation
            # does, and the judge needs to see that the call failed.
            records.append(
                {
                    "type": "tool_result",
                    "tool_call_id": getattr(event, "tool_call_id", None),
                    "content": f"[agent error] {getattr(event, 'error', '') or ''}",
                    "timestamp": timestamp,
                }
            )

        elif kind == "TokenEvent":
            if last_agent_timestamp and records:
                for record in reversed(records):
                    if record.get("timestamp") == last_agent_timestamp:
                        record["token_ids"] = {
                            "prompt_token_ids": getattr(event, "prompt_token_ids", []),
                            "response_token_ids": getattr(
                                event, "response_token_ids", []
                            ),
                        }
                        break

    return records


def build_trajectory(
    events: list[dict[str, Any]],
    llm_metrics: dict[str, Any],
    model_name: str,
    system_prompt: str | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    agent_version: str = "unknown",
    reasoning_effort: str | None = None,
    agent_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ATIF-format trajectory from conversation records."""
    steps: list[dict[str, Any]] = []
    step_id = 1

    for event in events:
        event_type = event.get("type", "")

        if event_type == "user_message":
            steps.append(
                {
                    "step_id": step_id,
                    "timestamp": event.get("timestamp"),
                    "source": "user",
                    "message": event.get("content", ""),
                }
            )
            step_id += 1

        elif event_type == "assistant_message":
            step: dict[str, Any] = {
                "step_id": step_id,
                "timestamp": event.get("timestamp"),
                "source": "agent",
                "message": event.get("content", ""),
                "model_name": model_name,
            }
            if reasoning_effort:
                step["reasoning_effort"] = reasoning_effort
            reasoning = event.get("reasoning_content")
            if reasoning:
                step["reasoning_content"] = reasoning

            # Add tool calls if present
            tool_calls = event.get("tool_calls", [])
            if tool_calls:
                step["tool_calls"] = [
                    {
                        "tool_call_id": tc.get("id", ""),
                        "function_name": tc.get("name", ""),
                        "arguments": tc.get("arguments", {}),
                    }
                    for tc in tool_calls
                ]

            metrics: dict[str, Any] = dict(event.get("metrics") or {})
            token_data = event.get("token_ids")
            if token_data:
                metrics["prompt_token_ids"] = token_data.get("prompt_token_ids", [])
                metrics["completion_token_ids"] = token_data.get(
                    "response_token_ids", []
                )
            if metrics:
                step["metrics"] = metrics

            steps.append(step)
            step_id += 1

        elif event_type == "tool_result":
            # Attach to the agent step that issued this call; with parallel
            # tool calls that is not necessarily the previous step.
            call_id = event.get("tool_call_id")
            target: dict[str, Any] | None = None
            if call_id:
                for candidate in reversed(steps):
                    if candidate.get("source") != "agent":
                        continue
                    ids = {
                        tc["tool_call_id"] for tc in candidate.get("tool_calls") or []
                    }
                    if call_id in ids:
                        target = candidate
                        break
            if target is None and steps and steps[-1].get("source") == "agent":
                target = steps[-1]
            if target is not None:
                target.setdefault("observation", {"results": []})["results"].append(
                    {
                        "source_call_id": call_id,
                        "content": event.get("content", ""),
                    }
                )

    if system_prompt:
        system_step: dict[str, Any] = {
            "step_id": 0,
            "timestamp": steps[0]["timestamp"] if steps else None,
            "source": "system",
            "message": system_prompt,
        }
        steps.insert(0, system_step)

    for i, step in enumerate(steps):
        step["step_id"] = i + 1

    final_extra: dict[str, Any] = {}
    if llm_metrics.get("cache_write_tokens"):
        final_extra["total_cache_write_tokens"] = llm_metrics["cache_write_tokens"]
    if llm_metrics.get("reasoning_tokens"):
        final_extra["total_reasoning_tokens"] = llm_metrics["reasoning_tokens"]

    trajectory = {
        "schema_version": "ATIF-v1.5",
        "session_id": os.environ.get("SESSION_ID", "harbor-session"),
        "agent": {
            "name": "openhands-sdk",
            "version": agent_version,
            "model_name": model_name,
            "tool_definitions": tool_definitions if tool_definitions else None,
            **({"extra": agent_extra} if agent_extra else {}),
        },
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": llm_metrics.get("prompt_tokens", 0),
            "total_completion_tokens": llm_metrics.get("completion_tokens", 0),
            "total_cached_tokens": llm_metrics.get("cached_tokens", 0),
            "total_cost_usd": llm_metrics.get("cost_usd", 0.0),
            "total_steps": len(steps),
            **({"extra": final_extra} if final_extra else {}),
        },
    }

    return trajectory


def _register_model_info(model: str, routed_model: str) -> None:
    """Teach LiteLLM about a model it has no metadata for.

    Registered under both the bare id and the routed (prefixed) id so that
    whichever one LiteLLM looks up finds the entry.
    """
    raw = os.environ.get("LLM_MODEL_INFO_JSON", "").strip()
    if not raw:
        return
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        print(
            f"Warning: LLM_MODEL_INFO_JSON is not valid JSON ({e}); ignored",
            file=sys.stderr,
        )
        return
    if not isinstance(info, dict):
        print(
            "Warning: LLM_MODEL_INFO_JSON must be a JSON object; ignored",
            file=sys.stderr,
        )
        return
    import litellm

    litellm.register_model({key: dict(info) for key in {model, routed_model}})
    print(f"Registered model info for {sorted({model, routed_model})}")


def main():
    from openhands.sdk import (
        LLM,
        Agent,
        AgentContext,
        Conversation,
        Tool,
        get_logger,
    )
    from openhands.sdk.context import Skill
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    logger = get_logger(__name__)

    def load_skill_from_file(skill_path: Path) -> Skill | None:
        """Load a skill from a SKILL.md file."""
        if not skill_path.exists():
            return None

        content = skill_path.read_text()
        name = skill_path.parent.name

        return Skill(
            name=name,
            content=content,
            source=str(skill_path),
            trigger=None,  # Always active
        )

    def discover_skills(skill_paths: list[str]) -> list[Skill]:
        """Discover skills from SkillsBench skill paths."""
        seen_names: set[str] = set()
        skills: list[Skill] = []

        for base_path_str in skill_paths:
            base_path = Path(base_path_str).expanduser()
            if not base_path.exists():
                continue

            # Look for SKILL.md files in immediate subdirectories
            for skill_dir in base_path.iterdir():
                if not skill_dir.is_dir():
                    continue

                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill = load_skill_from_file(skill_file)
                    if skill and skill.name not in seen_names:
                        seen_names.add(skill.name)
                        skills.append(skill)
                        logger.debug(f"Loaded skill: {skill.name} from {skill_file}")

        return skills

    parser = argparse.ArgumentParser(description="Run OpenHands SDK agent")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--instruction", help="Task instruction (inline)")
    src.add_argument("--instruction-file", help="Path to a file containing the instruction")
    parser.add_argument("--logs-dir", required=True, help="Directory for logs")
    parser.add_argument(
        "--trajectory-path", required=True, help="Path to save trajectory"
    )
    args = parser.parse_args()
    if args.instruction_file:
        args.instruction = Path(args.instruction_file).read_text(encoding="utf-8")

    # Get configuration from environment
    model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929")
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")

    if not api_key:
        print("Error: LLM_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    # A prefix-less id (served by a proxy that speaks a known wire format)
    # gets its provider from LLM_PROVIDER for routing; the trajectory keeps
    # the bare id so the model is not labelled as the provider's own.
    provider = os.environ.get("LLM_PROVIDER", "").strip()
    routed_model = model
    if provider and "/" not in model:
        routed_model = f"{provider}/{model}"
    _register_model_info(model, routed_model)

    # Create logs directory
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Parse optional litellm extra body (for token ID collection with SGLang/vLLM)
    litellm_extra_body: dict[str, Any] = {}
    extra_body_raw = os.environ.get("LITELLM_EXTRA_BODY")
    if extra_body_raw:
        litellm_extra_body = json.loads(extra_body_raw)
        logger.debug(f"LiteLLM extra body: {litellm_extra_body}")

    # Configure LLM
    llm_kwargs: dict[str, Any] = {
        "model": routed_model,
        "api_key": api_key,
        "base_url": base_url,
    }
    if litellm_extra_body:
        llm_kwargs["litellm_extra_body"] = litellm_extra_body
    reasoning_effort_raw = os.environ.get("LLM_REASONING_EFFORT")
    if reasoning_effort_raw:
        llm_kwargs["reasoning_effort"] = reasoning_effort_raw
    temperature_raw = os.environ.get("LLM_TEMPERATURE")
    if temperature_raw:
        llm_kwargs["temperature"] = float(temperature_raw)
    timeout = _env_int("LLM_TIMEOUT")
    if timeout is not None:
        llm_kwargs["timeout"] = timeout
    num_retries = _env_int("LLM_NUM_RETRIES")
    if num_retries is not None:
        llm_kwargs["num_retries"] = num_retries
    max_input_tokens = _env_int("LLM_MAX_INPUT_TOKENS")
    if max_input_tokens is not None:
        llm_kwargs["max_input_tokens"] = max_input_tokens
    max_output_tokens = _env_int("LLM_MAX_OUTPUT_TOKENS")
    if max_output_tokens is not None:
        llm_kwargs["max_output_tokens"] = max_output_tokens
    log_completions = _env_flag("LLM_LOG_COMPLETIONS")
    if log_completions:
        completions_dir = logs_dir / "completions"
        completions_dir.mkdir(parents=True, exist_ok=True)
        llm_kwargs["log_completions"] = True
        llm_kwargs["log_completions_folder"] = str(completions_dir)
    llm = LLM(**llm_kwargs)

    # Configure tools
    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]

    # Load skills if enabled
    skills: list[Skill] = []
    if os.environ.get("LOAD_SKILLS", "1") == "1":
        skill_paths_str = os.environ.get("SKILL_PATHS", "")
        if skill_paths_str:
            skill_paths = skill_paths_str.split(":")
            skills = discover_skills(skill_paths)
            logger.debug(f"Loaded {len(skills)} skills")

    # Create agent context with skills
    agent_context = AgentContext(skills=skills)

    # Parse MCP server config from environment (serialized by openhands_sdk.py).
    # OpenHands SDK >=1.35 expects a flat dict[str, MCPServer], not the older
    # Claude-style {"mcpServers": {...}} wrapper.
    mcp_config = None
    mcp_servers_raw = os.environ.get("MCP_SERVERS_JSON")
    if mcp_servers_raw:
        mcp_servers = json.loads(mcp_servers_raw)
        mcp_config = {}
        for mcp in mcp_servers:
            server_name = mcp.get("name", "mcp-server")
            transport = mcp.get("transport", "stdio")
            server_cfg: dict[str, Any] = {}
            if transport == "stdio":
                if mcp.get("command"):
                    server_cfg["command"] = mcp["command"]
                if mcp.get("args"):
                    server_cfg["args"] = mcp["args"]
            else:
                if mcp.get("url"):
                    server_cfg["url"] = mcp["url"]
                # Harbor transports (http, streamable-http, sse) match the SDK.
                server_cfg["transport"] = transport
            mcp_config[server_name] = server_cfg
        logger.debug(f"MCP config: {json.dumps(mcp_config, indent=2)}")

    # Create agent (with optional MCP config)
    agent_kwargs: dict[str, Any] = {
        "llm": llm,
        "tools": tools,
        "agent_context": agent_context,
    }
    if mcp_config:
        agent_kwargs["mcp_config"] = mcp_config
    agent = Agent(**agent_kwargs)

    # Run conversation
    # Use the container's current working directory (set by Dockerfile WORKDIR)
    workspace = os.getcwd()
    conv_kwargs: dict[str, Any] = {"agent": agent, "workspace": workspace}
    max_iter_raw = os.environ.get("MAX_ITERATIONS")
    if max_iter_raw:
        conv_kwargs["max_iteration_per_run"] = int(max_iter_raw)
        logger.debug(f"Max iterations per run: {max_iter_raw}")
    conversation = Conversation(**conv_kwargs)

    print(f"Starting agent with instruction: {args.instruction[:200]}...")
    print(
        f"Using model: {model}"
        + (f" (routed as {routed_model})" if routed_model != model else "")
    )
    if base_url:
        print(f"Base URL: {base_url}")
    if reasoning_effort_raw:
        print(f"Reasoning effort: {reasoning_effort_raw}")
    if temperature_raw:
        print(f"Temperature: {temperature_raw}")
    if timeout is not None:
        print(f"LLM timeout: {timeout}s")
    if max_output_tokens is not None:
        print(f"Max output tokens: {max_output_tokens}")
    if max_iter_raw:
        print(f"Max iterations per run: {max_iter_raw}")
    print(f"Loaded {len(skills)} skills")
    if mcp_config:
        print(f"MCP servers: {list(mcp_config.keys())}")

    # Send instruction and run. A crash mid-run still writes the steps completed
    # so far, then re-raises so Harbor records the failure.
    try:
        conversation.send_message(args.instruction)
        conversation.run()
    except BaseException:
        try:
            _write_trajectory(
                conversation,
                llm,
                agent,
                model,
                args.trajectory_path,
                reasoning_effort_raw,
            )
        except Exception as e:  # noqa: BLE001 - never mask the run's own error
            print(
                f"Warning: could not write trajectory after failure: {e}",
                file=sys.stderr,
            )
        raise
    _write_trajectory(
        conversation, llm, agent, model, args.trajectory_path, reasoning_effort_raw
    )


def _usage_by_response(metrics: Any) -> dict[str, Any]:
    """Per-call usage keyed by the LLM response id each event carries.

    The SDK appends one TokenUsage per response and, separately, one Cost per
    response that cost anything — the costs carry no response id. When the
    two lists are the same length every response was priced and they line up
    one to one, so each usage gets its cost; otherwise (a zero-priced model,
    say) steps carry tokens only and the total cost still lands in
    final_metrics.
    """
    usages = list(getattr(metrics, "token_usages", None) or [])
    costs = list(getattr(metrics, "costs", None) or [])
    paired = len(costs) == len(usages)
    usage_by_response: dict[str, Any] = {}
    for index, usage in enumerate(usages):
        response_id = getattr(usage, "response_id", "") or ""
        if not response_id or response_id in usage_by_response:
            continue
        if paired:
            try:
                usage.cost_usd = float(getattr(costs[index], "cost", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError):
                pass
        usage_by_response[response_id] = usage
    return usage_by_response


def _write_trajectory(
    conversation: Any,
    llm: Any,
    agent: Any,
    model: str,
    trajectory_path_str: str,
    reasoning_effort: str | None,
) -> None:
    from openhands.sdk import get_logger

    logger = get_logger(__name__)

    # Collect metrics from accumulated_token_usage
    token_usage = llm.metrics.accumulated_token_usage
    metrics = {
        "prompt_tokens": token_usage.prompt_tokens if token_usage else 0,
        "completion_tokens": token_usage.completion_tokens if token_usage else 0,
        "cached_tokens": token_usage.cache_read_tokens if token_usage else 0,
        "cache_write_tokens": getattr(token_usage, "cache_write_tokens", 0)
        if token_usage
        else 0,
        "reasoning_tokens": getattr(token_usage, "reasoning_tokens", 0)
        if token_usage
        else 0,
        "cost_usd": llm.metrics.accumulated_cost,
    }
    usage_by_response = _usage_by_response(llm.metrics)

    # Extract system prompt and tool definitions from the initialized agent
    system_prompt = None
    tool_definitions: list[dict[str, Any]] = []
    try:
        system_prompt = agent.static_system_message
    except Exception as e:
        logger.debug(f"Could not extract system prompt: {e}")
    try:
        for tool_name, tool_obj in agent.tools_map.items():
            tool_definitions.append(tool_obj.to_openai_tool())
    except Exception as e:
        logger.debug(f"Could not extract tool definitions: {e}")

    if system_prompt:
        print(f"Captured system prompt ({len(system_prompt)} chars)")
    print(f"Captured {len(tool_definitions)} tool definitions")

    try:
        import openhands.sdk as sdk_pkg

        agent_version = str(getattr(sdk_pkg, "__version__", "unknown") or "unknown")
    except ImportError:
        agent_version = "unknown"

    records = records_from_events(list(conversation.state.events), usage_by_response)
    with_reasoning = sum(1 for r in records if r.get("reasoning_content"))
    print(
        f"Converted {len(records)} events; "
        f"{with_reasoning} agent steps carry reasoning_content"
    )

    # Build and save trajectory
    # How the LLM was configured, the way terminus-2 records its llm_kwargs.
    llm_extra: dict[str, Any] = {}
    for name in (
        "reasoning_effort",
        "timeout",
        "max_input_tokens",
        "max_output_tokens",
    ):
        value = getattr(llm, name, None)
        if value is not None:
            llm_extra[name] = value
    trajectory = build_trajectory(
        records,
        metrics,
        model,
        system_prompt=system_prompt,
        tool_definitions=tool_definitions,
        agent_version=agent_version,
        reasoning_effort=reasoning_effort,
        agent_extra={"llm": llm_extra} if llm_extra else None,
    )

    trajectory_path = Path(trajectory_path_str)
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trajectory_path, "w") as f:
        json.dump(trajectory, f, indent=2)

    print(f"Agent completed. Trajectory saved to {trajectory_path}")
    print(f"Total cost: ${metrics['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
