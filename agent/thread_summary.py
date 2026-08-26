"""Gördülő szál-összefoglaló a persistent claude-processz primingjához.

A PROBLÉMA. Egy friss (respawn/recycle után induló) claude-processzt a szál eddigi
előzményével töltünk fel (`_render_history`). Ez az előzmény bounded — az utolsó
HERMES_CLAUDE_MAX_PRIME_CHARS karakter —, a szál viszont nő. Amikor átlépi az ablakot,
minden respawnnál a szál ELEJE csendben leesik. Élő adaton mérve (2026-08-26): a „Peak"
szál 114 600 tokennyi történetéből 50 000 fér az ablakba, tehát 64 600 token esik ki; a
„Lineric" szál 40 800-at veszít. Pont a legértékesebb rész megy el: ahol a döntések és
az indoklásuk születtek. Innen a felhasználói panasz, hogy „néha nem emlékszik".

A MEGOLDÁS. A kieső részről tömör összefoglalót tartunk, és azt tesszük a priming elé:
a friss körök szó szerint, a régiek sűrítve. Az összefoglaló INKREMENTÁLIS — a korábbi
összefoglaló + az azóta kiesett rész → új összefoglaló —, tehát a hívás mérete nem nő a
szállal együtt.

KÉT NEM-ALKUDHATÓ TULAJDONSÁG:
  • A user-turn SOSEM vár rá. Ha nincs kész (vagy elavult) az összefoglaló, a priming a
    korábbi viselkedést adja, a frissítés pedig HÁTTÉRSZÁLON fut a következő respawnra.
    Egy összefoglaló-hívás 1-2 perc — azt a latenciát nem tehetjük a beszélgetésbe.
  • Fail-safe: bármilyen hiba esetén némán a régi viselkedés marad. Az összefoglaló
    kényelmi réteg, sosem törheti el a turn-t.

A hívás a KÖNNYŰ aux-úton megy (persona/MCP nélkül, semleges munkakönyvtárból), mint a
kozos-memória jegyzetelése.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "thread_summary.db"
# Ennél kevesebb kieső sornál nem éri meg összefoglalni (a priming úgyis majdnem teljes).
_MIN_DROPPED = int(os.environ.get("HERMES_SUMMARY_MIN_DROPPED", "20"))
# Az összefoglalót akkor frissítjük, ha ennyi ÚJ sor esett ki a legutóbbi óta — különben
# minden respawn új hívást indítana pár sornyi különbségért.
_REFRESH_STEP = int(os.environ.get("HERMES_SUMMARY_REFRESH_STEP", "15"))
_MODEL = os.environ.get("HERMES_SUMMARY_MODEL", "apollo-aux:sonnet")
_TIMEOUT = float(os.environ.get("HERMES_SUMMARY_TIMEOUT", "240"))
_MAX_INPUT_CHARS = int(os.environ.get("HERMES_SUMMARY_MAX_INPUT", "120000"))
# Az összefoglaló felső mérete. Bőkezű: ~12k karakter ≈ 3k token, ami elenyésző a
# 200k karakteres priming-ablakhoz képest, cserébe több tízezer tokennyi történetet
# képvisel. (Élő próbán a Peak szál összefoglalója 6000-nél FÉLMONDATBAN vágódott el.)
_MAX_SUMMARY_CHARS = int(os.environ.get("HERMES_SUMMARY_MAX_CHARS", "12000"))

_lock = threading.Lock()
_inflight: set[str] = set()

_PROMPT = """\
Az alábbi egy hosszú munkabeszélgetés KORÁBBI szakasza. A friss része külön megvan, azt \
nem kell lefedned. Készíts belőle tömör, tényszerű ÖSSZEFOGLALÓT, hogy az asszisztens \
folytatni tudja a munkát anélkül, hogy elolvasná az egészet.

Vedd bele:
- mi a feladat/projekt, és hol tart most,
- a MEGHOZOTT DÖNTÉSEKET és az indokukat,
- a megállapodásokat, és az ELVETETT irányokat azzal együtt, hogy miért vetettétek el,
- a fontos neveket, fájlokat, számokat, azonosítókat.

