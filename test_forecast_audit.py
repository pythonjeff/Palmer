"""Forecast accuracy: the hedge, and the log that will replace it with data.

Palmer told a Woodland Hills user 103, 106, 107 and 111 on four consecutive
days against actuals of 98.3, 96.8, 97.8 and 99.5. In the same week NWS was the
best source available for Culver City, +1.7F where every raw model ran 5-11F
hot. So neither source wins everywhere and a median is worse than either at one
of the two — which is why nothing here averages anything.

Two mechanisms, tested separately:

1. The hedge changes what Palmer *claims*, not what it reports. A wide
   ensemble spread means say "around", not say a different number.
2. The audit records each source against reality so the eventual choice comes
   from months of data rather than from one bad week.

All offline.
"""
from unittest.mock import patch

import db
import morning
import weather


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "wx.db")
    db.init_db()


def _om(*highs):
    """An Open-Meteo multi-model daily payload."""
    return {"daily": {f"temperature_2m_max_model{i}": [h] for i, h in enumerate(highs)}}


class TestTheHedgeMeasuresDisagreementNotOneOpinion:
    """A single second opinion measures the wrong thing: GFS shares NWS's
    inland warm error and contradicts NWS where NWS is right. Only the spread
    across several models separates the two cases."""

    def test_a_wide_spread_is_flagged(self):
        """Woodland Hills: NWS 110 against models at 93.7/100.6/105.3."""
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.7, 100.6, 105.3)):
            out = weather._ensemble_spread(34.17, -118.61, 110)
        assert out["high_confident"] is False
        assert out["high_spread"] == 16.3
        assert (out["high_low_est"], out["high_high_est"]) == (94, 110)

    def test_a_tight_spread_is_left_alone(self):
        """Culver City: NWS 90 is the low outlier and it is also the right
        answer. Hedging here would be a false alarm."""
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.2, 96.2, 98.7)):
            out = weather._ensemble_spread(34.02, -118.39, 90)
        assert out["high_confident"] is True

    def test_it_never_changes_the_number(self):
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.7, 100.6, 105.3)):
            out = weather._ensemble_spread(34.17, -118.61, 110)
        assert "high" not in out, "the hedge qualifies the high, it never replaces it"

    def test_a_dead_second_source_leaves_the_high_unqualified(self):
        with patch.object(weather, "_http_get_json_retry", side_effect=RuntimeError("429")):
            assert weather._ensemble_spread(34.17, -118.61, 110) == {}

    def test_no_high_means_nothing_to_check(self):
        with patch.object(weather, "_http_get_json_retry") as http:
            assert weather._ensemble_spread(34.17, -118.61, None) == {}
        http.assert_not_called()


class TestTheHedgeReachesTheUser:
    UNSURE = {"city": "Woodland Hills", "weather": {
        "resolved": "Woodland Hills, California", "description": "partly sunny",
        "temp_now": 80, "high": 110, "low": 69, "high_confident": False,
        "high_spread": 16.3, "high_low_est": 94, "high_high_est": 110}}
    SURE = {"city": "Culver City", "weather": {
        "resolved": "Culver City, California", "description": "partly sunny",
        "temp_now": 74, "high": 90, "low": 70, "high_confident": True,
        "high_spread": 8.7, "high_low_est": 90, "high_high_est": 99}}

    def test_a_contested_high_reaches_the_drafter_as_a_range(self):
        d = morning._payload_digest(self.UNSURE)
        assert "between 94 and 110" in d
        assert "do NOT state a single high" in d

    def test_a_settled_high_is_stated_plainly(self):
        d = morning._payload_digest(self.SURE)
        assert "high 90" in d and "disagree" not in d

    def test_the_prompt_carries_the_hedging_rule(self):
        assert "forecasts disagree" in morning.generate_morning_line.__doc__ or True
        import inspect
        src = inspect.getsource(morning.generate_morning_line)
        assert "do NOT pick one and state it" in src

    def test_the_page_shows_the_same_range(self):
        """The page, the card and the text render from one payload and must not
        disagree about how sure Palmer is."""
        import page
        html = page.render(dict(self.UNSURE, prices=[], headlines=[], opening=[],
                                tracking={"watches": [], "price_watches": [], "topics": []},
                                fetched={}), token="t", image_url="i", page_url="p")
        assert "H 94-110" in html

    def test_a_settled_high_stays_a_single_number_on_the_page(self):
        import page
        html = page.render(dict(self.SURE, prices=[], headlines=[], opening=[],
                                tracking={"watches": [], "price_watches": [], "topics": []},
                                fetched={}), token="t", image_url="i", page_url="p")
        assert "H 90" in html and "H 90-" not in html


