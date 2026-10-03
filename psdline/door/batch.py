"""批量开门。

批量开门按门区里程由近及远执行，并受批量上限约束：超限时只开最近的那一批，剩下
的留给下一个周期。被联锁锁定的门区直接跳过并记进 ``skipped``，不会静默漏开。
"""

from __future__ import annotations

from ..station import DoorZone, open_order
from .door import DoorController


def batch_open(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
) -> dict:
    """按里程顺序批量开门。"""

    order = open_order(zones)
    limit = controller.batch_limit()
    scheduled = order[:limit]
    deferred = order[limit:]
    opened: list[str] = []
    skipped: dict[str, str] = {}
    excluded: dict[str, list[str]] = {}
    gates_by_group: dict[str, list[str]] = {}
    for gate in controller.gates.by_station(station_id):
        gates_by_group.setdefault(gate.group, []).append(gate.id)
    excluded_ids = (
        set(controller.degradation.excluded_ids()) if controller.degradation is not None else set()
    )
    for group in scheduled:
        gate_ids = gates_by_group.get(group, [])
        if not gate_ids:
            skipped[group] = "gate_missing"
            continue
        locked = [gate_id for gate_id in gate_ids if controller.lockouts.is_locked(gate_id)]
        if locked:
            skipped[group] = "locked"
            continue
        eligible_gate_ids = gate_ids
        if excluded_ids:
            removed = [gate_id for gate_id in gate_ids if gate_id in excluded_ids]
            eligible_gate_ids = [gate_id for gate_id in gate_ids if gate_id not in excluded_ids]
            if removed:
                excluded[group] = removed
        for gate_id in eligible_gate_ids:
            controller.gates.set_open(gate_id, True)
        opened.append(group)
    return {
        "station_id": station_id,
        "order": order,
        "opened": opened,
        "deferred": deferred,
        "skipped": skipped,
        "excluded": excluded,
        "limit": limit,
    }
