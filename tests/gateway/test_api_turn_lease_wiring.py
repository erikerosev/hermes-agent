"""Regresszió: az API-dispatch is a per-session turn-lease alá tartozik (#64934).

A lease-t a `GatewayRunner._handle_message` szerezte meg, de az API-szerver SAJÁT
`_run_agent` útvonalon fut, ami nem megy át azon — így két párhuzamos API-kérés
ugyanarra a session_id-ra egyszerre futtatott turn-t.

Miért ütközik két kérés egy session_id-n: a `_derive_chat_session_id` a
(system prompt + első user-üzenet) hashéből képzi az id-t, tehát egy beszélgetés
MINDEN kérése ugyanazt kapja — külön routing key-jel. Egy retry vagy egy
újracsatlakozó stream-kliens elég hozzá.

2026-08-18: 189 ilyen ütközés egy nap alatt; mindegyik saját claude-processzt
kért a pool egyetlen kulcsán → élő, nyomon követetlen processzek → 100% swap.
"""

import asyncio
from unittest.mock import patch

import pytest

from gateway.platforms.api_server import APIServerAdapter
from gateway.turn_lease import SessionTurnLeaseRegistry


class _GatewayStub:
    def __init__(self):
        self._turn_leases = SessionTurnLeaseRegistry()


class _AdapterStub:
    """Csak azt hordozza, amit a _run_agent az executor-ugrás ELŐTT megérint."""

    _run_agent = APIServerAdapter._run_agent

    def __init__(self, gateway_runner):
        self.gateway_runner = gateway_runner
        self._inflight_agent_runs = 0
        self.admitted = 0

    def _activate_admitted_request(self):
        self.admitted += 1


class _Harness:
    """Az executor-ugrást vezérelhető kapukra cseréli, taskonként (task-név = kulcs).

    EGYSZER patchel a teszt teljes idejére — beágyazott patch/restore mellett a
    később induló turn már a VALÓDI executorba futna be.
    """

    def __init__(self, keys):
        self.order = []
        self.started = {k: asyncio.Event() for k in keys}
        self.gate = {k: asyncio.Event() for k in keys}

    async def executor(self, _executor, _fn):
        key = asyncio.current_task().get_name()
        self.order.append(f"start:{key}")
        self.started[key].set()
        await self.gate[key].wait()
        self.order.append(f"end:{key}")
        return ({"response": key}, {})

    def turn(self, adapter, session_id, key):
        return asyncio.create_task(
            adapter._run_agent(
                user_message="szia",
                conversation_history=[],
                session_id=session_id,
                gateway_session_key=key,
            ),
            name=key,
        )


@pytest.mark.asyncio
async def test_two_api_turns_on_one_session_id_are_serialized():
    adapter = _AdapterStub(_GatewayStub())
    h = _Harness(["key-a", "key-b"])
    loop = asyncio.get_running_loop()

    with patch.object(loop, "run_in_executor", h.executor):
        task_a = h.turn(adapter, "api-deadbeef", "key-a")
        await asyncio.wait_for(h.started["key-a"].wait(), timeout=5)

        # 'b' UGYANARRA a session_id-ra érkezik, de MÁS routing key-jel — pont az
        # alias-eset, amit a routing-key-re kulcsolt busy guard nem lát.
        task_b = h.turn(adapter, "api-deadbeef", "key-b")
        await asyncio.sleep(0.15)
        assert not h.started["key-b"].is_set(), (
            "a második turn elindult, miközben az első még futott — nincs sorosítás")

        h.gate["key-a"].set()
        await asyncio.wait_for(task_a, timeout=5)
        await asyncio.wait_for(h.started["key-b"].wait(), timeout=5)
        h.gate["key-b"].set()
        await asyncio.wait_for(task_b, timeout=5)

    assert h.order == ["start:key-a", "end:key-a", "start:key-b", "end:key-b"]


@pytest.mark.asyncio
async def test_turns_on_distinct_sessions_still_run_concurrently():
    """A lease NEM sorosíthat különböző beszélgetéseket — az áteresztőképesség marad."""
    adapter = _AdapterStub(_GatewayStub())
    h = _Harness(["key-a", "key-b"])
    loop = asyncio.get_running_loop()

    with patch.object(loop, "run_in_executor", h.executor):
        task_a = h.turn(adapter, "api-1111", "key-a")
        task_b = h.turn(adapter, "api-2222", "key-b")

        # Mindkettő elindul, egyik sem várja a másikat.
        await asyncio.wait_for(h.started["key-a"].wait(), timeout=5)
        await asyncio.wait_for(h.started["key-b"].wait(), timeout=5)

        h.gate["key-a"].set()
        h.gate["key-b"].set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_a_second_turn_is_not_blocked_after_the_first_completes():
    """A lease felszabadul: ugyanaz a session újra kiszolgálható."""
    adapter = _AdapterStub(_GatewayStub())
    h = _Harness(["key-a", "key-b"])
    loop = asyncio.get_running_loop()

    with patch.object(loop, "run_in_executor", h.executor):
        h.gate["key-a"].set()
        await asyncio.wait_for(h.turn(adapter, "api-seq", "key-a"), timeout=5)
        h.gate["key-b"].set()
        await asyncio.wait_for(h.turn(adapter, "api-seq", "key-b"), timeout=5)

    assert h.order == ["start:key-a", "end:key-a", "start:key-b", "end:key-b"]


@pytest.mark.asyncio
async def test_lease_is_released_even_when_the_turn_raises():
    """Hibás turn se hagyja fogva a session-t — különben a beszélgetés bewedgelődik."""
    gw = _GatewayStub()
    adapter = _AdapterStub(gw)

    async def _boom(_executor, _fn):
        raise RuntimeError("turn felrobbant")

    loop = asyncio.get_running_loop()
    with patch.object(loop, "run_in_executor", _boom):
        with pytest.raises(RuntimeError):
            await adapter._run_agent(
                user_message="szia", conversation_history=[],
                session_id="api-boom", gateway_session_key="key-x")

    lease = gw._turn_leases._leases.get("api-boom")
    assert lease is not None and lease.holder is None, "a lease nem szabadult fel"
    assert not lease.lock.locked()


@pytest.mark.asyncio
async def test_missing_gateway_runner_fails_open():
    """Lease-registry nélkül (bare adapter) a kérés fusson le, ne dőljön el."""
    adapter = _AdapterStub(gateway_runner=None)
    h = _Harness(["k"])
    h.gate["k"].set()
    loop = asyncio.get_running_loop()

    with patch("gateway.run._gateway_runner_ref", lambda: None):
        with patch.object(loop, "run_in_executor", h.executor):
            result, _usage = await asyncio.wait_for(
                h.turn(adapter, "api-none", "k"), timeout=5)

    assert result == {"response": "k"}
