"""Post-task review — closes the self-improvement loop for autonomous kanban work.

The naplo write-back is PRIMARY-ONLY (workers/cron are excluded so execution noise
doesn't pollute the project log). That means an autonomous kanban worker, by itself,
teaches the system nothing. This review is the bridge: after a task completes, an
aux model reads the task + its result, decides which enrolled project it belongs to
and distills ONE durable lesson/decision (or none), and appends it to that project's
``naplo.md`` — the same ledger naplo writes. Each task is reviewed once (tracked in a
state file); routing reuses naplo's classify approach, so it does NOT depend on the
board being named after the project.

Wired into ``hermes kanban dispatch`` (and the daemon) so it runs automatically after
each pass. Gated by ``HERMES_KANBAN_REVIEW`` (default on; set 0/off to disable).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("kanban_review")

_STATE = Path(os.path.expanduser("~/.hermes/kanban_reviewed.json"))
_ENROLL_MARKERS = ("tech_leiras.md", "naplo.md")
_NAPLO_FILE = "naplo.md"


def _enabled() -> bool:
    return os.environ.get("HERMES_KANBAN_REVIEW", "1").strip().lower() not in ("0", "off", "false", "no")


def _roots() -> List[Path]:
    raw = os.environ.get("HERMES_PROJECTS_ROOT") or "~/project, ~/hermes-projects"
    out: List[Path] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(os.path.expanduser(part)).resolve()
        if p not in out:
            out.append(p)
    return out


def _enrolled_projects() -> dict:
    """slug -> dir, for every project carrying an enrollment marker (mirrors naplo)."""
    reg: dict = {}
    for root in _roots():
        if not root.is_dir():
            continue
        for marker in _ENROLL_MARKERS:
            for hit in list(root.glob(f"*/{marker}")) + list(root.glob(f"*/*/{marker}")):
                slug = hit.parent.name
                reg.setdefault(slug, hit.parent)
    return reg


def _load_state() -> set:
    try:
        return set(json.loads(_STATE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_state(ids: set) -> None:
    try:
        _STATE.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except Exception as e:
        logger.debug("kanban_review: state save failed: %s", e)


_SYS = (
    "Egy ELVÉGZETT kanban-feladatból kell self-improvement célból rögzítened a tartós "
    "tudást a megfelelő projekt változásnaplójába. Adott az ismert projektek listája. "
    "Döntsd el: (1) melyik projekthez tartozik a feladat (slug, vagy null ha egyikhez sem), "
    "(2) keletkezett-e VALÓDI, tartós tanulság/döntés/megváltozott állapot (NEM a végrehajtás "
    "lépései, NEM triviális). Csak akkor adj entry-t, ha tényleg érdemes megjegyezni. "
    'Válasz CSAK JSON: {"project": "<slug vagy null>", "entry": "<egy tömör magyar mondat vagy null>"}'
)


def _classify(title: str, body: Optional[str], result: Optional[str], projects: dict) -> Optional[Tuple[str, str]]:
    try:
        from agent.auxiliary_client import call_llm
    except Exception as e:
        logger.debug("kanban_review: call_llm unavailable: %s", e)
        return None
    proj_list = "\n".join(f"- {slug}" for slug in sorted(projects))
    user = (f"Ismert projektek:\n{proj_list}\n\n--- FELADAT ---\n"
            f"Cím: {title}\nLeírás: {(body or '')[:1000]}\nEredmény: {(result or '')[:1500]}")
    try:
        resp = call_llm(
            task="naplo",
            messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
            max_tokens=200, temperature=0.0, timeout=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.debug("kanban_review: classify call failed: %s", e)
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    slug = data.get("project")
    entry = (data.get("entry") or "").strip()
    if slug in (None, "null", "none", "") or slug not in projects:
        return None
    if not entry or entry.lower() in ("null", "none"):
        return None
    return slug, entry


def _append_naplo(proj_dir: Path, slug: str, entry: str, task_id: str) -> None:
    path = proj_dir / _NAPLO_FILE
    # de-dup against the last few lines
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]:
                if entry in line:
                    return
    except Exception:
        pass
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- {ts} — {entry}  _(forrás: kanban-task {str(task_id)[:12]})_"
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write(f"# Napló — {slug}\n\nAppend-only változásnapló (naplo write-back + kanban post-task review).\n\n")
        f.write(line + "\n")
    logger.info("kanban_review: logged lesson to %s/naplo.md — %s", slug, entry[:60])


def review_pending(board: Optional[str] = None, max_tasks: int = 20) -> int:
    """Review newly-completed tasks; append distilled lessons to project naplo.md.
    Returns the number of lessons logged. Safe/idempotent; never raises."""
    if not _enabled():
        return 0
    try:
        from hermes_cli import kanban_db as kb
    except Exception as e:
        logger.debug("kanban_review: kanban_db unavailable: %s", e)
        return 0
    projects = _enrolled_projects()
    if not projects:
        return 0
    try:
        db_path = kb.kanban_db_path(board)
        if not Path(db_path).exists():
            return 0
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            done = kb.list_tasks(conn, status="done", limit=200)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("kanban_review: list_tasks failed: %s", e)
        return 0

    seen = _load_state()
    logged = 0
    processed = 0
    for t in done:
        if t.id in seen:
            continue
        if processed >= max_tasks:
            break
        processed += 1
        seen.add(t.id)  # mark attempted (don't re-distill even if no lesson)
        verdict = _classify(t.title, getattr(t, "body", None), getattr(t, "result", None), projects)
        if not verdict:
            continue
        slug, entry = verdict
        try:
            _append_naplo(projects[slug], slug, entry, t.id)
            logged += 1
        except Exception as e:
            logger.debug("kanban_review: append failed for %s: %s", t.id, e)
    _save_state(seen)
    if logged:
        logger.info("kanban_review: %d lesson(s) logged from %d task(s)", logged, processed)
    return logged
