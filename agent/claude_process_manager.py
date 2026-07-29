"""Persistent claude-cli process manager — per-session persistence.

CÉL: ne fizessük körönként a ~7s claude-CLI-indítást. Egy persistent `claude`
processz életben marad sessionönként; a turn 2+ csak stdin-en küld új promptot a
live processznek (stream-json), így gyors.

OPT-IN: HERMES_CLAUDE_PERSIST=1. A bevált spawn-per-turn (ClaudeCliRunner.run) marad
a default fallback → nulla regressziós kockázat. CSAK interaktív sessionökre (a
kanban-workerek külön, egyszer-futó subprocesszek → ott a spawn-per-turn marad).

MEGJEGYZÉS: a warm-pool/prewarm ÖTLETET kimértük és ELVETETTÜK — a `claude`-processz
~25s tétlenség után kihűl (~5s újra-bemelegítés), ezért egy parkolt standby adoptálásig
mindig kihűl → a prewarm nem ad nyereséget. A persistence viszont a GYORS egymás utáni
turnökön nyer (az aktív beszélgetés tipikus esete). Lásd [[claude-code-provider-blueprint]].
"""
from __future__ import annotations

import json
import logging
import os
import signal
import queue
import subprocess
import threading
import time

from agent.claude_cli_runner import ClaudeCliRunner
from claude_code_core.parser import parse_line
from claude_code_core.types import MessageType, StreamEvent

logger = logging.getLogger(__name__)

# A session persistent processzének idle-TTL-je (RAM-felszabadítás). 20 perc.
# RÖVID, mert minden processz NEM csak ~240MB RAM, hanem KÉT MCP-szervert is tart
# (hermes_tools + kozos) → N processz = 2N MCP-kapcsolat a gateway event-loopján.
# A korábbi 4h korlátlanul halmozott (21 processz / 42 MCP-szerver 1.5h alatt),
# ami megfojtotta a gateway-t → minden turn lassú, cold-spawn >120s. Lásd
# [[claude-cli-early-return-trap]] szomszédja: a persistence erőforrás-költsége.
_IDLE_TTL = float(os.environ.get("HERMES_CLAUDE_IDLE_TTL", str(20 * 60)))

# A párhuzamosan melegen tartott processzek FELSŐ korlátja. Egy 2-fős háztartásban
# tipikusan 1-2 aktív beszélgetés van; a cap fölött a legrégebben használtat (LRU)
# kilőjük. Ez köti meg az erőforrást, függetlenül a session- id-k számától
# (teszt-sessionök, voice-hívások, session-id rotáció mind új kulcsot adnak).
_MAX_PROCS = int(os.environ.get("HERMES_CLAUDE_MAX_PROCS", "3"))

# A meleg processzt PROAKTÍVAN újrahasznosítjuk, MIELŐTT a kontextusa túlcsordulna.
# A live `claude` processz turn-ről turn-re halmoz a memóriájában, és a gateway 60k-s
# tömörítése NEM éri el → enélkül X üzenet után a kontextus túlcsordul és a processz
# "elfelejt válaszolni" (Erik bug-ja). N turn után kilőjük + újraprimingoljuk a
# (recent) state.db-history-val → bounded kontextus, mégis MELEG (gyors) a turn-ök
# többségén. Minden N. turn fizeti az indítást (~8s), a többi ~2s.
_RECYCLE_TURNS = int(os.environ.get("HERMES_CLAUDE_RECYCLE_TURNS", "8"))
# A primingkor átadott history felső mérete (recent-truncation, ~40k token). A live
# processz kontextusa így indulásból is bounded; a teljes history a state.db-ben marad.
_MAX_PRIME_CHARS = int(os.environ.get("HERMES_CLAUDE_MAX_PRIME_CHARS", "160000"))


def enabled() -> bool:
    """Persistence CSAK interaktív sessionökre (Apollo gateway-út).

    A kanban-workerek külön, egyszer-futó `hermes -p` subprocesszek (HERMES_KANBAN_TASK
    be van állítva) — ott a spawn-per-turn marad (a runner rendesen kitakarít, nincs
    orphan-kockázat)."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    # FONTOS: NE bool(str) — a bool("0") True! A "0"/"false"/üres = KI.
    return os.environ.get("HERMES_CLAUDE_PERSIST", "").strip().lower() in ("1", "true", "yes", "on")


def _msg_text(content) -> str:
    """OpenAI-formátumú content (str vagy [{type,text}] lista) → sima szöveg."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text") or "") for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ).strip()
    return "" if content is None else str(content)


