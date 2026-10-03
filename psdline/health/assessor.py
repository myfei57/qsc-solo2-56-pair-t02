"""劣化评估器。

评估器是纯函数：结论只由（运行时状态、样本、限值、评估时刻）决定，不读时钟、
不读全局，因此崩溃后按流水重放会得到与在线评估一致的结论 —— 这是崩溃安全
与审计可复核的基础。

评分由三个分量加权而成（权重在配置里，总和为 1）：

* 水平 level：窗口内时长/阻力中位数相对基线的抬升，取两者中更差的那个 —
  任何一个单项严重恶化都足以拉高评分；
* 趋势 trend：窗口内时长/阻力的 Theil-Sen 斜率，用于在水平尚未越限时提前
  预警；趋势单独不会把门摘除，只会推到 SUSPECT；
* 寿命 life：投运（或上次检修）以来累计动作次数对额定次数的占比 —— 这是
  唯一按累计计算的指标，权重不足以单独摘除一樘门，但会把临界门推过线。

状态机带滞回：进入 SUSPECT/DEGRADED 需要连续若干次越限，退出 SUSPECT 需要
连续若干次回到安全线以下，阈值之间的死区保持计数不清零，避免单点噪声让
状态来回抖动。DEGRADED/CRITICAL 不会自愈：磨损不会凭空消失，只有检修动作
能复位。连续出现卡阻特征（阻力/时长极端越限）直接升 CRITICAL，不等评分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import HealthSection
from .maintenance import advice_for
from .model import STICKY_STATES, ActionSample, Conclusion, DoorState, eligible_for
from .window import median, window_snapshot

#: 评分历史保留长度，供检修建议外推斜率。
SCORE_HISTORY_LIMIT = 200
#: 无效样本占比超过该值时置信度降为低（配置里的 data_quality_ratio 只负责报警）。
LOW_CONFIDENCE_QUALITY = 0.5


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class DoorRuntime:
    """一樘门的评估运行时状态；整体随结论文档原子落盘。"""

    psd_id: str
    state: str = DoorState.INSUFFICIENT_DATA.value
    total_cycles: int = 0
    cycles_at_maintenance: int = 0
    last_seq: int = 0
    maintenances: int = 0
    baseline_duration: float | None = None
    baseline_resistance: float | None = None
    baseline_pending_durations: list[float] = field(default_factory=list)
    baseline_pending_resistances: list[float] = field(default_factory=list)
    window: list[list] = field(default_factory=list)
    recent_quality: list[bool] = field(default_factory=list)
    c_warn: int = 0
    c_exclude: int = 0
    c_clear: int = 0
    c_jam: int = 0
    score_history: list[list] = field(default_factory=list)
    last_maintenance_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "state": self.state,
            "total_cycles": self.total_cycles,
            "cycles_at_maintenance": self.cycles_at_maintenance,
            "last_seq": self.last_seq,
            "maintenances": self.maintenances,
            "baseline_duration": self.baseline_duration,
            "baseline_resistance": self.baseline_resistance,
            "baseline_pending_durations": list(self.baseline_pending_durations),
            "baseline_pending_resistances": list(self.baseline_pending_resistances),
            "window": [list(item) for item in self.window],
            "recent_quality": list(self.recent_quality),
            "c_warn": self.c_warn,
            "c_exclude": self.c_exclude,
            "c_clear": self.c_clear,
            "c_jam": self.c_jam,
            "score_history": [list(point) for point in self.score_history],
            "last_maintenance_at": self.last_maintenance_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "DoorRuntime":
        return cls(
            psd_id=str(payload.get("psd_id", "")),
            state=str(payload.get("state", DoorState.INSUFFICIENT_DATA.value)),
            total_cycles=int(payload.get("total_cycles", 0)),
            cycles_at_maintenance=int(payload.get("cycles_at_maintenance", 0)),
            last_seq=int(payload.get("last_seq", 0)),
            maintenances=int(payload.get("maintenances", 0)),
            baseline_duration=_optional_float(payload.get("baseline_duration")),
            baseline_resistance=_optional_float(payload.get("baseline_resistance")),
            baseline_pending_durations=[
                float(item) for item in payload.get("baseline_pending_durations", [])
            ],
            baseline_pending_resistances=[
                float(item) for item in payload.get("baseline_pending_resistances", [])
            ],
            window=[list(item) for item in payload.get("window", [])],
            recent_quality=[bool(item) for item in payload.get("recent_quality", [])],
            c_warn=int(payload.get("c_warn", 0)),
            c_exclude=int(payload.get("c_exclude", 0)),
            c_clear=int(payload.get("c_clear", 0)),
            c_jam=int(payload.get("c_jam", 0)),
            score_history=[list(point) for point in payload.get("score_history", [])],
            last_maintenance_at=float(payload.get("last_maintenance_at", 0.0)),
        )

    def apply_maintenance(self, at: float) -> None:
        """检修复位：清空基线与窗口重新标定，寿命计数从当前次数重新起算。

        累计动作次数（total_cycles）不清零 —— 历史不可变，检修只是给寿命
        计算换一个新的起算点。
        """

        self.state = DoorState.INSUFFICIENT_DATA.value
        self.cycles_at_maintenance = self.total_cycles
        self.baseline_duration = None
        self.baseline_resistance = None
        self.baseline_pending_durations = []
        self.baseline_pending_resistances = []
        self.window = []
        self.recent_quality = []
        self.c_warn = 0
        self.c_exclude = 0
        self.c_clear = 0
        self.c_jam = 0
        self.score_history = []
        self.maintenances += 1
        self.last_maintenance_at = at


def is_qualified(sample: ActionSample, policy: HealthSection) -> bool:
    """判定样本是否能进入统计。

    时长缺失或非法、阻力采样完整度不足、阻力峰值缺失，都判为不合格：样本
    本身被隔离在统计之外（动作次数照记），只影响数据质量与置信度。
    """

    if sample.duration_s is None or sample.duration_s <= 0:
        return False
    if not sample.has_resistance_sensor:
        return True
    completeness = sample.completeness
    if completeness is None or completeness < policy.min_completeness:
        return False
    return sample.resistance_peak is not None and sample.resistance_peak > 0


def confidence_of(runtime: DoorRuntime, policy: HealthSection) -> str:
    """当前证据是否足以给出结论。"""

    if len(runtime.window) < policy.min_window_actions:
        return "low"
    if runtime.recent_quality:
        bad = runtime.recent_quality.count(False) / len(runtime.recent_quality)
        if bad > LOW_CONFIDENCE_QUALITY:
            return "low"
    return "high"


def data_quality_ok(runtime: DoorRuntime, policy: HealthSection) -> bool:
    if not runtime.recent_quality:
        return True
    bad = runtime.recent_quality.count(False) / len(runtime.recent_quality)
    return bad <= policy.data_quality_ratio


def assess(
    runtime: DoorRuntime,
    sample: ActionSample,
    policy: HealthSection,
    now: float,
) -> Conclusion:
    """消费一条样本，返回新结论（版本号由登记处统一分配，这里置 0）。"""

    previous = runtime.state
    runtime.total_cycles += 1
    runtime.last_seq = sample.seq
    qualified = is_qualified(sample, policy)
    runtime.recent_quality.append(qualified)
    del runtime.recent_quality[:-policy.window_size]

    score: float | None = None
    snapshot: dict | None = None
    components: dict = {}

    if qualified and runtime.baseline_duration is None:
        _collect_baseline(runtime, sample, policy)
    elif qualified:
        _update_window(runtime, sample, policy)

    # 窗口统计始终随结论给出：单条缺失样本不抹掉既有的最佳认知。
    if runtime.window:
        snapshot = window_snapshot(runtime.window, runtime.recent_quality.count(False))

    # 只有合格样本才算"新证据"：评分、评分历史与滞回计数只在证据到达时推进，
    # 缺失样本既不升级也不清除（详见 _next_state 的置信度规则）。
    if qualified and snapshot is not None and confidence_of(runtime, policy) == "high":
        level, trend, life = _components(snapshot, runtime, policy)
        score = round(
            policy.weight_level * level + policy.weight_trend * trend + policy.weight_life * life,
            4,
        )
        components = {"level": level, "trend": trend, "life": life}
        runtime.score_history.append([sample.at, score])
        del runtime.score_history[:-SCORE_HISTORY_LIMIT]
        _update_counters(runtime, score, policy)

    runtime.state = _next_state(runtime, policy)
    reasons = _reasons(runtime, snapshot, policy)
    advice = advice_for(
        policy,
        runtime.total_cycles,
        runtime.cycles_at_maintenance,
        runtime.score_history,
        score,
        now,
        runtime.state,
    )
    return Conclusion(
        psd_id=runtime.psd_id,
        version=0,
        at=sample.at,
        state=runtime.state,
        previous_state=previous,
        score=score,
        confidence=confidence_of(runtime, policy),
        eligible=eligible_for(runtime.state, policy.exclude_on_insufficient_data),
        components=components,
        window=snapshot,
        reasons=tuple(reasons),
        advice=advice,
        data_quality_ok=data_quality_ok(runtime, policy),
    )


def _collect_baseline(runtime: DoorRuntime, sample: ActionSample, policy: HealthSection) -> None:
    """基线期：攒够合格样本后冻结基线中位数。"""

    runtime.baseline_pending_durations.append(float(sample.duration_s))
    if sample.resistance_peak is not None:
        runtime.baseline_pending_resistances.append(float(sample.resistance_peak))
    if len(runtime.baseline_pending_durations) >= policy.baseline_actions:
        runtime.baseline_duration = median(runtime.baseline_pending_durations)
        runtime.baseline_resistance = (
            median(runtime.baseline_pending_resistances)
            if runtime.baseline_pending_resistances
            else None
        )
        runtime.baseline_pending_durations = []
        runtime.baseline_pending_resistances = []


def _update_window(runtime: DoorRuntime, sample: ActionSample, policy: HealthSection) -> None:
    duration_ratio = float(sample.duration_s) / float(runtime.baseline_duration)
    resistance_ratio = None
    if runtime.baseline_resistance and sample.resistance_peak is not None:
        resistance_ratio = float(sample.resistance_peak) / float(runtime.baseline_resistance)
    runtime.window.append(
        [
            runtime.total_cycles,
            round(duration_ratio, 6),
            round(resistance_ratio, 6) if resistance_ratio is not None else None,
        ]
    )
    del runtime.window[:-policy.window_size]
    jammed = duration_ratio >= policy.jam_duration_ratio or (
        resistance_ratio is not None and resistance_ratio >= policy.jam_resistance_ratio
    )
    runtime.c_jam = runtime.c_jam + 1 if jammed else 0


def _components(snapshot: dict, runtime: DoorRuntime, policy: HealthSection) -> tuple:
    med_duration = snapshot["median_duration_ratio"]
    med_resistance = snapshot["median_resistance_ratio"]
    level = clamp01(
        max(
            (med_duration - 1.0) / (policy.duration_crit_ratio - 1.0),
            (med_resistance - 1.0) / (policy.resistance_crit_ratio - 1.0)
            if med_resistance is not None
            else 0.0,
        )
    )
    slope_duration = snapshot["slope_duration"] or 0.0
    slope_resistance = snapshot["slope_resistance"] or 0.0
    trend = clamp01(max(slope_duration, slope_resistance) / policy.slope_crit_per_action)
    life = clamp01(
        (runtime.total_cycles - runtime.cycles_at_maintenance) / policy.rated_cycles
    )
    return round(level, 4), round(trend, 4), round(life, 4)


def _update_counters(runtime: DoorRuntime, score: float, policy: HealthSection) -> None:
    if score >= policy.exclude_threshold:
        runtime.c_exclude += 1
    else:
        runtime.c_exclude = 0
    if score >= policy.warn_threshold:
        runtime.c_warn += 1
        runtime.c_clear = 0
    elif score < policy.clear_threshold:
        runtime.c_clear += 1
        runtime.c_warn = 0
    # 死区（clear_threshold ~ warn_threshold 之间）：两个计数都保持，实现滞回。


def _next_state(runtime: DoorRuntime, policy: HealthSection) -> str:
    # 卡阻特征是硬证据，不受窗口置信度限制，从任何状态直接升级。
    if runtime.c_jam >= policy.jam_consecutive:
        return DoorState.CRITICAL.value
    if runtime.state in STICKY_STATES:
        return runtime.state
    if confidence_of(runtime, policy) == "low":
        # 缺数据只影响"健康/未知"之间的判定，绝不升级也绝不清除既有告警。
        if runtime.state in (DoorState.HEALTHY.value, DoorState.INSUFFICIENT_DATA.value):
            return DoorState.INSUFFICIENT_DATA.value
        return runtime.state
    if runtime.c_exclude >= policy.exclude_consecutive:
        return DoorState.DEGRADED.value
    if runtime.c_warn >= policy.warn_consecutive:
        return DoorState.SUSPECT.value
    if runtime.c_clear >= policy.clear_consecutive:
        return DoorState.HEALTHY.value
    if runtime.state == DoorState.INSUFFICIENT_DATA.value:
        return DoorState.HEALTHY.value
    return runtime.state


def _reasons(runtime: DoorRuntime, snapshot: dict | None, policy: HealthSection) -> list[str]:
    reasons: list[str] = []
    if snapshot:
        med_duration = snapshot["median_duration_ratio"]
        med_resistance = snapshot["median_resistance_ratio"]
        if med_duration >= 1.05:
            reasons.append(f"动作时长中位数升至基线的 {med_duration:.2f} 倍")
        if med_resistance is not None and med_resistance >= 1.05:
            reasons.append(f"阻力峰值中位数升至基线的 {med_resistance:.2f} 倍")
        slope_duration = snapshot["slope_duration"] or 0.0
        slope_resistance = snapshot["slope_resistance"] or 0.0
        if slope_duration >= policy.slope_crit_per_action * 0.25:
            reasons.append(f"动作时长趋势 {slope_duration * 100:+.2f}%/次")
        if slope_resistance >= policy.slope_crit_per_action * 0.25:
            reasons.append(f"阻力趋势 {slope_resistance * 100:+.2f}%/次")
    life = (runtime.total_cycles - runtime.cycles_at_maintenance) / policy.rated_cycles
    if life >= policy.life_warn_ratio:
        reasons.append(f"寿命消耗 {life * 100:.0f}%（额定 {policy.rated_cycles} 次）")
    if runtime.c_jam:
        reasons.append(f"连续 {runtime.c_jam} 次动作出现卡阻特征")
    if not data_quality_ok(runtime, policy):
        bad = runtime.recent_quality.count(False)
        reasons.append(f"最近 {len(runtime.recent_quality)} 次动作中 {bad} 次采样无效")
    return reasons


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
