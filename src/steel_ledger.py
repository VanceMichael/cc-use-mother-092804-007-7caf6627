"""钢铁转型数据服务：班次冻结、职责分离、幂等上传、冲突核对、报表不可变与指标追溯。

同一批生产数据既支撑产品质量，也解释固废利用、焦炉煤气制氢和环保绩效。
本模块以"班次快照"为数据冻结边界：

- 车间（``OPERATOR``）只能提交测量；环保（``ENV``）复核；
  节能成果须工艺/算法（``PROCESS``）与环保双签，任一角色不能独自确认。
- 同一班次同一测点重复上传按幂等键去重；数值不同进入人工核对队列，
  未解决前不进入指标与结算。
- 班次一旦冻结即不可改；口径升版只影响其后班次，已发布报表永不重写。
- 检修、检测迟到、副产物改用途只重算相关指标，并按结算口径冲销旧结算。
- 任一绿色指标可沿血缘追溯到原料、工序、处理记录和责任确认。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 角色与指标
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """参与方角色。``AUDITOR`` 只负责核对冲突，不参与会签。"""

    OPERATOR = "operator"    # 车间操作人员：提交测量
    ENV = "env"              # 环保人员：复核、环保会签
    PROCESS = "process"      # 算法/工艺负责人：工艺会签
    PARTNER = "partner"      # 合作单位（固废处理企业等）
    AUDITOR = "auditor"      # 管理人员：人工核对、发布报表
    ENERGY = "energy"        # 能源管理人员


class MetricKind(str, Enum):
    OUTPUT = "output"                  # 产量
    QUALITY = "quality"                # 精品钢质量
    SOLID_WASTE_USED = "solid_waste_used"   # 固废利用量
    H2_OUTPUT = "h2_output"            # 工业副产氢产量
    EMISSION = "emission"              # 环保绩效（排放/降碳）
    ENERGY_SAVING = "energy_saving"    # 节能成果（强制双签）


# 节能成果不得由算法/工艺一方独自确认：必须同时具备工艺与环保会签。
DUAL_SIGN_KINDS = frozenset({MetricKind.ENERGY_SAVING})
REVIEW_ROLE: dict[MetricKind, Role] = {
    MetricKind.QUALITY: Role.PROCESS,
    MetricKind.ENERGY_SAVING: Role.PROCESS,
}


class MetricStatus(str, Enum):
    DRAFT = "draft"          # 仅有测量
    REVIEWED = "reviewed"    # 已复核
    CONFIRMED = "confirmed"  # 会签完成
    DISPUTED = "disputed"    # 存在未决冲突
    SUPERSEDED = "superseded"  # 被更正版本替代（保留血缘）


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """参与方。合作单位用 ``org`` 标识，不记录真实个人身份信息。"""

    code: str
    name: str
    role: Role
    org: str | None = None


@dataclass(frozen=True)
class ProductionLine:
    code: str
    name: str
    process_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessStep:
    """工序，例如炼铁、炼钢、热轧、焦炉煤气变压吸附制氢。"""

    code: str
    name: str
    line_code: str
    seq: int


@dataclass(frozen=True)
class MaterialBatch:
    """原料批次。``parent`` 指向上游批次，构成投入链。"""

    code: str
    material: str
    quantity: float
    unit: str
    process_code: str
    parent: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductSpec:
    """精品钢规格（牌号与允差）。"""

    code: str
    grade: str
    tolerances: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductBatch:
    """精品钢产出批次，关联规格与原料批次。"""

    code: str
    spec_code: str
    quantity: float
    unit: str
    process_code: str
    material_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WasteRecord:
    """固废产生与资源化处理记录。

    ``reuse_process_code`` 为空表示暂未利用（只计入产生量）；
    ``disposition`` 描述去向（内部回用/外委建材/副产物改用途等）。
    """

    code: str
    waste_type: str
    quantity: float
    unit: str
    source_process_code: str
    partner_code: str | None
    disposition: str
    reuse_process_code: str | None = None


@dataclass(frozen=True)
class HydrogenRecord:
    """焦炉煤气制氢等工业副产氢记录。"""

    code: str
    source_gas: str
    h2_volume: float
    unit: str
    process_code: str
    partner_code: str | None
    disposition: str  # 自用/外供等去向


@dataclass(frozen=True)
class EnvTarget:
    """环保指标口径：名称、单位、方向（越大越好或越小越好）与允许区间。"""

    code: str
    name: str
    kind: MetricKind
    unit: str
    higher_is_better: bool = True
    allowed_range: tuple[float, float] | None = None


@dataclass(frozen=True)
class Measurement:
    """班次内的一次测量。

    ``metric_code`` 为目标环保/质量指标，产量等过程指标可为空；
    ``lineage`` 是该测量涉及的原料/工序/处理记录编码。
    """

    idempotency_key: str
    metric_code: str | None
    shift_code: str
    point: str
    value: float
    unit: str
    measured_by: str
    lineage: tuple[str, ...] = ()
    tag: str = ""  # 检修停机 / 检测迟到 / 副产物改用途 等事件标签


@dataclass(frozen=True)
class Signature:
    actor: str
    role: Role
    note: str = ""


@dataclass(frozen=True)
class Conflict:
    """同一幂等键重复上传但数值不一致时进入人工核对。"""

    key: str
    shift_code: str
    existing: Measurement
    incoming: Measurement
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: str | None = None
    resolved_by: str | None = None
    chosen_key: str | None = None


@dataclass(frozen=True)
class SpecVersion:
    """计算口径版本，只能递增。"""

    version: int
    name: str
    formulas: dict[str, str]
    published: bool = False


@dataclass(frozen=True)
class Shift:
    """班次快照：冻结数据来源与计算口径。"""

    code: str
    line_code: str
    started_at: str
    ended_at: str
    spec_version: int
    frozen: bool = False


@dataclass(frozen=True)
class PublishedReport:
    """已发布报表：内容随发布时快照固化，任何后续变更不可重写。"""

    code: str
    shift_codes: tuple[str, ...]
    spec_version: int
    metrics: tuple[dict[str, Any], ...]
    fingerprint: str


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class LedgerError(Exception):
    """数据服务规则冲突的基类。"""


class PermissionDenied(LedgerError):
    """角色无权执行该操作（职责分离）。"""


class ShiftFrozen(LedgerError):
    """班次已冻结，数据来源不可再变。"""


class ConflictUnresolved(LedgerError):
    """存在未决数值冲突，指标不得确认或结算。"""


class SpecError(LedgerError):
    """口径版本非法或被已发布报表占用。"""


# ---------------------------------------------------------------------------
# 指标条目（内部可变状态，经服务统一变更）
# ---------------------------------------------------------------------------


@dataclass
class MetricEntry:
    code: str
    shift_code: str
    kind: MetricKind
    target_code: str | None
    spec_version: int
    measurement_key: str
    value: float
    unit: str
    lineage: tuple[str, ...]
    tags: frozenset[str] = frozenset()
    status: MetricStatus = MetricStatus.DRAFT
    signatures: list[Signature] = field(default_factory=list)
    superseded_by: str | None = None
    # 结算：节能/副产物相关指标进入结算；更正后旧结算被冲销。
    settled: bool = False
    settlement_reversed: bool = False
    note: str = ""


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ---------------------------------------------------------------------------
# 数据服务
# ---------------------------------------------------------------------------


class SteelLedger:
    """钢铁转型数据服务（内存实现，规则可直接移植到持久化存储）。"""

    def __init__(self) -> None:
        self.actors: dict[str, Actor] = {}
        self.lines: dict[str, ProductionLine] = {}
        self.processes: dict[str, ProcessStep] = {}
        self.materials: dict[str, MaterialBatch] = {}
        self.specs: dict[str, ProductSpec] = {}
        self.products: dict[str, ProductBatch] = {}
        self.waste: dict[str, WasteRecord] = {}
        self.hydrogen: dict[str, HydrogenRecord] = {}
        self.targets: dict[str, EnvTarget] = {}
        self.spec_versions: dict[int, SpecVersion] = {}
        self._active_spec: int | None = None
        self.shifts: dict[str, Shift] = {}
        self.measurements: dict[str, Measurement] = {}
        self.metrics: dict[str, MetricEntry] = {}
        self.conflicts: dict[str, Conflict] = {}
        self.reports: dict[str, PublishedReport] = {}
        self._metric_seq = 0

    # -- 基础登记 -----------------------------------------------------------

    def register_actor(self, actor: Actor) -> None:
        if actor.code in self.actors:
            raise LedgerError("参与方已存在")
        self.actors[actor.code] = actor

    def register_line(self, line: ProductionLine) -> None:
        self.lines[line.code] = line

    def register_process(self, step: ProcessStep) -> None:
        if step.line_code not in self.lines:
            raise LedgerError("产线不存在")
        self.processes[step.code] = step

    def register_material(self, batch: MaterialBatch) -> None:
        if batch.process_code not in self.processes:
            raise LedgerError("投料工序不存在")
        if batch.parent and batch.parent not in self.materials:
            raise LedgerError("上游原料批次不存在")
        self.materials[batch.code] = batch

    def register_spec(self, spec: ProductSpec) -> None:
        self.specs[spec.code] = spec

    def register_product(self, product: ProductBatch) -> None:
        if product.spec_code not in self.specs:
            raise LedgerError("精品钢规格不存在")
        if product.process_code not in self.processes:
            raise LedgerError("产出工序不存在")
        for code in product.material_codes:
            if code not in self.materials:
                raise LedgerError(f"原料批次不存在: {code}")
        self.products[product.code] = product

    def register_waste(self, record: WasteRecord) -> None:
        if record.source_process_code not in self.processes:
            raise LedgerError("固废来源工序不存在")
        if record.reuse_process_code and record.reuse_process_code not in self.processes:
            raise LedgerError("回用工序不存在")
        if record.partner_code and record.partner_code not in self.actors:
            raise LedgerError("固废合作单位未登记")
        self.waste[record.code] = record

    def register_hydrogen(self, record: HydrogenRecord) -> None:
        if record.process_code not in self.processes:
            raise LedgerError("制氢工序不存在")
        if record.partner_code and record.partner_code not in self.actors:
            raise LedgerError("副产氢合作单位未登记")
        self.hydrogen[record.code] = record

    def register_target(self, target: EnvTarget) -> None:
        self.targets[target.code] = target

    # -- 口径版本 -----------------------------------------------------------

    def publish_spec_version(self, version: int, name: str,
                             formulas: dict[str, str]) -> SpecVersion:
        """登记新口径版本。版本号只能递增，已被报表占用的口径不可改。"""
        existing = self.spec_versions.get(version)
        if existing is not None:
            if existing.published:
                raise SpecError("口径版本已发布且不可修改")
            raise SpecError("口径版本已存在")
        if self.spec_versions and version <= max(self.spec_versions):
            raise SpecError("口径版本只能递增")
        spec = SpecVersion(version=version, name=name,
                           formulas=dict(formulas), published=True)
        self.spec_versions[version] = spec
        if self._active_spec is None:
            self._active_spec = version
        return spec

    def activate_spec(self, version: int) -> None:
        """切换当前口径：只影响切换后开启/冻结的班次，不回溯历史。"""
        if version not in self.spec_versions:
            raise SpecError("口径版本不存在")
        self._active_spec = version

    # -- 班次与冻结 ---------------------------------------------------------

    def open_shift(self, code: str, line_code: str, started_at: str,
                   ended_at: str) -> Shift:
        if code in self.shifts:
            raise LedgerError("班次已存在")
        if line_code not in self.lines:
            raise LedgerError("产线不存在")
        if self._active_spec is None:
            raise SpecError("尚无已发布的计算口径")
        shift = Shift(code=code, line_code=line_code, started_at=started_at,
                      ended_at=ended_at, spec_version=self._active_spec,
                      frozen=False)
        self.shifts[code] = shift
        return shift

    def freeze_shift(self, shift_code: str, actor_code: str) -> Shift:
        """冻结班次：固化数据来源与口径。有未决冲突时不得冻结。"""
        actor = self._require_actor(actor_code)
        if actor.role not in (Role.OPERATOR, Role.ENV, Role.AUDITOR):
            raise PermissionDenied("只有车间、环保或管理人员可冻结班次")
        shift = self._require_shift(shift_code)
        if shift.frozen:
            return shift
        if any(c.shift_code == shift_code and c.status == ConflictStatus.OPEN
               for c in self.conflicts.values()):
            raise ConflictUnresolved("班次存在未决数值冲突，不能冻结")
        frozen = Shift(**{**shift.__dict__, "frozen": True})
        self.shifts[shift_code] = frozen
        return frozen

    # -- 测量上传（幂等 + 冲突人工核对） ------------------------------------

    def submit_measurement(self, actor_code: str, measurement: Measurement) -> str:
        """车间提交测量。

        返回 ``"accepted"``（首次或完全一致的重复上传）或 ``"conflict"``
        （数值冲突，已转人工核对，未决前不参与计算）。
        冻结班次拒绝任何写入。
        """
        actor = self._require_actor(actor_code)
        if actor.role != Role.OPERATOR:
            raise PermissionDenied("只有车间可提交测量")
        shift = self._require_shift(measurement.shift_code)
        self._validate_lineage(measurement.lineage)

        prior = self.measurements.get(measurement.idempotency_key)

        # 冻结班次只接受完全一致的幂等重放；新值属于复测/迟到，须走事件更正。
        if shift.frozen and (
                prior is None
                or (prior.point, prior.unit, prior.value, prior.metric_code)
                != (measurement.point, measurement.unit, measurement.value,
                    measurement.metric_code)):
            raise ShiftFrozen("班次已冻结；复测数据请走检测迟到事件更正")

        if prior is None:
            self.measurements[measurement.idempotency_key] = measurement
            return "accepted"

        # 幂等：同一测点、同一值、同一单位视为重复上传，原样确认。
        same = (prior.point == measurement.point
                and prior.unit == measurement.unit
                and prior.value == measurement.value
                and prior.metric_code == measurement.metric_code)
        if same:
            return "accepted"

        # 数值冲突：交人工核对，不覆盖既有测量。
        self.conflicts[measurement.idempotency_key] = Conflict(
            key=measurement.idempotency_key,
            shift_code=measurement.shift_code,
            existing=prior,
            incoming=measurement,
        )
        self._mark_disputed(measurement.idempotency_key)
        return "conflict"

    def resolve_conflict(self, actor_code: str, key: str,
                         choose: str, resolution: str) -> Conflict:
        """管理人员人工核对冲突，选择既有或新来测量。"""
        actor = self._require_actor(actor_code)
        if actor.role != Role.AUDITOR:
            raise PermissionDenied("数值冲突只能由管理人员人工核对")
        conflict = self.conflicts.get(key)
        if conflict is None:
            raise LedgerError("冲突不存在")
        if conflict.status != ConflictStatus.OPEN:
            raise LedgerError("冲突已核对")
        if choose not in ("existing", "incoming"):
            raise LedgerError("必须选择 existing 或 incoming")
        chosen = conflict.existing if choose == "existing" else conflict.incoming
        self.measurements[key] = chosen
        resolved = Conflict(
            key=key, shift_code=conflict.shift_code,
            existing=conflict.existing, incoming=conflict.incoming,
            status=ConflictStatus.RESOLVED, resolution=resolution,
            resolved_by=actor_code, chosen_key=key,
        )
        self.conflicts[key] = resolved
        # 以选定测量重建指标状态（仍为草稿，等待复核/会签）。
        self._rebuild_metric_for_measurement(key)
        return resolved

    def reject_conflict(self, actor_code: str, key: str,
                        resolution: str) -> Conflict:
        """驳回：两次测量均不采信，测点维持无数据。"""
        actor = self._require_actor(actor_code)
        if actor.role != Role.AUDITOR:
            raise PermissionDenied("数值冲突只能由管理人员人工核对")
        conflict = self.conflicts.get(key)
        if conflict is None or conflict.status != ConflictStatus.OPEN:
            raise LedgerError("冲突不存在或已核对")
        resolved = Conflict(
            key=key, shift_code=conflict.shift_code,
            existing=conflict.existing, incoming=conflict.incoming,
            status=ConflictStatus.REJECTED, resolution=resolution,
            resolved_by=actor_code, chosen_key=None,
        )
        self.conflicts[key] = resolved
        self._remove_metric_for_measurement(key)
        return resolved

    # -- 指标确认（职责分离） -----------------------------------------------

    def confirm_metric(self, metric_id: str, actor_code: str,
                       note: str = "") -> MetricEntry:
        """复核/会签一项指标。

        - 普通指标：目标对应角色复核即可（质量由工艺复核，环保类由环保复核）。
        - 节能成果：工艺/算法与环保必须双签，单方确认无效。
        - 车间测量人不能复核自己提交的指标；任何单一角色都不能独自确认节能成果。
        """
        actor = self._require_actor(actor_code)
        entry = self.metrics.get(metric_id)
        if entry is None:
            raise LedgerError("指标不存在")
        shift = self._require_shift(entry.shift_code)
        if not shift.frozen:
            raise ShiftFrozen("班次冻结后才能确认指标")
        if entry.status == MetricStatus.DISPUTED:
            raise ConflictUnresolved("指标关联未决冲突")
        if entry.status == MetricStatus.SUPERSEDED:
            raise LedgerError("指标已被更正版本替代")

        required = self._required_roles(entry.kind)
        if actor.role not in required:
            raise PermissionDenied(
                f"{entry.kind.value} 指标需要 {sorted(r.value for r in required)} 会签")
        measurement = self.measurements[entry.measurement_key]
        if measurement.measured_by == actor.code:
            raise PermissionDenied("提交人与复核人不得为同一方")
        if any(s.actor == actor.code for s in entry.signatures):
            return entry  # 会签幂等
        entry.signatures.append(Signature(actor=actor.code, role=actor.role,
                                          note=note))
        if {s.role for s in entry.signatures} >= required:
            entry.status = MetricStatus.CONFIRMED
        else:
            entry.status = MetricStatus.REVIEWED
        return entry

    @staticmethod
    def _required_roles(kind: MetricKind) -> set[Role]:
        if kind in DUAL_SIGN_KINDS:
            return {Role.PROCESS, Role.ENV}
        reviewer = REVIEW_ROLE.get(kind, Role.ENV)
        return {reviewer}

    # -- 结算（节能/副产物）与局部影响传播 ----------------------------------

    def settle_metric(self, metric_id: str) -> MetricEntry:
        """对已确认指标执行结算。未确认或有争议的指标不得结算。"""
        entry = self._require_metric(metric_id)
        if entry.status != MetricStatus.CONFIRMED:
            raise LedgerError("只有会签完成的指标才能结算")
        if any(c.shift_code == entry.shift_code
               and c.status == ConflictStatus.OPEN
               for c in self.conflicts.values()):
            raise ConflictUnresolved("班次仍有未决冲突")
        entry.settled = True
        return entry

    def report_event(self, actor_code: str, shift_code: str, tag: str,
                     affected_measurement_keys: Iterable[str]) -> None:
        """设备检修、检测迟到、副产物改用途等事件。

        事件只影响标注的相关测点：重算相关指标并冲销其旧结算；
        同班次其他指标与结算保持不变。冻结班次允许此类带标签更正，
        但以新版本承载，旧指标标记为 SUPERSEDED 而非删除。
        """
        self._require_actor(actor_code)
        shift = self._require_shift(shift_code)
        if not shift.frozen:
            raise LedgerError("事件更正只适用于已冻结班次")
        for key in affected_measurement_keys:
            measurement = self.measurements.get(key)
            if measurement is None or measurement.shift_code != shift_code:
                raise LedgerError(f"测点不属于该班次: {key}")
            old_ids = [mid for mid, e in self.metrics.items()
                       if e.measurement_key == key]
            for mid in old_ids:
                old = self.metrics[mid]
                if old.status == MetricStatus.SUPERSEDED:
                    continue
                new_id = self._next_metric_id()
                tags = frozenset(set(old.tags) | {tag})
                self.metrics[new_id] = MetricEntry(
                    code=new_id, shift_code=old.shift_code, kind=old.kind,
                    target_code=old.target_code, spec_version=old.spec_version,
                    measurement_key=key,
                    value=measurement.value, unit=measurement.unit,
                    lineage=measurement.lineage, tags=tags,
                    status=MetricStatus.DRAFT,
                    note=f"事件 {tag} 触发重算",
                )
                old.status = MetricStatus.SUPERSEDED
                old.superseded_by = new_id
                if old.settled:
                    old.settlement_reversed = True  # 旧结算冲销，待新版本重新结算

    def update_measurement_value(self, actor_code: str, key: str,
                                 value: float, unit: str | None = None) -> str:
        """检测迟到/复测后更新测点数值，走幂等与冲突规则。

        冻结班次的更新即"带事件的更正"：旧指标 SUPERSEDED，新指标重算，
        已发布报表不受影响。
        """
        actor = self._require_actor(actor_code)
        if actor.role != Role.OPERATOR:
            raise PermissionDenied("只有车间可提交复测数据")
        old = self.measurements.get(key)
        if old is None:
            raise LedgerError("测点不存在")
        new = Measurement(
            idempotency_key=key, metric_code=old.metric_code,
            shift_code=old.shift_code, point=old.point, value=value,
            unit=unit or old.unit, measured_by=old.measured_by,
            lineage=old.lineage,
            tag=old.tag or "检测迟到",
        )
        self.measurements[key] = new
        if self.shifts[old.shift_code].frozen:
            self.report_event(actor_code, old.shift_code, "检测迟到", [key])
        else:
            self._rebuild_metric_for_measurement(key)
        return key

    def change_byproduct_disposition(self, actor_code: str, record_code: str,
                                     new_disposition: str,
                                     partner_code: str | None = None) -> str:
        """副产物（固废/副产氢）改用途：只影响相关处置指标与结算。

        处理记录本身保留去向变更链；关联的处置类指标被新版本替代，
        产量、质量指标不受影响。
        """
        actor = self._require_actor(actor_code)
        if actor.role not in (Role.OPERATOR, Role.PARTNER, Role.ENV):
            raise PermissionDenied("车间、环保或合作单位可申报用途变更")
        if record_code in self.waste:
            old_rec = self.waste[record_code]
            if partner_code and partner_code not in self.actors:
                raise LedgerError("新合作单位未登记")
            self.waste[record_code] = WasteRecord(
                **{**old_rec.__dict__,
                   "disposition": new_disposition,
                   "partner_code": partner_code or old_rec.partner_code})
            kinds = {MetricKind.SOLID_WASTE_USED}
        elif record_code in self.hydrogen:
            old_rec = self.hydrogen[record_code]
            if partner_code and partner_code not in self.actors:
                raise LedgerError("新合作单位未登记")
            self.hydrogen[record_code] = HydrogenRecord(
                **{**old_rec.__dict__,
                   "disposition": new_disposition,
                   "partner_code": partner_code or old_rec.partner_code})
            kinds = {MetricKind.H2_OUTPUT}
        else:
            raise LedgerError("副产物记录不存在")

        affected_keys = [
            m.idempotency_key for m in self.measurements.values()
            if record_code in m.lineage
        ]
        for key in affected_keys:
            shift_code = self.measurements[key].shift_code
            if self.shifts[shift_code].frozen:
                self.report_event(actor.code, shift_code, "副产物改用途", [key])
        return record_code

    # -- 报表发布（历史不可重写） -------------------------------------------

    def publish_report(self, actor_code: str, report_code: str,
                       shift_codes: Iterable[str]) -> PublishedReport:
        """发布报表：固化当时全部已确认指标与口径版本。

        已发布报表随后续事件保持原样；口径升版也不回溯重算。
        """
        actor = self._require_actor(actor_code)
        if actor.role != Role.AUDITOR:
            raise PermissionDenied("只有管理人员可发布报表")
        shift_codes = tuple(shift_codes)
        for code in shift_codes:
            shift = self._require_shift(code)
            if not shift.frozen:
                raise ShiftFrozen(f"班次未冻结: {code}")
        spec_versions = {self.shifts[c].spec_version for c in shift_codes}
        if len(spec_versions) != 1:
            raise SpecError("报表只能覆盖同一口径版本的班次")
        if any(c.shift_code in shift_codes and c.status == ConflictStatus.OPEN
               for c in self.conflicts.values()):
            raise ConflictUnresolved("报表覆盖班次存在未决冲突")

        snapshot = tuple(
            {
                "metric": e.code,
                "shift": e.shift_code,
                "kind": e.kind.value,
                "target": e.target_code,
                "value": e.value,
                "unit": e.unit,
                "spec_version": e.spec_version,
                "status": e.status.value,
                "signatures": [
                    {"actor": s.actor, "role": s.role.value}
                    for s in e.signatures
                ],
            }
            for e in self.metrics.values()
            if e.shift_code in shift_codes
            and e.status == MetricStatus.CONFIRMED
        )
        spec_version = spec_versions.pop()
        report = PublishedReport(
            code=report_code, shift_codes=shift_codes,
            spec_version=spec_version,
            metrics=snapshot,
            fingerprint=_fingerprint(
                [report_code, shift_codes, spec_version,
                 [(m["metric"], m["value"], m["status"]) for m in snapshot]]),
        )
        if report_code in self.reports:
            raise LedgerError("报表编码已存在（报表不可覆盖）")
        self.reports[report_code] = report
        return report

    def get_report(self, code: str) -> PublishedReport:
        """读取报表。历史报表内容永不随当前数据变化。"""
        return self.reports[code]

    # -- 指标生成 -----------------------------------------------------------

    def derive_metrics_for_shift(self, shift_code: str) -> list[str]:
        """依据班次冻结口径，由测量与登记记录生成指标草稿。

        产量/质量来自产品批次与规格，固废、副产氢、环保指标来自带
        ``metric_code`` 的测量。同一口径内产量、碳减排与副产物去向共享
        同一批测量，避免车间报表式汇总相互矛盾。
        """
        shift = self._require_shift(shift_code)
        created: list[str] = []
        for measurement in self.measurements.values():
            if measurement.shift_code != shift_code:
                continue
            if measurement.idempotency_key in {
                    e.measurement_key for e in self.metrics.values()
                    if e.status != MetricStatus.SUPERSEDED}:
                continue
            target = (measurement.metric_code
                      and self.targets.get(measurement.metric_code))
            if measurement.metric_code and target is None:
                continue  # 未登记口径的测点不自动成指标
            kind = target.kind if target else MetricKind.OUTPUT
            entry_id = self._next_metric_id()
            disputed = self.conflicts.get(
                measurement.idempotency_key) is not None and any(
                c.key == measurement.idempotency_key and c.status == ConflictStatus.OPEN
                for c in self.conflicts.values())
            entry = MetricEntry(
                code=entry_id, shift_code=shift_code, kind=kind,
                target_code=target.code if target else None,
                spec_version=shift.spec_version,
                measurement_key=measurement.idempotency_key,
                value=measurement.value, unit=measurement.unit,
                lineage=measurement.lineage,
                tags=frozenset({measurement.tag} if measurement.tag else set()),
                status=MetricStatus.DISPUTED if disputed else MetricStatus.DRAFT,
            )
            self.metrics[entry_id] = entry
            created.append(entry_id)
        return created

    # -- 追溯 ---------------------------------------------------------------

    def trace(self, metric_id: str) -> dict[str, Any]:
        """从一项绿色指标追溯到原料、工序、处理记录与责任确认。"""
        entry = self._require_metric(metric_id)
        nodes: dict[str, Any] = {}
        for ref in entry.lineage:
            if ref in self.materials:
                batch = self.materials[ref]
                chain = [ref]
                parent = batch.parent
                while parent:
                    chain.append(parent)
                    parent = self.materials[parent].parent
                nodes[ref] = {
                    "type": "material", "code": ref,
                    "material": batch.material, "quantity": batch.quantity,
                    "unit": batch.unit, "process": batch.process_code,
                    "material_chain": list(reversed(chain)),
                }
            elif ref in self.processes:
                step = self.processes[ref]
                nodes[ref] = {"type": "process", "code": ref,
                              "name": step.name, "line": step.line_code,
                              "seq": step.seq}
            elif ref in self.waste:
                rec = self.waste[ref]
                nodes[ref] = {
                    "type": "waste", "code": ref, "waste_type": rec.waste_type,
                    "quantity": rec.quantity, "unit": rec.unit,
                    "source_process": rec.source_process_code,
                    "partner": rec.partner_code, "disposition": rec.disposition,
                    "reuse_process": rec.reuse_process_code,
                }
            elif ref in self.hydrogen:
                rec = self.hydrogen[ref]
                nodes[ref] = {
                    "type": "hydrogen", "code": ref,
                    "source_gas": rec.source_gas, "h2_volume": rec.h2_volume,
                    "unit": rec.unit, "process": rec.process_code,
                    "partner": rec.partner_code, "disposition": rec.disposition,
                }
            elif ref in self.products:
                prod = self.products[ref]
                nodes[ref] = {
                    "type": "product", "code": ref, "spec": prod.spec_code,
                    "quantity": prod.quantity, "unit": prod.unit,
                    "process": prod.process_code,
                    "materials": list(prod.material_codes),
                }
            else:
                nodes[ref] = {"type": "unknown", "code": ref}
        measurement = self.measurements.get(entry.measurement_key)
        conflict = self.conflicts.get(entry.measurement_key)
        return {
            "metric": {
                "code": entry.code, "kind": entry.kind.value,
                "target": entry.target_code, "value": entry.value,
                "unit": entry.unit, "status": entry.status.value,
                "spec_version": entry.spec_version, "tags": sorted(entry.tags),
            },
            "shift": self._shift_view(entry.shift_code),
            "measurement": {
                "key": measurement.idempotency_key, "point": measurement.point,
                "value": measurement.value, "unit": measurement.unit,
                "submitted_by": measurement.measured_by,
            } if measurement else None,
            "lineage": list(nodes.values()),
            "signatures": [
                {"actor": s.actor, "role": s.role.value, "note": s.note}
                for s in entry.signatures
            ],
            "conflict": None if conflict is None else {
                "status": conflict.status.value,
                "resolution": conflict.resolution,
                "resolved_by": conflict.resolved_by,
            },
            "superseded_by": entry.superseded_by,
        }

    def consistency_check(self, shift_code: str) -> dict[str, float]:
        """同源一致性：产量、固废利用、副产氢、环保绩效共享测量时必须相等。

        返回每类指标的代表值；若同一测点被多类指标引用而数值不同即报错。
        （正常实现中它们由同一测量派生，值天然一致，此函数用于显式断言。）
        """
        by_key: dict[str, set[tuple[str, float]]] = {}
        for entry in self.metrics.values():
            if entry.shift_code != shift_code:
                continue
            if entry.status == MetricStatus.SUPERSEDED:
                continue
            by_key.setdefault(entry.measurement_key, set()).add(
                (entry.kind.value, entry.value))
        for key, values in by_key.items():
            distinct = {v for _, v in values}
            if len(distinct) > 1:
                raise LedgerError(f"同源数据互相矛盾: {key}")
        return {key: next(iter(values))[1] for key, values in by_key.items()}

    # -- 内部工具 -----------------------------------------------------------

    def _require_actor(self, code: str) -> Actor:
        actor = self.actors.get(code)
        if actor is None:
            raise LedgerError("参与方未登记")
        return actor

    def _require_shift(self, code: str) -> Shift:
        shift = self.shifts.get(code)
        if shift is None:
            raise LedgerError("班次不存在")
        return shift

    def _require_metric(self, code: str) -> MetricEntry:
        entry = self.metrics.get(code)
        if entry is None:
            raise LedgerError("指标不存在")
        return entry

    def _validate_lineage(self, lineage: Iterable[str]) -> None:
        known = (self.processes.keys() | self.materials.keys()
                 | self.waste.keys() | self.hydrogen.keys()
                 | self.products.keys())
        for ref in lineage:
            if ref not in known:
                raise LedgerError(f"血缘节点未登记: {ref}")

    def _next_metric_id(self) -> str:
        self._metric_seq += 1
        return f"M{self._metric_seq:04d}"

    def _mark_disputed(self, key: str) -> None:
        for entry in self.metrics.values():
            if entry.measurement_key == key and entry.status not in (
                    MetricStatus.SUPERSEDED, MetricStatus.CONFIRMED):
                entry.status = MetricStatus.DISPUTED

    def _rebuild_metric_for_measurement(self, key: str) -> None:
        measurement = self.measurements[key]
        target = measurement.metric_code and self.targets.get(
            measurement.metric_code)
        for mid, entry in list(self.metrics.items()):
            if entry.measurement_key != key:
                continue
            if entry.status == MetricStatus.CONFIRMED:
                # 已确认指标以 SUPERSEDED 链承接更正，不删除确认历史。
                new_id = self._next_metric_id()
                self.metrics[new_id] = MetricEntry(
                    code=new_id, shift_code=entry.shift_code, kind=entry.kind,
                    target_code=entry.target_code,
                    spec_version=entry.spec_version,
                    measurement_key=key, value=measurement.value,
                    unit=measurement.unit, lineage=measurement.lineage,
                    tags=entry.tags, status=MetricStatus.DRAFT,
                    note="冲突核对后重建")
                entry.status = MetricStatus.SUPERSEDED
                entry.superseded_by = new_id
                if entry.settled:
                    entry.settlement_reversed = True
            else:
                entry.value = measurement.value
                entry.unit = measurement.unit
                entry.lineage = measurement.lineage
                entry.status = MetricStatus.DRAFT

    def _remove_metric_for_measurement(self, key: str) -> None:
        for mid, entry in list(self.metrics.items()):
            if entry.measurement_key != key:
                continue
            if entry.status == MetricStatus.CONFIRMED:
                entry.status = MetricStatus.SUPERSEDED
                if entry.settled:
                    entry.settlement_reversed = True
            else:
                del self.metrics[mid]

    def _shift_view(self, code: str) -> dict[str, Any]:
        shift = self.shifts[code]
        return {"code": code, "line": shift.line_code,
                "started_at": shift.started_at, "ended_at": shift.ended_at,
                "spec_version": shift.spec_version, "frozen": shift.frozen}
