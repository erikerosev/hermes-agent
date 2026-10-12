"""``request_approval`` blocks on a human's phone decision under its own capped deadline (max 1500 s), so the
generic 420 s sequential deadline must not cut it: with it, every 600-900 s approval was killed at 7 min while
the push card stayed open, and the late approval reached no one."""
from agent import tool_executor as te


def test_request_approval_is_exempt_from_the_sequential_deadline():
    assert "request_approval" in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS
