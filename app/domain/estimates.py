"""Learns how long you really take compared with what you estimate."""

from __future__ import annotations

import math
from statistics import median

MIN_SAMPLES = 3
CLAMP = (0.5, 3.0)


def calibration_factors(samples: list[tuple[str | None, int, int]]) -> dict[str, float]:
    """samples: (category, estimated_min, actual_min). Returns {category: ratio, '*': overall ratio}.

    Only categories with at least MIN_SAMPLES completed tasks get their own factor.
    """
    by_cat: dict[str, list[float]] = {}
    overall: list[float] = []
    for category, est, actual in samples:
        if est <= 0 or actual <= 0:
            continue
        ratio = actual / est
        overall.append(ratio)
        if category:
            by_cat.setdefault(category.lower(), []).append(ratio)

    def clamp(x: float) -> float:
        return round(min(CLAMP[1], max(CLAMP[0], x)), 2)

    factors = {c: clamp(median(r)) for c, r in by_cat.items() if len(r) >= MIN_SAMPLES}
    if len(overall) >= MIN_SAMPLES:
        factors["*"] = clamp(median(overall))
    return factors


def apply_factor(minutes: int, category: str | None, factors: dict[str, float]) -> int:
    factor = factors.get((category or "").lower()) or factors.get("*") or 1.0
    return max(1, math.ceil(minutes * factor))
