"""Regresszió: a claude-processz-pool nem szivárogtathat el ÉLŐ processzt.

2026-08-18: a `run_turn` három helyen nyersen felülírta a `_procs[key]`-t. Ha két
routing key ugyanarra a session_id-ra mappelődött (a gateway
`two routing keys are mapped to one session_id` tripwire-je), párhuzamos turn-ök
futottak EGY pool-kulcson és egymás bejegyzését írták felül. A felülírt processz
ÉLT tovább (claude + 4 MCP-python, ~380 MB), de kiesett a _procs-ból, ezért sem a
TTL-reap, sem a _MAX_PROCS-eviction nem látta. Egy nap alatt 77 ilyen árva gyűlt
össze (~29 GB) → 100% swap → a gép befagyott.
"""
import os
import subprocess
import time

import pytest

from agent import claude_process_manager as cpm


class _FakePopen:
    def __init__(self, pid=4242):
        self.pid = pid
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return -9

    def poll(self):
        return -9 if self.killed else None


class _FakeProc:
    """A _PersistentProc felülete annyiban, amennyit az _install_locked használ."""

    def __init__(self, pid=4242, alive=True):
        self.popen = _FakePopen(pid)
        self._alive = alive
        self.busy = False
        self.last_used = time.time()
        self.kill_calls = 0

    def is_running(self):
        return self._alive and self.popen.poll() is None

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        self.popen.kill()


def test_install_locked_kills_the_live_process_it_replaces():
    """A pool-kulcsra tett új processz ELŐTT a régi ÉLŐ processzt ki kell lőni."""
    mgr = cpm.ClaudeProcessManager()
    victim = _FakeProc(pid=1001)
    mgr._procs["sess-A"] = victim

    replacement = _FakeProc(pid=1002)
    mgr._install_locked("sess-A", replacement)

    assert victim.kill_calls == 1, "a felülírt ÉLŐ processzt ki kell lőni (különben árva)"
    assert not victim.is_running()
    assert mgr._procs["sess-A"] is replacement


def test_install_locked_is_a_noop_for_an_already_dead_predecessor():
    """A már kilőtt előd nem árva — ne lőjük újra, és ne zajongjunk róla."""
    mgr = cpm.ClaudeProcessManager()
    dead = _FakeProc(pid=1003, alive=False)
    mgr._procs["sess-B"] = dead

    replacement = _FakeProc(pid=1004)
    mgr._install_locked("sess-B", replacement)

    assert dead.kill_calls == 0
    assert mgr._procs["sess-B"] is replacement


def test_install_locked_does_not_kill_when_reinstalling_the_same_object():
    mgr = cpm.ClaudeProcessManager()
    proc = _FakeProc(pid=1005)
    mgr._procs["sess-C"] = proc

    mgr._install_locked("sess-C", proc)

    assert proc.kill_calls == 0
    assert proc.is_running()
    assert mgr._procs["sess-C"] is proc


def test_concurrent_turns_on_one_key_leave_no_live_orphan():
    """A tényleges incidens-forgatókönyv: N egymást felülíró turn EGY kulcson.

    A javítás előtt mind az N-1 korábbi processz élve maradt a poolon kívül.
    """
    mgr = cpm.ClaudeProcessManager()
    procs = [_FakeProc(pid=2000 + i) for i in range(20)]
    for p in procs:
        mgr._install_locked("sess-hot", p)

    survivors = [p for p in procs if p.is_running()]
    assert len(survivors) == 1, f"{len(survivors)} élő processz maradt, 1 helyett"
    assert survivors[0] is procs[-1]
    assert mgr._procs["sess-hot"] is procs[-1]


def test_sweep_kills_a_registered_process_that_fell_out_of_the_pool():
    """Biztonsági háló: regisztrált, de egyetlen kulcson sem szereplő ÉLŐ processz."""
    mgr = cpm.ClaudeProcessManager()
    orphan = subprocess.Popen(["sleep", "60"])
    cpm._register_spawned(orphan.pid)
    try:
        assert os.path.exists(f"/proc/{orphan.pid}")

        mgr._sweep_orphans_locked()

        orphan.wait(timeout=10)
        assert orphan.poll() is not None, "az árvát ki kellett volna lőni"
        assert orphan.pid not in cpm._SPAWNED
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)
        cpm._unregister_spawned(orphan.pid)


def test_sweep_spares_processes_that_are_still_in_the_pool():
    mgr = cpm.ClaudeProcessManager()
    keeper = subprocess.Popen(["sleep", "60"])
    cpm._register_spawned(keeper.pid)
    mgr._procs["sess-live"] = _FakeProc(pid=keeper.pid)
    try:
        mgr._sweep_orphans_locked()
        time.sleep(0.2)
        assert keeper.poll() is None, "a poolban lévő processzt NEM szabad kilőni"
    finally:
        keeper.kill()
        keeper.wait(timeout=5)
        cpm._unregister_spawned(keeper.pid)


def test_sweep_never_touches_unregistered_children():
    """A spawn-per-turn (kanban-worker) claude-jai nincsenek regisztrálva → tabu."""
    mgr = cpm.ClaudeProcessManager()
    outsider = subprocess.Popen(["sleep", "60"])
    try:
        mgr._sweep_orphans_locked()
        time.sleep(0.2)
        assert outsider.poll() is None, "nem-regisztrált gyereket sosem söprünk"
    finally:
        outsider.kill()
        outsider.wait(timeout=5)


def test_kill_reaps_the_process_so_it_does_not_linger_as_a_zombie():
    """A kill() wait()-eljen: enélkül a megölt claude zombiként foglal PID-et."""
    proc = cpm._PersistentProc.__new__(cpm._PersistentProc)
    proc._alive = True
    proc.popen = _FakePopen(pid=1234)
    proc.runner = type("R", (), {"_cleanup_tempfile": lambda self: None})()
    cpm._register_spawned(1234)

    proc.kill()

    assert proc.popen.killed
    assert proc.popen.waited, "kill() után wait() kell, különben zombi marad"
    assert 1234 not in cpm._SPAWNED
