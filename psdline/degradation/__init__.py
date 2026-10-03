"""门机劣化评估。"""

from __future__ import annotations

from .journal import DoorActionJournal
from .model import (
    CLOSE,
    OPEN,
    STATUS_CRITICAL,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    DoorAction,
    DegradationVerdict,
    MetricStats,
)
from .registry import DegradationRegistry
from .schedule import MaintenanceWindow, next_window
from .scoring import evaluate, metric_stats, select_window

__all__ = [
    "CLOSE",
    "OPEN",
    "DoorAction",
    "DoorActionJournal",
    "DegradationRegistry",
    "DegradationVerdict",
    "MaintenanceWindow",
    "MetricStats",
    "STATUS_CRITICAL",
    "STATUS_DEGRADED",
    "STATUS_HEALTHY",
    "STATUS_UNKNOWN",
    "STATUS_WATCH",
    "evaluate",
    "metric_stats",
    "next_window",
    "select_window",
]
