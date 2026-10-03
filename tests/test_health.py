"""门机劣化评估：评分、状态机、摘除时机、缺失样本、崩溃恢复与同一份结论。"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from psdline.app import build_platform
from psdline.config import default_config
from psdline.console.api import Api
from psdline.door import batch_open
from psdline.errors import ConfigError
from psdline.health.model import DoorState

HEALTH_OVERRIDES = dict(
    window_size=20,
    min_window_actions=8,
    baseline_actions=10,
    warn_consecutive=2,
    exclude_consecutive=2,
    clear_consecutive=3,
)


@pytest.fixture()
def hconfig():
    config = default_config()
    return replace(config, health=replace(config.health, **HEALTH_OVERRIDES))


@pytest.fixture()
def hplatform(tmp_path, clock, hconfig):
    return build_platform(hconfig, clock=clock, storage_root=str(tmp_path / "state"))


def gate_of(platform, station_id="ST1", index=0):
    return platform.gates.by_station(station_id)[index].id


def drive(platform, psd_id, seqs, duration=12.0, resistance=1.0, completeness=1.0):
    """按给定参数逐次采集，返回最后一条结论。"""

    conclusion = None
    for seq in seqs:
        value_d = duration(seq) if callable(duration) else duration
        value_r = resistance(seq) if callable(resistance) else resistance
        expected = 10 if value_r is not None else 0
        received = int(round(expected * completeness))
        conclusion = platform.record_action(
            psd_id,
            duration_s=value_d,
            resistance_peak=value_r,
            samples_expected=expected,
            samples_received=received,
            seq=seq,
        )["conclusion"]
    return conclusion


def ramp(start_seq, rate=1.03, cap=2.0, base=12.0):
    """指数劣化曲线（封顶以避免触发卡阻比值）。"""

    def value(seq):
        ratio = min(rate ** max(0, seq - start_seq), cap)
        return round(base * ratio, 4)

    return value


def test_healthy_door_stays_healthy(hplatform):
    gate = gate_of(hplatform)
    conclusion = drive(hplatform, gate, range(1, 41))
    assert conclusion["state"] == DoorState.HEALTHY.value
    assert conclusion["score"] < 0.4
    assert conclusion["eligible"] is True
    assert conclusion["confidence"] == "high"


def test_gradual_degradation_excludes_in_order(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    states = []
    for seq in range(11, 61):
        conclusion = drive(hplatform, gate, [seq], duration=ramp(10))
        states.append(conclusion["state"])
    assert states[-1] == DoorState.DEGRADED.value
    first_suspect = states.index(DoorState.SUSPECT.value)
    first_degraded = states.index(DoorState.DEGRADED.value)
    assert first_suspect < first_degraded
    conclusion = hplatform.health.get(gate)
    assert conclusion.eligible is False
    assert conclusion.reasons
    assert conclusion.advice["urgent"] is True


def test_hysteresis_no_flapping(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    # 持续越预警线但不到摘除线 → SUSPECT
    states = [
        drive(hplatform, gate, [seq], duration=16.8)["state"] for seq in range(11, 25)
    ]
    assert states[-1] == DoorState.SUSPECT.value
    assert DoorState.DEGRADED.value not in states
    # 两次好转不足以清除（clear_consecutive=3，且窗口还没换完）
    drive(hplatform, gate, [25, 26], duration=12.0)
    assert hplatform.health.get(gate).state == DoorState.SUSPECT.value
    # 持续好转后清除，且全程没有来回抖动
    drive(hplatform, gate, range(27, 50), duration=12.0)
    assert hplatform.health.get(gate).state == DoorState.HEALTHY.value


def test_jam_spike_goes_critical_immediately(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0, resistance=1.0)
    first = drive(hplatform, gate, [11], duration=12.0, resistance=2.6)
    assert first["state"] != DoorState.CRITICAL.value
    second = drive(hplatform, gate, [12], duration=12.0, resistance=2.6)
    assert second["state"] == DoorState.CRITICAL.value
    assert second["eligible"] is False


def test_degraded_is_sticky_until_maintenance(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    drive(hplatform, gate, range(11, 61), duration=ramp(10))
    assert hplatform.health.get(gate).state == DoorState.DEGRADED.value
    # 磨损不会自愈：连续完美样本也不退出
    drive(hplatform, gate, range(61, 91), duration=12.0)
    assert hplatform.health.get(gate).state == DoorState.DEGRADED.value
    # 检修复位 → 重新建基线 → 恢复健康
    maintained = hplatform.record_maintenance(gate, "更换导靴")["conclusion"]
    assert maintained["state"] == DoorState.INSUFFICIENT_DATA.value
    drive(hplatform, gate, range(91, 130), duration=12.0)
    assert hplatform.health.get(gate).state == DoorState.HEALTHY.value
    # 历史不可变：流水里仍能看到曾经的 degraded 结论与检修记录
    history = hplatform.health.history(gate)
    assert any(record.get("type") == "maintenance" for record in history)
    assert any(
        record.get("type") == "assessment"
        and record["conclusion"]["state"] == DoorState.DEGRADED.value
        for record in history
    )


def test_missing_samples_never_skew(hplatform):
    good = gate_of(hplatform, index=0)
    flaky = gate_of(hplatform, index=1)
    drive(hplatform, good, range(1, 41), duration=12.0)
    for seq in range(1, 41):
        if seq % 2:
            # 合格样本：与对照门完全一致的真实时长
            drive(hplatform, flaky, [seq], duration=12.0)
        else:
            # 采样缺失：带着荒谬时长，完整度不足必须被隔离在统计之外
            drive(hplatform, flaky, [seq], duration=999.0, completeness=0.4)
    conclusion = hplatform.health.get(flaky)
    assert conclusion.state == DoorState.HEALTHY.value
    assert conclusion.window["median_duration_ratio"] == pytest.approx(1.0, abs=0.01)
    assert conclusion.data_quality_ok is False
    assert flaky in hplatform.health.summary()["data_quality_low"]
    assert good not in hplatform.health.summary()["data_quality_low"]


def test_completeness_boundary(hplatform):
    gate = gate_of(hplatform)
    # 完整度 0.8 合格、0.7 不合格，边界不能让缺失样本混进统计
    drive(hplatform, gate, range(1, 16), duration=12.0, completeness=0.8)
    assert hplatform.health.get(gate).window["qualified"] == 5
    drive(hplatform, gate, [16], duration=50.0, completeness=0.7)
    conclusion = hplatform.health.get(gate)
    assert conclusion.window["qualified"] == 5
    assert conclusion.window["median_duration_ratio"] < 1.5


def test_insufficient_data_is_fail_operational(hplatform):
    gate = gate_of(hplatform)
    conclusion = drive(hplatform, gate, range(1, 4), duration=12.0)
    assert conclusion["state"] == DoorState.INSUFFICIENT_DATA.value
    assert conclusion["eligible"] is True
    outcome = hplatform.batch_open("ST1")
    assert gate not in outcome["health_skipped"]
    assert hplatform.gates.get(gate).open is True


def test_degraded_survives_data_loss(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    drive(hplatform, gate, range(11, 61), duration=ramp(10))
    assert hplatform.health.get(gate).state == DoorState.DEGRADED.value
    # 缺数据永远不能"治好"一樘门
    drive(hplatform, gate, range(61, 81), duration=None, resistance=None)
    assert hplatform.health.get(gate).state == DoorState.DEGRADED.value


def test_door_without_resistance_sensor(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0, resistance=None)
    conclusion = drive(hplatform, gate, range(11, 61), duration=ramp(10), resistance=None)
    assert conclusion["state"] == DoorState.DEGRADED.value
    assert conclusion["window"]["median_resistance_ratio"] is None


def test_duplicate_ingest_is_idempotent(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 16), duration=12.0)
    before = hplatform.health.get(gate)
    journal_before = hplatform.health.summary()["journal_records"]
    again = drive(hplatform, gate, [15], duration=12.0)
    assert again["version"] == before.version
    assert hplatform.health.summary()["journal_records"] == journal_before


def test_restart_and_crash_tail_recovery(tmp_path, clock, hconfig):
    root = str(tmp_path / "state")
    first = build_platform(hconfig, clock=clock, storage_root=root)
    gate = gate_of(first)
    drive(first, gate, range(1, 26), duration=ramp(10, rate=1.01))
    before = first.health.get(gate)

    restarted = build_platform(hconfig, clock=clock, storage_root=root)
    after = restarted.health.get(gate)
    assert after.state == before.state
    assert after.score == before.score
    assert after.version == before.version
    assert restarted.health.replayed == 0

    # 模拟掉电窗口：流水已追加、文档未落盘
    sample = {
        "psd_id": gate,
        "seq": 26,
        "at": clock.now(),
        "kind": "open",
        "duration_s": 14.0,
        "resistance_peak": 1.0,
        "samples_expected": 10,
        "samples_received": 10,
    }
    restarted.health._journal.append({"type": "action", "sample": sample})
    recovered = build_platform(hconfig, clock=clock, storage_root=root)
    assert recovered.health.replayed == 1
    conclusion = recovered.health.get(gate)
    assert conclusion.version == after.version + 1
    assert conclusion.at == sample["at"]


def test_batch_open_excludes_degraded(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    drive(hplatform, gate, range(11, 61), duration=ramp(10))
    outcome = hplatform.batch_open("ST1")
    assert outcome["health_skipped"][gate] == DoorState.DEGRADED.value
    assert outcome["health_view"] > 0
    assert hplatform.gates.get(gate).open is False
    siblings = [
        item
        for item in hplatform.gates.by_station("ST1")
        if item.group == "G01" and item.id != gate
    ]
    assert all(item.open for item in siblings)


def test_batch_open_rechecks_critical_at_dispatch(hplatform):
    gate = gate_of(hplatform)

    class FlippedHealth:
        """快照之后、派发之前才翻成 CRITICAL 的登记处替身。"""

        def view(self):
            return SimpleNamespace(
                version=7,
                excluded=lambda _psd_id: False,
                state_of=lambda _psd_id: "",
            )

        def critical(self, psd_id):
            return psd_id == gate

    hplatform.doors.attach_health(FlippedHealth())
    outcome = batch_open(hplatform.doors, "ST1", list(hplatform.layouts.get("ST1").zones))
    assert outcome["health_skipped"][gate] == "critical"
    assert hplatform.gates.get(gate).open is False
    assert outcome["health_view"] == 7


def test_same_conclusion_across_readers(hplatform):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    drive(hplatform, gate, range(11, 61), duration=ramp(10))
    conclusion = hplatform.health.get(gate)

    status = hplatform.status()["health"]
    assert gate in status["excluded"]
    view = hplatform.health.view()
    assert view.get(gate).state == DoorState.DEGRADED.value
    assert view.version == conclusion.version
    detail = hplatform.door_health(gate)
    assert detail["conclusion"]["version"] == conclusion.version
    transitions = [
        entry for entry in hplatform.audit.list() if entry.kind == "health_transition"
    ]
    assert transitions
    # 审计记录的跃迁与流水里同版本结论说的是同一件事
    last = transitions[-1]
    assert last.detail["state"] == DoorState.DEGRADED.value
    journal_version = {
        record["conclusion"]["version"]: record["conclusion"]["state"]
        for record in hplatform.health.history(gate)
        if record.get("type") == "assessment"
    }
    assert journal_version[last.detail["version"]] == last.detail["state"]


def test_health_api(hplatform):
    api = Api(hplatform)
    gate = gate_of(hplatform)
    status, payload = api.handle(
        "POST",
        "/api/health/action",
        {},
        {"psd_id": gate, "duration_s": 12.0, "samples_expected": 10, "samples_received": 10},
    )
    assert status == 200 and payload["conclusion"]["psd_id"] == gate
    status, summary = api.handle("GET", "/api/health", {}, {})
    assert status == 200 and summary["doors"] == 1
    status, one = api.handle("GET", f"/api/health/{gate}", {}, {})
    assert status == 200 and one["conclusion"]["psd_id"] == gate
    status, maintained = api.handle(
        "POST", "/api/health/maintenance", {}, {"psd_id": gate, "note": "巡检"}
    )
    assert status == 200 and maintained["conclusion"]["state"] == DoorState.INSUFFICIENT_DATA.value
    status, missing = api.handle("POST", "/api/health/action", {}, {})
    assert status == 400


def test_health_config_validation(hconfig):
    assert hconfig.health.window_size == 20
    config = default_config()
    bad_thresholds = replace(
        config, health=replace(config.health, warn_threshold=0.9, exclude_threshold=0.5)
    )
    with pytest.raises(ConfigError):
        bad_thresholds.validate()
    bad_weights = replace(
        config, health=replace(config.health, weight_level=0.9, weight_trend=0.9)
    )
    with pytest.raises(ConfigError):
        bad_weights.validate()
    bad_window = replace(config, health=replace(config.health, off_peak_windows=((5, 1),)))
    with pytest.raises(ConfigError):
        bad_window.validate()


def test_health_config_roundtrip():
    from psdline.config import config_from_mapping, config_to_mapping

    config = default_config()
    mapped = config_to_mapping(config)
    assert mapped["health"]["off_peak_windows"] == ((1, 5),)
    restored = config_from_mapping(config_to_mapping(config))
    assert restored.health == config.health


def test_maintenance_advice_falls_in_off_peak(hplatform, clock):
    gate = gate_of(hplatform)
    drive(hplatform, gate, range(1, 11), duration=12.0)
    conclusion = drive(hplatform, gate, range(11, 61), duration=ramp(10))
    advice = conclusion["advice"]
    assert advice["urgent"] is True
    hour = time.gmtime(advice["not_before"]).tm_hour
    assert hour == 1  # 低谷窗口 01:00–05:00 UTC
