"""Turns ranked tasks + free time into time blocks. Pure: no database, no network, no clock.

Algorithm
  1. free time = working hours  -  busy intervals (events, calendar)  -  blocked windows (lunch, gym...)
     (for today, nothing before `now`)
  2. free time is split at the focus-window edges so each slot is either "focus" or "normal"
  3. tasks are taken in priority order; each is cut into even chunks of <= max_block_min
  4. a chunk goes into the earliest slot that fits (P1/P2 prefer focus slots, others prefer normal ones);
     if nothing is big enough it may be split across smaller slots (>= min_block_min)
  5. a buffer is left after every block; a task with a deadline is only placed before that deadline
  6. whatever cannot be placed is returned as `deferred`, with the reason
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.prioritizer import PlanTask, rank
from app.domain.timeutil import fmt_minutes, local_dt


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class BlockedWindow:
    label: str
    start: time
    end: time  # if end <= start the window runs past midnight


@dataclass(frozen=True)
class PlanPrefs:
    tz: ZoneInfo
    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    focus_start: time = time(9, 0)
    focus_end: time = time(12, 0)
    max_block_min: int = 90
    min_block_min: int = 15
    buffer_min: int = 10
    blocked: tuple[BlockedWindow, ...] = ()


@dataclass(frozen=True)
class PlannedBlock:
    task_id: str
    title: str
    start: datetime
    end: datetime
    priority: str
    reason: str


@dataclass(frozen=True)
class Deferred:
    task_id: str
    title: str
    reason: str


@dataclass
class PlanResult:
    day: date
    blocks: list[PlannedBlock] = field(default_factory=list)
    deferred: list[Deferred] = field(default_factory=list)
    window: Interval | None = None
    planned_minutes: int = 0
    free_minutes_left: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Slot:
    start: datetime
    end: datetime
    focus: bool

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _subtract(intervals: list[Interval], cut: Interval) -> list[Interval]:
    out: list[Interval] = []
    for iv in intervals:
        if cut.end <= iv.start or cut.start >= iv.end:
            out.append(iv)
            continue
        if cut.start > iv.start:
            out.append(Interval(iv.start, cut.start))
        if cut.end < iv.end:
            out.append(Interval(cut.end, iv.end))
    return out


def chunk_sizes(total: int, max_block: int) -> list[int]:
    """Cut `total` minutes into the fewest, most even chunks that are each <= max_block."""
    n = max(1, math.ceil(total / max_block))
    base, extra = divmod(total, n)
    return [base + 1] * extra + [base] * (n - extra)


def _make_slots(free: list[Interval], focus: Interval) -> list[_Slot]:
    slots: list[_Slot] = []
    for iv in free:
        inner_start, inner_end = max(iv.start, focus.start), min(iv.end, focus.end)
        if inner_start < inner_end:
            if iv.start < inner_start:
                slots.append(_Slot(iv.start, inner_start, False))
            slots.append(_Slot(inner_start, inner_end, True))
            if inner_end < iv.end:
                slots.append(_Slot(inner_end, iv.end, False))
        else:
            slots.append(_Slot(iv.start, iv.end, False))
    return sorted(slots, key=lambda s: s.start)


def _usable_minutes(slot: _Slot, limit: datetime | None) -> int:
    end = min(slot.end, limit) if limit else slot.end
    return max(0, int((end - slot.start).total_seconds() // 60))


def _pick(slots: list[_Slot], need: int, limit: datetime | None, important: bool, min_block: int) -> int | None:
    """Index of the best slot for `need` minutes, or None."""
    order = (True, False) if important else (False, True)

    def first(min_minutes: int) -> int | None:
        for want_focus in order:
            for i, s in enumerate(slots):  # slots are sorted by start -> earliest first
                if s.focus == want_focus and _usable_minutes(s, limit) >= min_minutes:
                    return i
        return None

    found = first(need)
    if found is None:
        found = first(min(need, min_block))  # no slot is big enough: allow splitting across slots
    return found


def plan_day(
    day: date,
    tasks: list[PlanTask],
    busy: list[Interval],
    prefs: PlanPrefs,
    now: datetime,
) -> PlanResult:
    tz = prefs.tz
    if prefs.work_end <= prefs.work_start:
        raise ValueError("work_end must be later than work_start.")

    window = Interval(local_dt(day, prefs.work_start, tz), local_dt(day, prefs.work_end, tz))
    result = PlanResult(day=day, window=window)

    start = window.start
    if now > start:  # planning today: nothing in the past, start on a 5-minute mark
        rounded = now.replace(second=0, microsecond=0)
        rounded += timedelta(minutes=(-rounded.minute) % 5)
        start = max(start, rounded)
    if start >= window.end:
        result.deferred = [Deferred(t.id, t.title, "the working day is already over") for t in _ranked(tasks, now)]
        return result

    free = [Interval(start, window.end)]
    for b in busy:
        free = _subtract(free, b)
    for w in prefs.blocked:
        for offset in (-1, 0):  # a window from yesterday evening may run into today
            d = day + timedelta(days=offset)
            w_start = local_dt(d, w.start, tz)
            w_end = local_dt(d if w.end > w.start else d + timedelta(days=1), w.end, tz)
            free = _subtract(free, Interval(w_start, w_end))

    focus = Interval(local_dt(day, prefs.focus_start, tz), local_dt(day, prefs.focus_end, tz))
    slots = [s for s in _make_slots([f for f in free if f.minutes >= 5], focus) if s.minutes >= 5]

    buffer = timedelta(minutes=prefs.buffer_min)
    for scored in rank(tasks, now):
        task = scored.task
        important = task.priority in ("P1", "P2")
        limit = task.due_at if task.due_at and task.due_at > now else None  # overdue tasks have no cut-off
        reason = " · ".join(scored.reasons)
        placed = 0

        for size in chunk_sizes(max(1, task.remaining_min), prefs.max_block_min):
            need = size
            while need > 0:
                idx = _pick(slots, need, limit, important, prefs.min_block_min)
                if idx is None:
                    break
                slot = slots[idx]
                take = min(need, _usable_minutes(slot, limit))
                b_start, b_end = slot.start, slot.start + timedelta(minutes=take)
                result.blocks.append(PlannedBlock(task.id, task.title, b_start, b_end, task.priority, reason))
                placed += take
                need -= take
                rest_start = min(b_end + buffer, slot.end)
                if (slot.end - rest_start).total_seconds() >= 5 * 60:
                    slots[idx] = _Slot(rest_start, slot.end, slot.focus)
                else:
                    slots.pop(idx)
            if need > 0:
                break

        wanted = max(1, task.remaining_min)
        if placed < wanted:
            if limit is not None and placed == 0:
                why = "can't fit before its deadline"
            elif placed == 0:
                why = "no free time left"
            else:
                why = f"only {fmt_minutes(placed)} of {fmt_minutes(wanted)} fit"
            result.deferred.append(Deferred(task.id, task.title, why))

    result.blocks.sort(key=lambda b: b.start)
    result.planned_minutes = sum(int((b.end - b.start).total_seconds() // 60) for b in result.blocks)
    result.free_minutes_left = sum(s.minutes for s in slots)
    return result


def _ranked(tasks: list[PlanTask], now: datetime) -> list[PlanTask]:
    return [s.task for s in rank(tasks, now)]
