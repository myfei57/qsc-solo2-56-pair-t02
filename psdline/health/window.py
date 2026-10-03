"""滑动窗口统计。

劣化是"最近一段"的现象，窗口只保留最近若干次合格动作的比值样本，用中位数
与 Theil-Sen 斜率刻画水平与趋势：两者都对离群点稳健，个别毛刺不会把评估
带偏。不合格样本（采样缺失、时长缺失）根本不进窗口 —— 宁可窗口短一点，
也不让采集故障污染门机结论。
"""

from __future__ import annotations


def median(values: list[float]) -> float:
    """返回中位数；空列表抛 ``ValueError``，调用方保证非空。"""

    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise ValueError("中位数需要至少一个样本")
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def theil_sen_slope(xs: list[float], ys: list[float]) -> float:
    """Theil-Sen 稳健斜率：全部点对斜率的中位数。

    窗口最多几十个点，点对数量级在千以内，每次评估全量算代价可忽略；
    相比最小二乘，它不假定噪声形态，对卡阻尖峰这类重尾更稳。
    """

    pairs = len(xs)
    if pairs < 2:
        return 0.0
    slopes: list[float] = []
    for i in range(pairs):
        for j in range(i + 1, pairs):
            span = xs[j] - xs[i]
            if span > 0:
                slopes.append((ys[j] - ys[i]) / span)
    if not slopes:
        return 0.0
    return median(slopes)


def window_snapshot(window: list[list], unqualified: int) -> dict:
    """把窗口里的 [cycle, 时长比, 阻力比|None] 汇总成一份统计快照。"""

    cycles = [item[0] for item in window]
    durations = [item[1] for item in window]
    resistance_pairs = [(item[0], item[2]) for item in window if item[2] is not None]
    return {
        "qualified": len(window),
        "unqualified": unqualified,
        "median_duration_ratio": round(median(durations), 4),
        "median_resistance_ratio": (
            round(median([value for _cycle, value in resistance_pairs]), 4)
            if resistance_pairs
            else None
        ),
        "slope_duration": round(theil_sen_slope(cycles, durations), 6),
        "slope_resistance": (
            round(
                theil_sen_slope(
                    [cycle for cycle, _value in resistance_pairs],
                    [value for _cycle, value in resistance_pairs],
                ),
                6,
            )
            if len(resistance_pairs) >= 2
            else None
        ),
    }
