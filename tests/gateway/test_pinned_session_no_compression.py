"""A pinned (X-Hermes-Session-Id) session NEM tömöríthető gateway-oldalon.

Miért van rá teszt: ezt a fixet a 72d0c8c991 (2026-07-03) már egyszer megoldotta, a
v2026-os rebase mégis elvesztette — méghozzá NÉMÁN: a `pinned_session` paraméter átjött
a `_run_agent` szignatúráján, de a törzsből eltűnt a használata, így semmi nem tört el,
csak a viselkedés állt vissza a hibásra. Következmény élő adaton (2026-08-26): a „Peak"
szálban 1682 üzenetből 1352 lett `compacted`, az aktív készlet közepén 34 napos lyukkal
és 219 user / 111 assistant aránnyal — a szál „durván elveszti a fonalat".

A két teszt szándékosan más szögből fog:
  • a viselkedés (a compress-forwarder korán kilép),
  • és hogy a paraméter ne válhasson újra HOLTTÁ.
"""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_external_session_skips_compression(monkeypatch):
    """_compress_context pinned sessionnél vissza sem hívja a tömörítőt."""
    import run_agent

    called = []

    class _Fake:
        _external_session = True
        _cached_system_prompt = "SYS"
        _compress_context = run_agent.AIAgent._compress_context

    import agent.conversation_compression as cc
    monkeypatch.setattr(cc, "compress_context",
                        lambda *a, **k: called.append(1) or ([], ""))

    msgs = [{"role": "user", "content": "szia"}]
    out_msgs, out_sp = _Fake()._compress_context(msgs, "sys-msg")

    assert out_msgs is msgs, "a pinned session üzeneteit érintetlenül kell visszaadni"
    assert out_sp == "SYS"
    assert not called, "pinned sessionnél NEM szabad tömöríteni"


def test_compression_still_runs_for_normal_session(monkeypatch):
    """Ellenpróba: a nem-pinned (CLI/messaging) út tömörítése változatlan."""
    import run_agent
    import agent.conversation_compression as cc

    called = []

    class _Fake:
        _external_session = False
        _compress_context = run_agent.AIAgent._compress_context

    monkeypatch.setattr(cc, "compress_context",
                        lambda *a, **k: called.append(1) or (["compressed"], "SP"))
    out_msgs, _ = _Fake()._compress_context([{"role": "user", "content": "x"}], "sys")
    assert called, "a normál session tömörítése nem eshet ki"
    assert out_msgs == ["compressed"]


def test_pinned_session_parameter_is_not_dead():
    """A `pinned_session` nem lehet PUSZTA SZIGNATÚRA: a törzsben használni kell.

    Pont ez a rebase-veszteség formája — a paraméter megmarad, a hatása eltűnik.
    """
    from gateway.platforms.api_server import APIServerAdapter

    src = inspect.getsource(APIServerAdapter._run_agent)
    assert "pinned_session" in src.split(") -> tuple:", 1)[1], \
        "a pinned_session paraméter nincs használva a _run_agent törzsében"
    body = src.split(") -> tuple:", 1)[1]
    assert re.search(r"_external_session\s*=\s*True", body), \
        "pinned sessionnél be kell állítani az _external_session jelzőt"
    assert re.search(r"compression_enabled\s*=\s*False", body), \
        "pinned sessionnél ki kell kapcsolni a compression-t"
