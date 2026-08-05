"""Plugin-requested hard stop: a ``pre_tool_call`` block can end the run.

Before this, ``pre_tool_call`` was a plugin's only veto point and it only ever
skipped ONE tool call. A tripped cost cap / circuit breaker therefore blocked
every subsequent tool while the conversation loop kept producing (billable)
model turns until something external ended the session — and on a kanban worker
it also vetoed the worker's own ``kanban_complete`` / ``kanban_block``, so the
run exited rc=0 with the card still ``running`` (a protocol violation) and got
respawned into the same cap.

``{"action": "block", "hard_stop": True}`` escalates the veto to "end the run".
The latch is keyed BY SESSION (2026-08-05 review finding 5): the gateway runs
turns for several sessions on a thread pool, so a module-global latch lets one
session's trip kill an innocent sibling, and a sibling's run-start clear drop a
real trip.
"""

from pathlib import Path

import pytest

from hermes_cli import plugins


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clean_latch():
    plugins.clear_hard_stop()
    yield
    plugins.clear_hard_stop()


class _FakeHookManager:
    """Minimal stand-in for the plugin manager's ``invoke_hook``."""

    def __init__(self, results):
        self._results = results
        self.calls = []

    def __call__(self, hook_name, **kwargs):
        self.calls.append((hook_name, kwargs))
        return list(self._results)


def _install_hook(monkeypatch, results):
    fake = _FakeHookManager(results)
    monkeypatch.setattr(plugins, "invoke_hook", fake)
    return fake


def test_core_advertises_hard_stop_support():
    """Plugins probe this before claiming a hard stop in their audit log.

    Without the marker a breaker cannot distinguish an enforcing core from one
    that only skips the tool, and would log 'session terminated' while the loop
    kept spending — the green-when-broken failure being fixed here.
    """
    assert getattr(plugins, "PRE_TOOL_CALL_HARD_STOP_SUPPORTED", False) is True


def test_hard_stop_block_latches_stop_and_still_blocks(monkeypatch):
    _install_hook(
        monkeypatch,
        [{"action": "block", "message": "cost cap $12.00 reached", "hard_stop": True}],
    )

    message = plugins.resolve_pre_tool_block(
        "kanban_complete", {}, session_id="sess-a"
    )

    # The tool is still blocked with the plugin's message ...
    assert message == "cost cap $12.00 reached"
    # ... AND the run-level stop is latched for THIS session.
    assert plugins.hard_stop_requested("sess-a") == "cost cap $12.00 reached"
    assert plugins.consume_hard_stop("sess-a") == "cost cap $12.00 reached"
    # Consuming clears it — one trip must not stop two runs.
    assert plugins.hard_stop_requested("sess-a") is None


def test_latch_is_keyed_by_session(monkeypatch):
    """Review finding 5: concurrent gateway sessions must not cross-trip.

    Session B's drain must not consume A's stop (and kill an innocent run),
    and B's run-start clear must not drop A's latch.
    """
    plugins.request_hard_stop("A tripped", session_id="sess-a")

    # B's drain sees nothing.
    assert plugins.consume_hard_stop("sess-b") is None
    # B's run-start clear leaves A's latch alone.
    plugins.clear_hard_stop("sess-b")
    assert plugins.hard_stop_requested("sess-a") == "A tripped"
    # A's own drain gets it.
    assert plugins.consume_hard_stop("sess-a") == "A tripped"


def test_anonymous_bucket_serves_sessionless_callers():
    """Callers that pass no session id share the '' bucket (single-run CLI
    contexts with no sibling-session concurrency); a session-scoped drain
    falls back to it so a trip from a dispatch path that could not name its
    session still stops the run that is draining."""
    plugins.request_hard_stop("anon trip")
    assert plugins.consume_hard_stop("sess-a") == "anon trip"
    assert plugins.hard_stop_requested() is None


def test_plain_block_does_not_latch_hard_stop(monkeypatch):
    """A normal policy block keeps its existing skip-one-tool semantics."""
    _install_hook(
        monkeypatch,
        [{"action": "block", "message": "not allowed here"}],
    )

    assert (
        plugins.resolve_pre_tool_block("terminal", {}, session_id="sess-a")
        == "not allowed here"
    )
    assert plugins.hard_stop_requested("sess-a") is None


