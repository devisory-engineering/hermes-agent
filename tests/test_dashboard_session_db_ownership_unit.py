"""Portable ownership tests for profile-scoped dashboard database handles."""

from tui_gateway import server as srv


class _FakeDB:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_adoption_closes_a_different_rejected_handle():
    first = _FakeDB()
    second = _FakeDB()
    session = {}

    assert srv._adopt_owned_session_db(session, first) is True
    assert srv._adopt_owned_session_db(session, second) is False

    assert session[srv._OWNED_DB_KEY] is first
    assert first.close_calls == 0
    assert second.close_calls == 1

    assert srv._release_owned_session_db(session) is True
    assert first.close_calls == 1


def test_re_adopting_the_same_handle_is_idempotent():
    db = _FakeDB()
    session = {}

    assert srv._adopt_owned_session_db(session, db) is True
    assert srv._adopt_owned_session_db(session, db) is True
    assert db.close_calls == 0

    assert srv._release_owned_session_db(session) is True
    assert db.close_calls == 1