def _render_history(history) -> str:
    """A szál előzményét (prior user/assistant turn-ök) egy priming-szöveggé alakítja,
    amit egy FRISS processz első üzeneteként küldünk, hogy legyen kontextusa."""
    if not history:
        return ""
    lines = []
    for m in history:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        text = _msg_text(m.get("content"))
        if not text:
            continue
        who = "Felhasznalo" if role == "user" else "Te (asszisztens)"
        lines.append(f"{who}: {text[:2000]}")
    if not lines:
        return ""
    # Recent-truncation: csak az utolsó _MAX_PRIME_CHARS-nyi előzményt primingoljuk
    # (a legújabb turn-öket tartjuk meg) → a live processz kontextusa indulásból
    # bounded, sosem csordul túl. A teljes history a state.db-ben marad (nem vész el),
    # és a tartós tények a kozos-memóriában. Régebbi nyers turn-ök elöl esnek ki.
    body = "\n".join(lines)
    truncated = False
    if len(body) > _MAX_PRIME_CHARS:
        body = body[-_MAX_PRIME_CHARS:]
        truncated = True
    head = (
        "[A szal eddigi beszelgetese ebben a fonalban — TE vagy az asszisztens, "
        "folytasd termeszetesen, mintha vegig te valaszoltal volna; ne ismeteld vissza, "
        "csak emlekezz ra. Az aktualis uj uzenet a vegen jon."
        + (" (A legregebbi reszek lemaradtak — a tartos tenyeket a memoria orzi.)]"
           if truncated else "]")
    )
    return head + "\n\n" + body + "\n\n[Aktualis uj uzenet:]\n"


