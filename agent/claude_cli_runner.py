"""Claude Code CLI subprocess runner for Hermes agent integration.

Spawns `claude -p --output-format stream-json` and collects StreamEvent
objects. This is a simplified runner -- unlike CCDB's version, it collects
all events and returns them (no streaming to Discord needed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from claude_code_core.parser import parse_line
from claude_code_core.types import MessageType, StreamEvent

logger = logging.getLogger(__name__)

_STRIPPED_ENV_KEYS = frozenset({
    "CLAUDECODE",
    "DISCORD_BOT_TOKEN",
    "DISCORD_TOKEN",
    "API_SECRET_KEY",
})


# ── Stall watchdog + human-readable upstream errors ───────────────────────
#
# On 2026-08-24 an upstream 529 wave made every gateway turn sit silent for
# exactly 180s (the CLI's own retry budget) and then hand the user this, as
# the assistant's answer:
#
#   API Error: 529 {"type":"error","error":{"type":"overloaded_error", …}}
#
# Two separate defects. The wait was invisible — nothing distinguished
# "retrying upstream" from "thinking hard" or "wedged" — and the payoff was
# raw JSON. The watchdog fixes the first by narrating the stall into the
# thinking panel; ``humanize_cli_error`` fixes the second.

# How long without a single stdout line before we tell the user something.
_STALL_NOTICE_SECONDS = float(os.environ.get("HERMES_CLAUDE_STALL_NOTICE", "30"))
# How often to repeat the notice while the stall continues.
_STALL_REPEAT_SECONDS = float(os.environ.get("HERMES_CLAUDE_STALL_REPEAT", "45"))

_API_ERROR_RE = re.compile(
    r"^\s*API Error:\s*(?P<code>\d{3})\b(?P<rest>.*)$", re.DOTALL
)
_REQUEST_ID_RE = re.compile(r'"request_id"\s*:\s*"(?P<rid>[^"]+)"')

# Only codes where the raw JSON tells the user nothing they can act on.
# Anything else keeps its original text — inventing a friendly paraphrase for
# an error we do not recognise would hide detail that matters when debugging.
_API_ERROR_HUMAN = {
    "429": "Az API rate limitre futott (429) — túl sok kérés rövid idő alatt.",
    "500": "Az API belső hibát adott (500).",
    "502": "Az API átjárója hibázott (502).",
    "503": "Az API átmenetileg nem elérhető (503).",
    "529": "Az Anthropic API jelenleg túlterhelt (529) — ez náluk van, nem nálunk.",
}


def humanize_cli_error(text: str) -> str:
    """Turn a raw ``API Error: <code> {json}`` blob into a sentence.

    Returns ``text`` unchanged when it is not one of the recognised upstream
    errors, so normal answers — and unfamiliar errors, whose detail is worth
    more than a vague paraphrase — pass through untouched.
    """
    if not text or "API Error:" not in text:
        return text
    m = _API_ERROR_RE.match(text)
    if not m:
        return text
    human = _API_ERROR_HUMAN.get(m.group("code"))
    if not human:
        return text

    out = [human, "Nem a te kérdéseddel van baj — egyetlen token sem készült el."]
    rid = _REQUEST_ID_RE.search(m.group("rest") or "")
    if rid:
        out.append(f"(request_id: {rid.group('rid')})")
    out.append("Küldd el újra; ha percekig ismétlődik, az upstream kiesés.")
    return " ".join(out)


_CLAUDE_BIN_LOGGED = False


def _resolve_claude_command(explicit: str | None = None) -> str:
    """Pick which ``claude`` binary to spawn.

    The gateway is launched from a shell whose PATH does NOT contain
    ``~/.local/bin`` (it is ``…/agent-venv/bin:/usr/local/sbin:/usr/local/bin:
    /usr/sbin:/usr/bin:/bin:/snap/bin``). A bare ``claude`` therefore resolved
    to ``/usr/bin/claude`` — a stale *npm-global* install that never
    self-updates. On 2026-08-24 that was 2.1.49 while the user's own shell ran
    2.1.241, ~190 versions newer: the gateway had been silently running a
    months-old client. The npm-global copy is not on any update path, so this
    drift only ever grows.

    Resolution order, first hit wins:
      1. ``HERMES_CLAUDE_BIN`` — explicit override, no questions asked.
      2. ``~/.local/bin/claude`` — the official installer's location, which
         self-updates. This is what makes the fix durable rather than a
         one-off ``npm -g update``.
      3. ``shutil.which("claude")`` — whatever PATH offers.
      4. the literal string, so the failure mode stays the old one.

    An explicit ``command`` argument from the caller always wins over all of
    the above; only the default is resolved here.
    """
    global _CLAUDE_BIN_LOGGED

    if explicit and explicit != "claude":
        return explicit

    resolved: str | None = None
    override = os.environ.get("HERMES_CLAUDE_BIN")
    if override:
        if os.access(override, os.X_OK):
            resolved = override
        else:
            logger.warning(
                "HERMES_CLAUDE_BIN=%s is not executable; falling back to PATH",
                override,
            )

    if resolved is None:
        local_bin = Path.home() / ".local" / "bin" / "claude"
        if os.access(local_bin, os.X_OK):
            resolved = str(local_bin)

    if resolved is None:
        resolved = shutil.which("claude") or "claude"

    if not _CLAUDE_BIN_LOGGED:
        _CLAUDE_BIN_LOGGED = True
        logger.info("Claude CLI binary: %s", resolved)

    return resolved


def _resolve_windows_cmd(cmd_path: Path) -> list[str] | None:
    """Resolve a Windows npm .cmd/.bat wrapper to [node, cli_js].

    npm installs a thin .cmd wrapper that references the real .js entry-point
    via the %~dp0 batch variable. create_subprocess_exec cannot execute .cmd
    files directly, so we parse the wrapper to find the .js path and prepend
    the node executable.

    Returns [node_exe, js_path] on success, None if unresolvable.
    """
    try:
        content = cmd_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'"%~dp0\\([^"]+\.js)"', content)
        if match:
            cli_js = cmd_path.parent / match.group(1)
            if cli_js.exists():
                node = shutil.which("node") or "node"
                return [node, str(cli_js)]
    except OSError:
        pass

    # Fallback: check the standard npm layout.
    cli_js = cmd_path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    if cli_js.exists():
        node = shutil.which("node") or "node"
        return [node, str(cli_js)]

    logger.warning(
        "Windows .cmd wrapper %s could not be resolved to a Node.js script; "
        "Claude CLI will likely fail to start",
        cmd_path,
    )
    return None


class ClaudeCliRunner:
    """Manages the lifecycle of a Claude Code CLI subprocess.

    Spawns ``claude -p --output-format stream-json`` processes, sends prompts
    via stdin in stream-json format, and collects parsed StreamEvent objects.
    """

    def __init__(
        self,
        command: str | None = None,
        working_dir: str | None = None,
        apollo_session_id: str | None = None,
    ) -> None:
        self.command = _resolve_claude_command(command)
        self.working_dir = working_dir
        # Az Apollo/fenris session (= thread) azonosítója. NEM a claude --resume id-je:
        # ez a hívó oldali szál, amit az MCP-szerverek megkapnak (pl. az emlékeztető
        # ehhez a beszélgetéshez kötődik → a push a helyes szálba deep-linkel).
        self.apollo_session_id = apollo_session_id
        self._process: asyncio.subprocess.Process | None = None
        self._system_prompt_tempfile: str | None = None
        self._mcp_config_tempfile: str | None = None

    async def run(
        self,
        prompt: str,
        model: str,
        append_system_prompt: str | None = None,
        session_id: str | None = None,
        timeout: int = 600,
        on_delta=None,
        on_thinking=None,
        thinking_tokens=None,
        no_mcp: bool = False,
    ) -> list[StreamEvent]:
        """Run a prompt through Claude Code CLI and return all stream events.

        Args:
            prompt: The user message to send.
            model: Model identifier (e.g. "sonnet", "opus").
            append_system_prompt: Optional text appended to the system prompt.
            session_id: Optional session ID to resume (hex + hyphens only).
            timeout: Maximum seconds to wait before killing the process.
            on_delta: Optional callable(str) invoked with each incremental text
                chunk as it streams (gépelő-effekt + hang). Hibatűrő.

        Returns:
            List of StreamEvent objects collected from the CLI output.
        """
        args = self._build_args(model, append_system_prompt, session_id, no_mcp=no_mcp)
        env = self._build_env(thinking_tokens=thinking_tokens)
        cwd = self.working_dir or os.getcwd()

        logger.info(
            "Starting Claude CLI: %s (cwd=%s)",
            " ".join(args[:6]) + " ...",
            cwd,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=10 * 1024 * 1024,
            )
        except Exception:
            logger.error("Failed to spawn Claude CLI process", exc_info=True)
            self._cleanup_tempfile()
            return [StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Failed to spawn Claude CLI process",
            )]

        logger.info("Claude CLI started: pid=%s", self._process.pid)

        # Send the prompt via stdin as stream-json.
        await self._send_prompt(prompt)

        events: list[StreamEvent] = []
        try:
            events = await asyncio.wait_for(
                self._read_all_events(on_delta=on_delta, on_thinking=on_thinking),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Claude CLI timed out after %ds", timeout)
            events.append(StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"Timed out after {timeout} seconds",
            ))
        finally:
            await self._cleanup()

        return events

    async def interrupt(self) -> None:
        """Send SIGINT (or terminate on Windows) for a graceful stop."""
        if self._process is None or self._process.returncode is not None:
            return

        if sys.platform == "win32":
            self._process.terminate()
        else:
            self._process.send_signal(signal.SIGINT)

        try:
            await asyncio.wait_for(self._process.wait(), timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            await self.kill()

    async def kill(self) -> None:
        """Terminate the subprocess, force-killing if it doesn't stop.

        A claude gyermek MCP-szervereit (hermes_tools, kozos, behavior-rules,
        reminders, delegation) IS kilőjük — claude nem
        mindig viszi őket magával SIGTERM-re, és minden lemaradt MCP-szerver felhalmozódik
        (turn-önként friss processz → leak → a gateway lassul/befullad). A gyerekeket a
        kill ELŐTT gyűjtjük (amíg a ppid-link él), és csak a VALÓDI Apollo-MCP-ket lőjük
        (cmdline-guard) → nincs pid-reuse-tévedés."""
        if self._process is None or self._process.returncode is not None:
            return

        pid = self._process.pid
        try:
            from agent.claude_process_manager import _descendant_pids
            _children = _descendant_pids(pid)
        except Exception:
            _children = []

        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            self._process.kill()
            await self._process.wait()

        # A lemaradt gyermek MCP-szerverek explicit kilövése (cmdline-guarddal).
        import os as _os
        import signal as _signal
        for _cpid in _children:
            try:
                with open(f"/proc/{_cpid}/cmdline", "rb") as _f:
                    _cl = _f.read().decode("utf-8", "replace")
                if ("hermes_tools_mcp_server" in _cl or "kozos_mcp_server" in _cl
                        or "behavior_rules_mcp_server" in _cl
                        or "reminder_mcp_server" in _cl
                        or "delegate_mcp_server" in _cl):
                    _os.kill(_cpid, _signal.SIGKILL)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_args(
        self,
        model: str,
        append_system_prompt: str | None,
        session_id: str | None,
        no_mcp: bool = False,
    ) -> list[str]:
        """Build the CLI argument list."""
        args = [
            self.command,
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--model", model,
            "--verbose",
            # Inkrementális text_delta eventek a streaminghez (gépelő-effekt + hang).
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]

        # PreToolUse biztonsági kapu: a --dangerously-skip-permissions a normál
        # opokat átengedi (nincs beragadás), DE a PreToolUse hook deny-je a
        # skip-permissions MELLETT is blokkol. A hook (settings.json) csak a
        # valóban destruktív Bash-mintázatokra kérdez (push-approval). Minden
        # más (Read/Write/Edit/normál Bash) némán átmegy. Kikapcs: HERMES_CLAUDE_NO_GATE.
        if not os.environ.get("HERMES_CLAUDE_NO_GATE"):
            settings_path = os.environ.get(
                "HERMES_CLAUDE_SETTINGS",
                os.path.expanduser("~/.hermes/claude-hooks/settings.json"),
            )
            if Path(settings_path).is_file():
                args.extend(["--settings", settings_path])

        # Hermes-moat MCP-híd: a curated Hermes-toolokat (kanban/web/böngésző/
        # vízió/skills — lásd agent/transports/hermes_tools_mcp_server.py) MCP-n
        # adjuk a Claude-nak a saját Read/Edit/Bash mellé. A --strict-mcp-config
        # kizárja a felhasználó személyes claude.ai-konnektorait (Slack/Gmail).
        # Kikapcsolható: HERMES_CLAUDE_CLI_NO_MCP.
        # Könnyű aux-út (no_mcp): a side-taskek (tömörítés, napló, kozos-konszolidáció,
        # garden-kutatás) NEM kapnak MCP-szervert → nincs per-hívás 2× MCP-spawn (ez
        # fojtotta a gateway-t, amikor sok aux-turn torlódott). A user-turn változatlan.
        mcp_config = None if no_mcp else self._build_hermes_tools_mcp_config()
        if mcp_config:
            args.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])

        # Web-tool arbitrázs. A beépített WebFetch nem tartalmat ad vissza,
        # hanem LLM-összefoglalót: augusztusi mérésen 60 hívásból 19 hibázott
        # (403/404 + kaszkádoló "sibling tool call errored"), a medián válasz
        # 624 bájt parafrázis — a lap elveszik. A moat web_extract-je a valódi
        # szöveget hozza (medián 4,2 KB) 0,5 s alatt, ezért ahol a moat jelen
        # van, ott a WebFetch csak rosszabb utat kínál. Az aux-úton (no_mcp)
        # NINCS web_extract, ott a WebFetch marad az egyetlen lehetőség.
        #
        # A WebSearch szándékosan MARAD: lassabb (medián 8,7 s a moat
        # web_search 1,7 s-ához képest), de az Anthropic-index friss és
        # niche kérdéseken jobb a lokális ddgs-backendnél, és a keresés
        # minősége jog/egészség/pénzügy kérdéseknél nem csereszabatos.
        # Felülírás: HERMES_CLAUDE_DISALLOWED_TOOLS (vesszős lista; üres
        # string = semmit nem tiltunk).
        _disallowed_raw = os.environ.get("HERMES_CLAUDE_DISALLOWED_TOOLS")
        if _disallowed_raw is None:
            disallowed = ["WebFetch"] if mcp_config else []
        else:
            disallowed = [t.strip() for t in _disallowed_raw.split(",") if t.strip()]
        if disallowed:
            # Egy vesszős tokenben adjuk át: a --disallowedTools variadikus,
            # így a szóközös alak felfalná az utána jövő pozicionális
            # argumentumokat (a bare `claude -p` a promptot is elnyelte a
            # tesztben). Egy tokennel ez nem fordulhat elő.
            args.extend(["--disallowedTools", ",".join(disallowed)])

        # System prompt: use a temp file for large prompts to avoid
        # exceeding OS argument-length limits.
        if append_system_prompt:
            if len(append_system_prompt) > 7000:
                tmpf = tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".txt",
                    delete=False,
                    encoding="utf-8",
                )
                tmpf.write(append_system_prompt)
                tmpf.close()
                self._system_prompt_tempfile = tmpf.name
                args.extend(["--append-system-prompt-file", tmpf.name])
            else:
                args.extend(["--append-system-prompt", append_system_prompt])

        if session_id:
            if not re.match(r"^[a-f0-9\-]+$", session_id):
                raise ValueError(f"Invalid session_id format: {session_id!r}")
            args.extend(["--resume", session_id])

        # Windows .cmd resolution: create_subprocess_exec cannot run .cmd
        # files or bare command names directly.  Resolve to [node, cli.js].
        if sys.platform == "win32":
            cmd = args[0]
            cmd_path = Path(cmd)

            # If bare name (e.g. "claude"), resolve via shutil.which and
            # check for the .cmd variant that npm creates.
            if not cmd_path.suffix:
                resolved_path = shutil.which(cmd)
                if resolved_path:
                    cmd_path = Path(resolved_path)
                # Also check for the .cmd sibling explicitly
                cmd_variant = shutil.which(cmd + ".cmd")
                if cmd_variant:
                    cmd_path = Path(cmd_variant)

            if cmd_path.suffix.lower() in (".cmd", ".bat"):
                resolved = _resolve_windows_cmd(cmd_path)
                if resolved:
                    args = resolved + args[1:]
            elif not cmd_path.suffix and cmd_path.exists():
                # POSIX shell script on Windows — can't exec directly.
                # Look for the .cmd sibling in the same directory.
                cmd_sibling = cmd_path.with_suffix(".cmd")
                if cmd_sibling.exists():
                    resolved = _resolve_windows_cmd(cmd_sibling)
                    if resolved:
                        args = resolved + args[1:]

        return args

    def _build_env(self, thinking_tokens=None) -> dict[str, str]:
        """Build a clean environment for the subprocess.

        Strips nesting-detection keys and known secrets so the child
        process cannot leak them via tool calls.
        """
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in _STRIPPED_ENV_KEYS
        }
        # Extended thinking: a claude-cli kiadja a model gondolatmenetét
        # (thinking_delta) a stream-json-ban, amit élőben mutatunk. A PER-REQUEST
        # érték (a UI Beállítások-kapcsolójából) felülírja az env-defaultot
        # (HERMES_CLAUDE_THINKING_TOKENS, default 0 = ki). "0"/üres = nincs thinking.
        _think = thinking_tokens if thinking_tokens is not None else os.environ.get(
            "HERMES_CLAUDE_THINKING_TOKENS", "0")
        _think = str(_think or "").strip()
        # FONTOS: a claude a GLOBÁLIS ~/.claude.json configjából ALAPBÓL gondolkodik.
        # Ezt csak az EXPLICIT MAX_THINKING_TOKENS=0 kapcsolja ki (kimérve). Ezért
        # KIKAPCSOLÁSkor "0"-t állítunk be (nem kivesszük) — különben a globális
        # default thinking-ON érvényesülne, és a UI-kapcsoló hatástalan lenne.
        env["MAX_THINKING_TOKENS"] = _think if (_think and _think != "0") else "0"
        return env

    async def _send_prompt(self, prompt: str) -> None:
        """Write the user prompt to stdin in stream-json format."""
        assert self._process is not None and self._process.stdin is not None

        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        line = json.dumps(message) + "\n"
        try:
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
            logger.debug("Sent stream-json user message")
        except Exception:
            logger.warning("Failed to write prompt to stdin", exc_info=True)

    @staticmethod
    def _maybe_emit_delta(raw_line: str, on_delta, on_thinking=None) -> None:
        """Ha a sor egy inkrementális delta stream_event, továbbítja a chunkot.
        A --include-partial-messages ad ilyet: type=stream_event →
        event.type=content_block_delta → delta.type=text_delta → delta.text.
        Extended thinking (MAX_THINKING_TOKENS) esetén delta.type=thinking_delta
        → delta.thinking — ezt az on_thinking callbacken adjuk tovább (élő
        gondolatmenet-megjelenítés)."""
        try:
            obj = json.loads(raw_line)
        except Exception:
            return
        if obj.get("type") != "stream_event":
            return
        ev = obj.get("event") or {}
        if ev.get("type") != "content_block_delta":
            return
        delta = ev.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text") or ""
            if text and on_delta:
                try:
                    on_delta(text)
                except Exception:
                    pass
        elif dtype == "thinking_delta" and on_thinking:
            tk = delta.get("thinking") or ""
            if tk:
                try:
                    on_thinking(tk)
                except Exception:
                    pass

    async def _read_all_events(self, on_delta=None, on_thinking=None) -> list[StreamEvent]:
        """Read stdout line by line, parse events, and collect them.

        If ``on_delta`` is given, incremental text_delta chunks are forwarded to
        it as they arrive (streaming), while events are still collected for the
        final normalize."""
        assert self._process is not None and self._process.stdout is not None

        events: list[StreamEvent] = []
        line_count = 0

        # Stall watchdog. Runs beside the reader rather than wrapping
        # readline() in wait_for: cancelling a StreamReader mid-line is a
        # needless risk when all the watchdog needs is a timestamp. It only
        # ever calls on_thinking, so a silent turn is impossible to confuse
        # with a wedged one.
        last_activity = [asyncio.get_running_loop().time()]
        watchdog: asyncio.Task | None = None

        async def _watch_stall() -> None:
            notified = False
            while True:
                await asyncio.sleep(1.0)
                idle = asyncio.get_running_loop().time() - last_activity[0]
                threshold = (
                    _STALL_NOTICE_SECONDS if not notified
                    else _STALL_NOTICE_SECONDS + _STALL_REPEAT_SECONDS
                )
                if idle < threshold:
                    continue
                notified = True
                last_activity[0] = asyncio.get_running_loop().time()
                logger.info(
                    "Claude CLI: no output for %.0fs (pid=%s) — likely retrying upstream",
                    idle, getattr(self._process, "pid", "?"),
                )
                try:
                    on_thinking(
                        f"[{int(idle)} másodperce nincs válasz az API-tól — "
                        f"valószínűleg újrapróbálkozik. Még várok.]"
                    )
                except Exception:
                    pass

        if on_thinking is not None and _STALL_NOTICE_SECONDS > 0:
            watchdog = asyncio.create_task(_watch_stall())

        try:
            while True:
                line = await self._process.stdout.readline()
                last_activity[0] = asyncio.get_running_loop().time()
                if not line:
                    logger.info("Claude CLI stdout EOF after %d lines", line_count)
                    break

                line_count += 1
                decoded = line.decode("utf-8", errors="replace")

                if line_count <= 3:
                    logger.info(
                        "Claude CLI stdout line %d: %.100s",
                        line_count,
                        decoded.strip(),
                    )

                if on_delta is not None or on_thinking is not None:
                    self._maybe_emit_delta(decoded, on_delta, on_thinking)

                event = parse_line(decoded)
                if event:
                    events.append(event)
                    if event.is_complete:
                        return events
        finally:
            if watchdog is not None:
                watchdog.cancel()

        # Process exited without a RESULT event -- check for errors.
        if self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError):
                pass

        if self._process.returncode is not None and self._process.returncode > 0:
            stderr_data = b""
            if self._process.stderr:
                stderr_data = await self._process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            logger.error(
                "Claude CLI exited with code %d: %s",
                self._process.returncode,
                stderr_text[:500],
            )
            events.append(StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"CLI exited with code {self._process.returncode}",
            ))

        return events

    async def _cleanup(self) -> None:
        """Ensure the subprocess is dead and temp files are removed."""
        await self.kill()
        self._cleanup_tempfile()

    def _build_hermes_tools_mcp_config(self) -> str | None:
        """Write a --mcp-config JSON that launches the hermes-tools MCP server.

        Exposes the curated Hermes toolset (kanban/web/browser/vision/skills/TTS,
        see hermes_tools_mcp_server.py) to the `claude` subprocess as a stdio MCP
        server, so Claude can call Hermes tools natively. Returns the temp-file
        path, or None to skip (HERMES_CLAUDE_CLI_NO_MCP, or write failure)."""
        if os.environ.get("HERMES_CLAUDE_CLI_NO_MCP"):
            return None
        repo_root = Path(__file__).resolve().parents[1]
        env: dict[str, str] = {}
        # Pass through the context the Hermes tools need (kanban worker identity,
        # hermes home, profile). These come from the runner's own environment.
        for key in (
            "HERMES_HOME", "HERMES_PROFILE", "HERMES_TENANT",
            "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_DB",
            "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
            # Aux-endpoint: a moat-toolok (web_extract, vízió, summarize) ezzel az
            # LLM-mel dolgozzák fel a tartalmat. Claude-only setupnál a gateway-re
            # mutat (Claude); enélkül "web extract backend nincs konfigurálva".
            "HERMES_AUX_BASE_URL", "HERMES_AUX_API_KEY", "HERMES_AUX_API_MODE",
            "KOZOS_SERVICE_URL",
        ):
            val = os.environ.get(key)
            if val:
                env[key] = val
        env.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
        existing_pp = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) + (os.pathsep + existing_pp if existing_pp else "")
        mcp_servers: dict[str, Any] = {
            "hermes-tools": {
                "command": sys.executable,
                "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
                "env": env,
            }
        }
        # Közös-memória híd: a megosztott háztartási memória (:8765) save/recall
        # MCP-toolként. Opt-in: HERMES_KOZOS_MCP a kozos_mcp_server.py útja.
        kozos_script = os.environ.get("HERMES_KOZOS_MCP")
        if kozos_script and Path(kozos_script).is_file():
            kozos_env = {"KOZOS_SERVICE_URL": os.environ.get("KOZOS_SERVICE_URL", "http://localhost:8765")}
            if env.get("HERMES_PROFILE"):
                kozos_env["HERMES_PROFILE"] = env["HERMES_PROFILE"]
            mcp_servers["kozos-memory"] = {
                "command": sys.executable,
                "args": [kozos_script],
                "env": kozos_env,
            }
        # Behavior-rules híd: Apollo önmódosuló viselkedési szabály-tára (add/list/
        # disable/update). Opt-in: HERMES_BEHAVIOR_MCP a behavior_rules_mcp_server.py
        # útja. A szerver a ~/.hermes/behavior-rules.json-t írja (HOST-fájl); a fenris
        # backend RO olvassa és user-turn direktívaként injektálja. Mindkét profil (a
        # kozos-hoz hasonlóan) — a szabály maga hordozza a profil-célzást.
        behavior_script = os.environ.get("HERMES_BEHAVIOR_MCP")
        if behavior_script and Path(behavior_script).is_file():
            # A store-utat MINDIG abszolútként adjuk át — a claude az MCP-alfolyamatnak a
            # megadott env-et adja (HOME nélkül a szerver Path.home()-ja elszállna).
            behavior_path = os.environ.get("HERMES_BEHAVIOR_RULES_PATH") or str(
                Path.home() / ".hermes" / "behavior-rules.json"
            )
            mcp_servers["behavior-rules"] = {
                "command": sys.executable,
                "args": [behavior_script],
                "env": {"HERMES_BEHAVIOR_RULES_PATH": behavior_path},
            }
        # Atlassian (Jira/Confluence/JSM/Bitbucket/Compass) remote MCP — OPT-IN.
        # HERMES_ATLASSIAN_MCP: teljes URL, vagy "1"/"on" a hivatalos endpointhoz. Az
        # OAuth-ot maga a claude bináris intézi (nincs client-id/secret a configban); a
        # token a KÖZÖS ~/.claude store-ba cache-elődik, így egy egyszeri böngészős
        # engedélyezés után a headless (-p) futás is újrahasználja. A --strict-mcp-config
        # a szerver-DEKLARÁCIÓT szűri, a credential-lookupot NEM, ezért működik.
        # NINCS profil-kapu: a fenris erik/tundi megkülönböztetés fenris-SZINTŰ (profile_id
        # a WS-en), NEM ér el ide HERMES_PROFILE-ként (a gateway Hermes-profilja "default").
        # Egy távoli http-MCP olcsó (nincs lokális processz-spawn), ezért mindkét profilnak
        # elérhető; ha valaha kell a szűkítés, a fenris profile_id-t kell a runnerig vezetni.
        # Emlékeztető-híd: Apollo emlékeztető-eszköze (create/list/cancel/snooze). A tár
        # és az ütemező a fenris backendben él — a lejáratkor NEM fut modell, a felvett
        # szöveg megy ki verbatim pusholva. Opt-in: HERMES_REMINDER_MCP a szerver útja.
        # Az APOLLO_THREAD_ID-t azért adjuk át, hogy a lejáró emlékeztető pushja ABBA a
        # beszélgetésbe vigyen, amelyikben felvették (a frontend a ?thread_id= deep-linket
        # kezeli); a profilt a backend a threadből vezeti le.
        reminder_script = os.environ.get("HERMES_REMINDER_MCP")
        if reminder_script and Path(reminder_script).is_file():
            reminder_env = {
                "APOLLO_API_BASE": os.environ.get("APOLLO_API_BASE", "http://localhost:8001"),
                "APOLLO_NOTIFY_TOKEN_PATH": os.environ.get(
                    "APOLLO_NOTIFY_TOKEN_PATH",
                    str(Path.home() / ".hermes" / "apollo_notify_token.txt"),
                ),
            }
            if self.apollo_session_id:
                reminder_env["APOLLO_THREAD_ID"] = str(self.apollo_session_id)
            mcp_servers["reminders"] = {
                "command": sys.executable,
                "args": [reminder_script],
                "env": reminder_env,
            }
        # Delegálás: aszinkron kiszervezés kanban-workernek, az eredmény visszaírásával
        # ABBA a beszélgetésbe, amelyikben kiadták. Ugyanaz a minta, mint az
        # emlékeztetőnél: az APOLLO_THREAD_ID hordozza a visszacsatolás címzését,
        # a profilt a fenris a szálból vezeti le. Opt-in: HERMES_DELEGATE_MCP.
        delegate_script = os.environ.get("HERMES_DELEGATE_MCP")
        if delegate_script and Path(delegate_script).is_file():
            delegate_env = {
                "APOLLO_API_BASE": os.environ.get("APOLLO_API_BASE", "http://localhost:8001"),
                "APOLLO_NOTIFY_TOKEN_PATH": os.environ.get(
                    "APOLLO_NOTIFY_TOKEN_PATH",
                    str(Path.home() / ".hermes" / "apollo_notify_token.txt"),
                ),
            }
            if self.apollo_session_id:
                delegate_env["APOLLO_THREAD_ID"] = str(self.apollo_session_id)
            mcp_servers["delegation"] = {
                "command": sys.executable,
                "args": [delegate_script],
                "env": delegate_env,
            }
            # Jelzés a hermes-tools moatnak, hogy VAN delegálási út: ilyenkor a
            # kanban_create nem adhat ki ügynök-munkát (az eredménye elveszne),
            # csak emberi teendőt. Lásd tools/kanban_tools.py::_handle_create.
            env["APOLLO_DELEGATION_ACTIVE"] = "1"
        atl = (os.environ.get("HERMES_ATLASSIAN_MCP") or "").strip()
        if atl:
            url = atl if atl.lower().startswith("http") else "https://mcp.atlassian.com/v1/mcp/authv2"
            mcp_servers["atlassian"] = {"type": "http", "url": url}
        config = {"mcpServers": mcp_servers}
        try:
            tmpf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8",
            )
            json.dump(config, tmpf)
            tmpf.close()
            self._mcp_config_tempfile = tmpf.name
            return tmpf.name
        except OSError as exc:
            logger.warning("claude-cli: could not write hermes-tools MCP config: %s", exc)
            return None

    def _cleanup_tempfile(self) -> None:
        """Remove the system-prompt + MCP-config temp files if created."""
        for attr in ("_system_prompt_tempfile", "_mcp_config_tempfile"):
            path = getattr(self, attr, None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                setattr(self, attr, None)
