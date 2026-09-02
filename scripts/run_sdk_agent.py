#!/usr/bin/env python3
# DEPRECATED: legacy runner. Scores on a binary scale, writes no reward/ctrf/
# rubric files, applies no network lockdown and pins no platform. Use
# run_harbor.py for anything you intend to report.
"""Run a cybergym agent using the OpenHands Software Agent SDK.

Reads the task prompt from /opt/prompt.txt (written by the harness before
invocation) and drives the default agent (Terminal + FileEditor + TaskTracker)
with built-in stuck detection.

Environment variables (set by the harness):
    LLM_MODEL       — litellm-style model id (e.g. anthropic/claude-opus-4-8)
    LLM_API_KEY     — API key (or bridge stub secret)
    LLM_BASE_URL    — optional base URL for the provider (e.g. OAuth bridge)
"""

import os
import sys
import json
import time
from pathlib import Path

from pydantic import SecretStr

from openhands.sdk import LLM, Agent, Conversation, Event, LLMConvertibleEvent, Tool
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool


def main():
    prompt_file = Path("/opt/prompt.txt")
    if not prompt_file.exists():
        print("ERROR: /opt/prompt.txt not found", file=sys.stderr)
        sys.exit(1)
    prompt = prompt_file.read_text()

    api_key = os.getenv("LLM_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
    model = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929")
    base_url = os.getenv("LLM_BASE_URL", os.getenv("ANTHROPIC_BASE_URL"))

    is_bedrock = model.startswith("bedrock/")

    if not is_bedrock and "/" not in model:
        model = f"anthropic/{model}"

    print(f"[sdk-agent] model={model} base_url={base_url or 'default'}")
    print(f"[sdk-agent] prompt length={len(prompt)} chars")

    llm_kwargs = dict(
        model=model,
        usage_id="cybergym-agent",
        drop_params=True,
    )

    if is_bedrock:
        aws_region = os.getenv("LLM_AWS_REGION_NAME", os.getenv("AWS_REGION_NAME", "us-west-2"))
        llm_kwargs["aws_region_name"] = aws_region
        bearer = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
        if bearer:
            llm_kwargs["api_key"] = SecretStr(bearer)
            print(f"[sdk-agent] Bedrock auth: bearer token, region={aws_region}")
        else:
            llm_kwargs["api_key"] = SecretStr("bedrock")
            print(f"[sdk-agent] Bedrock auth: SigV4, region={aws_region}")
    else:
        llm_kwargs["api_key"] = SecretStr(api_key)
        if base_url:
            llm_kwargs["base_url"] = base_url

    llm = LLM(**llm_kwargs)

    condenser = LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}),
        max_size=80,
        keep_first=4,
    )

    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
        system_prompt_kwargs={"cli_mode": True},
        condenser=condenser,
    )

    events_log = []

    def on_event(event: Event):
        entry = {"ts": time.time(), "type": type(event).__name__}
        if isinstance(event, LLMConvertibleEvent):
            msg = event.to_llm_message()
            entry["message"] = str(msg)[:2000]
        events_log.append(entry)
        # Print a dot for progress
        print(".", end="", flush=True)

    workspace = os.getenv("WORKSPACE_DIR", "/src")
    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        callbacks=[on_event],
        stuck_detection=True,
    )

    conversation.send_message(prompt)

    start = time.time()
    conversation.run()
    elapsed = time.time() - start

    print(f"\n[sdk-agent] finished in {elapsed:.1f}s ({elapsed/60:.1f}m)")

    cost = llm.metrics.accumulated_cost
    print(f"[sdk-agent] LLM cost: ${cost:.4f}")

    # Save event log
    traj_dir = Path("/agent_trajectory")
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj_file = traj_dir / "sdk_events.json"
    with open(traj_file, "w") as f:
        json.dump(events_log, f, indent=2, default=str)
    print(f"[sdk-agent] trajectory: {traj_file} ({len(events_log)} events)")


if __name__ == "__main__":
    main()
