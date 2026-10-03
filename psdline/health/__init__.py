"""门机劣化评估。

门机原来只有开/关两个状态，检修全凭经验。本包按每樘门的动作次数、动作时长
与阻力峰值评估劣化程度，产出带版本号的结论（``Conclusion``），并在结论越限
时把门从批量开门中摘除，同时给出建议检修时段。

两个关键取舍（结论因此与另一种取法不同，但都写死在这里，全系统一致）：

1. 指标按**最近一段滑动窗口**计算（默认最近 60 次合格动作），不按投运起累计
   平均 —— 劣化是"最近"的现象，累计平均会把最新的恶化稀释掉；只有寿命消耗
   （动作次数对额定次数）按累计计算，因为疲劳是累积的。
2. 摘除分两级：**DEGRADED**（评分越限）在下一轮批量开门才摘除，本轮作业不受
   影响，避免半批次状态不一致；**CRITICAL**（连续出现卡阻特征）立即摘除 —
— 批量派发每樘门前实时复查，本轮内新出现的卡阻也不会再被驱动，但在途动作
   允许完成（中途急停是硬件安全层的职责，本层不下发）。

采集、门控、联锁、审计读同一份结论：``HealthRegistry`` 是唯一写入口，其余
组件要么拿版本一致的快照（``view()``），要么读实时结论（``critical()``），
不存在各算各的。采样缺失的样本被隔离在统计之外，只影响置信度，不会把评估
带偏；缺数据永远不会把一樘已摘除的门"治"回健康。
"""

from .assessor import DoorRuntime, assess
from .model import ActionSample, Conclusion, DoorState
from .registry import HealthRegistry, HealthView

__all__ = [
    "ActionSample",
    "Conclusion",
    "DoorRuntime",
    "DoorState",
    "HealthRegistry",
    "HealthView",
    "assess",
]
