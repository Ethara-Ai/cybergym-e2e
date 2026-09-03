"""Offline self-test: ``python -m glm_bridge.selftest`` (run from scripts/).

Stands up a fake Z.ai upstream in-process (ASGI, no network) and drives the
bridge through it: auth, model mapping, header substitution, non-streaming
relay, streaming relay with keep-alives, error passthrough, and the OpenAI
chat path.  No key or network needed; a live check is ``python -m glm_bridge --check``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import bridge as B
from .credentials import CredentialProvider

REAL_KEY = "zai-real-key.secretpart"
SECRET = "per-run-stub-secret"


def fake_upstream() -> FastAPI:
    up = FastAPI()
    up.state.seen = []

    @up.post("/api/anthropic/v1/messages")
    async def messages(request: Request):
        body = json.loads(await request.body())
        up.state.seen.append({"headers": dict(request.headers), "body": body, "path": "messages"})
        if body.get("model") == "glm-boom":
            return JSONResponse({"type": "error", "error": {"type": "rate_limit_error",
                                                            "message": "slow down"}},
                                status_code=429, headers={"retry-after": "7"})
        if body.get("stream"):
            async def gen():
                yield b'event: message_start\ndata: {"type":"message_start"}\n\n'
                await asyncio.sleep(0.25)   # longer than the test keep-alive interval
                yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse({"id": "msg_1", "model": body["model"], "role": "assistant",
                             "content": [{"type": "text", "text": "hi"}],
                             "usage": {"input_tokens": 3, "output_tokens": 1}})

    @up.post("/api/coding/paas/v4/chat/completions")
    async def chat(request: Request):
        body = json.loads(await request.body())
        up.state.seen.append({"headers": dict(request.headers), "body": body, "path": "chat"})
        return JSONResponse({"id": "c1", "model": body["model"],
                             "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                             "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    return up


async def run() -> None:
    os.environ["GLM_UPSTREAM"] = "http://zai.test/api/anthropic"
    os.environ["GLM_OPENAI_UPSTREAM"] = "http://zai.test/api/coding/paas/v4"
    os.environ["KAKASHI_GLM_BRIDGE_SECRET"] = SECRET
    os.environ["GLM_MODEL_ID"] = "glm-5.3"
    os.environ["GLM_SMALL_MODEL_ID"] = "glm-5.3-flash"
    os.environ["GLM_BRIDGE_KEEPALIVE_SEC"] = "0.05"

    up = fake_upstream()
    up_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=up), base_url="http://zai.test")
    app = B.build_app(CredentialProvider(REAL_KEY), client=up_client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://bridge") as c:
        # 1. health without secret: ok but redacted; with secret: key prefix present
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] and "key_prefix" not in r.json(), r.text
        r = await c.get("/healthz", headers={"x-api-key": SECRET})
        assert r.json()["key_prefix"].startswith("zai-real"), r.text

        # 2. wrong / missing secret is rejected before touching upstream
        r = await c.post("/v1/messages", json={"model": "claude-opus-5", "messages": []},
                         headers={"x-api-key": "wrong"})
        assert r.status_code == 401, r.text
        assert not up.state.seen

        # 3. claude-* -> glm-5.3, real key substituted, stub never leaks
        r = await c.post("/v1/messages", json={"model": "claude-opus-5", "max_tokens": 8,
                                               "messages": [{"role": "user", "content": "x"}]},
                         headers={"x-api-key": SECRET, "anthropic-beta": "interleaved-thinking-2025-05-14"})
        assert r.status_code == 200, r.text
        assert r.json()["model"] == "glm-5.3" and r.json()["usage"]["output_tokens"] == 1
        seen = up.state.seen[-1]
        assert seen["body"]["model"] == "glm-5.3"
        assert seen["headers"]["authorization"] == f"Bearer {REAL_KEY}"
        assert seen["headers"]["x-api-key"] == REAL_KEY
        assert seen["headers"]["anthropic-beta"] == "interleaved-thinking-2025-05-14"
        assert seen["headers"]["anthropic-version"] == "2023-06-01"
        assert SECRET not in json.dumps(seen["headers"])

        # 4. haiku -> small model; glm-* passes through; bearer auth accepted
        r = await c.post("/v1/messages", json={"model": "claude-haiku-4-5", "messages": []},
                         headers={"authorization": f"Bearer {SECRET}"})
        assert r.json()["model"] == "glm-5.3-flash", r.text
        r = await c.post("/v1/messages", json={"model": "glm-4.7", "messages": []},
                         headers={"x-api-key": SECRET})
        assert r.json()["model"] == "glm-4.7", r.text

        # 5. streaming relayed verbatim (the ASGI test transport buffers the
        #    upstream body, so the idle-gap keep-alive is exercised in check 9)
        chunks = []
        async with c.stream("POST", "/v1/messages",
                            json={"model": "claude-sonnet-5", "stream": True, "messages": []},
                            headers={"x-api-key": SECRET}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            async for chunk in r.aiter_raw():
                chunks.append(chunk)
        joined = b"".join(chunks)
        assert b"message_start" in joined and b"message_stop" in joined, joined
        assert up.state.seen[-1]["body"]["model"] == "glm-5.3"

        # 6. upstream errors pass through with status + retry-after
        r = await c.post("/v1/messages", json={"model": "glm-boom", "messages": []},
                         headers={"x-api-key": SECRET})
        assert r.status_code == 429 and r.headers.get("retry-after") == "7", r.text
        assert r.json()["error"]["type"] == "rate_limit_error"

        # 7. OpenAI chat path -> coding endpoint, same mapping
        r = await c.post("/v1/chat/completions",
                         json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "x"}]},
                         headers={"authorization": f"Bearer {SECRET}"})
        assert r.status_code == 200 and r.json()["model"] == "glm-5.3", r.text
        assert up.state.seen[-1]["path"] == "chat"

        # 8. pure mapping helper
        assert B.map_model(None) == "glm-5.3"
        assert B.map_model("claude-3-5-haiku-20241022") == "glm-5.3-flash"
        assert B.map_model("GLM-5.3") == "GLM-5.3"

    # 9. keep-alive: an idle upstream gap gets an SSE comment, real chunks are
    #    never split or reordered, and the upstream is closed afterwards.
    closed = []

    async def slow_upstream():
        yield b"data: a\n\n"
        await asyncio.sleep(0.2)
        yield b"data: b\n\n"

    async def aclose():
        closed.append(True)

    out = [c async for c in B._stream_with_keepalive(slow_upstream(), aclose, 0.05)]
    assert out[0] == b"data: a\n\n" and out[-1] == b"data: b\n\n", out
    assert out.count(B._KEEPALIVE_LINE) >= 1, out
    assert closed == [True]
    await up_client.aclose()
    print("glm_bridge self-test OK (9 checks)")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as e:
        print(f"glm_bridge self-test FAILED: {e}", file=sys.stderr)
        raise SystemExit(1)
