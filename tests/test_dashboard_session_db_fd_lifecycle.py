"""Regression: the dashboard backend must not leak SQLite descriptors.

Scope / evidence note
---------------------
This guards a RESOURCE-LIFECYCLE defect only: ``hermes -p <profile> dashboard
--isolated`` (and ``serve``, the same backend) opened one profile-scoped
``SessionDB`` per session for a non-launch profile and never closed it, so a
long-lived backend accrued one open ``state.db`` (plus its ``-wal``/``-shm``)
descriptor per closed session. It is explicitly NOT a claim about any wider
fleet corruption incident: investigation t_031859ab established that the
deleted WAL/SHM descriptors observed on gateway masters were inert orphan
inodes held by no live writer, with exactly one live writer per DB. Nothing
here reproduces or explains corruption; these tests only assert bounded FDs.

The 24h operational soak is documented in the module docstring of the
``test_dashboard_fd_soak_procedure_is_documented`` test below.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from tui_gateway import server as srv


REPO_ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="descriptor accounting requires /proc (Linux)",
)


def _open_fd_targets() -> list[str]:
    base = "/proc/self/fd"
    targets = []
    for name in os.listdir(base):
        try:
            targets.append(os.readlink(os.path.join(base, name)))
        except OSError:
            # The fd vanished between listdir and readlink (our own listdir fd).
            continue
    return targets


def _state_db_fd_count() -> int:
    return sum(1 for t in _open_fd_targets() if "state.db" in t)


def _deleted_state_db_fd_count() -> int:
    return sum(
        1 for t in _open_fd_targets() if "state.db" in t and "(deleted)" in t
    )


@pytest.fixture()
def profile_homes(tmp_path):
    """Three initialised profile homes, each with a real ``state.db``."""
    from hermes_state import SessionDB

    homes = []
    for name in ("alpha", "beta", "gamma"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        db = SessionDB(db_path=home / "state.db")
        db.create_session(f"sess-{name}", "cli")
        db.append_message(f"sess-{name}", "user", "hello")
        db.close()
        homes.append(home)
    return homes


class _StubAgent:
    """Stands in for AIAgent on the teardown path.

    Mirrors the real contract that makes this a leak: ``AIAgent.close`` ends
    the session row through the handed-in db (step 7) and deliberately does
    NOT close the connection — closing is the caller's job.
    """

    def __init__(self, session_db, session_id):
        self._session_db = session_db
        self.session_id = session_id
        self.closed = False

    def close(self):
        self.closed = True
        try:
            self._session_db.end_session(self.session_id, "agent_close")
        except Exception:
            pass


def _make_profile_session(home: Path, *, key: str) -> dict:
    """Build the session record ``_start_agent_build`` produces for a profile."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    session = {
        "session_key": key,
        "profile_home": str(home),
        "agent": _StubAgent(db, key),
        "history": [],
        "_finalized": True,  # skip the plugin/memory finalize machinery
    }
    srv._adopt_owned_session_db(session, db)
    return session


# ── the regression itself ──────────────────────────────────────────────────


def test_teardown_closes_profile_scoped_session_db(profile_homes):
    """One profile session, opened and torn down, must leave no state.db fd."""
    home = profile_homes[0]
    before = _state_db_fd_count()
    session = _make_profile_session(home, key="sess-alpha")
    assert _state_db_fd_count() > before, "fixture did not actually open a db"

    srv._teardown_session(session, end_reason="tui_close")

    assert _state_db_fd_count() == before
    assert session.get(srv._OWNED_DB_KEY) is None


def test_repeated_session_churn_keeps_fd_count_bounded(profile_homes):
    """The CI stand-in for the 24h soak.

    Churns enough profile-scoped sessions that a one-descriptor-per-session
    leak is unmistakable (pre-fix: exactly 1.0 leaked fd per closed session,
    so 90 leaked descriptors here), and asserts both a hard bound and a flat
    trend rather than a single end-state number.
    """
    rounds = 30
    baseline = _state_db_fd_count()
    samples: list[int] = []

    for round_index in range(rounds):
        for home in profile_homes:
            key = f"sess-{home.name}"
            session = _make_profile_session(home, key=key)
            srv._teardown_session(session, end_reason="tui_close")
        samples.append(_state_db_fd_count())

    churned = rounds * len(profile_homes)
    assert churned == 90

    # Hard bound: no accumulation at all across the whole run.
    assert max(samples) <= baseline, (
        f"state.db fd count grew above baseline {baseline}: samples={samples}"
    )
    # Zero growth trend between the first and last sample.
    assert samples[-1] == samples[0], (
        f"state.db fd count drifted: first={samples[0]} last={samples[-1]}"
    )
    # And nothing accumulating on deleted inodes either.
    assert _deleted_state_db_fd_count() == 0


