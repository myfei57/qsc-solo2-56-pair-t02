"""控制服务配置。

配置分三类：平台（名称、运行单元、存储目录）、现场限值（对位公差、探测确认时间、
批量上限、配额与序号模数）、以及控制台监听地址。所有取值在装配期校验，非法配置
直接抛 :class:`~psdline.errors.ConfigError`，不允许带着默认值悄悄跑起来。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

from .errors import ConfigError


@dataclass(frozen=True)
class PlatformSection:
    """平台标识与存储。"""

    name: str = "psdline"
    unit: str = "line1"
    storage_root: str = "var/state"


@dataclass(frozen=True)
class NamespaceSection:
    """线路与站台。"""

    line: str = "L1"
    stations: tuple[str, ...] = ("ST1", "ST2", "ST3")


@dataclass(frozen=True)
class StationSection:
    """站台与门区布置。"""

    groups_per_station: int = 4
    doors_per_group: int = 6
    group_span_m: float = 24.0
    stop_tolerance_m: float = 0.8


@dataclass(frozen=True)
class TrainSection:
    """列车限值。"""

    length_m: float = 118.0
    max_position_m: int = 240
    dwell_default_s: float = 45.0
    dwell_max_s: float = 600.0


@dataclass(frozen=True)
class DoorSection:
    """门控限值。"""

    batch_limit: int = 64
    open_hold_s: float = 30.0
    close_confirm_s: float = 6.0
    lockout_release_s: float = 12.0


@dataclass(frozen=True)
class GapSection:
    """间隙探测限值。"""

    confirm_s: float = 4.0
    probe_timeout_s: float = 20.0
    min_clearance_mm: float = 12.0


@dataclass(frozen=True)
class SignalSection:
    """信号刷新限值。"""

    refresh_period_s: float = 1.0
    stale_after_s: float = 8.0
    sequence_modulus: int = 65536


@dataclass(frozen=True)
class InterlockSection:
    """联锁限值。"""

    hold_after_fault_s: float = 3.0
    max_locked_ratio: float = 0.5


@dataclass(frozen=True)
class QuotaSection:
    """运行配额。"""

    arrival_limit: int = 1000
    depart_limit: int = 1000


@dataclass(frozen=True)
class AuditSection:
    """审计流水分段。"""

    journal_segment: int = 512


@dataclass(frozen=True)
class HealthSection:
    """门机劣化评估限值。

    指标按最近 ``window_size`` 次合格动作的滑动窗口计算；寿命消耗按累计
    动作次数对 ``rated_cycles`` 的占比计算。评分 = 水平、趋势、寿命三个
    分量按权重求和，越过 ``warn_threshold`` 记 SUSPECT，越过
    ``exclude_threshold`` 记 DEGRADED 并在下一轮批量开门摘除；连续
    ``jam_consecutive`` 次出现卡阻特征直接记 CRITICAL 并立即摘除。
    """

    window_size: int = 60
    min_window_actions: int = 20
    baseline_actions: int = 30
    min_completeness: float = 0.8
    data_quality_ratio: float = 0.3
    duration_crit_ratio: float = 1.5
    resistance_crit_ratio: float = 1.6
    slope_crit_per_action: float = 0.004
    weight_level: float = 0.6
    weight_trend: float = 0.25
    weight_life: float = 0.15
    warn_threshold: float = 0.4
    exclude_threshold: float = 0.6
    clear_threshold: float = 0.32
    warn_consecutive: int = 3
    exclude_consecutive: int = 2
    clear_consecutive: int = 5
    jam_resistance_ratio: float = 2.2
    jam_duration_ratio: float = 2.5
    jam_consecutive: int = 2
    rated_cycles: int = 100000
    life_warn_ratio: float = 0.9
    off_peak_windows: tuple = ((1, 5),)
    exclude_on_insufficient_data: bool = False


@dataclass(frozen=True)
class ConsoleSection:
    """控制台监听地址。"""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class PlatformConfig:
    """一份完整的控制服务配置。"""

    platform: PlatformSection = field(default_factory=PlatformSection)
    namespace: NamespaceSection = field(default_factory=NamespaceSection)
    station: StationSection = field(default_factory=StationSection)
    train: TrainSection = field(default_factory=TrainSection)
    door: DoorSection = field(default_factory=DoorSection)
    gap: GapSection = field(default_factory=GapSection)
    signal: SignalSection = field(default_factory=SignalSection)
    interlock: InterlockSection = field(default_factory=InterlockSection)
    quota: QuotaSection = field(default_factory=QuotaSection)
    audit: AuditSection = field(default_factory=AuditSection)
    health: HealthSection = field(default_factory=HealthSection)
    console: ConsoleSection = field(default_factory=ConsoleSection)

    @property
    def name(self) -> str:
        return self.platform.name

    @property
    def storage_root(self) -> str:
        return self.platform.storage_root

    def validate(self) -> "PlatformConfig":
        """校验配置，返回自身以便链式调用。"""

        if not self.platform.name:
            raise ConfigError("平台名称不能为空", component="config")
        if not self.platform.unit:
            raise ConfigError("运行单元不能为空", component="config")
        if not self.platform.storage_root:
            raise ConfigError("存储目录不能为空", component="config")
        if not self.namespace.stations:
            raise ConfigError("线路至少需要一个站台", component="config")
        if self.station.groups_per_station <= 0:
            raise ConfigError("每站门区数必须为正", component="config")
        if self.station.doors_per_group <= 0:
            raise ConfigError("每门区门数必须为正", component="config")
        if self.station.stop_tolerance_m <= 0:
            raise ConfigError("对位公差必须为正", component="config")
        if self.train.max_position_m <= 0:
            raise ConfigError("列车位置上限必须为正", component="config")
        if self.train.dwell_max_s <= 0:
            raise ConfigError("停站时长上限必须为正", component="config")
        if self.door.batch_limit <= 0:
            raise ConfigError("批量开门上限必须为正", component="config")
        if self.gap.min_clearance_mm < 0:
            raise ConfigError("最小间隙不能为负", component="config")
        if self.signal.stale_after_s <= 0:
            raise ConfigError("位置过期时间必须为正", component="config")
        if self.signal.sequence_modulus != 65536:
            raise ConfigError("序号模数固定为 65536", component="config")
        if not 0 < self.interlock.max_locked_ratio <= 1:
            raise ConfigError("锁定比例上限必须落在 (0, 1]", component="config")
        if self.quota.arrival_limit <= 0 or self.quota.depart_limit <= 0:
            raise ConfigError("运行配额必须为正", component="config")
        if self.audit.journal_segment <= 0:
            raise ConfigError("审计分段长度必须为正", component="config")
        self._validate_health()
        if not 0 < self.console.port <= 65535:
            raise ConfigError("控制台端口必须落在 1..65535", component="config")
        return self

    def _validate_health(self) -> None:
        """校验劣化评估限值：阈值次序、权重归一与窗口关系。"""

        health = self.health
        if health.window_size < health.min_window_actions:
            raise ConfigError("评估窗口长度不能小于最少合格动作数", component="config")
        if health.min_window_actions < 2 or health.baseline_actions < 2:
            raise ConfigError("评估窗口与基线样本数至少为 2", component="config")
        if not 0 < health.min_completeness <= 1:
            raise ConfigError("采样完整度下限必须落在 (0, 1]", component="config")
        if not 0 <= health.data_quality_ratio <= 1:
            raise ConfigError("数据质量告警比例必须落在 [0, 1]", component="config")
        if health.duration_crit_ratio <= 1 or health.resistance_crit_ratio <= 1:
            raise ConfigError("时长/阻力临界比值必须大于 1", component="config")
        if health.slope_crit_per_action <= 0:
            raise ConfigError("趋势临界斜率必须为正", component="config")
        if abs(health.weight_level + health.weight_trend + health.weight_life - 1.0) > 1e-6:
            raise ConfigError("评分权重之和必须等于 1", component="config")
        if not 0 < health.clear_threshold < health.warn_threshold < health.exclude_threshold <= 1:
            raise ConfigError("评估阈值必须满足 0 < 清除 < 预警 < 摘除 ≤ 1", component="config")
        if health.jam_resistance_ratio <= health.resistance_crit_ratio:
            raise ConfigError("卡阻阻力比值必须大于阻力临界比值", component="config")
        if health.jam_duration_ratio <= health.duration_crit_ratio:
            raise ConfigError("卡阻时长比值必须大于时长临界比值", component="config")
        if min(
            health.warn_consecutive,
            health.exclude_consecutive,
            health.clear_consecutive,
            health.jam_consecutive,
        ) < 1:
            raise ConfigError("滞回计数必须为正", component="config")
        if health.rated_cycles <= 0:
            raise ConfigError("额定动作次数必须为正", component="config")
        if not 0 < health.life_warn_ratio <= 1:
            raise ConfigError("寿命告警比例必须落在 (0, 1]", component="config")
        for window in health.off_peak_windows:
            if len(window) != 2 or not 0 <= window[0] < window[1] <= 24:
                raise ConfigError("低谷检修窗口必须是合法的 [起, 止) 小时段", component="config")


def default_config() -> PlatformConfig:
    """返回默认配置。"""

    return PlatformConfig().validate()


def _section(cls, payload: object):
    """按数据类字段构造配置小节，忽略未知键。"""

    if not isinstance(payload, dict):
        return cls()
    known = {field_name for field_name in cls.__dataclass_fields__}
    kwargs = {key: value for key, value in payload.items() if key in known}
    if cls is NamespaceSection and "stations" in kwargs:
        kwargs["stations"] = tuple(str(item) for item in kwargs["stations"])
    if cls is HealthSection and "off_peak_windows" in kwargs:
        kwargs["off_peak_windows"] = tuple(
            tuple(int(hour) for hour in window) for window in kwargs["off_peak_windows"]
        )
    return cls(**kwargs)


def config_to_mapping(config: PlatformConfig) -> dict:
    """把配置对象转成可落盘的字典。"""

    return {
        "platform": {
            "name": config.platform.name,
            "unit": config.platform.unit,
            "storage_root": config.platform.storage_root,
        },
        "namespace": {
            "line": config.namespace.line,
            "stations": list(config.namespace.stations),
        },
        "station": dict(config.station.__dict__),
        "train": dict(config.train.__dict__),
        "door": dict(config.door.__dict__),
        "gap": dict(config.gap.__dict__),
        "signal": dict(config.signal.__dict__),
        "interlock": dict(config.interlock.__dict__),
        "quota": dict(config.quota.__dict__),
        "audit": dict(config.audit.__dict__),
        "health": dict(config.health.__dict__),
        "console": dict(config.console.__dict__),
    }


def config_from_mapping(payload: dict) -> PlatformConfig:
    """从一个字典构造配置。"""

    if not isinstance(payload, dict):
        raise ConfigError("配置必须是一个 JSON 对象", component="config")
    config = PlatformConfig(
        platform=_section(PlatformSection, payload.get("platform")),
        namespace=_section(NamespaceSection, payload.get("namespace")),
        station=_section(StationSection, payload.get("station")),
        train=_section(TrainSection, payload.get("train")),
        door=_section(DoorSection, payload.get("door")),
        gap=_section(GapSection, payload.get("gap")),
        signal=_section(SignalSection, payload.get("signal")),
        interlock=_section(InterlockSection, payload.get("interlock")),
        quota=_section(QuotaSection, payload.get("quota")),
        audit=_section(AuditSection, payload.get("audit")),
        health=_section(HealthSection, payload.get("health")),
        console=_section(ConsoleSection, payload.get("console")),
    )
    return config.validate()


def load_config(path: str) -> PlatformConfig:
    """从 JSON 文件加载配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"读取配置失败: {error}", component="config") from error
    return config_from_mapping(payload)


def with_storage_root(config: PlatformConfig, storage_root: str) -> PlatformConfig:
    """返回一份替换了存储目录的配置。"""

    if not storage_root:
        raise ConfigError("存储目录不能为空", component="config")
    return replace(config, platform=replace(config.platform, storage_root=storage_root))
