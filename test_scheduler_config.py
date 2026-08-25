"""Scheduler registration properties that are invisible at runtime.

Every job here failed, or could fail, the same way: an APScheduler *interval*
job schedules its first run at `start + interval`, and that clock restarts on
every dyno boot — which means every deploy. A job whose period is long relative
to the gap between deploys therefore runs on a cadence set by deploy history
rather than by the clock, and since a tick that finds nothing to do logs
nothing, it fails silently. run_price_watches at 12h only ran on days
production was left alone; run_followups at 4h had 1.5 ticks of margin against
a 6h delivery window before the reset was even considered.

The property under test throughout is phase-independence: fire times must not
move when the process starts at a different moment.
"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger


def _scheduler():
    with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
        import main
    return main._scheduler


def _trigger_for(func):
    jobs = [j for j in _scheduler().get_jobs() if j.func is func]
    assert len(jobs) == 1, f"expected exactly one job for {func.__name__}"
    return jobs[0].trigger


def _fire_times(trigger, start, count):
    out, fire = [], None
    for _ in range(count):
        fire = trigger.get_next_fire_time(fire, start if fire is None else fire + timedelta(seconds=1))
        out.append(fire)
    return out


# Jobs whose period is an hour or longer must be phase-stable. The short jobs
# (reminders 1m, morning 5m, watches 30m) stay on interval deliberately —
# losing up to 30 minutes to a deploy is immaterial there.
def _long_period_jobs():
    from followup import run_followups
    from alerts import run_alert_checks
    from morning import send_missing_data_asks
    from shopping import run_price_watches
    return [run_followups, run_alert_checks, send_missing_data_asks, run_price_watches]


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_are_cron(func):
    assert isinstance(_trigger_for(func), CronTrigger)


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_fire_on_round_clock_positions(func):
    """Phase-independence is structural once the trigger is cron — a CronTrigger
    holds no boot-derived state, where an IntervalTrigger's grid is anchored to
    the start_date it was given at add_job time. What is checkable here is the
    visible consequence: fire times sit on round clock positions rather than on
    whatever minute the dyno happened to boot at."""
    trigger = _trigger_for(func)
    for fire in _fire_times(trigger, datetime(2026, 8, 25, 3, 17, tzinfo=timezone.utc), 6):
        utc = fire.astimezone(timezone.utc)
        assert utc.second == 0 and utc.microsecond == 0
        assert utc.minute in (0, 30), f"{func.__name__} fires at :{utc.minute:02d}"


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_pin_their_timezone(func):
    """A bare BackgroundScheduler() inherits the PROCESS timezone. That is
    Etc/UTC on the dyno and the developer's own zone locally, so an unpinned
    cron grid means local runs disagree with production while both look right —
    and a TZ config var would rotate the live schedule with nothing to show for
    it. Caught exactly this: three jobs registered as America/Chicago on a
    laptop and Etc/UTC in prod."""
    assert str(_trigger_for(func).timezone) in ("UTC", "Etc/UTC")


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_survive_a_delayed_tick(func):
    """APScheduler's default misfire_grace_time is 1 second, which drops a tick
    delayed behind a slow job instead of running it late."""
    jobs = [j for j in _scheduler().get_jobs() if j.func is func]
    assert jobs[0].misfire_grace_time and jobs[0].misfire_grace_time >= 600


class TestFollowupGrid:
    """run_followups gates on a 13:00-19:00 window in the USER's timezone, so the
    grid has to land inside that window for every timezone served — not just for
    whichever one the UTC hours were picked against."""

    SERVED = ("America/Chicago", "America/Los_Angeles")

    def _ticks_in_window(self, tz_name):
        trigger = _trigger_for(__import__("followup").run_followups)
        start = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        fires = _fire_times(trigger, start, 24)  # a full day at 2h spacing
        return [f for f in fires
                if 13 <= f.astimezone(ZoneInfo(tz_name)).hour < 19
                and f < start + timedelta(days=1)]

    @pytest.mark.parametrize("tz_name", SERVED)
    def test_window_gets_multiple_chances(self, tz_name):
        # The old 4h interval gave 1.5 ticks of margin against this 6h window.
        assert len(self._ticks_in_window(tz_name)) >= 3

    def test_grid_is_timezone_agnostic(self):
        # Both zones get the same coverage — the property that keeps working as
        # users are added in zones nobody picked hours for.
        counts = {tz: len(self._ticks_in_window(tz)) for tz in self.SERVED}
        assert len(set(counts.values())) == 1, counts


class TestShortJobsStayOnInterval:
    """Not everything should be cron. These are frequent enough that a deploy
    reset costs less than the added rigidity is worth."""

    def test_short_jobs_are_interval(self):
        from apscheduler.triggers.interval import IntervalTrigger
        from send_reminders import send_due_reminders
        from morning import send_morning_messages
        from watches import run_watches
        for func in (send_due_reminders, send_morning_messages, run_watches):
            assert isinstance(_trigger_for(func), IntervalTrigger), func.__name__
