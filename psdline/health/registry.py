"""劣化结论登记处：采集、门控、联锁、审计共用的一份结论。

写路径只有一条：``record`` / ``record_maintenance``。读路径有两条：

* ``view()`` 返回版本一致的全量快照，供批量计划、状态快照与控制台使用 —
  一次计划周期内所有门读到的结论属于同一个版本；
* ``critical()`` / ``excluded()`` 读实时结论，供批量派发每樘门前复查 —
  轮次内新出现的卡阻必须立刻可见。

持久化分两层，都是既有存储原语：

* ``health/<psd_id>`` 文档（JsonStore，临时文件 + 原子替换）：结论与评估
  运行时状态写在同一份文档里，一次原子落盘，要么全旧要么全新，不会出现
  结论新、状态旧的撕裂；
* ``health`` 流水（JsonlJournal，只追加）：动作样本、逐次结论与检修记录
  的完整历史，永不改写 —— 审计回放与崩溃恢复的唯一事实来源。

崩溃恢复：掉电窗口可能让流水领先文档（先写流水后写文档），启动时把每樘门
流水里序号更新的记录重放一遍即可收敛；评估器是纯函数，重放结论与在线评估
一致。重复采集（同序号重发）按序号幂等丢弃。
"""

from __future__ import annotations

import threading
from dataclasses import replace

from ..clock import Clock
from ..config import HealthSection
from ..eventbus import Event, EventBus
from ..store import JsonlJournal, JsonStore
from .assessor import DoorRuntime, assess
from .model import EXCLUDED_STATES, ActionSample, Conclusion, DoorState, eligible_for


class HealthView:
    """某一版本的全量结论快照（只读，随版本单调推进）。"""

    def __init__(self, version: int, conclusions: dict[str, Conclusion]) -> None:
        self._version = version
        self._conclusions = conclusions

    @property
    def version(self) -> int:
        return self._version

    def get(self, psd_id: str) -> Conclusion | None:
        return self._conclusions.get(psd_id)

    def state_of(self, psd_id: str) -> str:
        conclusion = self._conclusions.get(psd_id)
        return conclusion.state if conclusion else ""

    def excluded(self, psd_id: str) -> bool:
        conclusion = self._conclusions.get(psd_id)
        return conclusion is not None and conclusion.state in EXCLUDED_STATES

    def eligible(self, psd_id: str) -> bool:
        """没有结论的门默认放行（缺数据不摘除，见 model.eligible_for）。"""

        conclusion = self._conclusions.get(psd_id)
        return conclusion.eligible if conclusion else True

    def as_dict(self) -> dict:
        return {
            "version": self._version,
            "conclusions": {
                psd_id: conclusion.as_dict()
                for psd_id, conclusion in sorted(self._conclusions.items())
            },
        }


