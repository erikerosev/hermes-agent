"""claude_cli transport normalization — tool_use collection under partial messages.

Regression guard for the silent 2026-07-21 breakage: the runner passes
``--include-partial-messages`` (needed for the live thinking-stream), and the CLI
emits EVERY assistant message with ``stop_reason: null``, so the parser marks them
``is_partial=True``.  The normalizer used to collect ``tool_use`` blocks below the
is_partial guard, which dropped 100% of them — ``normalized.tool_calls`` was always
empty, so every consumer (notably the ``skill_capture`` plugin's per-session tool
counter, which gates skill self-capture) saw zero tool calls forever.
"""

from claude_code_core.types import MessageType, StreamEvent, ToolCategory, ToolUseEvent
from agent.transports import get_transport


def _tool_event(tool_id: str, name: str = "Read", *, partial: bool = True) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.ASSISTANT,
        is_partial=partial,
        tool_use=ToolUseEvent(
            tool_id=tool_id,
            tool_name=name,
            tool_input={"file_path": "/etc/hostname"},
            category=ToolCategory.OTHER,
        ),
    )


def _result_event(text: str = "done") -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.RESULT, text=text, is_complete=True, session_id="sid-1"
    )


def test_tool_use_collected_from_partial_assistant_events():
    """A tool_use riding on a partial assistant message must still be reported."""
    transport = get_transport("claude_cli")
    normalized = transport.normalize_response(
        [_tool_event("toolu_1"), _result_event()]
    )

    assert [tc.name for tc in (normalized.tool_calls or [])] == ["Read"]


def test_repeated_tool_use_deduped_by_tool_id():
    """Successive partial snapshots repeat the same block — count it once."""
    transport = get_transport("claude_cli")
    normalized = transport.normalize_response(
        [
            _tool_event("toolu_1"),
            _tool_event("toolu_1"),
            _tool_event("toolu_2", name="Bash"),
            _result_event(),
        ]
    )

    assert [tc.name for tc in (normalized.tool_calls or [])] == ["Read", "Bash"]


def test_partial_text_still_excluded():
    """Only the tool_use collection moved above the guard — text must not follow."""
    transport = get_transport("claude_cli")
    partial_text = StreamEvent(
        message_type=MessageType.ASSISTANT, text="half-written", is_partial=True
    )
    normalized = transport.normalize_response([partial_text, _result_event("final")])

    assert "half-written" not in (normalized.content or "")
    assert "final" in (normalized.content or "")
