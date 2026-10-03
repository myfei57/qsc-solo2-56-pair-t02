"""门控控制器装配。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..psd import LockoutRegistry, PSDRegistry
from ..store import CommandLog, JsonStore
from ..train import AlignRegistry

if TYPE_CHECKING:  # 避免门控与劣化评估互相导入形成环
    from ..health import HealthRegistry


class DoorController:
    """门控动作的装配入口。"""

    def __init__(
        self,
        store: JsonStore,
        align: AlignRegistry,
        commands: CommandLog,
        gates: PSDRegistry,
        lockouts: LockoutRegistry,
    ) -> None:
        self._store = store
        self._align = align
        self._commands = commands
        self._gates = gates
        self._lockouts = lockouts
        self._health: HealthRegistry | None = None
        self._batch_limit = 64
        self._opens = 0
        self._closes = 0

    @property
    def store(self) -> JsonStore:
        return self._store

    @property
    def align(self) -> AlignRegistry:
        return self._align

    @property
    def commands(self) -> CommandLog:
        return self._commands

    @property
    def gates(self) -> PSDRegistry:
        return self._gates

    @property
    def lockouts(self) -> LockoutRegistry:
        return self._lockouts

    @property
    def health(self) -> "HealthRegistry | None":
        """劣化结论登记处；未接线时批量开门不做健康过滤。"""

        return self._health

    def attach_health(self, health: "HealthRegistry") -> None:
        """接上劣化结论登记处，批量开门随即开始按结论摘除。"""

        self._health = health

    @property
    def opens(self) -> int:
        """返回累计开门次数。"""

        return self._opens

    @property
    def closes(self) -> int:
        """返回累计关门次数。"""

        return self._closes

    def set_batch_limit(self, limit: int) -> None:
        """调整一次批量开门的门区上限。"""

        if limit <= 0:
            raise ValueError("批量上限必须为正")
        self._batch_limit = limit

    def note_open(self) -> int:
        """在一次成功开门后累加计数。"""

        self._opens += 1
        return self._opens

    def note_close(self) -> int:
        """在一次成功关门后累加计数。"""

        self._closes += 1
        return self._closes

    def batch_limit(self) -> int:
        """返回当前批量上限。"""

        return self._batch_limit
