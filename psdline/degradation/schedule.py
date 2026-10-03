"""建议检修时段。

评估只给“最晚在何时前安排”，具体排程仍由运营选择。默认每天有一个可作业窗口；
严重劣化建议最近窗口，一般劣化建议在 ``maintenance_lead_s`` 前的最近窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
import time

from ..config import DegradationSection


@dataclass(frozen=True)
class MaintenanceWindow:
    """建议的检修窗口。"""

    label: str
    starts_at: float
    deadline: float

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "starts_at": self.starts_at,
            "starts_at_utc": _utc(self.starts_at),
            "deadline": self.deadline,
            "deadline_utc": _utc(self.deadline),
        }


def parse_daily_at(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    return int(hour_text), int(minute_text)


def next_window(now: float, section: DegradationSection, urgent: bool = False) -> MaintenanceWindow:
    """返回下一个每日检修窗口；``urgent`` 时忽略提前量。"""

    hour, minute = parse_daily_at(section.maintenance_daily_at)
    day_seconds = hour * 3600 + minute * 60
    current_day_start = int(now // 86400) * 86400
    starts = current_day_start + day_seconds
    if starts <= now:
        starts += 86400
    lead = 0.0 if urgent else section.maintenance_lead_s
    deadline = now + lead
    while starts > deadline:
        starts -= 86400
    if starts <= now:
        starts += 86400
    return MaintenanceWindow(
        label=f"每日 {section.maintenance_daily_at} 检修窗",
        starts_at=float(starts),
        deadline=float(deadline),
    )


def _utc(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