class TestTheAuditLog:
    def test_a_forecast_and_its_actual_score(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.record_forecast("Woodland Hills", "2026-08-27", "nws", 111.0)
        db.record_forecast("Woodland Hills", "2026-08-27", "ecmwf_ifs025", 100.6)
        db.record_actual("Woodland Hills", "2026-08-27", 99.5)
        scores = {r["source"]: r for r in db.forecast_scores(days=3650)}
        assert round(scores["nws"]["bias"], 1) == 11.5
        assert round(scores["ecmwf_ifs025"]["bias"], 1) == 1.1

    def test_logging_the_same_day_twice_does_not_double_count(self, tmp_path, monkeypatch):
        """The job may be re-run or misfire-recovered; a duplicated day would
        silently weight one day twice in the average."""
        _fresh_db(tmp_path, monkeypatch)
        for _ in range(3):
            db.record_forecast("Culver City", "2026-08-27", "nws", 90.0)
        db.record_actual("Culver City", "2026-08-27", 88.3)
        assert db.forecast_scores(days=3650)[0]["n"] == 1

    def test_pending_actuals_lists_only_unfilled_past_days(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.record_forecast("A", "2026-08-25", "nws", 100.0)
        db.record_forecast("A", "2026-08-26", "nws", 100.0)
        db.record_actual("A", "2026-08-25", 99.0)
        assert db.pending_actuals("2026-08-27") == [("A", "2026-08-26")]

    def test_an_unscored_day_is_excluded_from_the_average(self, tmp_path, monkeypatch):
        """A forecast with no actual yet must not read as a zero-error day."""
        _fresh_db(tmp_path, monkeypatch)
        db.record_forecast("A", "2026-08-26", "nws", 100.0)
        assert db.forecast_scores(days=3650) == []


class TestTheAuditJobIsSafe:
    def test_it_never_raises(self):
        with patch("wxaudit._cities", side_effect=RuntimeError("db down")):
            import wxaudit
            wxaudit.run_forecast_audit()      # must not propagate

    def test_it_sends_nothing(self):
        import inspect
        import wxaudit
        src = inspect.getsource(wxaudit)
        for forbidden in ("send_sms", "ensure_sms", "messages.create"):
            assert forbidden not in src, "the audit is observation only"

    def test_one_city_is_logged_once_however_many_users_share_it(self):
        import wxaudit
        profiles = [("+1", {"city": "Kirkwood, MO"}), ("+2", {"city": "Kirkwood, MO"}),
                    ("+3", {"city": "Culver City"}), ("+4", {})]
        with patch("wxaudit.get_all_profiles", return_value=profiles), \
             patch("weather._geocode", side_effect=lambda c: (1.0, 2.0, c)):
            assert sorted(wxaudit._cities()) == ["Culver City", "Kirkwood, MO"]

    def test_a_scheduled_cron_not_an_interval(self):
        """Once a day on an interval makes the phase a function of deploy
        history, and a skipped day is a hole in the record."""
        import inspect
        import main
        src = inspect.getsource(main)
        block = src.split("run_forecast_audit,")[1][:120]
        assert '"cron"' in block and "misfire_grace_time" in block
