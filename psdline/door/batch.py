"""批量开门。

批量开门按门区里程由近及远执行，并受批量上限约束：超限时只开最近的那一批，剩下
的留给下一个周期。被联锁锁定的门区直接跳过并记进 ``skipped``，不会静默漏开。

劣化摘除分两级（结论来自门控装配的 ``health`` 登记处，与联锁、审计同一份）：

* DEGRADED：轮次开始时取一份版本一致的快照（``health_view``），快照里被摘除
  的门本轮不再带上；轮次内才恶化的门不受影响，下一轮生效 —— 保证一轮作业
  内部的判定基准一致；
* CRITICAL：派发每樘门前实时复查，轮次内新出现的卡阻特征立即拦下；正在动作
  中的门允许完成当前行程（中途急停属于硬件安全层职责，本层不再下发新指令）。

被健康摘除的门记进 ``health_skipped`` 并带上摘除时的状态，审计能据此还原
"这轮为什么没带它"。
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
    health = controller.health
    view = health.view() if health is not None else None
    opened: list[str] = []
    skipped: dict[str, str] = {}
    health_skipped: dict[str, str] = {}
    for group in scheduled:
        gate_ids = [gate.id for gate in controller.gates.by_station(station_id) if gate.group == group]
        if not gate_ids:
            skipped[group] = "gate_missing"
            continue
        if any(controller.lockouts.is_locked(gate_id) for gate_id in gate_ids):
            skipped[group] = "locked"
            continue
        group_opened: list[str] = []
        for gate_id in gate_ids:
            if view is not None and view.excluded(gate_id):
                health_skipped[gate_id] = view.state_of(gate_id)
                continue
            if health is not None and health.critical(gate_id):
                health_skipped[gate_id] = "critical"
                continue
            controller.gates.set_open(gate_id, True)
            group_opened.append(gate_id)
        if group_opened:
            opened.append(group)
        elif gate_ids:
            skipped[group] = "health_excluded"
    return {
        "station_id": station_id,
        "order": order,
        "opened": opened,
        "deferred": deferred,
        "skipped": skipped,
        "health_skipped": health_skipped,
        "health_view": view.version if view is not None else 0,
        "limit": limit,
    }
