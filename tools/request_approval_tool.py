"""request_approval — aszinkron emberi jóváhagyás az Apollo push/control csatornán.

Amikor egy agent (főleg háttér-kanban-worker) kockázatos/visszafordíthatatlan
lépés előtt áll ÉS a felhasználó nincs a gép előtt, ezt hívja. A hívás létrehoz
egy pending jóváhagyást az Apollo control-backendben (fenris), ami push-t küld a
telefonra és egy igen/nem kártyát mutat a `/control`-on. A tool BLOKKOL (pollozza
a döntést), amíg a user dönt vagy lejár, majd visszaadja a verdiktet, hogy az
agent továbbmehessen vagy leálljon.

Le van csatolva a gateway belső veszélyes-parancs jóváhagyó gépezetéről — sima
HTTP-t beszél a fenris approval-REST-tel. Token-auth a megosztott
`~/.hermes/apollo_notify_token.txt`-vel (ugyanaz, mint scripts/push.py|approve.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_API_BASE = os.environ.get("APOLLO_API_BASE", "http://localhost:8001")
_TOKEN_PATH = Path(os.environ.get(
    "APOLLO_NOTIFY_TOKEN_PATH",
    str(Path.home() / ".hermes" / "apollo_notify_token.txt"),
))
_DEFAULT_TIMEOUT = int(os.environ.get("APOLLO_APPROVAL_TIMEOUT", "600"))
_MAX_TIMEOUT = int(os.environ.get("APOLLO_APPROVAL_MAX_TIMEOUT", "1500"))
_POLL = 3.0


def _token() -> str:
    try:
        return _TOKEN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _req(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        _API_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Apollo-Notify-Token": _token()},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_requirements() -> bool:
    """Csak akkor elérhető, ha a megosztott token létezik (van control-backend)."""
    return bool(_token())


def request_approval(title: str, detail: str = "", timeout=None,
                     profile=None, task=None) -> str:
    if not title:
        return "ERROR: 'title' is required. Nothing was sent."
    if not _token():
        return ("ERROR: approval channel not configured (no token). "
                "Treat as DENIED and stop — do NOT perform the action.")
    try:
        timeout = int(timeout) if timeout else _DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(30, min(timeout, _MAX_TIMEOUT))
    task = task or os.environ.get("HERMES_KANBAN_TASK") or None

    try:
        body = {"title": title, "detail": detail or "", "ttl": timeout}
        if profile:
            body["profile"] = profile
        if task:
            body["task"] = task
        rec = _req("POST", "/api/approvals", body)
    except Exception as e:
        logger.warning("request_approval: create failed: %s", e)
        return (f"ERROR: could not reach the approval channel ({e}). "
                "Treat as DENIED and stop — do NOT perform the action.")

    aid = rec.get("id")
    logger.info("request_approval: waiting on %s (timeout=%ss): %s", aid, timeout, title)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(_POLL)
        try:
            cur = _req("GET", f"/api/approvals/{aid}")
        except Exception as e:
            logger.debug("request_approval: poll error: %s", e)
            continue
        status = cur.get("status")
        if status == "resolved":
            decision = (cur.get("decision") or "").strip()
            if decision == "approve":
                return ("APPROVED by the user. You may proceed with exactly the action "
                        "you described — nothing broader.")
            return ("DENIED by the user. Do NOT perform the action. Stop and report that "
                    "the user declined; if useful, propose a safer alternative.")
        if status == "expired":
            break
    return ("NO RESPONSE within the time limit — treat as DENIED. Do NOT perform the "
            "action. Stop and hand it back to the user (e.g. kanban_block) instead of guessing.")


REQUEST_APPROVAL_SCHEMA = {
    "name": "request_approval",
    "description": (
        "Ask the human to APPROVE or DENY a risky action, asynchronously, via a "
        "phone push notification + an approve/deny card in the Apollo control panel. "
        "This BLOCKS until the user decides or it times out, then returns the verdict.\n\n"
        "Use this BEFORE an irreversible or high-impact action when the user is likely "
        "AWAY from the keyboard — e.g. deleting data, overwriting files, deploying, "
        "spending money, sending an external message, running a destructive command. "
        "This is the safe gate for autonomous/background (kanban daemon) work.\n\n"
        "Choose the right tool: use `clarify` for an in-app question when the user is "
        "present; use `kanban_block` to PARK a task on ambiguity without notifying; use "
        "`request_approval` when you specifically need a yes/no GO decision and want to "
        "reach the user wherever they are. If the result is DENIED or times out, you MUST "
        "NOT perform the action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short, specific question shown as the notification + card title, "
                               "e.g. 'Töröljek 12 fájlt a /tmp/x alatt?'",
            },
            "detail": {
                "type": "string",
                "description": "Context the user needs to decide: what exactly will happen, the "
                               "impact, and why. Keep it concrete. No secrets/tokens.",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait for a decision (default 600, max 1500). "
                               "On timeout the verdict is treated as DENIED.",
            },
        },
        "required": ["title"],
    },
}


from tools.registry import registry  # noqa: E402

registry.register(
    name="request_approval",
    toolset="approval",
    schema=REQUEST_APPROVAL_SCHEMA,
    handler=lambda args, **kw: request_approval(
        title=args.get("title", ""),
        detail=args.get("detail", ""),
        timeout=args.get("timeout"),
        profile=args.get("profile"),
    ),
    check_fn=check_requirements,
    emoji="🔔",
)
