from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.estimates import apply_factor, calibration_factors
from app.domain.planner import BlockedWindow, Interval, PlanPrefs, chunk_sizes, plan_day
from app.domain.prioritizer import PlanTask, rank, score_task
from app.domain.recurrence import next_occurrence
from app.domain.timeutil import UTC, local_dt, parse_dt, parse_when, resolve_day

TZ = ZoneInfo("Asia/Karachi")  # UTC+5, no DST
DAY = date(2026, 10, 14)  # a Wednesday


def at(h, m=0, day=DAY):
    return local_dt(day, time(h, m), TZ)


def task(id, priority="P3", minutes=30, due=None, created=None, title=None, category=None):
    return PlanTask(id, title or id, priority, minutes, due, created or at(8, day=date(2026, 10, 13)), category)


PREFS = PlanPrefs(tz=TZ)
EARLY = at(6)  # "now" before the work day


# ----------------------------------------------------------------------------- time parsing
class TestParseWhen:
    NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)  # 13:00 in Karachi

    def local(self, dt):
        return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")

    def test_tomorrow_at_time(self):
        assert self.local(parse_when("call mom tomorrow at 7pm", self.NOW, TZ)) == "2026-10-15 19:00"

    def test_tomorrow_typo_and_default_time(self):
        assert self.local(parse_when("by tomorrw", self.NOW, TZ)) == "2026-10-15 09:00"

    def test_relative_minutes_and_hours(self):
        assert parse_when("in 20 mins", self.NOW, TZ) == self.NOW + timedelta(minutes=20)
        assert parse_when("in 2 hours", self.NOW, TZ) == self.NOW + timedelta(hours=2)

    def test_bare_clock_rolls_to_next_day_when_passed(self):
        assert self.local(parse_when("at 9am", self.NOW, TZ)) == "2026-10-15 09:00"
        assert self.local(parse_when("at 6pm", self.NOW, TZ)) == "2026-10-14 18:00"

    def test_weekday_is_always_in_the_future(self):
        assert self.local(parse_when("friday 3pm", self.NOW, TZ)) == "2026-10-16 15:00"
        assert self.local(parse_when("wednesday 3pm", self.NOW, TZ)) == "2026-10-21 15:00"

    def test_no_time_in_text(self):
        assert parse_when("buy milk", self.NOW, TZ) is None

    def test_twelve_hour_edge_cases(self):
        assert self.local(parse_when("tomorrow 12am", self.NOW, TZ)) == "2026-10-15 00:00"
        assert self.local(parse_when("tomorrow 12pm", self.NOW, TZ)) == "2026-10-15 12:00"


def test_parse_dt_naive_is_local_and_z_is_utc():
    assert parse_dt("2026-10-14T19:00:00", TZ) == datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    assert parse_dt("2026-10-14T19:00:00Z", TZ) == datetime(2026, 10, 14, 19, 0, tzinfo=UTC)
    assert parse_dt("garbage", TZ) is None
    assert parse_dt("", TZ) is None


def test_resolve_day():
    now = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    assert resolve_day("today", now, TZ) == DAY
    assert resolve_day("tomorrow", now, TZ) == date(2026, 10, 15)
    assert resolve_day("2026-12-25", now, TZ) == date(2026, 12, 25)
    with pytest.raises(ValueError):
        resolve_day("someday", now, TZ)


# ----------------------------------------------------------------------------- prioritizer
class TestPrioritizer:
    def test_priority_orders_tasks(self):
        ranked = rank([task("low", "P4"), task("high", "P1"), task("mid", "P2")], EARLY)
        assert [s.task.id for s in ranked] == ["high", "mid", "low"]

    def test_imminent_deadline_beats_higher_priority(self):
        urgent = task("urgent", "P3", due=EARLY + timedelta(hours=2))
        important = task("important", "P1")
        assert rank([important, urgent], EARLY)[0].task.id == "urgent"

    def test_overdue_gets_boost_and_reason(self):
        s = score_task(task("late", "P3", due=EARLY - timedelta(hours=5)), EARLY)
        assert "overdue" in s.reasons and s.score > 100

    def test_reasons_are_human_readable(self):
        s = score_task(task("x", "P1", minutes=10, due=EARLY + timedelta(hours=3)), EARLY)
        assert s.reasons[0] == "P1" and "due in 3h" in s.reasons and "quick win" in s.reasons

    def test_ties_break_on_deadline_then_age(self):
        a = task("a", due=EARLY + timedelta(days=30))
        b = task("b", due=EARLY + timedelta(days=29))
        assert rank([a, b], EARLY)[0].task.id == "b"

    def test_old_tasks_slowly_rise(self):
        fresh = task("fresh", created=EARLY)
        old = task("old", created=EARLY - timedelta(days=9))
        assert rank([fresh, old], EARLY)[0].task.id == "old"


