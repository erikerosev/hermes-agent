"""Apollo thread ↔ claude-cli session-id leképezés perzisztens tárolása.

A claude-cli `--resume <session-id>` a SAJÁT lemez-session-jét tölti be (a
~/.claude/projects/.../<id>.jsonl transcriptet), így turn-önként friss processz
 kaphat teljes kontextust ANÉLKÜL, hogy a history-t újraküldenénk → nincs
stream-multiplexing-üres (mint a persistent), és nem lassul (mint a folded-preamble
stateless). Hogy a leképezés túlélje a gateway-restartot/cold-startot (a claude
disk-session is túléli), kis sqlite-ban tartjuk.

Kulcs: az Apollo session-id (= a fenris thread-id, X-Hermes-Session-Id).
Érték: a claude-cli legutóbbi session-id-je (rotálódhat — mindig a friss megy).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DB_PATH = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "claude_resume.db"
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection | None:
    global _conn
    if _conn is not None:
        return _conn
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        c.execute("CREATE TABLE IF NOT EXISTS resume_map "
                  "(apollo_session TEXT PRIMARY KEY, claude_session TEXT, updated_at INTEGER)")
        c.commit()
        _conn = c
        return c
    except Exception as exc:
        logger.warning("claude_resume_store: DB init failed: %s", exc)
        return None


def get_claude_session(apollo_session: str | None) -> str | None:
    """A threadhez tartozó claude-session-id, vagy None (új thread / nincs még)."""
    if not apollo_session:
        return None
    with _LOCK:
        c = _db()
        if c is None:
            return None
        try:
            row = c.execute(
                "SELECT claude_session FROM resume_map WHERE apollo_session = ?",
                (apollo_session,),
            ).fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None


def set_claude_session(apollo_session: str | None, claude_session: str | None) -> None:
    """Eltárolja/frissíti a thread claude-session-id-jét (rotáció-követés)."""
    if not apollo_session or not claude_session:
        return
    import time
    with _LOCK:
        c = _db()
        if c is None:
            return
        try:
            c.execute(
                "INSERT INTO resume_map (apollo_session, claude_session, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(apollo_session) DO UPDATE SET "
                "claude_session = excluded.claude_session, updated_at = excluded.updated_at",
                (apollo_session, claude_session, int(time.time())),
            )
            c.commit()
        except Exception as exc:
            logger.debug("claude_resume_store: set failed: %s", exc)
