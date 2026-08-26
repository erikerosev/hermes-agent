"""The aux concurrency bound: side tasks must not crowd out real turns.

`_max_concurrent_runs` counts every in-flight run but cannot tell a user turn
from a background one, so a runaway side-task loop can take every slot — which
is exactly how the naplo loop 429'd a person mid-conversation on 2026-08-18.
`_acquire_aux_slot` reserves the difference: at most HERMES_AUX_MAX_CONCURRENCY
side tasks at a time, with an acquire timeout so a wedged run cannot park the
rest in an unbounded queue.
"""

import asyncio
import json

import pytest

from gateway.platforms.api_server import APIServerAdapter


class _Stub:
    """Minimal carrier for the slot methods (they only touch _aux_semaphore)."""

    _get_aux_semaphore = APIServerAdapter._get_aux_semaphore
    _aux_acquire_timeout = staticmethod(APIServerAdapter._aux_acquire_timeout)
    _acquire_aux_slot = APIServerAdapter._acquire_aux_slot
    _release_aux_slot = APIServerAdapter._release_aux_slot
    _aux_saturated_response = APIServerAdapter._aux_saturated_response


class TestAcquireTimeout:
    def test_defaults_to_30s(self, monkeypatch):
        monkeypatch.delenv("HERMES_AUX_ACQUIRE_TIMEOUT", raising=False)
        assert APIServerAdapter._aux_acquire_timeout() == 30.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "2.5")
        assert APIServerAdapter._aux_acquire_timeout() == 2.5

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "soon")
        assert APIServerAdapter._aux_acquire_timeout() == 30.0


@pytest.mark.asyncio
class TestSlotBound:
    async def test_second_side_task_is_refused_when_the_bound_is_one(self, monkeypatch):
        monkeypatch.setenv("HERMES_AUX_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "0.1")
        stub = _Stub()
        assert await stub._acquire_aux_slot() is True
        assert await stub._acquire_aux_slot() is False

    async def test_refusal_does_not_hang_past_the_timeout(self, monkeypatch):
        monkeypatch.setenv("HERMES_AUX_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "0.2")
        stub = _Stub()
        assert await stub._acquire_aux_slot() is True
        # A wedged holder must not park the caller forever.
        await asyncio.wait_for(stub._acquire_aux_slot(), timeout=3)

    async def test_release_frees_the_slot_for_the_next_side_task(self, monkeypatch):
        monkeypatch.setenv("HERMES_AUX_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "0.1")
        stub = _Stub()
        assert await stub._acquire_aux_slot() is True
        assert await stub._acquire_aux_slot() is False
        stub._release_aux_slot()
        assert await stub._acquire_aux_slot() is True

    async def test_default_bound_admits_two_but_not_three(self, monkeypatch):
        monkeypatch.delenv("HERMES_AUX_MAX_CONCURRENCY", raising=False)
        monkeypatch.setenv("HERMES_AUX_ACQUIRE_TIMEOUT", "0.1")
        stub = _Stub()
        assert await stub._acquire_aux_slot() is True
        assert await stub._acquire_aux_slot() is True
        assert await stub._acquire_aux_slot() is False

    async def test_release_without_a_semaphore_is_a_noop(self):
        _Stub()._release_aux_slot()  # must not raise


class TestSaturatedResponse:
    def test_is_a_429_the_aux_client_recognises_as_a_rate_limit(self):
        resp = _Stub()._aux_saturated_response()
        assert resp.status == 429
        assert resp.headers["Retry-After"] == "5"
        payload = json.loads(resp.body.decode())
        assert payload["error"]["type"] == "rate_limit_error"
        assert payload["error"]["code"] == "rate_limit_exceeded"