# ----------------------------------------------------------------------------- planner
class TestChunks:
    @pytest.mark.parametrize("total,max_block,expected", [(30, 90, [30]), (90, 90, [90]), (120, 90, [60, 60]), (100, 90, [50, 50]), (200, 90, [67, 67, 66])])
    def test_even_chunks(self, total, max_block, expected):
        assert chunk_sizes(total, max_block) == expected
        assert sum(expected) == total


class TestPlanner:
    def test_places_highest_priority_first_in_focus_window(self):
        r = plan_day(DAY, [task("low", "P4", 60), task("high", "P1", 60)], [], PREFS, EARLY)
        by_id = {b.task_id: b for b in r.blocks}
        assert by_id["high"].start == at(9)  # focus window starts at 9
        assert by_id["low"].start >= at(12)  # P4 stays out of the focus window when it can
        assert not r.deferred

    def test_blocks_never_overlap_and_respect_buffer(self):
        r = plan_day(DAY, [task("a", "P1", 60), task("b", "P1", 60), task("c", "P2", 30)], [], PREFS, EARLY)
        blocks = sorted(r.blocks, key=lambda b: b.start)
        for prev, nxt in zip(blocks, blocks[1:]):
            assert nxt.start >= prev.end
            # the 10 minute buffer applies between blocks in the same slot
            if prev.end.astimezone(TZ).hour < 12 <= nxt.start.astimezone(TZ).hour:
                continue
            assert nxt.start - prev.end >= timedelta(minutes=10)

    def test_all_blocks_inside_working_hours(self):
        r = plan_day(DAY, [task(str(i), "P2", 90) for i in range(8)], [], PREFS, EARLY)
        for b in r.blocks:
            assert at(9) <= b.start and b.end <= at(18)

    def test_busy_intervals_are_avoided(self):
        busy = [Interval(at(9), at(11))]
        r = plan_day(DAY, [task("t", "P1", 60)], busy, PREFS, EARLY)
        assert r.blocks[0].start >= at(11)

    def test_blocked_window_is_avoided(self):
        prefs = PlanPrefs(tz=TZ, blocked=(BlockedWindow("lunch", time(13, 0), time(14, 0)),))
        r = plan_day(DAY, [task("t", "P2", 60) for _ in range(1)] + [task(str(i), "P2", 60) for i in range(6)], [], prefs, EARLY)
        for b in r.blocks:
            assert not (b.start < at(14) and b.end > at(13))

    def test_overflow_is_deferred_with_reason(self):
        tasks = [task(str(i), "P3", 90) for i in range(8)]  # 12h of work, ~9h window
        r = plan_day(DAY, tasks, [], PREFS, EARLY)
        assert r.deferred and all(d.reason for d in r.deferred)
        assert r.planned_minutes <= 9 * 60

    def test_low_priority_is_what_gets_deferred(self):
        tasks = [task(f"p1_{i}", "P1", 90) for i in range(4)] + [task(f"p4_{i}", "P4", 90) for i in range(4)]
        r = plan_day(DAY, tasks, [], PREFS, EARLY)
        assert all(d.task_id.startswith("p4") for d in r.deferred)

    def test_long_task_is_split_into_blocks(self):
        r = plan_day(DAY, [task("big", "P1", 180)], [], PREFS, EARLY)
        mine = [b for b in r.blocks if b.task_id == "big"]
        assert len(mine) >= 2 and sum((b.end - b.start).seconds // 60 for b in mine) == 180
        assert all((b.end - b.start) <= timedelta(minutes=90) for b in mine)

    def test_deadline_is_respected(self):
        r = plan_day(DAY, [task("d", "P1", 60, due=at(11))], [], PREFS, EARLY)
        assert r.blocks[0].end <= at(11)

    def test_impossible_deadline_is_deferred(self):
        busy = [Interval(at(9), at(18))]
        r = plan_day(DAY, [task("d", "P1", 60, due=at(11))], busy, PREFS, EARLY)
        assert not r.blocks and r.deferred[0].reason == "can't fit before its deadline"

    def test_deadline_before_the_day_starts_cannot_be_met(self):
        r = plan_day(DAY, [task("d", "P1", 30, due=at(8))], [], PREFS, EARLY)
        assert not r.blocks and "deadline" in r.deferred[0].reason

    def test_overdue_tasks_have_no_cutoff(self):
        r = plan_day(DAY, [task("late", "P1", 30, due=EARLY - timedelta(days=2))], [], PREFS, EARLY)
        assert r.blocks and not r.deferred

    def test_today_never_plans_in_the_past(self):
        now = at(14, 3)
        r = plan_day(DAY, [task("t", "P1", 30)], [], PREFS, now)
        assert r.blocks[0].start == at(14, 5)  # rounded up to the next 5 minutes

    def test_after_hours_defers_everything(self):
        r = plan_day(DAY, [task("t", "P1", 30)], [], PREFS, at(19))
        assert not r.blocks and "over" in r.deferred[0].reason

    def test_fragmented_day_splits_a_task_across_gaps(self):
        # free time: 09:00-10:00 and 10:30-11:30 (two 60 min gaps) -> a 120 min task is split 60 + 60
        busy = [Interval(at(10), at(10, 30)), Interval(at(11, 30), at(12))]
        prefs = PlanPrefs(tz=TZ, work_end=time(12, 0), buffer_min=0)
        r = plan_day(DAY, [task("t", "P1", 120)], busy, prefs, EARLY)
        assert [(b.start, b.end) for b in r.blocks] == [(at(9), at(10)), (at(10, 30), at(11, 30))]
        assert not r.deferred

    def test_partial_fit_reports_what_did_not_fit(self):
        prefs = PlanPrefs(tz=TZ, work_end=time(10, 0), buffer_min=0)
        r = plan_day(DAY, [task("t", "P1", 90)], [], prefs, EARLY)
        assert r.planned_minutes == 60 and r.deferred[0].reason == "only 1h of 1h 30m fit"

    def test_empty_inputs(self):
        r = plan_day(DAY, [], [], PREFS, EARLY)
        assert r.blocks == [] and r.deferred == [] and r.free_minutes_left > 0

    def test_invalid_working_hours_raise(self):
        with pytest.raises(ValueError):
            plan_day(DAY, [], [], PlanPrefs(tz=TZ, work_start=time(18), work_end=time(9)), EARLY)

    def test_deterministic(self):
        tasks = [task(str(i), "P2", 45) for i in range(5)]
        a = plan_day(DAY, tasks, [], PREFS, EARLY)
        b = plan_day(DAY, list(reversed(tasks)), [], PREFS, EARLY)
        assert [(x.task_id, x.start) for x in a.blocks] == [(x.task_id, x.start) for x in b.blocks]


# ----------------------------------------------------------------------------- recurrence
class TestRecurrence:
    def test_daily_and_weekly(self):
        d = at(19)
        assert next_occurrence("DAILY", d, TZ) == at(19, day=date(2026, 10, 15))
        assert next_occurrence("WEEKLY", d, TZ) == at(19, day=date(2026, 10, 21))

    def test_weekdays_skip_weekend(self):
        friday = at(9, day=date(2026, 10, 16))
        assert next_occurrence("WEEKDAYS", friday, TZ) == at(9, day=date(2026, 10, 19))

    def test_monthly_clamps_short_months(self):
        jan31 = local_dt(date(2027, 1, 31), time(9), TZ)
        assert next_occurrence("MONTHLY", jan31, TZ) == local_dt(date(2027, 2, 28), time(9), TZ)

    def test_late_completion_skips_past_occurrences(self):
        d = at(9)
        late = at(9, day=date(2026, 10, 20)) + timedelta(hours=1)
        assert next_occurrence("DAILY", d, TZ, after=late) == at(9, day=date(2026, 10, 21))

    def test_keeps_local_time_across_dst(self):
        ny = ZoneInfo("America/New_York")
        before = local_dt(date(2026, 11, 1), time(9), ny)  # DST ends Nov 1 2026
        nxt = next_occurrence("DAILY", before, ny)
        assert nxt.astimezone(ny).hour == 9

    def test_unknown_rule(self):
        with pytest.raises(ValueError):
            next_occurrence("HOURLY", at(9), TZ)


# ----------------------------------------------------------------------------- estimates
class TestEstimates:
    def test_needs_enough_samples(self):
        assert calibration_factors([("writing", 30, 60), ("writing", 30, 60)]) == {}

    def test_category_and_overall_factor(self):
        samples = [("writing", 30, 45)] * 3 + [("email", 10, 10)] * 3
        f = calibration_factors(samples)
        assert f["writing"] == 1.5 and f["email"] == 1.0 and f["*"] == 1.25

    def test_ignores_missing_actuals(self):
        assert calibration_factors([("a", 30, 0)] * 5) == {}

    def test_factor_is_clamped(self):
        assert calibration_factors([("a", 10, 100)] * 3)["a"] == 3.0
        assert calibration_factors([("a", 100, 1)] * 3)["a"] == 0.5

    def test_apply_falls_back_to_overall_then_identity(self):
        f = {"writing": 1.5, "*": 1.2}
        assert apply_factor(30, "Writing", f) == 45
        assert apply_factor(30, "other", f) == 36
        assert apply_factor(30, None, {}) == 30
