"""A gördülő szál-összefoglaló bekötése a persistent-processz primingjába.

Amit véd: egy hosszú szál priming-ablakából kieső rész NE tűnjön el csendben, és a
összefoglaló + a nyers rész között se átfedés, se LYUK ne legyen. (A lyuk-eset valós:
covered=30 mellett cut=32-nél a 30-31. üzenet sehol nem szerepelt.)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import claude_process_manager as cpm  # noqa: E402
from agent import thread_summary as ts  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Izolált összefoglaló-DB + a háttér-frissítés kikapcsolva (nem hívunk LLM-et)."""
    monkeypatch.setattr(ts, "_DB_PATH", tmp_path / "thread_summary.db")
    calls = []
    monkeypatch.setattr(ts, "maybe_refresh",
                        lambda s, dropped, covered, prev: calls.append((s, covered)))
    monkeypatch.setattr(cpm, "_MAX_PRIME_CHARS", 1000)
    return calls


def _history(n=40):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"uzenet-{i:03} " + "x" * 90} for i in range(n)]


def test_rovid_szal_valtozatlan(store, monkeypatch):
    """Ami befér az ablakba, az teljes egészében megy — összefoglaló nélkül."""
    monkeypatch.setattr(cpm, "_MAX_PRIME_CHARS", 10 ** 7)
    out = cpm._render_history(_history(), "T-rovid")
    assert "uzenet-000" in out and "uzenet-039" in out
    assert "osszefoglaloja" not in out
    assert store == []          # nincs mit összefoglalni


def test_osszefoglalo_nelkul_a_regi_viselkedes(store):
    """Nincs kész összefoglaló → a szál eleje kiesik (mint eddig), de a frissítés indul."""
    out = cpm._render_history(_history(), "T-nincs")
    assert "uzenet-000" not in out
    assert "uzenet-039" in out
    assert "lemaradtak" in out  # a modell tudja, hogy hiányos
    assert store and store[0][0] == "T-nincs"


def test_friss_osszefoglalo_nincs_atfedes(store):
    """Az összefoglaló többet fed le, mint ami kiesne → a nyers rész onnan indul."""
    ts._store("T-friss", "OSSZEFOGLALO-SZOVEG", 35)
    out = cpm._render_history(_history(), "T-friss")
    assert "OSSZEFOGLALO-SZOVEG" in out
    assert "uzenet-034" not in out   # lefedve, nem kell nyersen
    assert "uzenet-035" in out and "uzenet-039" in out


def test_elavult_osszefoglalo_nem_hagy_lyukat(store):
    """A REGRESSZIÓ: covered < cut esetén a köztes sorok NYERSEN kell hogy menjenek."""
    ts._store("T-elavult", "OSSZEFOGLALO-SZOVEG", 30)
    out = cpm._render_history(_history(), "T-elavult")
    assert "OSSZEFOGLALO-SZOVEG" in out
    assert all(f"uzenet-{i:03}" not in out for i in range(30))   # lefedve
    assert all(f"uzenet-{i:03}" in out for i in range(30, 40))   # egy sem veszhet el


def test_hibas_store_eseten_is_renderel(store, monkeypatch):
    """Fail-safe: az összefoglaló-réteg hibája nem törheti el a primingot."""
    monkeypatch.setattr(ts, "get", lambda s: (_ for _ in ()).throw(RuntimeError("db off")))
    out = cpm._render_history(_history(), "T-hibas")
    assert "uzenet-039" in out


def test_tul_nagy_nyers_resz_vagodik(store, monkeypatch):
    """Ha a frissítés tartósan bukna, a „inkább nyersen" szabály nem fújhatja fel
    korlátlanul a primingot."""
    ts._store("T-nagy", "OSSZEFOGLALO-SZOVEG", 0)
    monkeypatch.setattr(cpm, "_MAX_PRIME_CHARS", 200)
    out = cpm._render_history(_history(), "T-nagy")
    assert len(out) < 200 * 2 + 2000     # a cap + fejléc/összefoglaló
