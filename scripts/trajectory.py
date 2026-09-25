#!/usr/bin/env python3
"""Claude Code logs -> ATIF-v1.7 trajectory.json.

Two sources feed the converter:

* the CLI's on-disk session transcript (~/.claude/projects/<cwd-slug>/<session>.jsonl,
  copied out of the agent container).  Every entry is timestamped and each
  assistant entry carries the API message's final usage, output_tokens
  included, so it is the source for per-step metrics;
* the --output-format stream-json stdout the runner captured (agent.jsonl).
  Its assistant events repeat the message_start usage snapshot, so their
  output_tokens is a placeholder; its terminal `result` event is the
  authority for session totals and cost.

Token semantics follow ATIF: prompt_tokens counts every input token
(uncached + cache read + cache write); cached_tokens is the cache-read
subset; the uncached and cache-write parts live in metrics.extra.
"""
import json
from pathlib import Path

from judge_lib import estimate_cost_usd, pricing_known

SCHEMA_VERSION = "ATIF-v1.7"
SOURCE_TRANSCRIPT = "session_transcript"
SOURCE_STREAM = "stream_json"
TOOL_RESULT_CAP = 20000          # chars of one tool result kept in trajectory.json
UNSCORED_STATUSES = ("isolation_error", "verifier_error", "harness_error", "agent_error")


def load_jsonl(path):
    """Parse a JSONL file leniently. Returns (events, number of unparseable lines)."""
    events, bad = [], 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return events, bad


def pick_session_file(session_root, session_id=None):
    """The main-chain transcript for `session_id` under a copied
    ~/.claude/projects tree, else the largest main-chain transcript, else None."""
    root = Path(session_root)
    if not root.is_dir():
        return None
    files = [p for p in root.rglob("*.jsonl") if "subagents" not in p.parts]
    if session_id:
        for p in files:
            if p.stem == session_id:
                return p
    return max(files, key=lambda p: p.stat().st_size, default=None)


def _dedupe(events):
    seen, out = set(), []
    for ev in events:
        uid = ev.get("uuid")
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        out.append(ev)
    return out


def _fill_timestamps(events):
    """Per-event timestamps, gaps filled from the nearest stamped neighbour
    (stream-json stamps only user events)."""
    ts = [(ev.get("timestamp") or "") for ev in events]
    carried = ""
    for i, value in enumerate(ts):
        carried = value or carried
        ts[i] = carried
    carried = ""
    for i in range(len(ts) - 1, -1, -1):
        carried = ts[i] or carried
        ts[i] = carried
    return ts


def _content_parts(message):
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [p for p in (content or []) if isinstance(p, dict)]


def _result_text(part):
    content = part.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            p.get("text", "") if isinstance(p, dict) and p.get("type") == "text" else json.dumps(p)
            for p in content)
    elif not isinstance(content, str):
        content = json.dumps(content)
    if len(content) > TOOL_RESULT_CAP:
        content = content[:TOOL_RESULT_CAP] + f"\n... [truncated {len(content) - TOOL_RESULT_CAP} chars]"
    return content


def usage_metrics(usage, model_name, source):
    """ATIF metrics for one API message.  A stream-json snapshot has no
    trustworthy output_tokens, so completion_tokens is None for SOURCE_STREAM."""
    if not isinstance(usage, dict):
        return None
    uncached = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0) if source == SOURCE_TRANSCRIPT else None
    metrics = {
        "prompt_tokens": uncached + cache_read + cache_write,
        "completion_tokens": output,
        "cached_tokens": cache_read,
        "cost_usd": None,
        "extra": {
            "uncached_input_tokens": uncached,
            "cache_creation_input_tokens": cache_write,
            "completion_tokens_source": source if output is not None else "unavailable_in_stream_json",
        },
    }
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    if thinking is not None:
        metrics["extra"]["thinking_tokens"] = thinking
    if output is not None and pricing_known(model_name):
        metrics["cost_usd"] = round(estimate_cost_usd(model_name, uncached, output, cache_write, cache_read), 6)
    return metrics