Ne kommentáld a feladatot, ne szólíts meg senkit, ne írj bevezetőt. Tömör magyar pontok, \
legfeljebb kb. 8000 karakter — de csak annyi, amennyi tényleg kell. Ha kapsz KORÁBBI \
ÖSSZEFOGLALÓT, azt olvaszd össze az új szakasszal EGY összefoglalóvá (ne fűzd egymás \
után, és a régiből se veszíts döntést)."""


def _conn() -> sqlite3.Connection | None:
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), timeout=5)
        c.execute("CREATE TABLE IF NOT EXISTS thread_summary "
                  "(session TEXT PRIMARY KEY, summary TEXT, covered INTEGER, updated_at INTEGER)")
        return c
    except Exception as exc:
        logger.debug("thread_summary: DB init failed: %s", exc)
        return None


def get(session: str | None) -> tuple[str, int]:
    """(összefoglaló, hány előzmény-sort fed le). ("", 0) ha nincs."""
    if not session:
        return "", 0
    c = _conn()
    if c is None:
        return "", 0
    try:
        with c:
            row = c.execute("SELECT summary, covered FROM thread_summary WHERE session = ?",
                            (session,)).fetchone()
        return (row[0] or "", int(row[1] or 0)) if row else ("", 0)
    except Exception:
        return "", 0
    finally:
        c.close()


def _store(session: str, summary: str, covered: int) -> None:
    c = _conn()
    if c is None:
        return
    try:
        with c:
            c.execute("INSERT INTO thread_summary (session, summary, covered, updated_at) "
                      "VALUES (?,?,?,?) ON CONFLICT(session) DO UPDATE SET "
                      "summary=excluded.summary, covered=excluded.covered, "
                      "updated_at=excluded.updated_at",
                      (session, summary, covered, int(time.time())))
    except Exception as exc:
        logger.debug("thread_summary: store failed: %s", exc)
    finally:
        c.close()


def _aux_complete(prompt: str) -> str:
    base = (os.environ.get("HERMES_AUX_BASE_URL")
            or "http://localhost:8642/v1").rstrip("/")
    key = os.environ.get("HERMES_AUX_API_KEY") or os.environ.get("API_SERVER_KEY") or "x"
    r = httpx.post(f"{base}/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": _MODEL, "stream": False, "temperature": 0,
                         "messages": [{"role": "user", "content": prompt}]},
                   timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def _clip(text: str, limit: int) -> str:
    """Bekezdés- (végső soron sor-) határon vág, hogy az összefoglaló ne félmondatban
    érjen véget — egy csonka döntés-leírás félrevezetőbb, mint a hiánya."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("\n\n", "\n", ". "):
        idx = head.rfind(sep)
        if idx > limit * 0.6:
            return head[:idx].rstrip()
    # Végső esetben legalább szóhatáron (sose szó közepén).
    return head.rsplit(" ", 1)[0].rstrip() if " " in head else head.rstrip()


def _refresh(session: str, dropped_text: str, covered: int, prev: str) -> None:
    try:
        # DARABOLVA, ha a lefedendő szakasz nem fér egy hívásba. A rendes működésben a
        # kieső rész lépésenként nő, tehát egy hívás elég — de egy NAGY egyszeri ugrásnál
        # (első összefoglaló egy régi szálon, vagy visszaállított átirat) a levágott input
        # miatt a modell csak a szakasz VÉGÉT látná. Élesben pont ez történt: 1452 sorra
        # 4 265 karakteres összefoglaló született, miközben 490 sorhoz 11 299 kellett.
        # Darabonként haladunk, mindegyik az addigi összefoglalót kapja kiindulásnak.
        chunks = [dropped_text[i:i + _MAX_INPUT_CHARS]
                  for i in range(0, len(dropped_text), _MAX_INPUT_CHARS)] or [""]
        out = prev
        for n, body in enumerate(chunks, 1):
            prompt = _PROMPT
            if out:
                prompt += "\n\n--- KORÁBBI ÖSSZEFOGLALÓ ---\n" + out
            prompt += "\n\n--- A BESZÉLGETÉS KORÁBBI SZAKASZA ---\n" + body
            got = (_aux_complete(prompt) or "").strip()
            if len(got) < 80:
                logger.warning("thread_summary: túl rövid válasz a(z) %d/%d darabnál (%d kar)",
                               n, len(chunks), len(got))
                continue
            out = _clip(got, _MAX_SUMMARY_CHARS)
            if len(chunks) > 1:
                logger.info("thread_summary: %s — %d/%d darab kész (%d kar)",
                            session, n, len(chunks), len(out))
        if not out or len(out) < 80:
            logger.warning("thread_summary: nem született használható összefoglaló")
            return
        _store(session, out, covered)
        logger.info("thread_summary: %s frissítve (%d sort fed le, %d kar, %d darab)",
                    session, covered, len(out), len(chunks))
    except Exception as exc:
        logger.warning("thread_summary: frissítés sikertelen (%s)", exc)
    finally:
        with _lock:
            _inflight.discard(session)


def maybe_refresh(session: str | None, dropped_text: str, covered: int, prev: str) -> None:
    """Háttérszálon frissíti az összefoglalót, ha érdemes. SOSEM blokkol."""
    if not session or covered < _MIN_DROPPED or not dropped_text.strip():
        return
    with _lock:
        if session in _inflight:
            return
        _inflight.add(session)
    try:
        threading.Thread(target=_refresh, args=(session, dropped_text, covered, prev),
                         daemon=True, name=f"summary-{session[:8]}").start()
    except Exception:
        with _lock:
            _inflight.discard(session)


def should_refresh(covered_now: int, covered_stored: int) -> bool:
    """Elavult-e a tárolt összefoglaló ahhoz képest, ami MOST esik ki a primingból."""
    return covered_now - covered_stored >= _REFRESH_STEP
