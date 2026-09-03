"""Anthropic-compatible proxy in front of Z.ai's GLM endpoint.

Accepts what the Claude Code CLI sends (``POST /v1/messages`` and friends),
swaps the caller's stub key for the host's real ``ZAI_API_KEY``, rewrites
Claude model ids to GLM ids, forwards to::

    POST https://api.z.ai/api/anthropic/v1/messages       (GLM_UPSTREAM)

and streams the SSE response back byte-for-byte, interleaving an SSE keep-alive
comment when the upstream is idle so a long GLM reasoning pause cannot trip the
client's read timeout.

``/v1/chat/completions`` is also served, forwarded to the OpenAI-compatible
coding endpoint (``GLM_OPENAI_UPSTREAM``), so any OpenAI-style client can use
the same bridge.  The Anthropic path is the one the Harbor runner uses.

Point clients at it::

    export ANTHROPIC_BASE_URL=http://127.0.0.1:8790
    export ANTHROPIC_API_KEY=$KAKASHI_GLM_BRIDGE_SECRET   # stub; bridge substitutes the real key
    export ANTHROPIC_MODEL=glm-5.3

Security: set KAKASHI_GLM_BRIDGE_SECRET and give clients the same value as
ANTHROPIC_API_KEY; otherwise the bridge is unauthenticated and any local
process can spend the plan.  Bind loopback unless a secret is set.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .credentials import CredentialProvider, CredentialsError, key_prefix

_LOG = logging.getLogger(__name__)

# Z.ai international endpoints.  The mainland alternative is
# https://open.bigmodel.cn/api/anthropic; set GLM_UPSTREAM to switch.
UPSTREAM_DEFAULT = "https://api.z.ai/api/anthropic"
OPENAI_UPSTREAM_DEFAULT = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# GLM ids the Coding Plan lists today (docs.z.ai/devpack/overview, 2026-09):
# glm-5.3 is the main model; glm-5.3-flash the small/fast one.  Older ids
# (glm-5.2, glm-5.1, glm-4.7) are routed server-side to these.
DEFAULT_MODEL = "glm-5.3"
DEFAULT_SMALL_MODEL = "glm-5.3-flash"

SECRET_ENV = "KAKASHI_GLM_BRIDGE_SECRET"

# Inbound headers never forwarded: hop-by-hop, client auth (replaced), and
# compression negotiation (forced to identity so the keep-alive comment can
# never be spliced into a gzip stream).
_STRIP_REQUEST_HEADERS = frozenset({
    "host", "authorization", "x-api-key", "content-length", "connection",
    "accept-encoding", "proxy-authorization",
})
# Upstream headers never copied back (chunking / encoding artifacts).
_STRIP_RESPONSE_HEADERS = frozenset({
    "content-encoding", "transfer-encoding", "connection", "content-length",
})

_KEEPALIVE_LINE = b": keepalive\n\n"


# --- configuration (env-driven, read per request so a restart is never needed) --

def _upstream_base() -> str:
    return os.environ.get("GLM_UPSTREAM", UPSTREAM_DEFAULT).rstrip("/")


def _openai_upstream_base() -> str:
    return os.environ.get("GLM_OPENAI_UPSTREAM", OPENAI_UPSTREAM_DEFAULT).rstrip("/")


def _main_model() -> str:
    return os.environ.get("GLM_MODEL_ID", "").strip() or DEFAULT_MODEL


def _small_model() -> str:
    return os.environ.get("GLM_SMALL_MODEL_ID", "").strip() or DEFAULT_SMALL_MODEL


def _bridge_secret() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def _keepalive_interval() -> float:
    raw = os.environ.get("GLM_BRIDGE_KEEPALIVE_SEC", "15").strip()
    try:
        return float(raw)
    except ValueError:
        return 15.0


def _timeout(streaming: bool) -> httpx.Timeout:
    """GLM reasoning turns can pause for minutes between chunks; no total cap on
    streams, generous per-chunk read.  Override via GLM_BRIDGE_*_TIMEOUT."""
    def _f(env: str, default: float) -> float:
        try:
            return float(os.environ.get(env, "").strip() or default)
        except ValueError:
            return default
    connect = _f("GLM_BRIDGE_CONNECT_TIMEOUT", 30.0)
    if streaming:
        return httpx.Timeout(None, connect=connect,
                             read=_f("GLM_BRIDGE_STREAM_READ_TIMEOUT", 600.0),
                             write=None, pool=None)
    return httpx.Timeout(_f("GLM_BRIDGE_REQUEST_TIMEOUT", 600.0), connect=connect,
                         read=_f("GLM_BRIDGE_READ_TIMEOUT", 180.0))


# --- model mapping -------------------------------------------------------------

def map_model(requested: Optional[str]) -> str:
    """Rewrite a Claude model id to the configured GLM id.

    * empty / missing            -> GLM_MODEL_ID
    * ``claude-*haiku*``         -> GLM_SMALL_MODEL_ID (the CLI's background /
                                    title / summarisation calls)
    * any other ``claude-*``     -> GLM_MODEL_ID
    * anything else (``glm-*``)  -> passed through unchanged
    """
    if not requested:
        return _main_model()
    low = requested.lower()
    if "claude" not in low:
        return requested
    if "haiku" in low:
        return _small_model()
    return _main_model()


def rewrite_body(raw: bytes) -> tuple[bytes, Optional[str], Optional[str], bool]:
    """Apply the model mapping to a JSON body.

    Returns ``(body, requested_model, forwarded_model, client_wanted_stream)``.
    A body that is not a JSON object is forwarded untouched.
    """
    try:
        body = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return raw, None, None, False
    if not isinstance(body, dict):
        return raw, None, None, False
    requested = body.get("model") if isinstance(body.get("model"), str) else None
    forwarded = map_model(requested)
    if "model" in body or forwarded:
        body["model"] = forwarded
    return json.dumps(body).encode(), requested, forwarded, bool(body.get("stream"))


# --- auth ------------------------------------------------------------------------

def _client_authorized(request: Request) -> bool:
    secret = _bridge_secret()
    if not secret:
        return True  # unauthenticated mode (warned at startup)
    presented = request.headers.get("x-api-key", "").strip()
    if not presented:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
    return bool(presented) and hmac.compare_digest(presented, secret)


def _forward_headers(request: Request, api_key: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _STRIP_REQUEST_HEADERS}
    # Z.ai accepts either; the CLI's ANTHROPIC_AUTH_TOKEN path uses Bearer.
    headers["Authorization"] = f"Bearer {api_key}"
    headers["x-api-key"] = api_key
    headers["Accept-Encoding"] = "identity"
    if not any(k.lower() == "anthropic-version" for k in headers):
        headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items()
            if k.lower() not in _STRIP_RESPONSE_HEADERS}


# --- streaming -------------------------------------------------------------------

async def _stream_with_keepalive(chunks: AsyncIterator[bytes], aclose, interval: float
                                 ) -> AsyncIterator[bytes]:
    """Forward upstream chunks verbatim; emit an SSE comment whenever the upstream
    has been idle for ``interval`` seconds.  The comment is only ever yielded
    between reads, so it cannot land inside a ``data:`` event."""
    ait = chunks.__aiter__()
    nxt: Optional[asyncio.Future] = None
    try:
        while True:
            nxt = asyncio.ensure_future(ait.__anext__())
            while True:
                try:
                    if interval and interval > 0:
                        chunk = await asyncio.wait_for(asyncio.shield(nxt), interval)
                    else:
                        chunk = await nxt
                except asyncio.TimeoutError:
                    yield _KEEPALIVE_LINE
                    continue
                except StopAsyncIteration:
                    nxt = None
                    return
                else:
                    nxt = None
                    yield chunk
                    break
    except asyncio.CancelledError:
        if nxt is not None and not nxt.done():
            nxt.cancel()
        raise
    finally:
        await aclose()


def _usage_line(body: bytes) -> str:
    """One-line usage summary from a non-streaming Anthropic/OpenAI response."""
    try:
        usage = json.loads(body).get("usage") or {}
    except (ValueError, AttributeError):
        return ""
    if not isinstance(usage, dict):
        return ""
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "prompt_tokens", "completion_tokens")
    parts = [f"{k}={usage[k]}" for k in keys if k in usage]
    return " ".join(parts)


# --- app -------------------------------------------------------------------------

def build_app(provider: Optional[CredentialProvider] = None,
              client: Optional[httpx.AsyncClient] = None) -> FastAPI:
    """Construct the FastAPI app.  ``provider`` and ``client`` are injectable for
    tests (an ``httpx.AsyncClient`` with an ASGI transport stands in for Z.ai)."""
    provider = provider or CredentialProvider()
    app = FastAPI(title="glm-bridge", version="1.0.0")
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_timeout(streaming=True))

    if not _bridge_secret():
        _LOG.warning(
            "%s is not set: the bridge is UNAUTHENTICATED; any local process can "
            "spend this GLM plan.  Set it (and point clients' ANTHROPIC_API_KEY at "
            "the same value) to lock it down.", SECRET_ENV)

    def _unauthorized() -> JSONResponse:
        return JSONResponse({"type": "error", "error": {
            "type": "authentication_error",
            "message": "glm-bridge: missing/invalid bridge secret"}}, status_code=401)

    def _key_or_503():
        try:
            return provider.get_api_key(), None
        except CredentialsError as e:
            return None, JSONResponse({"type": "error", "error": {
                "type": "credentials_error", "message": f"glm-bridge: {e}"}}, status_code=503)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        authed = _client_authorized(request)
        try:
            key = provider.get_api_key()
        except CredentialsError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        info: dict[str, Any] = {"ok": True, "upstream": _upstream_base(),
                                "model": _main_model(), "small_model": _small_model()}
        if authed:
            info["key_prefix"] = key_prefix(key)
        return JSONResponse(info)

    @app.get("/quota")
    async def quota() -> JSONResponse:
        # Shape-compatible with the other bridges' /quota so shared tooling that
        # polls it keeps working; a Z.ai key has no per-account reset to report.
        return JSONResponse({"multi_account": False, "accounts": [], "next_reset_at_unix": None})

    async def _proxy(request: Request, url: str, raw: bytes) -> Response:
        """Forward ``raw`` (already rewritten) to ``url`` and relay the answer."""
        key, err = _key_or_503()
        if err is not None:
            return err
        prepared, requested, forwarded, wants_stream = rewrite_body(raw)
        headers = _forward_headers(request, key)
        t0 = time.monotonic()
        upstream_req = client.build_request(request.method, url, content=prepared,
                                            headers=headers, params=dict(request.query_params))
        try:
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            _LOG.warning("upstream request failed: %s", e)
            return JSONResponse({"type": "error", "error": {
                "type": "upstream_error",
                "message": f"glm-bridge: upstream request failed: {e}"}}, status_code=502)

        model_note = f"model={forwarded}" + (f" (requested {requested})"
                                             if requested and requested != forwarded else "")
        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            retry_after = upstream.headers.get("retry-after")
            _LOG.warning("upstream %s %s%s: %s", upstream.status_code, model_note,
                         f" retry-after={retry_after}" if retry_after else "",
                         body[:300].decode("utf-8", "replace"))
            return Response(content=body, status_code=upstream.status_code,
                            headers=_response_headers(upstream),
                            media_type=upstream.headers.get("content-type", "application/json"))

        if wants_stream:
            _LOG.info("stream %s %s", url.rsplit("/", 1)[-1], model_note)
            gen = _stream_with_keepalive(upstream.aiter_raw(), upstream.aclose,
                                         _keepalive_interval())
            return StreamingResponse(gen, status_code=upstream.status_code,
                                     headers=_response_headers(upstream),
                                     media_type=upstream.headers.get("content-type",
                                                                     "text/event-stream"))

        body = await upstream.aread()
        await upstream.aclose()
        _LOG.info("%s %s %s %.1fs %s", upstream.status_code, url.rsplit("/", 1)[-1],
                  model_note, time.monotonic() - t0, _usage_line(body))
        return Response(content=body, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type", "application/json"))

    # OpenAI-compatible path (secondary): any OpenAI client -> GLM coding endpoint.
    @app.api_route("/v1/chat/completions", methods=["POST"])
    @app.api_route("/chat/completions", methods=["POST"])
    async def chat_completions(request: Request) -> Response:
        if not _client_authorized(request):
            return _unauthorized()
        return await _proxy(request, f"{_openai_upstream_base()}/chat/completions",
                            await request.body())

    # Anthropic-compatible path (primary): everything else under the upstream base,
    # so /v1/messages, /v1/messages/count_tokens, /v1/models all reach Z.ai.
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(path: str, request: Request) -> Response:
        if not _client_authorized(request):
            return _unauthorized()
        norm = path.strip("/")
        return await _proxy(request, f"{_upstream_base()}/{norm}", await request.body())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if owns_client:
            await client.aclose()

    return app
