"""门机劣化登记与同一份结论的持久化。

登记服务是门控、联锁、控制台和审计之间的唯一裁决点：

* 原始动作进入只追加分段流水；
* 当前结论用原子 JSON 文档保存，崩溃后可从“流水末尾 + 结论快照”恢复；
* 采样缺失只产生数据告警，不用插值或零值改变劣化结论；
* ``degraded`` 若发生在作业中，等本轮作业结束后摘除；``critical`` 立即从后续
  批量开门中摘除，但不强行关闭已经打开的门。
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from ..audit import AuditLog
from ..config import DegradationSection
from ..errors import RailError, StorageError
from ..store import JsonStore
from .journal import DoorActionJournal
from .model import (
    STATUS_CRITICAL,
    STATUS_DEGRADED,
    STATUS_UNKNOWN,
    DoorAction,
    DegradationVerdict,
)
from .scoring import evaluate

OPERATIONS_DOC = "degradation/operations"
SNAPSHOT_PREFIX = "degradation/state/"


@dataclass
class _State:
    psd_id: str
    lifetime_actions: int = 0
    installed_at: float | None = None
    recent: deque[DoorAction] = field(default_factory=deque)
    seen_event_ids: deque[str] = field(default_factory=deque)
    verdict: DegradationVerdict | None = None
    revision: int = 0
    last_published: DegradationVerdict | None = None


class DegradationRegistry:
    """提供门动作登记、结论查询、摘除延迟和检修复位。"""

    def __init__(
        self,
        store: JsonStore,
        section: DegradationSection,
        clock,
        audit: AuditLog | None = None,
    ) -> None:
        self._store = store
        self._section = section
        self._clock = clock
        self._audit = audit
        self._journal = DoorActionJournal(store)
        self._lock = threading.RLock()
        self._states: dict[str, _State] = {}
        self._active_operations: dict[str, dict] = {}
        self._restored = False
        self._audit_ids: set[str] | None = None

    @property
    def section(self) -> DegradationSection:
        return self._section

    @property
    def journal(self) -> DoorActionJournal:
        """暴露原始动作流水只读/恢复能力，不提供删除或改写入口。"""

        return self._journal

    def ensure_loaded(self) -> None:
        """幂等恢复全部门机状态。"""

        with self._lock:
            if self._restored:
                return
            self._active_operations = {}
            for psd_id in self._journal.list_psd_ids():
                self._restore_psd(psd_id)
            for doc_id in self._store.list_ids():
                if doc_id.startswith(SNAPSHOT_PREFIX):
                    psd_id = doc_id[len(SNAPSHOT_PREFIX) :]
                    self._states.setdefault(psd_id, _State(psd_id))
                    self._restore_snapshot(psd_id)
            # 进程重启意味着旧作业的控制授权已经中断；不能让待摘除门机跨重启进入
            # 下一轮批量开门。恢复时先应用待处理摘除，再由审计说明原因。
            raw_operations = self._store.read(OPERATIONS_DOC)
            if isinstance(raw_operations, dict):
                self._active_operations = dict(raw_operations)
            else:
                self._active_operations = {}
            self._replay_history_audits()
            self._apply_pending(self._clock.now(), reason="服务重启后作业边界恢复")
            self._active_operations.clear()
            self._write_operations()
            self._restored = True

    def register_gate(self, psd_id: str, installed_at: float | None = None) -> None:
        """登记一樘门的评估槽位；已有投运时间不会被覆盖。"""

        self.ensure_loaded()
        changed = False
        with self._lock:
            state = self._state(psd_id)
            if installed_at is not None and state.installed_at is None:
                state.installed_at = installed_at
                changed = True
            if changed:
                self._write_snapshot(state)

    def record_action(self, action: DoorAction) -> DegradationVerdict:
        """记录一次已完成动作并返回该门的最新结论。"""

        action.validate()
        self.ensure_loaded()
        with self._lock:
            return self._record_action_locked(action)

    def _record_action_locked(self, action: DoorAction) -> DegradationVerdict:
        state = self._state(action.psd_id)
        if action.event_id in set(state.seen_event_ids):
            if state.verdict is None:
                raise RailError(f"重复门动作事件尚无结论: {action.event_id}", component="degradation")
            return state.verdict
        self._journal.append(action.psd_id, action.as_dict())
        state.lifetime_actions += 1
        state.recent.append(action)
        state.seen_event_ids.append(action.event_id)
        self._trim(state)
        verdict = self._evaluate(state)
        self._write_snapshot(state)
        self._publish_verdict(state, verdict)
        return verdict

    def record_actions(self, actions: Iterable[DoorAction]) -> dict:
        """批量采集入口；逐条进入同一流水，返回汇总。"""

        items = list(actions)
        if not items:
            return {"accepted": 0, "duplicates": 0, "psd_ids": []}
        verdicts: list[DegradationVerdict] = []
        duplicates = 0
        with self._lock:
            for action in items:
                state = self._state(action.psd_id)
                before = len(state.seen_event_ids)
                verdict = self._record_action_locked(action)
                after = len(state.seen_event_ids)
                if after == before:
                    duplicates += 1
                verdicts.append(verdict)
        return {
            "accepted": len(items) - duplicates,
            "duplicates": duplicates,
            "psd_ids": sorted({verdict.psd_id for verdict in verdicts}),
        }

    def verdict(self, psd_id: str) -> DegradationVerdict | None:
        self.ensure_loaded()
        with self._lock:
            state = self._states.get(psd_id)
            return state.verdict if state else None

    def list_verdicts(self) -> list[DegradationVerdict]:
        self.ensure_loaded()
        with self._lock:
            return [
                state.verdict
                for state in self._states.values()
                if state.verdict is not None
            ]

    def excluded_ids(self) -> list[str]:
        """返回当前不得被后续批量开门带上的门机。"""

        return sorted(
            verdict.psd_id
            for verdict in self.list_verdicts()
            if verdict.excluded_from_batch and not verdict.pending_exclusion
        )

    def eligible_gate_ids(self, gate_ids: Iterable[str]) -> list[str]:
        """从候选门机中过滤已摘除门机，保持输入顺序。"""

        excluded = set(self.excluded_ids())
        return [psd_id for psd_id in gate_ids if psd_id not in excluded]

    def begin_operation(self, operation_id: str, psd_ids: Iterable[str], at: float | None = None) -> dict:
        """登记一轮作业开始，固定本轮作业的门机集合。"""

        self.ensure_loaded()
        moment = self._clock.now() if at is None else at
        ids = sorted(set(psd_ids))
        with self._lock:
            existing = self._active_operations.get(operation_id)
            if existing is not None:
                return {
                    "operation_id": operation_id,
                    "psd_ids": list(existing["psd_ids"]),
                    "at": existing["at"],
                }
            self._active_operations[operation_id] = {"psd_ids": ids, "at": moment}
            self._write_operations()
        return {"operation_id": operation_id, "psd_ids": ids, "at": moment}

    def end_operation(self, operation_id: str, at: float | None = None) -> dict:
        """作业结束后应用本轮内挂起的摘除。"""

        self.ensure_loaded()
        moment = self._clock.now() if at is None else at
        with self._lock:
            operation = self._active_operations.pop(operation_id, None)
            self._write_operations()
            if operation is None:
                return {"operation_id": operation_id, "applied": [], "found": False}
            targets = set(operation["psd_ids"])
            applied = self._apply_pending(moment, targets=targets, reason=f"作业 {operation_id} 结束")
            return {"operation_id": operation_id, "found": True, "applied": applied}

    def sweep_stale_operations(self, now: float | None = None) -> dict:
        """结束超时仍未显式收尾的作业，避免摘除永久挂起。"""

        self.ensure_loaded()
        moment = self._clock.now() if now is None else now
        with self._lock:
            stale = [
                operation_id
                for operation_id, operation in self._active_operations.items()
                if moment - float(operation["at"]) >= self._section.operation_timeout_s
            ]
        results = {operation_id: self.end_operation(operation_id, moment) for operation_id in stale}
        return {"ended": stale, "results": results}

    def complete_maintenance(self, psd_id: str, at: float | None = None) -> DegradationVerdict:
        """检修完成后解除摘除并重新标定最近窗口。

        累计动作次数和原始流水不重置；只清掉最近窗口的在线状态，让检修后的新动作
        重新建立证据，避免旧毛刺立即把刚修好的门再次摘除。
        """

        self.ensure_loaded()
        moment = self._clock.now() if at is None else at
        with self._lock:
            state = self.require_state(psd_id)
            state.recent.clear()
            state.revision += 1
            state.verdict = evaluate(
                psd_id,
                [],
                state.lifetime_actions,
                moment,
                self._section,
                revision=state.revision,
                previous=None,
                installed_at=state.installed_at,
            )
            # 检修复位本身解除隔离；未知状态等待新样本，但不继续阻断批量。
            state.verdict = self._clone_verdict(
                state.verdict,
                excluded_from_batch=False,
                excluded_at=None,
                pending_exclusion=False,
                reasons=("已完成检修，等待重新建立评估样本",),
            )
            self._write_snapshot(state)
            self._audit_event(
                "degradation_maintenance_reset",
                psd_id,
                moment,
                revision=state.revision,
            )
            state.last_published = state.verdict
            return state.verdict

    def summary(self) -> dict:
        verdicts = self.list_verdicts()
        counts: dict[str, int] = {}
        for verdict in verdicts:
            counts[verdict.status] = counts.get(verdict.status, 0) + 1
        return {
            "total": len(self._states),
            "evaluated": len(verdicts),
            "status_counts": counts,
            "excluded": self.excluded_ids(),
            "active_operations": len(self._active_operations),
            "policy": {
                "window_actions": self._section.window_actions,
                "window_max_age_s": self._section.window_max_age_s,
                "min_samples": self._section.min_samples,
                "required_coverage": self._section.required_coverage,
                "degraded_removal": "after_current_operation",
                "critical_removal": "immediate_for_future_batch",
            },
        }

    def require_state(self, psd_id: str) -> _State:
        with self._lock:
            state = self._states.get(psd_id)
            if state is None:
                raise RailError(f"门机未登记劣化评估: {psd_id}", component="degradation")
            return state

    def _state(self, psd_id: str) -> _State:
        state = self._states.get(psd_id)
        if state is None:
            state = _State(psd_id, installed_at=self._clock.now())
            self._states[psd_id] = state
        return state

    def _trim(self, state: _State) -> None:
        limit = self._section.window_actions
        while len(state.recent) > limit:
            state.recent.popleft()
        dedupe_limit = max(limit * 3, 4096)
        while len(state.seen_event_ids) > dedupe_limit:
            state.seen_event_ids.popleft()

    def _evaluate(self, state: _State) -> DegradationVerdict:
        state.revision += 1
        actions = list(state.recent)
        verdict = evaluate(
            state.psd_id,
            actions,
            state.lifetime_actions,
            actions[-1].at if actions else self._clock.now(),
            self._section,
            revision=state.revision,
            previous=state.verdict,
            installed_at=state.installed_at,
        )
        pending = False
        excluded = bool(state.verdict and state.verdict.excluded_from_batch)
        excluded_at = state.verdict.excluded_at if excluded else None
        if not excluded and verdict.status in (STATUS_DEGRADED, STATUS_CRITICAL):
            active = self._is_active(state.psd_id)
            if verdict.immediate_severity or not active:
                excluded = True
                excluded_at = verdict.evaluated_at
            else:
                pending = True
        if state.verdict and state.verdict.pending_exclusion and verdict.status == STATUS_DEGRADED:
            pending = self._is_active(state.psd_id)
            if not pending:
                excluded = True
                excluded_at = verdict.evaluated_at
        verdict = self._clone_verdict(
            verdict,
            excluded_from_batch=excluded,
            excluded_at=excluded_at,
            pending_exclusion=pending,
        )
        state.verdict = verdict
        return verdict

    def _is_active(self, psd_id: str) -> bool:
        return any(psd_id in operation["psd_ids"] for operation in self._active_operations.values())

    def _apply_pending(
        self,
        at: float,
        targets: set[str] | None = None,
        reason: str = "",
    ) -> list[str]:
        applied: list[str] = []
        for state in list(self._states.values()):
            verdict = state.verdict
            if verdict is None or not verdict.pending_exclusion:
                continue
            if targets is not None and state.psd_id not in targets:
                continue
            state.verdict = self._clone_verdict(
                verdict,
                excluded_from_batch=True,
                excluded_at=at,
                pending_exclusion=False,
            )
            self._write_snapshot(state)
            self._audit_event(
                "degradation_excluded",
                state.psd_id,
                at,
                reason=reason,
                status=state.verdict.status,
                score=state.verdict.score,
            )
            state.last_published = state.verdict
            applied.append(state.psd_id)
        return sorted(applied)

    def _publish_verdict(self, state: _State, verdict: DegradationVerdict) -> None:
        previous = state.last_published
        if previous is None:
            self._audit_event(
                "degradation_baseline",
                verdict.psd_id,
                verdict.evaluated_at,
                status=verdict.status,
                score=verdict.score,
            )
            state.last_published = verdict
            return
        if (
            previous.status != verdict.status
            or previous.excluded_from_batch != verdict.excluded_from_batch
            or previous.pending_exclusion != verdict.pending_exclusion
        ):
            kind = "degradation_excluded" if verdict.excluded_from_batch and not previous.excluded_from_batch else "degradation_changed"
            if verdict.pending_exclusion and not previous.pending_exclusion:
                kind = "degradation_exclusion_pending"
            self._audit_event(
                kind,
                verdict.psd_id,
                verdict.evaluated_at,
                previous_status=previous.status,
                status=verdict.status,
                score=verdict.score,
                reasons=";".join(verdict.reasons),
            )
        state.last_published = verdict

    def _audit_event(self, kind: str, psd_id: str, at: float, **detail) -> None:
        entry_id = f"degradation:{psd_id}:{kind}:{at}"
        # 本地不可变结论历史先落盘；外部审计短时失败不丢结论，重启后按 event_id 补发。
        path = self._verdict_history_path(psd_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"kind": kind, "psd_id": psd_id, "at": at, "event_id": entry_id, **detail}
        self._append_atomic(path, [record])
        if self._audit is not None:
            if self._audit_ids is None:
                self._audit_ids = {
                    str(item.detail.get("event_id", ""))
                    for item in self._audit.replay()
                    if item.detail.get("event_id")
                }
            if entry_id not in self._audit_ids:
                try:
                    self._audit.record(kind, "", "", at, event_id=entry_id, psd_id=psd_id, **detail)
                    self._audit_ids.add(entry_id)
                except StorageError:
                    self._append_atomic(path, [{"event_id": entry_id, "audit_pending": True}])

    def _replay_history_audits(self) -> None:
        if self._audit is None:
            return
        self._audit_ids = {
            str(entry.detail.get("event_id", ""))
            for entry in self._audit.replay()
            if entry.detail.get("event_id")
        }
        base = os.path.join(self._store.root, "degradation", "history")
        if not os.path.isdir(base):
            return
        for name in sorted(os.listdir(base)):
            if not name.endswith(".jsonl"):
                continue
            for record in _read_jsonl(os.path.join(base, name)):
                event_id = str(record.get("event_id", ""))
                if not event_id or event_id in self._audit_ids:
                    continue
                if record.get("audit_pending") or not record.get("kind"):
                    continue
                detail = {
                    key: value
                    for key, value in record.items()
                    if key not in {"kind", "psd_id", "at", "event_id"}
                }
                self._audit.record(
                    str(record.get("kind", "degradation_changed")),
                    "",
                    "",
                    float(record.get("at", self._clock.now())),
                    event_id=event_id,
                    psd_id=str(record.get("psd_id", "")),
                    **detail,
                )
                self._audit_ids.add(event_id)

    @staticmethod
    def _verdict_history_path_for(root: str, psd_id: str) -> str:
        return os.path.join(root, "degradation", "history", f"{psd_id}.jsonl")

    def _verdict_history_path(self, psd_id: str) -> str:
        return self._verdict_history_path_for(self._store.root, psd_id)

    def _snapshot_doc(self, psd_id: str) -> str:
        return f"{SNAPSHOT_PREFIX}{psd_id}"

    def _write_snapshot(self, state: _State) -> None:
        payload = {
            "psd_id": state.psd_id,
            "installed_at": state.installed_at,
            "lifetime_actions": state.lifetime_actions,
            "recent": [action.as_dict() for action in state.recent],
            "seen_event_ids": list(state.seen_event_ids),
            "revision": state.revision,
            "verdict": state.verdict.as_dict() if state.verdict else None,
            "last_published": state.last_published.as_dict() if state.last_published else None,
        }
        self._store.write(self._snapshot_doc(state.psd_id), payload)

    def _write_operations(self) -> None:
        self._store.write(OPERATIONS_DOC, dict(self._active_operations))

    def _restore_psd(self, psd_id: str) -> None:
        state = self._state(psd_id)
        snapshot = self._store.read(self._snapshot_doc(psd_id))
        if isinstance(snapshot, dict):
            self._load_snapshot(state, snapshot)
        raw_events = self._journal.read_tail(psd_id, max(self._section.window_actions * 2, 4096))
        known = set(state.seen_event_ids)
        appended = 0
        for payload in raw_events:
            action = DoorAction.from_dict(payload).validate()
            if action.event_id in known:
                continue
            state.recent.append(action)
            state.seen_event_ids.append(action.event_id)
            known.add(action.event_id)
            state.lifetime_actions += 1
            appended += 1
        self._trim(state)
        if appended:
            if state.recent:
                state.verdict = None
                state.revision = max(0, state.revision - 1)
                self._evaluate(state)
            self._write_snapshot(state)

    def _restore_snapshot(self, psd_id: str) -> None:
        state = self._state(psd_id)
        if state.verdict is not None:
            return
        snapshot = self._store.read(self._snapshot_doc(psd_id))
        if isinstance(snapshot, dict):
            self._load_snapshot(state, snapshot)

    def _load_snapshot(self, state: _State, snapshot: dict) -> None:
        installed_value = snapshot.get("installed_at")
        state.installed_at = None if installed_value is None else float(installed_value)
        state.lifetime_actions = int(snapshot.get("lifetime_actions", 0))
        state.revision = int(snapshot.get("revision", 0))
        state.recent = deque(
            DoorAction.from_dict(item).validate()
            for item in snapshot.get("recent", [])
            if isinstance(item, dict)
        )
        state.seen_event_ids = deque(str(item) for item in snapshot.get("seen_event_ids", []))
        verdict = snapshot.get("verdict")
        if isinstance(verdict, dict):
            state.verdict = _verdict_from_dict(verdict)
        published = snapshot.get("last_published")
        if isinstance(published, dict):
            state.last_published = _verdict_from_dict(published)

    def _clone_verdict(self, verdict: DegradationVerdict, **changes) -> DegradationVerdict:
        payload = verdict.as_dict()
        payload.update(changes)
        if "reasons" in changes:
            payload["reasons"] = list(changes["reasons"])
        if "data_warnings" in changes:
            payload["data_warnings"] = list(changes["data_warnings"])
        return _verdict_from_dict(payload)

    @staticmethod
    def _append_atomic(path: str, records: list[dict]) -> None:
        # 审计/历史是低频短记录；使用 O_APPEND 单次 write 并 fsync。并发由登记锁
        # 串行化，崩溃若留下残段由人工巡检处理，绝不覆盖已有历史。
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise StorageError(f"追加劣化历史失败: {error}", component="degradation") from error


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError(f"读取劣化历史失败: {path}", component="degradation") from error
    if not all(isinstance(item, dict) for item in records):
        raise StorageError(f"劣化历史记录不是对象: {path}", component="degradation")
    return records


def _verdict_from_dict(payload: dict) -> DegradationVerdict:
    return DegradationVerdict(
        psd_id=str(payload.get("psd_id", "")),
        status=str(payload.get("status", STATUS_UNKNOWN)),
        score=int(payload.get("score", 0)),
        excluded_from_batch=bool(payload.get("excluded_from_batch", False)),
        excluded_at=payload.get("excluded_at"),
        pending_exclusion=bool(payload.get("pending_exclusion", False)),
        immediate_severity=bool(payload.get("immediate_severity", False)),
        reasons=tuple(str(item) for item in payload.get("reasons", [])),
        metrics=dict(payload.get("metrics", {})),
        lifetime_actions=int(payload.get("lifetime_actions", 0)),
        window_actions=int(payload.get("window_actions", 0)),
        data_complete=bool(payload.get("data_complete", False)),
        data_warnings=tuple(str(item) for item in payload.get("data_warnings", [])),
        suggested_window=str(payload.get("suggested_window", "")),
        suggested_by=float(payload.get("suggested_by", 0.0)),
        evaluated_at=float(payload.get("evaluated_at", 0.0)),
        revision=int(payload.get("revision", 0)),
        based_on_event_ids=tuple(str(item) for item in payload.get("based_on_event_ids", [])),
    )
