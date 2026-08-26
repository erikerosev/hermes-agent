"""Regression: _run_agent accepted callbacks it never forwarded to _create_agent.

`_run_agent` declared `clarify_callback`, `status_callback`, `thinking_callback`
and `thinking_tokens`, and the /v1 SSE handler passed all four. Inside, the
`_create_agent(...)` call listed none of them, so they were silently discarded:
the AIAgent was built with `status_callback=None` / `thinking_callback=None`,
the clarify tool reported itself unavailable, and `agent._thinking_tokens`
stayed None — which made the claude-cli runner fall back to the env default
(`HERMES_CLAUDE_THINKING_TOKENS`, normally "0").

Visible symptom: the UI's "Élő gondolatmenet" switch did nothing, and the
process manager's toggle-respawn could never fire either, because the budget it
compares against never changed. Measured evidence: of 1665 claude transcripts
only 8 contain thinking blocks, all from the two days the feature was built
(2026-07-20/21); three live probes after a fresh gateway produced no respawn.

The test is structural on purpose. The bug was not a wrong value but a
*dropped* parameter, so it asserts that every argument the two signatures share
is actually forwarded at the call site — the shape of mistake that recurs
whenever a parameter is added to one signature and not threaded through.
"""

import ast
import inspect
from pathlib import Path

from gateway.platforms.api_server import APIServerAdapter


def _forwarded_kwargs(caller: str, callee: str) -> set[str]:
    """Keyword names passed to ``callee`` from inside ``caller``'s body."""
    src = Path(inspect.getfile(APIServerAdapter)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != caller:
            continue
        names: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if isinstance(fn, ast.Attribute) and fn.attr == callee:
                names |= {kw.arg for kw in sub.keywords if kw.arg}
        return names
    raise AssertionError(f"{caller} not found")


def _params(fn) -> set[str]:
    return {p for p in inspect.signature(fn).parameters if p != "self"}


class TestRunAgentForwardsToCreateAgent:
    def test_thinking_and_clarify_reach_create_agent(self):
        forwarded = _forwarded_kwargs("_run_agent", "_create_agent")
        for name in (
            "clarify_callback",
            "status_callback",
            "thinking_callback",
            "thinking_tokens",
        ):
            assert name in forwarded, f"_run_agent drops {name} on the floor"

    def test_no_shared_parameter_is_silently_dropped(self):
        shared = _params(APIServerAdapter._run_agent) & _params(
            APIServerAdapter._create_agent
        )
        forwarded = _forwarded_kwargs("_run_agent", "_create_agent")
        assert shared - forwarded == set(), (
            "these are accepted by both signatures but never handed over: "
            f"{sorted(shared - forwarded)}"
        )