def _final_metrics(steps, results, model_name):
    warnings = []
    agent = [s for s in steps if s["source"] == "agent" and s.get("metrics")]
    s_unc = sum(s["metrics"]["extra"]["uncached_input_tokens"] for s in agent)
    s_read = sum(s["metrics"]["cached_tokens"] for s in agent)
    s_write = sum(s["metrics"]["extra"]["cache_creation_input_tokens"] for s in agent)
    s_out = sum(s["metrics"]["completion_tokens"] or 0 for s in agent)
    out_known = bool(agent) and all(s["metrics"]["completion_tokens"] is not None for s in agent)
    thinks = [s["metrics"]["extra"].get("thinking_tokens") for s in agent]
    # None when any step lacks the count: a partial sum would read as a total.
    total_thinking = sum(thinks) if agent and all(t is not None for t in thinks) else None
    extra = {}
    if results:
        def rsum(key):
            return sum(int((r.get("usage") or {}).get(key) or 0) for r in results)
        unc, read, write, out = (rsum("input_tokens"), rsum("cache_read_input_tokens"),
                                 rsum("cache_creation_input_tokens"), rsum("output_tokens"))
        # The CLI prices with Anthropic's table, which is right for any Claude id
        # (api or Bedrock) and wrong for a non-Claude model behind a bridge.
        if "claude" in (model_name or "").lower():
            cost = round(sum(float(r.get("total_cost_usd") or 0.0) for r in results), 6)
            cost_source, cost_known = "cli_result_event", True
        else:
            cost_known = pricing_known(model_name)
            cost = round(estimate_cost_usd(model_name, unc, out, write, read), 6) if cost_known else 0.0
            cost_source = "estimated_from_result_usage" if cost_known else "unknown"
        # Each result event's modelUsage restates the running cumulative total
        # for the whole session lineage, so N sessions carry N snapshots of the
        # same total. Keep the max per field (the final cumulative value); summing
        # would multiply every count and the derived cost by the session count.
        model_usage = {}
        for r in results:
            for model, usage in (r.get("modelUsage") or {}).items():
                acc = model_usage.setdefault(model, {})
                for k, v in usage.items():
                    acc[k] = max(acc.get(k, 0), v) if isinstance(v, (int, float)) else v
        for model, acc in model_usage.items():
            if "claude" not in model.lower() and "costUSD" in acc:
                # The CLI priced this model with Anthropic's table; keep the
                # number, drop the name that presents it as a real cost.
                acc["cli_cost_usd_anthropic_rates"] = acc.pop("costUSD")
        checks = {"uncached_input_tokens": s_unc == unc, "cache_read_input_tokens": s_read == read,
                  "cache_creation_input_tokens": s_write == write}
        if out_known:
            checks["output_tokens"] = s_out == out
        extra.update({
            "cost_source": cost_source, "cost_known": cost_known, "model_usage": model_usage,
            "num_turns": sum(int(r.get("num_turns") or 0) for r in results),
            "duration_ms": sum(int(r.get("duration_ms") or 0) for r in results),
            "duration_api_ms": sum(int(r.get("duration_api_ms") or 0) for r in results),
            "result_subtype": results[-1].get("subtype"),
            "result_is_error": bool(results[-1].get("is_error")),
            "step_sums_match_result": checks,
            "completion_tokens_source": "cli_result_event",
        })
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            warnings.append("per-step sums differ from the CLI result event for: " + ", ".join(bad))
    else:
        unc, read, write, out = s_unc, s_read, s_write, (s_out if out_known else 0)
        known = out_known and pricing_known(model_name)
        cost = round(estimate_cost_usd(model_name, unc, out, write, read), 6) if known else 0.0
        extra.update({
            "cost_source": "estimated_from_steps" if known else "unknown", "cost_known": known,
            "result_subtype": None, "result_is_error": None,
            "completion_tokens_source": "session_transcript" if out_known else "unknown",
        })
        warnings.append("no CLI result event (run ended early): totals summed from steps"
                        + ("" if out_known else "; completion tokens unknown"))
    extra.update({"total_uncached_input_tokens": unc, "total_cache_creation_input_tokens": write,
                  "total_thinking_tokens": total_thinking, "result_events": len(results)})
    final = {
        "total_prompt_tokens": unc + read + write,
        "total_completion_tokens": out,
        "total_cached_tokens": read,
        "total_cost_usd": cost,
        "total_steps": len(steps),
        "extra": extra,
    }
    return final, warnings


