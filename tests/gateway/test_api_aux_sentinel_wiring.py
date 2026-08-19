"""Regression: the aux sentinel must reach the agent, not just _run_agent.

`_resolve_aux_model` recognised `apollo-aux:*` and `_run_agent` accepted an
`aux_lightweight` parameter, but nothing ever set the `_aux_lightweight`
attribute the light path in run_agent.py actually reads. The parameter was
inert, so every side task (naplo, compression, title generation) ran as a full
agent turn with memory providers and the on_session_end hook. The naplo
provider activated on those aux turns too and their session end fired yet
another naplo call — a self-sustaining loop that saturated
`max_concurrent_runs` and 429'd real user turns.
"""

from types import SimpleNamespace

from gateway.platforms.api_server import APIServerAdapter


class TestApplyAuxSentinel:
    def test_sets_attributes_the_light_path_reads(self):
        agent = SimpleNamespace()
        APIServerAdapter._apply_aux_sentinel(agent, True, "sonnet")
        assert agent._aux_lightweight is True
        assert agent._aux_model == "sonnet"

    def test_normal_turn_left_untouched(self):
        agent = SimpleNamespace()
        APIServerAdapter._apply_aux_sentinel(agent, False, None)
        assert not hasattr(agent, "_aux_lightweight")
        assert not hasattr(agent, "_aux_model")

    def test_sentinel_without_explicit_model_still_marks_light(self):
        agent = SimpleNamespace()
        APIServerAdapter._apply_aux_sentinel(agent, True, None)
        assert agent._aux_lightweight is True


class TestResolveAuxModelFeedsTheWiring:
    def test_apollo_aux_sonnet_round_trip(self):
        is_aux, model = APIServerAdapter._resolve_aux_model("apollo-aux:sonnet")
        agent = SimpleNamespace()
        APIServerAdapter._apply_aux_sentinel(agent, is_aux, model)
        assert (agent._aux_lightweight, agent._aux_model) == (True, "sonnet")

    def test_user_turn_model_is_never_light(self):
        is_aux, model = APIServerAdapter._resolve_aux_model("hermes-agent")
        agent = SimpleNamespace()
        APIServerAdapter._apply_aux_sentinel(agent, is_aux, model)
        assert is_aux is False
        assert not hasattr(agent, "_aux_lightweight")
