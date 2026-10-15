"""Ranks open tasks. Pure and explainable: every score comes with human-readable reasons."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

PRIORITY_WEIGHT = {"P1": 100, "P2": 60, "P3": 30, "P4": 10}


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    priority: str
    remaining_min: int
    due_at: datetime | None
    created_at: datetime
    category: str | None = None


@dataclass(frozen=True)
class Scored:
    task: PlanTask
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _hours_label(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def score_task(task: PlanTask, now: datetime) -> Scored:
    score = float(PRIORITY_WEIGHT.get(task.priority, 30))
    reasons = [task.priority]

    if task.due_at is not None:
        hours = (task.due_at - now).total_seconds() / 3600
        if hours < 0:
            score += 90 + min(20.0, -hours)
            reasons.append("overdue")
        else:
            if hours <= 3:
                score += 80
            elif hours <= 24:
                score += 45
            elif hours <= 72:
                score += 20
            elif hours <= 24 * 7:
                score += 8
            reasons.append(f"due in {_hours_label(hours)}")

    if task.remaining_min <= 15:
        score += 5
        reasons.append("quick win")

    age_days = max(0.0, (now - task.created_at).total_seconds() / 86400)
    score += min(10.0, age_days)  # slowly lifts neglected tasks
    return Scored(task, round(score, 2), tuple(reasons))


def rank(tasks: list[PlanTask], now: datetime) -> list[Scored]:
    """Highest score first; ties go to the earlier deadline, then the older task."""
    far = datetime.max.replace(tzinfo=now.tzinfo)
    scored = [score_task(t, now) for t in tasks]
    return sorted(scored, key=lambda s: (-s.score, s.task.due_at or far, s.task.created_at, s.task.id))
