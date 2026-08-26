"""Repeated-timeout circuit breaker + the naplo timeout carve-out.

Regression cover for the 2026-08-24 incident: an upstream 529 wave made the
aux endpoint burn its full 120s budget, and because nothing tracked repeated
timeouts, the naplo side-task re-fired on every session-end and paid the full
stall again — four doomed rounds in ten minutes, each spawning a CLI process
and competing with the user's own turn for the aux concurrency slots.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _is_provider_unhealthy,
    _note_provider_success,
    _note_provider_timeout,
    _reset_aux_unhealthy_cache,
    call_llm,
)


class _Timeout(Exception):
    pass


_Timeout.__name__ = "APITimeoutError"


def _patches(client):
    return (
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("openrouter", "some-model", None, None, None),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "some-model"),
        ),
        patch(
            "agent.auxiliary_client._validate_llm_response",
            side_effect=lambda resp, _task, **_kw: resp,
        ),
    )


class TestTimeoutCircuitBreaker:
    def test_single_timeout_does_not_open_the_breaker(self):
        """One stall is a blip — the provider stays usable."""
        _note_provider_timeout("openrouter", "naplo")
        assert not _is_provider_unhealthy("openrouter")

    def test_two_consecutive_timeouts_open_the_breaker(self):
        _note_provider_timeout("openrouter", "naplo")
        _note_provider_timeout("openrouter", "naplo")
        assert _is_provider_unhealthy("openrouter")

    def test_success_resets_the_tally(self):
        """A provider that answers between stalls never trips the breaker."""
        _note_provider_timeout("openrouter", "naplo")
        _note_provider_success("openrouter")
        _note_provider_timeout("openrouter", "naplo")
        assert not _is_provider_unhealthy("openrouter")

    def test_breaker_is_per_provider(self):
        _note_provider_timeout("openrouter", "naplo")
        _note_provider_timeout("openrouter", "naplo")
        assert _is_provider_unhealthy("openrouter")
        assert not _is_provider_unhealthy("nous")

    def test_reset_clears_breaker_state(self):
        _note_provider_timeout("openrouter", "naplo")
        _note_provider_timeout("openrouter", "naplo")
        _reset_aux_unhealthy_cache()
        assert not _is_provider_unhealthy("openrouter")

    def test_live_call_timeouts_open_the_breaker(self):
        """End-to-end: two naplo calls that time out mark the provider
        unhealthy, so the third never reaches the wire."""
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        client.chat.completions.create.side_effect = _Timeout("Request timed out.")
        p1, p2, p3 = _patches(client)
        with p1, p2, p3:
            for _ in range(2):
                with pytest.raises(Exception):
                    call_llm(task="naplo", messages=[{"role": "user", "content": "hi"}])
        assert _is_provider_unhealthy("openrouter")


class TestNaploTimeoutCarveOut:
    def test_naplo_timeout_skips_same_provider_retry(self):
        """The whole point: one full-budget stall, not three."""
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        client.chat.completions.create.side_effect = _Timeout("Request timed out.")
        p1, p2, p3 = _patches(client)
        with p1, p2, p3:
            with pytest.raises(Exception):
                call_llm(task="naplo", messages=[{"role": "user", "content": "hi"}])
        assert client.chat.completions.create.call_count == 1

    def test_naplo_fast_blip_still_retries(self):
        """Only full-budget timeouts are skipped; a cheap blip still retries."""
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        client.chat.completions.create.side_effect = [
            Exception(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            ),
            {"ok": True},
        ]
        p1, p2, p3 = _patches(client)
        with p1, p2, p3:
            result = call_llm(task="naplo", messages=[{"role": "user", "content": "hi"}])
        assert result == {"ok": True}
        assert client.chat.completions.create.call_count == 2

    def test_pinned_model_tasks_keep_their_retry(self):
        """MoA advisors pin a specific model, so falling back is not a
        recovery — they must keep the same-provider retry on a timeout."""
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        client.chat.completions.create.side_effect = [
            _Timeout("Request timed out."),
            {"ok": True},
        ]
        p1, p2, p3 = _patches(client)
        with p1, p2, p3:
            result = call_llm(
                task="moa_reference", messages=[{"role": "user", "content": "hi"}]
            )
        assert result == {"ok": True}
        assert client.chat.completions.create.call_count == 2