class HealthRegistry:
    """结论登记处：唯一写入口 + 两种读路径 + 崩溃恢复。"""

    def __init__(
        self,
        store: JsonStore,
        journal: JsonlJournal,
        policy: HealthSection,
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        self._store = store
        self._journal = journal
        self._policy = policy
        self._clock = clock
        self._bus = bus
        self._lock = threading.RLock()
        self._runtimes: dict[str, DoorRuntime] = {}
        self._conclusions: dict[str, Conclusion] = {}
        self._version = 0
        self._replayed = 0
        self._recover()

    @staticmethod
    def _doc_id(psd_id: str) -> str:
        return f"health/{psd_id}"

    @property
    def version(self) -> int:
        return self._version

    @property
    def replayed(self) -> int:
        """启动时从流水重放的记录条数（非零说明发生过掉电窗口）。"""

        return self._replayed

    # ---- 写路径 ---------------------------------------------------------

    def next_seq(self, psd_id: str) -> int:
        """采集侧没有自带序号时，由登记处按门分配下一个序号。"""

        with self._lock:
            runtime = self._runtimes.get(psd_id)
            return runtime.last_seq + 1 if runtime else 1

    def record(self, sample: ActionSample) -> Conclusion:
        """消费一条动作样本，返回最新结论；同序号重发幂等返回当前结论。"""

        with self._lock:
            runtime = self._runtimes.get(sample.psd_id)
            if runtime is not None and sample.seq <= runtime.last_seq:
                existing = self._conclusions.get(sample.psd_id)
                if existing is not None:
                    return existing
            if runtime is None:
                runtime = DoorRuntime(psd_id=sample.psd_id)
                self._runtimes[sample.psd_id] = runtime
            self._journal.append({"type": "action", "sample": sample.as_dict()})
            conclusion = self._apply(sample)
            self._journal.append({"type": "assessment", "conclusion": conclusion.as_dict()})
            self._persist(sample.psd_id)
            self._publish(conclusion)
            return conclusion

    def record_maintenance(self, psd_id: str, note: str, at: float) -> Conclusion:
        """登记一次检修：复位运行时状态，历史保留在流水里。"""

        with self._lock:
            runtime = self._runtimes.get(psd_id)
            if runtime is None:
                runtime = DoorRuntime(psd_id=psd_id)
                self._runtimes[psd_id] = runtime
            previous = runtime.state
            runtime.apply_maintenance(at)
            self._journal.append(
                {
                    "type": "maintenance",
                    "psd_id": psd_id,
                    "note": note,
                    "at": at,
                    "mseq": runtime.maintenances,
                }
            )
            conclusion = self._maintenance_conclusion(runtime, previous, note, at)
            self._journal.append({"type": "assessment", "conclusion": conclusion.as_dict()})
            self._persist(psd_id)
            self._publish(conclusion)
            return conclusion

    # ---- 读路径 ---------------------------------------------------------

    def view(self) -> HealthView:
        """返回当前版本的全量快照；快照内容不随后续写入变化。"""

        with self._lock:
            return HealthView(self._version, dict(self._conclusions))

    def get(self, psd_id: str) -> Conclusion | None:
        with self._lock:
            return self._conclusions.get(psd_id)

    def critical(self, psd_id: str) -> bool:
        """实时复查：这樘门此刻是否具有卡阻特征（派发前调用）。"""

        with self._lock:
            conclusion = self._conclusions.get(psd_id)
            return conclusion is not None and conclusion.state == DoorState.CRITICAL.value

    def excluded(self, psd_id: str) -> bool:
        with self._lock:
            conclusion = self._conclusions.get(psd_id)
            return conclusion is not None and conclusion.state in EXCLUDED_STATES

    def summary(self) -> dict:
        """汇总各状态门数与待检修清单，供状态快照与联锁读取。"""

        with self._lock:
            conclusions = list(self._conclusions.values())
            by_state: dict[str, int] = {}
            for conclusion in conclusions:
                by_state[conclusion.state] = by_state.get(conclusion.state, 0) + 1
            return {
                "version": self._version,
                "doors": len(conclusions),
                "by_state": by_state,
                "excluded": sorted(
                    item.psd_id for item in conclusions if item.state in EXCLUDED_STATES
                ),
                "suspect": sorted(
                    item.psd_id
                    for item in conclusions
                    if item.state == DoorState.SUSPECT.value
                ),
                "data_quality_low": sorted(
                    item.psd_id for item in conclusions if not item.data_quality_ok
                ),
                "maintenance_advised": sorted(
                    item.psd_id for item in conclusions if item.advice
                ),
                "journal_records": self._journal.count(),
            }

    def history(self, psd_id: str) -> list[dict]:
        """从流水回放一樘门的全部历史（动作 + 结论 + 检修），供审计核对。

        全量扫描流水，代价随历史长度线性增长 —— 审计路径专用，不在控制
        循环里调用。
        """

        records: list[dict] = []
        for record in self._journal.read_all():
            if record.get("psd_id") == psd_id:
                records.append(record)
                continue
            sample = record.get("sample")
            if isinstance(sample, dict) and sample.get("psd_id") == psd_id:
                records.append(record)
                continue
            conclusion = record.get("conclusion")
            if isinstance(conclusion, dict) and conclusion.get("psd_id") == psd_id:
                records.append(record)
        return records

    # ---- 内部 -----------------------------------------------------------

    def _apply(self, sample: ActionSample) -> Conclusion:
        runtime = self._runtimes[sample.psd_id]
        draft = assess(runtime, sample, self._policy, self._clock.now())
        self._version += 1
        conclusion = replace(draft, version=self._version)
        self._conclusions[sample.psd_id] = conclusion
        return conclusion

    def _maintenance_conclusion(
        self, runtime: DoorRuntime, previous: str, note: str, at: float
    ) -> Conclusion:
        self._version += 1
        reasons = ["检修复位：基线与统计窗口已清空，重新建立基线"]
        if note:
            reasons.append(f"检修备注：{note}")
        conclusion = Conclusion(
            psd_id=runtime.psd_id,
            version=self._version,
            at=at,
            state=runtime.state,
            previous_state=previous,
            score=None,
            confidence="low",
            eligible=eligible_for(runtime.state, self._policy.exclude_on_insufficient_data),
            reasons=tuple(reasons),
        )
        self._conclusions[runtime.psd_id] = conclusion
        return conclusion

    def _persist(self, psd_id: str) -> None:
        self._store.write(
            self._doc_id(psd_id),
            {
                "conclusion": self._conclusions[psd_id].as_dict(),
                "runtime": self._runtimes[psd_id].as_dict(),
            },
        )

    def _publish(self, conclusion: Conclusion) -> None:
        if self._bus is None:
            return
        if conclusion.state != conclusion.previous_state:
            self._bus.publish(
                Event.of(
                    "health",
                    "health",
                    "transition",
                    psd_id=conclusion.psd_id,
                    previous=conclusion.previous_state,
                    state=conclusion.state,
                    version=conclusion.version,
                    score=conclusion.score,
                )
            )
        if not conclusion.data_quality_ok:
            self._bus.publish(
                Event.of(
                    "health",
                    "health",
                    "data_quality",
                    psd_id=conclusion.psd_id,
                    version=conclusion.version,
                )
            )

    def _recover(self) -> None:
        """装文档 → 重放流水尾部，让掉电窗口内的记录收敛进运行时状态。"""

        for doc_id in self._store.list_ids():
            if not doc_id.startswith("health/") or doc_id.count("/") != 1:
                continue
            raw = self._store.read(doc_id)
            if not isinstance(raw, dict):
                continue
            conclusion = Conclusion.from_dict(raw.get("conclusion") or {})
            runtime = DoorRuntime.from_dict(raw.get("runtime") or {})
            if not conclusion.psd_id:
                continue
            self._conclusions[conclusion.psd_id] = conclusion
            self._runtimes[runtime.psd_id] = runtime
            self._version = max(self._version, conclusion.version)
        for record in self._journal.read_all():
            record_type = record.get("type")
            if record_type == "action":
                sample = ActionSample.from_dict(record.get("sample") or {})
                runtime = self._runtimes.get(sample.psd_id)
                if runtime is not None and sample.seq > runtime.last_seq:
                    self._apply(sample)
                    self._persist(sample.psd_id)
                    self._replayed += 1
            elif record_type == "maintenance":
                psd_id = str(record.get("psd_id", ""))
                runtime = self._runtimes.get(psd_id)
                if runtime is not None and int(record.get("mseq", 0)) > runtime.maintenances:
                    previous = runtime.state
                    runtime.apply_maintenance(float(record.get("at", 0.0)))
                    self._maintenance_conclusion(
                        runtime, previous, str(record.get("note", "")), float(record.get("at", 0.0))
                    )
                    self._persist(psd_id)
                    self._replayed += 1
