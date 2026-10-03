"""联锁状态快照。"""

from __future__ import annotations

from ..psd import PSDRegistry
from .interlock import FaultRegistry


def interlock_status(
    faults: FaultRegistry,
    gates: PSDRegistry,
    max_locked_ratio: float = 0.5,
    degraded_excluded: list[str] | None = None,
) -> dict:
    """汇总故障、锁定、劣化摘除与放行余量。"""

    active = faults.active_ids()
    locked = [gate.id for gate in gates.list() if gate.locked]
    excluded = sorted(degraded_excluded or [])
    total = len(gates.list())
    ratio = len(locked) / total if total else 0.0
    return {
        "faults_active": active,
        "fault_count": len(active),
        "locked": locked,
        "locked_count": len(locked),
        "degraded_excluded": excluded,
        "degraded_excluded_count": len(excluded),
        "locked_ratio": round(ratio, 4),
        "max_locked_ratio": max_locked_ratio,
        "can_release_depart": ratio <= max_locked_ratio,
    }