def test_hard_stop_ignored_on_approve_directive(monkeypatch):
    """``hard_stop`` is a block-only escalation.

    An ``approve`` directive routes to the human gate; letting it also kill the
    run would turn "ask a human" into "abort", which no caller expects.
    """
    _install_hook(
        monkeypatch,
        [{"action": "approve", "message": "confirm?", "hard_stop": True}],
    )

    details = plugins._get_pre_tool_call_directive_details("write_file", {})
    assert details.action == "approve"
    assert details.hard_stop is False


def test_hard_stop_without_message_is_ignored(monkeypatch):
    """A block directive still requires a message; ``hard_stop`` can't smuggle
    a message-less veto past the existing validation."""
    _install_hook(monkeypatch, [{"action": "block", "hard_stop": True}])

    assert plugins.resolve_pre_tool_block("terminal", {}, session_id="s") is None
    assert plugins.hard_stop_requested("s") is None


def test_first_hard_stop_wins_per_session():
    plugins.request_hard_stop("first", session_id="s")
    plugins.request_hard_stop("second", session_id="s")
    assert plugins.consume_hard_stop("s") == "first"


def test_clear_hard_stop_drops_pending_request():
    plugins.request_hard_stop("stale trip from a previous turn", session_id="s")
    plugins.clear_hard_stop("s")
    assert plugins.hard_stop_requested("s") is None


# ---------------------------------------------------------------------------
# Goal-mode behavior (2026-08-05 review findings 3 + 4)
# ---------------------------------------------------------------------------


def _goal_result(hard_stop: bool, text: str = "partial output"):
    result = {"final_response": text}
    if hard_stop:
        result["plugin_hard_stop"] = {"message": "cost cap reached"}
        result["failed"] = True
        result["failure_reason"] = "plugin_hard_stop"
    return result


def test_kanban_goal_loop_breaks_on_hard_stop(kanban_home, monkeypatch, capsys):
    """Finding 3: the -Q goal loop must END on a trip, not keep issuing model
    turns (plus judge calls) against the tripped cap for up to max_turns."""
    import cli as cli_mod
    from types import SimpleNamespace

    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="goal card", body="do the thing", assignee="a",
            goal_mode=True,
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    turns = []

    def _run_conversation(user_message, conversation_history=None):
        turns.append(user_message)
        # Trip on the FIRST goal-loop turn; without the break the loop would
        # keep calling this (and the judge) until max_turns.
        return _goal_result(hard_stop=True)

    judge_calls = []
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda *a, **k: judge_calls.append(1)
        or ("continue", "keep going", False, False, False),
    )

    fake_cli = SimpleNamespace(
        agent=SimpleNamespace(
            run_conversation=_run_conversation, session_id="sess-goal"
        ),
        conversation_history=[],
        session_id="sess-goal",
        _last_turn_hard_stop=False,
    )

    cli_mod._run_kanban_goal_loop_q(fake_cli, "first response")

    # Exactly one goal-loop turn ran — the trip broke the loop.
    assert len(turns) == 1
    assert fake_cli._last_turn_hard_stop is True


def test_interactive_goal_continuation_pauses_on_hard_stop(monkeypatch):
    """Finding 4: /goal continuation must auto-pause on a hard-stopped turn,
    exactly like the interrupt path — the judge would say "continue" and
    re-queue a turn into the tripped cap."""
    import cli as cli_mod

    pauses = []

    class _Mgr:
        def load(self):
            return {"goal": "ship it", "status": "active"}

        def is_active(self):
            return True

        def pause(self, reason=""):
            pauses.append(reason)

    inst = object.__new__(cli_mod.HermesCLI)
    inst._last_turn_interrupted = False
    inst._last_turn_hard_stop = True
    inst._get_goal_manager = lambda: _Mgr()  # instance attr shadows the method

    # Drive the real hook; it must pause and return without judging.
    judge = []
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda *a, **k: judge.append(1) or ("done", "done", False, False, False),
    )
    try:
        inst._maybe_continue_goal_after_turn()
    except Exception as exc:  # pragma: no cover - structure drift diagnostic
        pytest.fail(f"_maybe_continue_goal_after_turn raised: {exc}")

    assert judge == [], "hard-stopped turn must not reach the goal judge"
    assert pauses == ["plugin hard stop (budget/safety cap)"], (
        "the goal must be PAUSED (recoverable via /goal resume), "
        f"got pauses={pauses!r}"
    )
