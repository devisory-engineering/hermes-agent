"""A plugin hard stop is a terminal, classified failure in the turn result.

A cost-cap / circuit-breaker trip is not a normal answer and not a crash. The
finalizer must mark the turn failed and carry ``failure_reason`` so the caller
(notably the kanban worker exit paths in cli.py) can map it to a terminal exit
code. Without this the run looked like a clean rc=0 finish while the card was
still ``running`` — read as a protocol violation and respawned into the same
cap.
"""

from types import SimpleNamespace

from agent.turn_finalizer import finalize_turn


class _StopAgent:
    def __init__(self, hard_stop_message=None):
        self.max_iterations = 60
        self.iteration_budget = SimpleNamespace(remaining=50, used=10, max_total=60)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-hard-stop"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self._plugin_hard_stop_message = hard_stop_message

    def _emit_status(self, *_a, **_kw):
        pass

    def _safe_print(self, *_a, **_kw):
        pass

    def _save_trajectory(self, *_a, **_kw):
        pass

    def _cleanup_task_resources(self, *_a, **_kw):
        pass

    def _drop_trailing_empty_response_scaffolding(self, _messages):
        pass

    def _persist_session(self, _messages, _history):
        pass

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kw):
        pass

    def _handle_max_iterations(self, _messages, _count):
        return ""


def _finalize(agent, *, failed):
    return finalize_turn(
        agent,
        final_response="⛔ Run stopped by a plugin policy (hard stop).",
        api_call_count=4,
        interrupted=False,
        failed=failed,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="plugin_hard_stop",
    )


def test_hard_stop_is_a_classified_terminal_failure(monkeypatch):
    # finalize_turn fires on_session_end through hermes_cli.lifecycle.
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    agent = _StopAgent(hard_stop_message="cost cap $12.00 reached (est $12.03)")

    result = _finalize(agent, failed=True)

    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == "plugin_hard_stop"
    assert result["plugin_hard_stop"]["message"].startswith("cost cap")
    assert "cost cap" in result["error"]
    assert result["turn_exit_reason"] == "plugin_hard_stop"


def test_no_hard_stop_leaves_result_untouched(monkeypatch):
    """The marker keys appear ONLY on a real trip — an ordinary turn must not
    be tagged as budget-stopped."""
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    agent = _StopAgent(hard_stop_message=None)

    result = _finalize(agent, failed=False)

    assert "plugin_hard_stop" not in result
    assert "failure_reason" not in result
    assert result["failed"] is False
