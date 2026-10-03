"""门机劣化纯评估逻辑。

结论由三部分合成：

1. 最近动作窗口中的动作时长与阻力（判断正在发生的机械劣化）；
2. 投运以来累计动作次数（判断寿命消耗，历史不随窗口滚动而消失）；
3. 采样完整性（采样缺失只降低置信度，不用零值或插值冒充劣化）。

本模块不读写状态，便于门控、联锁、审计和测试复用同一套确定性规则。
"""

from __future__ import annotations

from ..config import DegradationSection
from .model import (
    EXCLUDING_STATUSES,
    STATUS_CRITICAL,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    DoorAction,
    DegradationVerdict,
    MetricStats,
)
from .schedule import next_window


def select_window(
    actions: list[DoorAction],
    now: float,
    section: DegradationSection,
) -> list[DoorAction]:
    """按最近动作数和最大窗口年龄取评估样本，事件时间相同则保留稳定顺序。"""

    ordered = sorted(enumerate(actions), key=lambda item: (item[1].at, item[0]))
    recent = [item[1] for item in ordered[-section.window_actions :]]
    return [
        action
        for action in recent
        if now - section.window_max_age_s <= action.at <= now
    ]


def metric_stats(
    actions: list[DoorAction],
    field_name: str,
    section: DegradationSection,
) -> MetricStats:
    """统计一个指标的覆盖率、均值、95 分位和最大值。"""

    expected = len(actions)
    values = [
        float(getattr(action, field_name))
        for action in actions
        if getattr(action, field_name) is not None
    ]
    if not values:
        return MetricStats(0, expected, expected, None, None, None)
    ordered = sorted(values)
    rank = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return MetricStats(
        count=len(values),
        expected=expected,
        missing=expected - len(values),
        mean=sum(values) / len(values),
        p95=ordered[rank],
        max=ordered[-1],
    )