def test_teardown_is_idempotent_and_does_not_double_close(profile_homes):
    home = profile_homes[1]
    before = _state_db_fd_count()
    session = _make_profile_session(home, key="sess-beta")

    assert srv._release_owned_session_db(session) is True
    # Second release is a no-op, not a double close.
    assert srv._release_owned_session_db(session) is False
    srv._teardown_session(session, end_reason="tui_close")

    assert _state_db_fd_count() == before


def test_launch_profile_sessions_do_not_adopt_the_shared_handle():
    """A launch-profile session borrows ``_get_db()`` and must never close it."""
    session = {"session_key": "sess-launch", "profile_home": None, "_finalized": True}
    assert srv._release_owned_session_db(session) is False
    srv._teardown_session(session, end_reason="tui_close")
    assert session.get(srv._OWNED_DB_KEY) is None


def test_adoption_is_first_writer_wins(profile_homes):
    """A rebuild must not silently orphan the handle already adopted."""
    from hermes_state import SessionDB

    home = profile_homes[2]
    before = _state_db_fd_count()
    first = SessionDB(db_path=home / "state.db")
    second = SessionDB(db_path=home / "state.db")
    session: dict = {"session_key": "sess-gamma", "_finalized": True}

    srv._adopt_owned_session_db(session, first)
    srv._adopt_owned_session_db(session, second)
    assert session[srv._OWNED_DB_KEY] is first

    srv._release_owned_session_db(session)
    second.close()
    assert _state_db_fd_count() == before


# ── structural guards: keep the ownership seam wired ───────────────────────


def _server_ast() -> ast.Module:
    return ast.parse(Path(srv.__file__).read_text(encoding="utf-8"))


def _function_named(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_in(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_teardown_path_releases_the_owned_handle():
    """Guard the wiring, not just the behaviour above.

    ``_teardown_session`` is the single funnel every close/reap path reaches;
    if a future refactor drops the release call the runtime tests here would
    still pass under a GC that happens to collect the stub.
    """
    teardown = _function_named(_server_ast(), "_teardown_session")
    assert teardown is not None
    assert "_release_owned_session_db" in _calls_in(teardown)


@pytest.mark.parametrize(
    "func_name",
    ["_start_agent_build", "_session_resume"],
)
def test_profile_db_mint_sites_declare_ownership(func_name):
    """Every site that mints a profile-scoped SessionDB must give it an owner."""
    node = _function_named(_server_ast(), func_name)
    assert node is not None, f"{func_name} disappeared — re-audit the mint sites"
    calls = _calls_in(node)
    assert "SessionDB" in calls, (
        f"{func_name} no longer mints a SessionDB; if the mint moved, move this "
        "guard with it rather than deleting it"
    )
    assert "_adopt_owned_session_db" in calls or "close" in calls, (
        f"{func_name} mints a profile SessionDB without adopting or closing it"
    )


def test_dashboard_fd_soak_procedure_is_documented():
    """The 24h operational soak must stay documented next to the CI proxy.

    CI cannot run 24h; the deterministic churn test above proves the bound.
    The operational soak procedure lives in the repo doc referenced here so an
    operator can reproduce the original report on a real desktop backend.
    """
    doc = REPO_ROOT / "docs" / "dashboard-fd-soak.md"
    assert doc.is_file(), (
        "docs/dashboard-fd-soak.md is the operator-facing 24h soak procedure "
        "for this regression; it must ship with the fix"
    )
    text = doc.read_text(encoding="utf-8")
    for needle in ("/proc/", "state.db", "24"):
        assert needle in text, f"soak doc lost its {needle!r} instructions"
