"""劣化评估的数据模型。

结论（``Conclusion``）是采集、门控、联锁、审计四方共用的唯一事实：每次评估
产生一条不可变结论，带全局单调版本号；任何组件读到的都是某个版本的结论。
结论里带上得出它的窗口统计与评分分量，审计追问"为什么这樘门被摘除"时可以
直接回看，不需要重新计算。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DoorState(enum.Enum):
    """门机健康状态。"""

    HEALTHY = "healthy"
    SUSPECT = "suspect"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    INSUFFICIENT_DATA = "insufficient_data"


#: 会被批量开门摘除的状态。
EXCLUDED_STATES = frozenset({DoorState.DEGRADED.value, DoorState.CRITICAL.value})

#: 只能被检修复位、不会随样本自行退出的状态。
STICKY_STATES = frozenset({DoorState.DEGRADED.value, DoorState.CRITICAL.value})


@dataclass(frozen=True)
class ActionSample:
    """一次门机动作的采集样本。

    ``seq`` 是采集侧按门单调递增的序号，重发、乱序与崩溃重放都靠它幂等。
    阻力采样允许缺失：``samples_expected`` 为 0 表示这樘门没有阻力传感器，
    只按动作时长评估；否则按完整度（received/expected）判定样本是否合格。
    """

    psd_id: str
    seq: int
    at: float
    kind: str = "open"
    duration_s: float | None = None
    resistance_peak: float | None = None
    samples_expected: int = 0
    samples_received: int = 0

    @property
    def has_resistance_sensor(self) -> bool:
        return self.samples_expected > 0

    @property
    def completeness(self) -> float | None:
        """阻力采样完整度；没有阻力传感器时返回 ``None``。"""

        if self.samples_expected <= 0:
            return None
        return min(1.0, max(0, self.samples_received) / self.samples_expected)

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "duration_s": self.duration_s,
            "resistance_peak": self.resistance_peak,
            "samples_expected": self.samples_expected,
            "samples_received": self.samples_received,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ActionSample":
        return cls(
            psd_id=str(payload.get("psd_id", "")),
            seq=int(payload.get("seq", 0)),
            at=float(payload.get("at", 0.0)),
            kind=str(payload.get("kind", "open")),
            duration_s=_optional_float(payload.get("duration_s")),
            resistance_peak=_optional_float(payload.get("resistance_peak")),
            samples_expected=int(payload.get("samples_expected", 0)),
            samples_received=int(payload.get("samples_received", 0)),
        )


@dataclass(frozen=True)
class Conclusion:
    """一次评估的结论（不可变，落盘后不回写）。"""

    psd_id: str
    version: int
    at: float
    state: str
    previous_state: str
    score: float | None
    confidence: str
    eligible: bool
    components: dict = field(default_factory=dict)
    window: dict | None = None
    reasons: tuple = ()
    advice: dict | None = None
    data_quality_ok: bool = True

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "version": self.version,
            "at": self.at,
            "state": self.state,
            "previous_state": self.previous_state,
            "score": self.score,
            "confidence": self.confidence,
            "eligible": self.eligible,
            "components": dict(self.components),
            "window": dict(self.window) if self.window else None,
            "reasons": list(self.reasons),
            "advice": dict(self.advice) if self.advice else None,
            "data_quality_ok": self.data_quality_ok,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Conclusion":
        return cls(
            psd_id=str(payload.get("psd_id", "")),
            version=int(payload.get("version", 0)),
            at=float(payload.get("at", 0.0)),
            state=str(payload.get("state", DoorState.INSUFFICIENT_DATA.value)),
            previous_state=str(payload.get("previous_state", "")),
            score=_optional_float(payload.get("score")),
            confidence=str(payload.get("confidence", "low")),
            eligible=bool(payload.get("eligible", True)),
            components=dict(payload.get("components") or {}),
            window=dict(payload["window"]) if isinstance(payload.get("window"), dict) else None,
            reasons=tuple(str(item) for item in payload.get("reasons", ())),
            advice=dict(payload["advice"]) if isinstance(payload.get("advice"), dict) else None,
            data_quality_ok=bool(payload.get("data_quality_ok", True)),
        )


def eligible_for(state: str, exclude_on_insufficient_data: bool = False) -> bool:
    """某状态下的门是否允许被批量开门带上。

    缺数据默认不摘除（采集故障不该让运营停摆，只报警）；站点可以通过配置
    ``exclude_on_insufficient_data`` 改成缺数据也摘除。
    """

    if state in EXCLUDED_STATES:
        return False
    if state == DoorState.INSUFFICIENT_DATA.value and exclude_on_insufficient_data:
        return False
    return True


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