def build_trajectory(transcript_events, stream_events, agent_name="claude-code"):
    """ATIF-v1.7 trajectory from the session transcript (preferred) and the
    stream-json events.  Returns (trajectory, warnings)."""
    warnings = []
    stream = _dedupe(stream_events or [])
    init = next((e for e in stream if e.get("type") == "system" and e.get("subtype") == "init"), {})
    results = [e for e in stream if e.get("type") == "result"]
    # message_start snapshots keep the API usage verbatim; the transcript may
    # normalise it.  The reasoning split is taken from the snapshot when the
    # transcript entry lacks it.
    stream_usage_by_id = {}
    for e in stream:
        m = e.get("message") if e.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("id") and isinstance(m.get("usage"), dict):
            stream_usage_by_id.setdefault(m["id"], m["usage"])
    if transcript_events:
        source, events = SOURCE_TRANSCRIPT, list(transcript_events)
    else:
        source, events = SOURCE_STREAM, stream
        warnings.append("no session transcript: per-step completion_tokens unavailable and "
                        "assistant timestamps approximated from neighbouring tool results")
    timestamps = _fill_timestamps(events)

    steps, by_msg_id, by_call_id = [], {}, {}
    skipped = {"sidechain": 0, "meta": 0, "subagent": 0, "unmatched_tool_results": 0}
    model_name = init.get("model") or ""
    version = init.get("claude_code_version") or ""
    session_id = init.get("session_id") or ""

    def new_step(src, ts, message=""):
        step = {"step_id": len(steps) + 1, "timestamp": ts, "source": src, "message": message}
        steps.append(step)
        return step

    for i, ev in enumerate(events):
        etype = ev.get("type")
        if etype not in ("user", "assistant"):
            continue
        if ev.get("isSidechain"):
            skipped["sidechain"] += 1
            continue
        if ev.get("isMeta"):
            skipped["meta"] += 1
            continue
        if ev.get("parent_tool_use_id"):
            skipped["subagent"] += 1
            continue
        version = version or ev.get("version") or ""
        session_id = session_id or ev.get("sessionId") or ev.get("session_id") or ""
        msg = ev.get("message")
        if etype == "user":
            texts, tool_results = [], []
            for part in _content_parts(msg):
                if part.get("type") == "tool_result":
                    tool_results.append(part)
                elif part.get("type") == "text":
                    texts.append(part.get("text", ""))
            for part in tool_results:
                owner = by_call_id.get(part.get("tool_use_id"))
                if owner is None:
                    skipped["unmatched_tool_results"] += 1
                    owner = next((s for s in reversed(steps) if s["source"] == "agent"), None)
                if owner is None:
                    continue
                owner.setdefault("observation", {"results": []})["results"].append(
                    {"source_call_id": part.get("tool_use_id", ""), "content": _result_text(part)})
            if texts and not tool_results:
                new_step("user", timestamps[i], "\n".join(texts))
            continue
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        step = by_msg_id.get(mid) if mid else None
        if step is None:
            step = new_step("agent", timestamps[i])
            step["model_name"] = msg.get("model") or model_name
            model_name = model_name or step["model_name"]
            if mid:
                by_msg_id[mid] = step
        texts = [step["message"]] if step["message"] else []
        reasoning = [step["reasoning_content"]] if step.get("reasoning_content") else []
        for part in _content_parts(msg):
            kind = part.get("type")
            if kind == "text":
                texts.append(part.get("text", ""))
            elif kind == "thinking":
                reasoning.append(part.get("thinking", ""))
            elif kind == "tool_use":
                call = {"tool_call_id": part.get("id", ""), "function_name": part.get("name", ""),
                        "arguments": part.get("input", {})}
                step.setdefault("tool_calls", []).append(call)
                by_call_id[call["tool_call_id"]] = step
        step["message"] = "\n".join(t for t in texts if t)
        if reasoning:
            step["reasoning_content"] = "\n".join(r for r in reasoning if r)
        # Every block of a message repeats the same usage; the last write is identical.
        usage = msg.get("usage")
        if source == SOURCE_TRANSCRIPT and isinstance(usage, dict) and "output_tokens_details" not in usage:
            snap = stream_usage_by_id.get(mid) or {}
            if isinstance(snap.get("output_tokens_details"), dict):
                usage = {**usage, "output_tokens_details": snap["output_tokens_details"]}
        metrics = usage_metrics(usage, step["model_name"], source)
        if metrics:
            step["metrics"] = metrics

    final, more = _final_metrics(steps, results, model_name)
    warnings.extend(more)
    final["extra"].update({
        "steps_source": source,
        "skipped_entries": skipped,
        "sessions": sum(1 for e in stream if e.get("type") == "system" and e.get("subtype") == "init"),
    })
    trajectory = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "agent": {"name": agent_name, "version": version, "model_name": model_name},
        "steps": steps,
        "final_metrics": final,
    }
    return trajectory, warnings


def write_trajectory(trajectory, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trajectory, f, indent=2, ensure_ascii=False)

