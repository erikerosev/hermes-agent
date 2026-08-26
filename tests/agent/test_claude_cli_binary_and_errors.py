"""Which `claude` binary the gateway spawns, and how upstream errors read.

Both cover the 2026-08-24 incident. The gateway is launched from a shell whose
PATH lacks ``~/.local/bin``, so a bare ``claude`` resolved to a stale npm-global
copy (2.1.49 against the user's own 2.1.241). And when the CLI gave up after
its retry budget it handed the user raw provider JSON as the assistant's answer.
"""

import os
from pathlib import Path
from unittest.mock import patch

from agent.claude_cli_runner import _resolve_claude_command, humanize_cli_error

_RAW_529 = (
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error",'
    '"message":"Overloaded. https://platform.claude.com/docs/en/api/errors"},'
    '"request_id":"req_011CeME4WUs3SVqCuYxkkHua"}'
)


class TestResolveClaudeCommand:
    def test_explicit_command_wins(self):
        assert _resolve_claude_command("/opt/custom/claude") == "/opt/custom/claude"

    def test_env_override_wins_when_executable(self, tmp_path, monkeypatch):
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", str(binary))
        assert _resolve_claude_command() == str(binary)

    def test_non_executable_override_falls_through(self, tmp_path, monkeypatch):
        dud = tmp_path / "not-executable"
        dud.write_text("")
        monkeypatch.setenv("HERMES_CLAUDE_BIN", str(dud))
        assert _resolve_claude_command() != str(dud)

    def test_prefers_local_bin_over_path(self, tmp_path, monkeypatch):
        """The installer's ~/.local/bin copy self-updates; the npm-global one
        on PATH does not. Preferring it is what keeps the fix durable."""
        monkeypatch.delenv("HERMES_CLAUDE_BIN", raising=False)
        home = tmp_path / "home"
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        installed = local_bin / "claude"
        installed.write_text("#!/bin/sh\n")
        installed.chmod(0o755)
        monkeypatch.setenv("HOME", str(home))
        with patch("agent.claude_cli_runner.shutil.which", return_value="/usr/bin/claude"):
            with patch("agent.claude_cli_runner.Path.home", return_value=home):
                assert _resolve_claude_command() == str(installed)

    def test_falls_back_to_path_then_literal(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_BIN", raising=False)
        empty_home = tmp_path / "empty"
        empty_home.mkdir()
        with patch("agent.claude_cli_runner.Path.home", return_value=empty_home):
            with patch("agent.claude_cli_runner.shutil.which", return_value="/usr/bin/claude"):
                assert _resolve_claude_command() == "/usr/bin/claude"
            with patch("agent.claude_cli_runner.shutil.which", return_value=None):
                assert _resolve_claude_command() == "claude"

    def test_bare_claude_is_treated_as_a_default(self, tmp_path, monkeypatch):
        """Callers pass the literal "claude" as their own default, so it must
        still be resolved — otherwise the fix never reaches the gateway."""
        monkeypatch.delenv("HERMES_CLAUDE_BIN", raising=False)
        empty_home = tmp_path / "empty"
        empty_home.mkdir()
        with patch("agent.claude_cli_runner.Path.home", return_value=empty_home):
            with patch("agent.claude_cli_runner.shutil.which", return_value="/resolved/claude"):
                assert _resolve_claude_command("claude") == "/resolved/claude"


class TestHumanizeCliError:
    def test_529_becomes_a_sentence(self):
        out = humanize_cli_error(_RAW_529)
        assert "API Error: 529" not in out
        assert "overloaded_error" not in out
        assert "529" in out

    def test_keeps_the_request_id_for_debugging(self):
        assert "req_011CeME4WUs3SVqCuYxkkHua" in humanize_cli_error(_RAW_529)

    def test_normal_answers_pass_through(self):
        answer = "A Copilot Business licenc 2 fejlesztőre havi 38 USD."
        assert humanize_cli_error(answer) == answer

    def test_unrecognised_code_keeps_its_detail(self):
        """Paraphrasing an error we do not know would hide detail that
        matters more than the friendlier wording."""
        raw = 'API Error: 418 {"error":"teapot"}'
        assert humanize_cli_error(raw) == raw

    def test_empty_and_none_are_safe(self):
        assert humanize_cli_error("") == ""
        assert humanize_cli_error(None) is None

    def test_text_merely_mentioning_the_phrase_is_untouched(self):
        """A turn that *discusses* the error must not be rewritten."""
        prose = "Tegnap egy API Error: 529 jött vissza, azt néztük meg."
        assert humanize_cli_error(prose) == prose

    def test_each_documented_code_is_rewritten(self):
        for code in ("429", "500", "502", "503", "529"):
            raw = f'API Error: {code} {{"type":"error"}}'
            assert humanize_cli_error(raw) != raw