def evaluate(
    psd_id: str,
    actions: list[DoorAction],
    lifetime_actions: int,
    now: float,
    section: DegradationSection,
    revision: int = 1,
    previous: DegradationVerdict | None = None,
    installed_at: float | None = None,
) -> DegradationVerdict:
    """生成一份门机劣化结论。"""

    window = select_window(actions, now, section)
    duration = metric_stats(window, "duration_s", section)
    resistance = metric_stats(window, "resistance_n", section)
    expected_total = max(1, len(window) * 2)
    observed_total = duration.count + resistance.count
    metric_coverage = observed_total / expected_total if window else 0.0
    enough_samples = len(window) >= section.min_samples
    enough_coverage = metric_coverage >= section.required_coverage
    complete = bool(enough_samples and enough_coverage)

    warnings: list[str] = []
    if not window:
        warnings.append("窗口内没有有效动作")
    elif not enough_samples:
        warnings.append(f"窗口样本不足: {len(window)}/{section.min_samples}")
    if window and not enough_coverage:
        warnings.append(f"采样覆盖率不足: {metric_coverage:.2f}/{section.required_coverage:.2f}")

    duration_ratio = 1.0 if duration.count == 0 else _ratio(duration.p95, section.nominal_duration_s)
    resistance_ratio = 1.0 if resistance.count == 0 else _ratio(resistance.p95, section.nominal_resistance_n)
    duration_score = _component_score(
        duration_ratio,
        section.duration_warn_ratio,
        section.duration_critical_ratio,
    )
    resistance_score = _component_score(
        resistance_ratio,
        section.resistance_warn_ratio,
        section.resistance_critical_ratio,
    )
    lifetime_ratio = lifetime_actions / section.lifetime_critical_actions
    lifetime_score = _bounded(int(round(100 * lifetime_ratio)))

    score = max(duration_score, resistance_score, lifetime_score)
    reasons: list[str] = []
    if duration_ratio >= section.duration_warn_ratio:
        reasons.append(
            f"动作时长 p95 为额定 {duration_ratio:.2f} 倍"
        )
    if resistance_ratio >= section.resistance_warn_ratio:
        reasons.append(f"阻力 p95 为额定 {resistance_ratio:.2f} 倍")
    if resistance.max is not None and resistance.max >= section.spike_resistance_n:
        score = max(score, section.critical_score)
        reasons.append("出现硬卡阻级阻力尖峰")
    if lifetime_actions >= section.lifetime_warn_actions:
        reasons.append(f"累计动作 {lifetime_actions} 次，接近或达到寿命检修区间")

    if not complete:
        if previous is not None and previous.status != STATUS_UNKNOWN:
            # 采样缺失不能制造“突然健康”，也不能在证据不足时立即新摘除。
            status = previous.status
        else:
            status = STATUS_UNKNOWN
    elif score >= section.critical_score:
        status = STATUS_CRITICAL
    elif score >= section.degraded_score:
        status = STATUS_DEGRADED
    elif score >= section.watch_score:
        status = STATUS_WATCH
    else:
        status = STATUS_HEALTHY

    urgent = status == STATUS_CRITICAL
    immediate_critical = status == STATUS_CRITICAL and (
        (resistance.max is not None and resistance.max >= section.spike_resistance_n)
        or lifetime_actions >= section.lifetime_critical_actions
        or resistance_ratio >= section.resistance_critical_ratio
        or duration_ratio >= section.duration_critical_ratio
    )
    window_suggestion = next_window(now, section, urgent=urgent)
    should_exclude = status in EXCLUDING_STATUSES
    immediate_severity = immediate_critical
    previous_excluded = bool(previous and previous.excluded_from_batch)
    excluded = previous_excluded or should_exclude
    pending = bool(should_exclude and not excluded and previous and previous.status != status)
    if previous and previous.pending_exclusion and status in EXCLUDING_STATUSES:
        pending = previous.pending_exclusion
    excluded_at = previous.excluded_at if previous_excluded else (now if excluded and not pending else None)

    return DegradationVerdict(
        psd_id=psd_id,
        status=status,
        score=_bounded(score),
        excluded_from_batch=excluded,
        excluded_at=excluded_at,
        pending_exclusion=pending,
        immediate_severity=immediate_severity,
        reasons=tuple(reasons),
        metrics={
            "duration": {
                **duration.as_dict(),
                "nominal_s": section.nominal_duration_s,
                "ratio": round(duration_ratio, 4),
                "score": duration_score,
            },
            "resistance": {
                **resistance.as_dict(),
                "nominal_n": section.nominal_resistance_n,
                "ratio": round(resistance_ratio, 4),
                "score": resistance_score,
            },
            "lifetime": {
                "actions": lifetime_actions,
                "installed_at": installed_at,
                "ratio": round(lifetime_ratio, 4),
                "score": lifetime_score,
            },
            "sampling_coverage": round(metric_coverage, 4),
        },
        lifetime_actions=lifetime_actions,
        window_actions=len(window),
        data_complete=complete,
        data_warnings=tuple(warnings),
        suggested_window=window_suggestion.label,
        suggested_by=window_suggestion.starts_at,
        evaluated_at=now,
        revision=revision,
        based_on_event_ids=tuple(action.event_id for action in window),
    )


def _ratio(value: float | None, nominal: float) -> float:
    if value is None:
        return 1.0
    return value / nominal


def _component_score(ratio: float, warn_ratio: float, critical_ratio: float) -> int:
    if ratio < warn_ratio:
        return _bounded(int(round(100 * (ratio / warn_ratio) * 0.35)))
    if ratio < critical_ratio:
        span = critical_ratio - warn_ratio
        position = min(1.0, max(0.0, (ratio - warn_ratio) / span))
        return _bounded(int(round(35 + position * 50)))
    overshoot = min(1.0, max(0.0, (ratio - critical_ratio) / critical_ratio))
    return _bounded(int(round(85 + overshoot * 15)))


def _bounded(value: int) -> int:
    return max(0, min(100, value))
