"""Kapu-napló — minden kikényszerítési döntés nyomot hagy.

Miért: 2026-09-11-én kimértük, hogy a rendszer szabályainak nagy része PRÓZA
(SOUL-szabály, persona-mondat), amit semmi nem tartat be, és amiről nincs adat:
nem tudtuk megmondani, hányszor sérült meg a „a memóriát tilos élő projekt-állapotra
használni" szabály. A KAPU ezzel szemben eseményt termel — és ez az egyetlen ok,
amiért az önfejlesztés nem elsodródás: mérés nélkül a következő javítás is hit
kérdése lenne (élő példa: a memória-konszolidáció három hétig némán rosszul merge-elt).

Szándékosan stdlib-only és fail-safe: a naplózás SOHA nem bukhat el úgy, hogy
közben a kaput is elrontja. Ha az írás nem megy, a kapu döntése attól még áll.

Rekord-alak (JSONL, append-only) — a fenris-oldali párja (backend/gatelog.py)
UGYANEZT az alakot írja a konténer saját kötetére, mert a host home ott RO:
  {"ts": epoch, "gate": "...", "decision": "allow|deny|route|empty",
   "reason": "...", "detail": {...}, "src": "hermes|fenris"}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DEFAULT = Path(os.path.expanduser("~/.hermes/gates/gates.jsonl"))
# Ennél nagyobb naplót forgatunk (a régit .1-re), hogy ne hízzon korlátlanul.
_MAX_BYTES = int(os.environ.get("HERMES_GATE_LOG_MAX", str(8 * 1024 * 1024)))


def _path() -> Path:
    p = os.environ.get("HERMES_GATE_LOG")
    return Path(p) if p else _DEFAULT


def log(gate: str, decision: str, reason: str = "", **detail) -> None:
    """Egy kapu-döntés rögzítése. Sosem dob."""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.exists() and p.stat().st_size > _MAX_BYTES:
                p.replace(p.with_suffix(p.suffix + ".1"))
        except OSError:
            pass
        rec = {"ts": time.time(), "gate": gate, "decision": decision,
               "reason": reason, "src": os.environ.get("HERMES_GATE_SRC", "hermes")}
        if detail:
            rec["detail"] = detail
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass            # a naplózás sosem ronthatja el a kaput
