"""事件信封与领域实体。

台账只允许追加事件，任何状态都由事件回放得到；事件一经追加不可修改、
不可删除。状态变更是事实，口径版本通过发布新口径事件推进。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .metrics import METRICS, ALL_ROLES

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-:]{0,63}$")

EVENT_TYPES = frozenset(
    {
        "production_line_registered",
        "process_registered",
        "material_batch_registered",
        "product_spec_registered",
        "product_batch_logged",
        "partner_registered",
        "shift_opened",
        "shift_frozen",
        "measurement_submitted",
        "measurement_confirmed",
        "conflict_raised",
        "conflict_resolved",
        "waste_record_submitted",
        "hydrogen_record_submitted",
        "record_confirmed",
        "record_amended",
        "maintenance_logged",
        "late_inspection_logged",
        "byproduct_diversion_logged",
        "caliber_published",
        "report_published",
    }
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def validate_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError(f"{field_name}标识无效: {value!r}")


def validate_role(role: str) -> None:
    if role not in ALL_ROLES:
        raise ValueError(f"未知角色: {role!r}")


def validate_metric(code: str) -> None:
    if code not in METRICS:
        raise ValueError(f"未知指标: {code!r}")


@dataclass(frozen=True)
class Event:
    """不可变事件信封。seq 与 ts 由存储在追加时赋值。"""

    type: str
    payload: dict[str, Any]
    actor: str
    id: str = field(default_factory=lambda: new_id("evt"))
    seq: int | None = None
    ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            id=raw["id"],
            seq=raw.get("seq"),
            ts=raw.get("ts"),
            type=raw["type"],
            actor=raw["actor"],
            payload=raw["payload"],
        )


# ---- 实体快照 -------------------------------------------------------------


@dataclass
class ProductionLine:
    id: str
    name: str


@dataclass
class ProcessStep:
    id: str
    line_id: str
    name: str
    order: int


@dataclass
class MaterialBatch:
    id: str
    name: str  # 铁矿石/废钢/焦炭/焦炉煤气等
    supplier: str
    amount: float
    unit: str
    process_id: str  # 投入工序


@dataclass
class ProductSpec:
    id: str
    grade: str  # 精品钢牌号/规格
    standard: str


@dataclass
class ProductBatch:
    """精品钢产品批次（合格品判定用于合格率）。"""

    id: str
    spec_id: str
    shift_id: str
    amount: float
    passed: bool


@dataclass
class Partner:
    id: str
    name: str
    kind: str  # waste / hydrogen / inspection


@dataclass
class Shift:
    id: str
    line_id: str
    date: str  # YYYY-MM-DD
    shift_no: int  # 1/2/3
    opened_by: str
    frozen: bool = False
    frozen_at: float | None = None
    caliber_version: int | None = None
    # 生产归属：班次内产出的精品钢批次
    product_batch_ids: list[str] = field(default_factory=list)


@dataclass
class Measurement:
    id: str
    shift_id: str
    metric_code: str
    value: float
    unit: str
    submitted_by: str
    submitted_at: float
    idempotency_key: str
    confirmed_by: list[str] = field(default_factory=list)
    status: str = "pending"  # pending / confirmed / conflict
    conflict_id: str | None = None
    maintenance: str | None = None  # 关联检修事件，指标标记为受影响
    late_inspection: str | None = None
    supersedes: str | None = None  # 冲突人工核对后替换的旧测量


@dataclass
class WasteRecord:
    id: str
    shift_id: str
    waste_type: str
    amount: float
    process_id: str
    material_batch_ids: list[str]
    partner_id: str
    destination: str  # 初始去向：资源化利用/贮存/外委处置
    utilized: bool
    submitted_by: str
    submitted_at: float
    confirmed_by: list[str] = field(default_factory=list)
    status: str = "pending"
    diversion: "ByproductDiversion | None" = None


@dataclass
class HydrogenRecord:
    id: str
    shift_id: str
    source_gas_batch_id: str  # 焦炉煤气来源批次
    volume_nm3: float
    process_id: str
    partner_id: str
    use: str  # 初始用途：自用燃料/外供/化工原料
    submitted_by: str
    submitted_at: float
    confirmed_by: list[str] = field(default_factory=list)
    status: str = "pending"
    diversion: "ByproductDiversion | None" = None


@dataclass
class ByproductDiversion:
    """副产物改用途：只重算相关指标与结算，不碰已发布报表。"""

    id: str
    record_id: str
    kind: str  # waste / hydrogen
    new_destination: str
    new_utilized: bool | None
    reason: str
    logged_by: str
    ts: float


@dataclass
class Conflict:
    id: str
    shift_id: str
    metric_code: str
    candidates: list[str]  # measurement id
    values: list[float]
    raised_by: str
    resolved: bool = False
    winning_measurement_id: str | None = None
    note: str = ""


@dataclass
class Report:
    id: str
    shift_id: str
    caliber_version: int
    published_by: str
    published_at: float
    values: dict[str, float]
    measurement_ids: list[str]
    record_ids: list[str]
    quality_pass_rate: float | None
    fingerprint: str


@dataclass
class Settlement:
    """与合作单位的结算依据，随相关记录改用途而局部更新。"""

    partner_id: str
    shift_id: str
    record_id: str
    kind: str
    amount_desc: str
    basis: str  # 去向/用途
    valid: bool = True
    report_id: str | None = None  # 被哪份报表冻结引用（冻结后仅可作废止重建）
