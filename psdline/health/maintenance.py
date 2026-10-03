"""检修时段建议。

按评分的时间斜率外推到达摘除阈值的剩余时间，再把建议窗口对齐到配置的运营
低谷（默认 UTC 01:00–05:00）。已经 DEGRADED/CRITICAL 的门不再外推：建议
立即（下一个低谷窗口内）检修。外推只用最近保留的评分历史，斜率不可信
（样本太少或不再恶化）时退化为"下次例行窗口"。
"""

from __future__ import annotations

from ..config import HealthSection
from .model import DoorState

DAY_S = 86400.0


def score_slope_per_day(history: list[list]) -> float | None:
    """评分历史（[[时刻, 评分], …]）的最小二乘斜率，单位：分/天。"""

    if len(history) < 4:
        return None
    xs = [point[0] for point in history]
    ys = [point[1] for point in history]
    if xs[-1] - xs[0] <= 0:
        return None
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return slope * DAY_S


def next_off_peak_start(now: float, windows: tuple) -> float:
    """返回 ``now`` 之后最近的低谷窗口起点（UTC 整点对齐）。"""

    midnight = now - (now % DAY_S)
    candidates: list[float] = []
    for day in range(3):
        for start, _end in windows:
            candidate = midnight + day * DAY_S + start * 3600.0
            if candidate >= now:
                candidates.append(candidate)
    return min(candidates) if candidates else now


def advice_for(
    policy: HealthSection,
    total_cycles: int,
    cycles_at_maintenance: int,
    score_history: list[list],
    score: float | None,
    now: float,
    state: str,
) -> dict | None:
    """给出建议检修时段；不需要检修时返回 ``None``。"""

    life = (total_cycles - cycles_at_maintenance) / policy.rated_cycles
    triggered = (
        state in (DoorState.SUSPECT.value, DoorState.DEGRADED.value, DoorState.CRITICAL.value)
        or (score is not None and score >= policy.warn_threshold * 0.75)
        or life >= policy.life_warn_ratio
    )
    if not triggered:
        return None

    urgent = state in (DoorState.DEGRADED.value, DoorState.CRITICAL.value)
    not_before = now if state == DoorState.CRITICAL.value else next_off_peak_start(
        now, policy.off_peak_windows
    )
    slope = score_slope_per_day(score_history)
    reasons: list[str] = []
    if state in (DoorState.DEGRADED.value, DoorState.CRITICAL.value):
        not_after = not_before
        reasons.append("已摘除，建议下一个低谷窗口内检修")
    else:
        not_after = now + 30 * DAY_S
        if slope is not None and slope > 1e-9 and score is not None:
            days = max(0.0, (policy.exclude_threshold - score) / slope)
            not_after = max(not_before, now + days * 0.8 * DAY_S)
            reasons.append(f"按当前劣化速率约 {days:.0f} 天后触及摘除线")
            if days <= 3:
                urgent = True
        if state == DoorState.SUSPECT.value:
            reasons.append("评分持续越过预警线")
    if life >= policy.life_warn_ratio:
        reasons.append(f"寿命消耗 {life * 100:.0f}%，接近额定 {policy.rated_cycles} 次")
    return {
        "not_before": int(not_before),
        "not_after": int(not_after),
        "urgent": urgent,
        "reasons": reasons,
    }
