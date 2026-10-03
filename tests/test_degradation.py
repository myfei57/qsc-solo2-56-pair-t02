"""门机劣化评估、摘除与恢复。"""

from __future__ import annotations

from psdline.config import (
    DegradationSection,
    PlatformConfig,
    config_from_mapping,
    config_to_mapping,
    default_config,
)
from psdline.console.api import Api
from psdline.degradation import DoorAction
from psdline.degradation.scoring import evaluate, select_window


def _section(**changes) -> DegradationSection:
    payload = config_to_mapping(default_config())["degradation"]
    payload.update(changes)
    return config_from_mapping({"degradation": payload}).degradation


def _actions(count: int, duration=2.9, resistance=300.0, start=1000.0):
    return [
        DoorAction(
            event_id=f"e-{index}",
            psd_id="G1",
            at=start + index * 60,
            direction="open",
            duration_s=duration,
            resistance_n=resistance,
        )
        for index in range(count)
    ]


def test_recent_window_controls_mechanical_degradation_but_lifetime_remains():
    section = _section(window_actions=5, min_samples=3, lifetime_warn_actions=10, lifetime_critical_actions=20)
    actions = _actions(6)
    window = select_window(actions, 1000 + 5 * 60, section)
    assert len(window) == 5

    verdict = evaluate("G1", window, lifetime_actions=11, now=1000 + 5 * 60, section=section)
    assert verdict.window_actions == 5
    assert verdict.lifetime_actions == 11
    assert verdict.status == "watch"
    assert any("累计动作" in reason for reason in verdict.reasons)


def test_missing_samples_do_not_become_zero_or_unhealthy():
    section = _section(min_samples=12, required_coverage=0.9)
    actions = _actions(12, duration=10.0, resistance=1000.0)
    incomplete = [
        DoorAction(
            event_id=action.event_id,
            psd_id=action.psd_id,
            at=action.at,
            direction=action.direction,
            duration_s=None,
            resistance_n=None,
        )
        for action in actions
    ]
    verdict = evaluate("G1", incomplete, 12, now=incomplete[-1].at, section=section)
    assert verdict.status == "unknown"
    assert verdict.data_complete is False
    assert verdict.excluded_from_batch is False
    assert "采样覆盖率不足" in verdict.data_warnings[0]


def test_degraded_defers_until_operation_ends(platform, clock):
    gate = platform.gates.by_station("ST1")[0]
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(gate.id, "open", duration_s=4.3, resistance_n=300, event_id=f"e{index}")
    verdict = platform.degradation.verdict(gate.id)
    assert verdict.status == "degraded"
    assert verdict.excluded_from_batch is True

    clock.advance(60)
    platform.record_door_action(gate.id, "open", duration_s=2.9, resistance_n=300, event_id="good-after-maintenance-data")
    platform.complete_door_maintenance(gate.id)
    platform.degradation.begin_operation("op-1", [gate.id], clock.now())
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(gate.id, "open", duration_s=4.3, resistance_n=300, event_id=f"d{index}")
    verdict = platform.degradation.verdict(gate.id)
    assert verdict.status == "degraded"
    assert verdict.pending_exclusion is True
    assert verdict.excluded_from_batch is False
    assert gate.id not in platform.degradation.excluded_ids()

    outcome = platform.degradation.end_operation("op-1", clock.now())
    assert outcome["applied"] == [gate.id]
    assert platform.degradation.verdict(gate.id).excluded_from_batch is True


def test_critical_mechanical_condition_is_immediate_but_score_only_critical_waits(platform, clock):
    gate = platform.gates.by_station("ST1")[0]
    section = platform.config.degradation
    platform.degradation.begin_operation("op-score", [gate.id], clock.now())
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(
            gate.id,
            "open",
            duration_s=section.nominal_duration_s * section.duration_critical_ratio,
            resistance_n=300,
            event_id=f"score{index}",
        )
    verdict = platform.degradation.verdict(gate.id)
    assert verdict.status == "critical"
    assert verdict.immediate_severity is True
    assert verdict.excluded_from_batch is True
    platform.degradation.end_operation("op-score", clock.now())


def test_critical_is_removed_from_future_batch_without_closing_open_door(platform, clock):
    gate = platform.gates.by_station("ST1")[0]
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(gate.id, "open", duration_s=10, resistance_n=2000, event_id=f"e{index}")
    assert platform.degradation.verdict(gate.id).status == "critical"
    platform.gates.set_open(gate.id, True)

    outcome = platform.batch_open("ST1")
    assert outcome["excluded"] == {gate.group: [gate.id]}
    assert platform.gates.require(gate.id).open is True


def test_batch_open_filters_individual_gates(platform, clock):
    gates = platform.gates.by_station("ST1")
    bad = gates[0]
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(bad.id, "open", duration_s=10, resistance_n=2000, event_id=f"e{index}")
    outcome = platform.batch_open("ST1")
    assert bad.id in outcome["excluded"][bad.group]
    assert bad.group in outcome["opened"]
    assert bad.group in outcome["opened"]
    assert platform.gates.require(gates[1].id).open is True
    assert platform.gates.require(bad.id).open is False


def test_lifetime_actions_and_history_survive_maintenance(platform, clock):
    gate = platform.gates.by_station("ST1")[0]
    for index in range(13):
        clock.advance(60)
        platform.record_door_action(gate.id, "open", duration_s=10, resistance_n=2000, event_id=f"e{index}")
    platform.complete_door_maintenance(gate.id)
    verdict = platform.degradation.verdict(gate.id)
    assert verdict.lifetime_actions == 13
    assert verdict.window_actions == 0
    assert verdict.excluded_from_batch is False
    assert platform.degradation.journal.read_tail(gate.id, 100)[0]["event_id"] == "e0"


def test_event_id_is_idempotent_and_restart_preserves_conclusion(tmp_path, clock):
    from psdline.app import build_platform

    platform = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    platform.start()
    gate = platform.gates.by_station("ST1")[0]
    action = {
        "psd_id": gate.id,
        "direction": "open",
        "duration_s": 10,
        "resistance_n": 2000,
        "event_id": "same-event",
    }
    platform.record_door_actions([action])
    platform.record_door_actions([action])
    assert platform.degradation.verdict(gate.id).lifetime_actions == 1

    restored = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    assert restored.degradation.verdict(gate.id).lifetime_actions == 1
    assert restored.degradation.journal.read_all(gate.id)[0]["event_id"] == "same-event"


def test_door_actions_api_and_query_share_one_verdict(platform):
    gate = platform.gates.by_station("ST1")[0]
    api = Api(platform)
    for index in range(12):
        platform.clock.advance(60)
        status, payload = api.handle(
            "POST",
            "/api/door-actions",
            {},
            {
                "psd_id": gate.id,
                "direction": "open",
                "duration_s": 10,
                "resistance_n": 2000,
                "event_id": f"e{index}",
            },
        )
        assert status == 200
    status, payload = api.handle("GET", f"/api/degradation/{gate.id}", {}, {})
    assert status == 200
    assert payload["verdict"]["status"] == "critical"
    status, summary = api.handle("GET", "/api/degradation", {}, {})
    assert gate.id in summary["summary"]["excluded"]


def test_audit_records_status_transition(platform, clock):
    gate = platform.gates.by_station("ST1")[0]
    before = len(platform.audit.list())
    for index in range(12):
        clock.advance(60)
        platform.record_door_action(gate.id, "open", duration_s=10, resistance_n=2000, event_id=f"e{index}")
    kinds = [entry.kind for entry in platform.audit.list()[before:]]
    assert "degradation_excluded" in kinds or "degradation_changed" in kinds
