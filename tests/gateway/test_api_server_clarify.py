"""Tests for clarify support on the api_server platform.

The agent's ``clarify`` tool asks the user a question and blocks until they
answer. On the chat-completions SSE channel this surfaces as a custom
``event: hermes.clarify`` frame; the client answers via ``POST /v1/clarify``,
which unblocks the agent's worker thread via ``resolve_gateway_clarify``.
"""

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import clarify_gateway


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


async def _collect_sse(item, suppress: bool) -> str:
    """Drive ``_write_sse_chat_completion`` with a single queue *item* and the
    end sentinel; return the raw SSE body."""
    adapter = _make_adapter()

    async def handler(request: web.Request) -> web.StreamResponse:
        # Upstream's SSE reader awaits ``stream_q.get()``, so the queue must be the
        # asyncio-backed ThreadSafeAsyncQueue, not a plain ``queue.Queue``.
        from gateway.platforms.api_server import ThreadSafeAsyncQueue
        stream_q = ThreadSafeAsyncQueue()
        stream_q.put_nowait(item)
        stream_q.put_nowait(None)

        async def _agent():
            return (None, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

        agent_task = asyncio.create_task(_agent())
        return await adapter._write_sse_chat_completion(
            request, completion_id="cmpl-test", model="test-model",
            created=0, stream_q=stream_q, agent_task=agent_task,
        )

    app = web.Application()
    app.router.add_get("/sse", handler)
    async with TestClient(TestServer(app)) as client:
        headers = {"X-Hermes-Tool-Progress": "off"} if suppress else {}
        resp = await client.get("/sse", headers=headers)
        assert resp.status == 200
        return await resp.text()


@pytest.mark.asyncio
async def test_clarify_event_emitted_over_sse():
    payload = {"clarify_id": "abc123", "question": "Melyiket?", "choices": ["A", "B"]}
    body = await _collect_sse(("__clarify__", payload), suppress=False)
    assert "event: hermes.clarify" in body
    assert "abc123" in body
    assert "Melyiket?" in body


@pytest.mark.asyncio
async def test_clarify_not_suppressed_by_tool_progress_header():
    """Clarify is essential — the X-Hermes-Tool-Progress opt-out must NOT drop it."""
    payload = {"clarify_id": "keep1", "question": "Folytassam?", "choices": None}
    body = await _collect_sse(("__clarify__", payload), suppress=True)
    assert "event: hermes.clarify" in body
    assert "keep1" in body


@pytest.mark.asyncio
async def test_post_clarify_resolves_waiting_agent():
    """POST /v1/clarify unblocks a thread waiting inside the clarify primitive."""
    adapter = _make_adapter()
    app = web.Application()
    app.router.add_post("/v1/clarify", adapter._handle_clarify)

    clarify_id = "resolve99"
    clarify_gateway.register(clarify_id, session_key="sess-1",
                             question="?", choices=None)

    result: dict = {}

    def _wait():
        result["answer"] = clarify_gateway.wait_for_response(clarify_id, timeout=10.0)

    t = threading.Thread(target=_wait)
    t.start()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/clarify",
                                 json={"clarify_id": clarify_id, "response": "igen"})
        assert resp.status == 200
        assert (await resp.json())["resolved"] is True

    t.join(timeout=5)
    assert result.get("answer") == "igen"


@pytest.mark.asyncio
async def test_post_clarify_unknown_id_returns_404():
    adapter = _make_adapter()
    app = web.Application()
    app.router.add_post("/v1/clarify", adapter._handle_clarify)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/clarify",
                                 json={"clarify_id": "nope", "response": "x"})
        assert resp.status == 404
        assert (await resp.json())["resolved"] is False