def _descendant_pids(root_pid: int) -> list[int]:
    """A root_pid összes leszármazottja (gyerek, unoka…) /proc-séta alapján — a
    claude → MCP-szerverek (hermes_tools, kozos) + node-alfák kilövéséhez kill-kor.
    Mélyebb-először sorrend (a levelek elöl), hogy a szülő-link még éljen kill közben."""
    children: dict[int, list[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status", "r") as f:
                    ppid = None
                    for line in f:
                        if line.startswith("PPid:"):
                            ppid = int(line.split()[1])
                            break
                if ppid is not None:
                    children.setdefault(ppid, []).append(int(entry))
            except Exception:
                continue
    except Exception:
        return []
    out: list[int] = []
    stack = [root_pid]
    while stack:
        p = stack.pop()
        for c in children.get(p, []):
            out.append(c)
            stack.append(c)
    out.reverse()  # levelek elöl
    return out


class _PersistentProc:
    """Egy életben tartott `claude` processz + a hozzá tartozó reader-szál."""

    def __init__(self, popen: subprocess.Popen, runner: ClaudeCliRunner,
                 model: str, append_system_prompt: str | None, thinking_tokens=None):
        self.popen = popen
        self.runner = runner  # a tempfile-ok (system-prompt, mcp-config) életben tartásához
        self.model = model
        self.append_system_prompt = append_system_prompt
        self.thinking_tokens = thinking_tokens  # amivel SPAWN-olták (toggle-váláskor recycle)
        self.lock = threading.Lock()   # egy turn egyszerre per processz
        self.last_used = time.time()
        self.turn_count = 0  # hány turn futott ezen a processzen (recycle-küszöbhöz)
        # ÉPP DOLGOZIK-e (mid-turn): a last_used csak a turn VÉGÉN frissül, ezért egy
        # generáló session régi last_used-dal LRU-áldozat lehetne → "megszakítja a másik
        # threadet". Az eviction/reap ezért a busy processzt SOSEM lövi ki.
        self.busy = False
        # Friss processz: még nincs benne a beszélgetés-history. Az ELSŐ turn-jén
        # feltöltjük a state.db-ből kapott előzménnyel (különben restart/respawn után
        # a kontextus elveszne, mert a live processz csak a memóriájában tartotta).
        self.primed = False
        self._alive = True
        self._q: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for raw in iter(self.popen.stdout.readline, b""):
                self._q.put(raw)
        except Exception:
            pass
        finally:
            self._alive = False
            self._q.put(None)  # EOF sentinel

    def is_running(self) -> bool:
        return self._alive and self.popen.poll() is None

    def send(self, prompt: str) -> None:
        msg = {"type": "user", "message": {"role": "user",
               "content": [{"type": "text", "text": prompt}]}}
        self.popen.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.popen.stdin.flush()

    def read_turn(self, on_delta, timeout: float, on_thinking=None,
                  on_tool_use=None, on_tool_result=None) -> list[StreamEvent]:
        """A queue-ből olvas a (result) complete-eventig. Közben on_delta/on_thinking-et,
        és — ha adottak — on_tool_use/on_tool_result-t hív (élő tool-státusz)."""
        events: list[StreamEvent] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self._q.get(timeout=min(5.0, max(0.1, deadline - time.time())))
            except queue.Empty:
                continue
            if raw is None:  # EOF — a processz meghalt
                self._alive = False
                break
            decoded = raw.decode("utf-8", errors="replace")
            if on_delta is not None or on_thinking is not None:
                ClaudeCliRunner._maybe_emit_delta(decoded, on_delta, on_thinking)
            ev = parse_line(decoded)
            if ev:
                events.append(ev)
                # Élő tool-státusz: a claude a toolokat a subprocesszében futtatja, a
                # stream-json a tool_use (assistant) / tool_result (user) blokkokat hordozza.
                if on_tool_use is not None and ev.tool_use is not None:
                    try:
                        on_tool_use(ev.tool_use)
                    except Exception:
                        pass
                if on_tool_result is not None and ev.tool_result_id:
                    try:
                        on_tool_result(ev.tool_result_id, ev.tool_result_content)
                    except Exception:
                        pass
                if ev.is_complete:
                    return events
        if not events:
            events.append(StreamEvent(
                raw={}, message_type=MessageType.RESULT, is_complete=True,
                error="persistent claude turn timed out / process died"))
        return events

    def kill(self) -> None:
        self._alive = False
        # A claude gyermek-processzeit (a hermes_tools + kozos MCP-szerverei) IS
        # kilőjük, nem csak a claude-ot — különben minden recycle/kill 2 orphan
        # MCP-szervert hagy, amik felhalmozódnak és megfojtják a gateway-t (üres
        # válasz). A gyerekeket ppid-séta alapján gyűjtjük (nem start_new_session-nel,
        # mert az a detached claude -p élettartamát eltörte). Rekurzív: a node-MCP
        # alfákat is. A claude-ot UTOLJÁRA öljük, hogy a ppid-linkek még éljenek.
        try:
            for cpid in _descendant_pids(self.popen.pid):
                try:
                    os.kill(cpid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.popen.kill()
        except Exception:
            pass
        try:
            self.runner._cleanup_tempfile()
        except Exception:
            pass


class ClaudeProcessManager:
    def __init__(self) -> None:
        self._procs: dict[str, _PersistentProc] = {}      # session_id -> proc
        self._lock = threading.Lock()

    def _spawn(self, model: str, append_system_prompt: str | None,
               working_dir: str | None, thinking_tokens=None) -> _PersistentProc:
        runner = ClaudeCliRunner(
            command=os.environ.get("HERMES_CLAUDE_COMMAND", "claude"),
            working_dir=working_dir,
        )
        # session_id=None → NINCS --resume; a kontextus a live processzben él.
        args = runner._build_args(model, append_system_prompt, session_id=None)
        # A thinking-budget (UI-kapcsoló) a SPAWN-kor dől el (a live processz végig
        # ezzel fut). Toggle-váláskor a run_turn recycle-öli a processzt.
        env = runner._build_env(thinking_tokens=thinking_tokens)
        popen = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=working_dir or os.getcwd(),
            env=env, bufsize=0,
        )
        logger.info("persistent claude spawned: pid=%s", popen.pid)
        return _PersistentProc(popen, runner, model, append_system_prompt, thinking_tokens)

    def run_turn(self, *, session_id: str | None, prompt: str, model: str,
                 append_system_prompt: str | None, working_dir: str | None,
                 on_delta=None, on_status=None, on_thinking=None, timeout: float = 600,
                 history: list | None = None, thinking_tokens=None,
                 stable_system_prompt: str | None = None,
                 on_tool_use=None, on_tool_result=None) -> list[StreamEvent]:
        # stable_system_prompt: a (elvetett) prewarm hagyatéka; itt nem használt,
        # a hívó-kompatibilitás miatt elfogadjuk és ignoráljuk.
        def _status(phase):
            if on_status:
                try:
                    on_status(phase)
                except Exception:
                    pass
        key = session_id or "default"
        with self._lock:
            proc = self._procs.get(key)
            # Proaktív recycle: a meleg processzt MIELŐTT túlcsordulna lecseréljük
            # (turn-küszöb). A friss processz újraprimingol a (recent-truncated)
            # state.db-historyval → bounded kontextus, nincs "elfelejt válaszolni".
            # Recycle, ha: (a) elérte a turn-küszöböt (kontextus-bound), VAGY (b) a
            # thinking-kapcsoló (UI) megváltozott a spawn óta — a budget a spawnkor dől el.
            _toggle_changed = proc is not None and proc.thinking_tokens != thinking_tokens
            recycle = proc is not None and proc.is_running() and (
                proc.turn_count >= _RECYCLE_TURNS or _toggle_changed)
            if recycle:
                logger.info("persistent claude recycle (session=%s, turns=%s, toggle_changed=%s)",
                            key, proc.turn_count, _toggle_changed)
            if proc is None or not proc.is_running() or recycle:
                if proc is not None:
                    proc.kill()
                # FRISS (cold) processz indul — ez a ~7s, jelezzük a felhasználónak.
                _status("starting")
                proc = self._spawn(model, append_system_prompt, working_dir, thinking_tokens)
                self._procs[key] = proc
            self._reap_idle_locked()
            self._evict_over_cap_locked(protect=key)
        # Egy turn egyszerre ezen a processzen (más session párhuzamos).
        with proc.lock:
            if not proc.is_running():
                with self._lock:
                    proc = self._spawn(model, append_system_prompt, working_dir, thinking_tokens)
                    self._procs[key] = proc

            def _payload(p) -> str:
                # FRISS (még nem primingolt) processz: a state.db-ből kapott előzményt
                # a prompt elé fűzzük, hogy legyen kontextusa (restart/respawn-biztos).
                # WARM processz: csak az új üzenet — a kontextus már a live processzben van.
                if not p.primed:
                    preamble = _render_history(history)
                    p.primed = True
                    if preamble:
                        return preamble + prompt
                return prompt

            proc.busy = True  # mid-turn → az eviction/reap nem lövi ki (más thread védve)
            try:
                try:
                    proc.send(_payload(proc))
                except Exception as exc:
                    logger.warning("persistent claude send failed (%s) — respawning", exc)
                    proc.kill()
                    with self._lock:
                        proc = self._spawn(model, append_system_prompt, working_dir, thinking_tokens)
                        self._procs[key] = proc
                    proc.busy = True
                    proc.send(_payload(proc))
                # A prompt elment, a model dolgozik — "Gondolkodik…" az első tokenig.
                _status("thinking")
                events = proc.read_turn(on_delta, timeout, on_thinking=on_thinking,
                                        on_tool_use=on_tool_use, on_tool_result=on_tool_result)
                proc.last_used = time.time()
                proc.turn_count += 1  # recycle-küszöbhöz
                return events
            finally:
                proc.busy = False

    def _reap_idle_locked(self) -> None:
        now = time.time()
        # busy (mid-turn) processzt sosem reap-elünk: a last_used stale, de él és dolgozik
        # (pl. hosszú tool-lánc > TTL alatt) — kilövése megszakítaná az aktív turn-t.
        dead = [k for k, p in self._procs.items()
                if not p.busy and (not p.is_running() or (now - p.last_used) > _IDLE_TTL)]
        for k in dead:
            self._procs[k].kill()
            del self._procs[k]
            logger.info("persistent claude reaped (session=%s)", k)

    def _evict_over_cap_locked(self, protect: str) -> None:
        """A _MAX_PROCS fölötti processzeket LRU-sorrendben kilövi (a `protect`
        kulcsot — az épp futó turn-t — sosem). Megakadályozza a korlátlan
        felhalmozást: minden új session-id (teszt, voice, rotáció) különben örökre
        bennmaradna a poolban a TTL lejártáig, és 2 MCP-szervert tartana fenn."""
        if len(self._procs) <= _MAX_PROCS:
            return
        # legrégebben használt elöl; a `protect` (induló turn) ÉS minden `busy` (más
        # threaden épp generáló) processz védett → sosem szakítunk meg aktív turn-t.
        victims = sorted(
            (k for k in self._procs if k != protect and not self._procs[k].busy),
            key=lambda k: self._procs[k].last_used,
        )
        while len(self._procs) > _MAX_PROCS and victims:
            k = victims.pop(0)
            self._procs[k].kill()
            del self._procs[k]
            logger.info("persistent claude evicted over cap (session=%s)", k)

    def shutdown(self) -> None:
        """Minden persistent processz kilövése (gateway-leállás / atexit)."""
        with self._lock:
            for p in list(self._procs.values()):
                p.kill()
            self._procs.clear()


_MANAGER = ClaudeProcessManager()

import atexit as _atexit  # noqa: E402
_atexit.register(_MANAGER.shutdown)


def get_manager() -> ClaudeProcessManager:
    return _MANAGER
