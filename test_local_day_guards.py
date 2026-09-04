"""A "once a day" guard must mean the reader's day.

(The daily news-alert job that first got this wrong — UTC-keyed guard, local
send window, two "daily" alerts in one Pacific day — has since been retired;
its lesson lives on in every remaining daily sender keying on local_today.)

morning._recent_assistant_texts took the last 4 assistant messages of any
kind, so for anyone who actually texts Palmer the anti-repetition guard was
comparing today's morning line against ordinary chat rather than against
yesterday's morning — which is the failure it was written for.
"""
from unittest.mock import patch

import db
import morning


PHONE = "+15550001111"


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_local_day.db")
    db.init_db()


class TestTheMorningComparesAgainstMornings:
    def test_chat_replies_do_not_crowd_out_prior_mornings(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "yesterday's morning line", kind="morning")
        for i in range(10):
            db.save_message(PHONE, "assistant", f"chat reply {i}", kind="reply")
        got = morning._recent_assistant_texts(PHONE, n=4)
        assert got == ["yesterday's morning line"], got

    def test_it_falls_back_for_history_predating_the_kind_column(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "an old untagged message")
        assert morning._recent_assistant_texts(PHONE, n=4) == ["an old untagged message"]

    def test_ordering_is_oldest_first(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        for i in range(3):
            db.save_message(PHONE, "assistant", f"morning {i}", kind="morning")
        assert morning._recent_assistant_texts(PHONE, n=3) == \
            ["morning 0", "morning 1", "morning 2"]

    def test_the_repetition_guard_now_sees_yesterday(self, tmp_path, monkeypatch):
        """guards.repeats_opening exists for three consecutive mornings that all
        opened the same way; it could not see them past a chatty user."""
        import guards
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "103 today in Woodland Hills, stay inside",
                        kind="morning")
        db.save_message(PHONE, "assistant", "sure, on it", kind="reply")
        recent = morning._recent_assistant_texts(PHONE, n=4)
        assert guards.repeats_opening("106 today in Woodland Hills, brutal again", recent)
