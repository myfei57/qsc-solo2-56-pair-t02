"""门机劣化事件与结论的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError

OPEN = "open"
CLOSE = "close"
VALID_DIRECTIONS = (OPEN, CLOSE)

STATUS_HEALTHY = "healthy"
STATUS_WATCH = "watch"
STATUS_DEGRADED = "degraded"
STATUS_CRITICAL = "critical"
STATUS_UNKNOWN = "unknown"
EXCLUDING_STATUSES = (STATUS_DEGRADED, STATUS_CRITICAL)


@dataclass(frozen=True)
class DoorAction:
    """一次已完成门动作的不可变事实。

    ``duration_s`` 与 ``resistance_n`` 允许为空，表示该路采样缺失；评估时按缺失
    处理，不用零值或插值补齐。
    """

    event_id: str
    psd_id: str
    at: float
    direction: str
    duration_s: float | None = None
    resistance_n: float | None = None
    sequence: int = 0

    def validate(self) -> "DoorAction":
        if not self.event_id:
            raise RailError("门动作事件标识为空", component="degradation")
        if not self.psd_id:
            raise RailError("门机标识为空", component="degradation")
        if self.direction not in VALID_DIRECTIONS:
            raise RailError(f"门动作方向非法: {self.direction}", component="degradation")
        if self.duration_s is not None and self.duration_s <= 0:
            raise RailError("动作时长必须为正或显式缺失", component="degradation")
        if self.resistance_n is not None and self.resistance_n < 0:
            raise RailError("阻力不能为负", component="degradation")
        return self

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "psd_id": self.psd_id,
            "at": self.at,
            "direction": self.direction,
            "duration_s": self.duration_s,
            "resistance_n": self.resistance_n,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "DoorAction":
        return cls(
            event_id=str(payload.get("event_id", "")),
            psd_id=str(payload.get("psd_id", "")),
            at=float(payload.get("at", 0.0)),
            direction=str(payload.get("direction", "")),
            duration_s=_optional_float(payload.get("duration_s")),
            resistance_n=_optional_float(payload.get("resistance_n")),
            sequence=int(payload.get("sequence", 0)),
        )


@dataclass(frozen=True)
class MetricStats:
    """一个指标在最近窗口中的统计。"""

    count: int
    expected: int
    missing: int
    mean: float | None
    p95: float | None
    max: float | None

    @property
    def coverage(self) -> float:
        if self.expected <= 0:
            return 0.0
        return self.count / self.expected

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "expected": self.expected,
            "missing": self.missing,
            "coverage": round(self.coverage, 4),
            "mean": _round(self.mean),
            "p95": _round(self.p95),
            "max": _round(self.max),
        }


@dataclass(frozen=True)
class DegradationVerdict:
    """门控、联锁、审计与控制台共同读取的一份当前结论。"""

    psd_id: str
    status: str
    score: int
    excluded_from_batch: bool
    excluded_at: float | None
    pending_exclusion: bool
    immediate_severity: bool
    reasons: tuple[str, ...]
    metrics: dict
    lifetime_actions: int
    window_actions: int
    data_complete: bool
    data_warnings: tuple[str, ...]
    suggested_window: str
    suggested_by: float
    evaluated_at: float
    revision: int
    based_on_event_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "status": self.status,
            "score": self.score,
            "excluded_from_batch": self.excluded_from_batch,
            "excluded_at": self.excluded_at,
            "pending_exclusion": self.pending_exclusion,
            "immediate_severity": self.immediate_severity,
            "reasons": list(self.reasons),
            "metrics": self.metrics,
            "lifetime_actions": self.lifetime_actions,
            "window_actions": self.window_actions,
            "data_complete": self.data_complete,
            "data_warnings": list(self.data_warnings),
            "suggested_window": self.suggested_window,
            "suggested_by": self.suggested_by,
            "evaluated_at": self.evaluated_at,
            "revision": self.revision,
            "based_on_event_ids": list(self.based_on_event_ids),
        }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)
