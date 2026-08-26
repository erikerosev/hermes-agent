"""The stall watchdog narrates a silent upstream retry instead of hanging mute.

On 2026-08-24 a 529 wave made every gateway turn sit still for exactly 180s —
the CLI's own retry budget — with no output at all. Nothing distinguished
"retrying upstream" from "thinking hard" or "wedged", so the user watched a
blank panel for three minutes and then got raw JSON.
"""

import json
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

import agent.claude_process_manager as cpm
from agent.claude_process_manager import _PersistentProc


def _make_proc(monkeypatch, notice=0.2, repeat=0.2):
    """A _PersistentProc wired to a queue we drive by hand — no real CLI."""
    monkeypatch.setattr(cpm, "_STALL_NOTICE_SECONDS", notice)
    monkeypatch.setattr(cpm, "_STALL_REPEAT_SECONDS", repeat)
    proc = _PersistentProc.__new__(_PersistentProc)
    proc.popen = MagicMock()
    proc.popen.pid = 4242
    proc._q = queue.Queue()
    proc._alive = True
    proc.dirty = False
    return proc


def _result_line():
    return (json.dumps({"type": "result", "subtype": "success", "is_error": False})
            + "\n").encode("utf-8")


class TestStallWatchdog:
    def test_notifies_while_the_stream_is_silent(self, monkeypatch):
        proc = _make_proc(monkeypatch)
        seen = []

        def feed():
            time.sleep(0.9)          # outlast the notice threshold
            proc._q.put(_result_line())

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        proc.read_turn(on_delta=None, timeout=5.0, on_thinking=seen.append)
        t.join(timeout=2)

        assert seen, "a silent stretch must produce at least one notice"
        assert "nincs válasz az API-tól" in seen[0]

    def test_stays_quiet_when_output_flows(self, monkeypatch):
        """A turn that is actually streaming must not be annotated."""
        proc = _make_proc(monkeypatch, notice=5.0)
        seen = []
        proc._q.put(_result_line())
        proc.read_turn(on_delta=None, timeout=5.0, on_thinking=seen.append)
        assert seen == []

    def test_disabled_by_zero_threshold(self, monkeypatch):
        proc = _make_proc(monkeypatch, notice=0.0)
        seen = []

        def feed():
            time.sleep(0.6)
            proc._q.put(_result_line())

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        proc.read_turn(on_delta=None, timeout=5.0, on_thinking=seen.append)
        t.join(timeout=2)
        assert seen == []

    def test_no_callback_is_not_an_error(self, monkeypatch):
        """on_thinking is optional — the reader must not care."""
        proc = _make_proc(monkeypatch)

        def feed():
            time.sleep(0.6)
            proc._q.put(_result_line())

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        events = proc.read_turn(on_delta=None, timeout=5.0, on_thinking=None)
        t.join(timeout=2)
        assert events and events[-1].is_complete

    def test_a_raising_callback_does_not_break_the_turn(self, monkeypatch):
        proc = _make_proc(monkeypatch)

        def boom(_text):
            raise RuntimeError("UI gone")

        def feed():
            time.sleep(0.9)
            proc._q.put(_result_line())

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        events = proc.read_turn(on_delta=None, timeout=5.0, on_thinking=boom)
        t.join(timeout=2)
        assert events and events[-1].is_complete
