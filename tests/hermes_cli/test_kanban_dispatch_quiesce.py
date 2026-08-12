"""A quiesced assignee is not dispatched to.

Stopping a profile's own gateway does not stop its kanban work. The
dispatcher runs inside the orchestrator's gateway and spawns
``hermes -p <assignee>`` as its own child, so those workers sit in the
orchestrator's cgroup and keep claiming tasks while systemd reports the
profile's unit inactive. Observed 2026-08-12: a profile stopped at 12:03:08
started new sessions at 12:05:52 and 12:09:52.

These tests pin the brake that actually holds.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def quiesce_dir(tmp_path, monkeypatch):
    d = tmp_path / "quiesced"
    d.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_QUIESCE_DIR", str(d))
    return d


# ── the predicate ────────────────────────────────────────────────────────


def test_absent_marker_means_in_service(quiesce_dir):
    assert kb.assignee_is_quiesced("chiron") is False


def test_present_marker_means_out_of_service(quiesce_dir):
    (quiesce_dir / "chiron").touch()
    assert kb.assignee_is_quiesced("chiron") is True


def test_quiescing_one_profile_does_not_quiesce_another(quiesce_dir):
    (quiesce_dir / "chiron").touch()
    assert kb.assignee_is_quiesced("ares") is False


@pytest.mark.parametrize("bad", ["", "   ", None, "..", ".", "../../etc/passwd"])
def test_path_traversal_and_empties_are_refused(quiesce_dir, bad):
    """A marker name comes from a task's assignee column, which is data.

    Without this, an assignee of ``../../something`` would probe outside the
    marker directory.
    """
    assert kb.assignee_is_quiesced(bad) is False


def test_unreadable_marker_directory_fails_open(quiesce_dir, monkeypatch):
    """A brake that jams ON under a filesystem error strands the fleet.

    The risk being managed is an agent running when it should not — visible
    and recoverable. Silently halting every profile is neither.
    """

    def _boom(self):
        raise OSError("simulated")

    monkeypatch.setattr("pathlib.Path.exists", _boom)
    assert kb.assignee_is_quiesced("chiron") is False


def test_marker_directory_is_overridable_and_defaults_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_QUIESCE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert kb.quiesce_dir() == tmp_path / "h" / "quiesced"
    monkeypatch.setenv("HERMES_KANBAN_QUIESCE_DIR", str(tmp_path / "elsewhere"))
    assert kb.quiesce_dir() == tmp_path / "elsewhere"


# ── the dispatch gate ────────────────────────────────────────────────────


def _ready_task(conn, assignee: str) -> str:
    task = kb.create_task(conn, title=f"work for {assignee}", assignee=assignee)
    return task["id"] if isinstance(task, dict) else task


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A fresh board under an isolated HERMES_HOME (the house fixture shape)."""
    from pathlib import Path as _P

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(_P, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def test_dispatch_skips_a_quiesced_assignee_and_leaves_the_task_ready(
    board, quiesce_dir, monkeypatch
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    tid = _ready_task(board, "chiron")
    (quiesce_dir / "chiron").touch()
    result = kb.dispatch_once(board, dry_run=True)
    assert (tid, "chiron") in result.skipped_quiesced
    assert not result.spawned, "a quiesced profile must not be spawned for"
    assert kb.get_task(board, tid).status == "ready", (
        "the work waits — it is not failed, blocked, or lost"
    )


def test_dispatch_still_serves_other_assignees(board, quiesce_dir, monkeypatch):
    """Quiesce is per-profile, not a global stop."""
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    stopped = _ready_task(board, "chiron")
    running = _ready_task(board, "ares")
    (quiesce_dir / "chiron").touch()
    spawned = [t for t, _a, _w in kb.dispatch_once(board, dry_run=True).spawned]
    assert running in spawned
    assert stopped not in spawned


def test_resuming_restores_dispatch(board, quiesce_dir, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    tid = _ready_task(board, "chiron")
    marker = quiesce_dir / "chiron"
    marker.touch()
    assert not kb.dispatch_once(board, dry_run=True).spawned
    marker.unlink()
    assert tid in [t for t, _a, _w in kb.dispatch_once(board, dry_run=True).spawned]
