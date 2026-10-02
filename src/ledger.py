"""钢铁转型数据台账服务。

一条数据链贯通：产线/工序 → 原料批次 → 班次测量与精品钢批次 →
固废/副产氢处理记录 → 指标与报表 → 合作单位结算。

关键规则：
- 班次冻结数据来源与计算口径；冻结后原始数据不得改动。
- 重复提交幂等；同班次同指标数值不一致挂冲突，交人工核对。
- 测量由车间提交，按指标类别由相应角色复核；工艺/算法人员不能确认节能成果。
- 检修、检测迟到、副产物改用途只影响相关指标与结算；已发布报表永不重写，
  口径变更只作用于此后发布的报表。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any

from .models import (
    Conflict,
    Event,
    HydrogenRecord,
    Measurement,
    ByproductDiversion,
    Partner,
    ProcessStep,
    ProductBatch,
    ProductSpec,
    MaterialBatch,
    Report,
    ProductionLine,
    Settlement,
    Shift,
    WasteRecord,
    new_id,
    validate_id,
    validate_role,
    validate_metric,
)
from .metrics import (
    DEFAULT_CALIBER,
    DERIVED_CODES,
    INITIAL_CALIBER_VERSION,
    METRICS,
    ROLE_ENERGY,
    ROLE_ENV,
    ROLE_MANAGER,
    ROLE_OPERATOR,
    required_reviewers,
)
from .store import EventStore

MEASUREMENT_ACTIVE = {"pending", "confirmed"}


def _confirm_key(actor: str, role: str) -> str:
    return f"{actor}@{role}"


class LedgerError(ValueError):
    """业务规则被违反。"""


class GreenSteelLedger:
    def __init__(self, store: EventStore | str):
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        self._init_state()
        for event in self.store.replay():
            self._apply(event)

    def _init_state(self) -> None:
        self.lines: dict[str, ProductionLine] = {}
        self.processes: dict[str, ProcessStep] = {}
        self.batches: dict[str, MaterialBatch] = {}
        self.specs: dict[str, ProductSpec] = {}
        self.product_batches: dict[str, ProductBatch] = {}
        self.partners: dict[str, Partner] = {}
        self.shifts: dict[str, Shift] = {}
        self.measurements: dict[str, Measurement] = {}
        self.wastes: dict[str, WasteRecord] = {}
        self.hydrogens: dict[str, HydrogenRecord] = {}
        self.conflicts: dict[str, Conflict] = {}
        self.reports: dict[str, Report] = {}
        self.settlements: dict[tuple[str, str, str], Settlement] = {}
        self.idem: dict[str, str] = {}
        self.calibers: dict[int, dict[str, Any]] = {
            INITIAL_CALIBER_VERSION: copy.deepcopy(DEFAULT_CALIBER)
        }
        self.current_caliber = INITIAL_CALIBER_VERSION
        self.maintenance: dict[str, list[dict[str, Any]]] = {}
        self.late_inspections: dict[tuple[str, str], dict[str, Any]] = {}

    # ============================================================== 基础资料

    def register_line(self, actor: str, role: str, line_id: str, name: str) -> str:
        validate_role(role)
        validate_id(line_id, "产线")
        if line_id in self.lines:
            raise LedgerError("产线已存在")
        self._emit("production_line_registered", {"id": line_id, "name": name}, actor)
        return line_id

    def register_process(self, actor, role, process_id, line_id, name, order) -> str:
        validate_role(role)
        validate_id(process_id, "工序")
        if line_id not in self.lines:
            raise LedgerError("产线不存在")
        if process_id in self.processes:
            raise LedgerError("工序已存在")
        self._emit(
            "process_registered",
            {"id": process_id, "line_id": line_id, "name": name, "order": order},
            actor,
        )
        return process_id

    def register_material_batch(
        self, actor, role, batch_id, name, supplier, amount, unit, process_id
    ) -> str:
        validate_role(role)
        validate_id(batch_id, "原料批次")
        if process_id not in self.processes:
            raise LedgerError("工序不存在")
        if amount <= 0:
            raise LedgerError("原料数量必须为正")
        self._emit(
            "material_batch_registered",
            {
                "id": batch_id,
                "name": name,
                "supplier": supplier,
                "amount": amount,
                "unit": unit,
                "process_id": process_id,
            },
            actor,
        )
        return batch_id

    def register_product_spec(self, actor, role, spec_id, grade, standard) -> str:
        validate_role(role)
        validate_id(spec_id, "精品钢规格")
        if spec_id in self.specs:
            raise LedgerError("精品钢规格已存在")
        self._emit(
            "product_spec_registered",
            {"id": spec_id, "grade": grade, "standard": standard},
            actor,
        )
        return spec_id

    def register_partner(self, actor, role, partner_id, name, kind) -> str:
        validate_role(role)
        validate_id(partner_id, "合作单位")
        if kind not in ("waste", "hydrogen", "inspection"):
            raise LedgerError("合作单位类别无效")
        if partner_id in self.partners:
            raise LedgerError("合作单位已存在")
        self._emit(
            "partner_registered",
            {"id": partner_id, "name": name, "kind": kind},
            actor,
        )
        return partner_id

    # ============================================================== 班次

    def open_shift(self, actor, role, shift_id, line_id, date, shift_no) -> str:
        validate_role(role)
        validate_id(shift_id, "班次")
        if line_id not in self.lines:
            raise LedgerError("产线不存在")
        if shift_id in self.shifts:
            raise LedgerError("班次已存在")
        if shift_no not in (1, 2, 3):
            raise LedgerError("班次号必须为 1/2/3")
        self._emit(
            "shift_opened",
            {
                "id": shift_id,
                "line_id": line_id,
                "date": date,
                "shift_no": shift_no,
                "opened_by": actor,
            },
            actor,
        )
        return shift_id

    def freeze_shift(self, actor: str, role: str, shift_id: str) -> None:
        """冻结班次：固化数据来源集合与当时口径版本。"""
        self._require_role(role, ROLE_MANAGER)
        shift = self._shift(shift_id)
        if shift.frozen:
            raise LedgerError("班次已冻结")
        open_conflicts = [
            c.id for c in self.conflicts.values()
            if c.shift_id == shift_id and not c.resolved
        ]
        if open_conflicts:
            raise LedgerError(f"仍有数值冲突未完成人工核对: {open_conflicts}")
        # pending = 待复核；冲突落败者保持 conflict 但已作废，不阻塞冻结
        blocked = [
            m.id
            for m in self.measurements.values()
            if m.shift_id == shift_id and m.status == "pending"
        ]
        if blocked:
            raise LedgerError(f"仍有测量未完成复核: {blocked}")
        blocked_records = [
            r.id
            for r in list(self.wastes.values()) + list(self.hydrogens.values())
            if r.shift_id == shift_id and r.status != "confirmed"
        ]
        if blocked_records:
            raise LedgerError(f"仍有处理记录未完成复核: {blocked_records}")
        if self._active_measurement(shift_id, "CRUDE_STEEL_OUTPUT") is None:
            raise LedgerError("缺少粗钢产量，不能冻结")
        pending_late = [
            code for (sid, code) in self.late_inspections if sid == shift_id
        ]
        if pending_late:
            raise LedgerError(f"检测迟到已登记但测量未补录: {pending_late}")
        self._emit(
            "shift_frozen",
            {"id": shift_id, "caliber_version": self.current_caliber},
            actor,
        )

    # ============================================================== 测量

    def submit_measurement(
        self,
        actor: str,
        role: str,
        shift_id: str,
        metric_code: str,
        value: float,
        idempotency_key: str,
        *,
        late: bool = False,
    ) -> str:
        self._require_role(role, ROLE_OPERATOR)
        shift = self._shift(shift_id)
        if shift.frozen:
            raise LedgerError("班次已冻结，不能再提交测量")
        validate_metric(metric_code)
        if metric_code in DERIVED_CODES:
            raise LedgerError(f"{metric_code} 为衍生指标，由报表口径计算")
        if value < 0:
            raise LedgerError("测量值不能为负")
        unit = METRICS[metric_code][1]

        # 幂等：同一幂等键返回既有测量，内容不一致则拒绝
        if idempotency_key in self.idem:
            existing = self.measurements.get(self.idem[idempotency_key])
            if (
                existing is None
                or existing.shift_id != shift_id
                or existing.metric_code != metric_code
                or existing.value != value
            ):
                raise LedgerError("幂等键已使用但提交内容不一致")
            return existing.id

        # 内容重复（换键重复上传）同样幂等
        twin = self._active_measurement(shift_id, metric_code)
        if twin is not None and twin.value == value:
            self.idem[idempotency_key] = twin.id
            return twin.id

        # 同指标存在未解决冲突时，必须先人工核对，不接受第三个值
        if twin is None and self._open_conflict(shift_id, metric_code) is not None:
            raise LedgerError("该指标冲突尚未核对，请等待人工处理")

        late_ref = None
        if late:
            late_ref = self.late_inspections.pop((shift_id, metric_code), None)
            if late_ref is None:
                raise LedgerError("该指标没有可使用的检测迟到登记")

        mid = new_id("meas")
        self._emit(
            "measurement_submitted",
            {
                "id": mid,
                "shift_id": shift_id,
                "metric_code": metric_code,
                "value": value,
                "unit": unit,
                "submitted_by": actor,
                "idempotency_key": idempotency_key,
                "late_inspection": late_ref["id"] if late_ref else None,
            },
            actor,
        )

        # 与当前有效值数值不一致 → 双方挂冲突，交人工核对
        prior = twin
        if prior is not None and prior.value != value:
            cid = new_id("conf")
            self._emit(
                "conflict_raised",
                {
                    "id": cid,
                    "shift_id": shift_id,
                    "metric_code": metric_code,
                    "candidates": [prior.id, mid],
                    "values": [prior.value, value],
                    "raised_by": actor,
                },
                actor,
            )
        return mid

    def confirm_measurement(self, actor: str, role: str, measurement_id: str) -> None:
        validate_role(role)
        m = self._measurement(measurement_id)
        if self.shifts[m.shift_id].frozen:
            raise LedgerError("班次已冻结，复核须在冻结前完成")
        if m.status not in ("pending", "confirmed"):
            raise LedgerError("测量处于冲突或作废状态，不能复核")
        required = required_reviewers(m.metric_code)
        if role not in required:
            raise LedgerError(
                f"{m.metric_code} 需要 {'/'.join(required)} 复核，{role}无权确认"
            )
        if actor == m.submitted_by:
            raise LedgerError("提交人不能复核本人提交的数据")
        key = _confirm_key(actor, role)
        if key in m.confirmed_by:
            return  # 复核幂等
        self._emit(
            "measurement_confirmed",
            {"id": measurement_id, "role": role},
            actor,
        )

    def resolve_conflict(self, actor, role, conflict_id, winning_measurement_id, note=""):
        """人工核对冲突：选定值回到待复核并重新走完整复核，其余候选作废。"""
        self._require_role(role, ROLE_MANAGER)
        conflict = self.conflicts.get(conflict_id)
        if conflict is None:
            raise LedgerError("冲突单不存在")
        if conflict.resolved:
            raise LedgerError("冲突已核对")
        if winning_measurement_id not in conflict.candidates:
            raise LedgerError("选定值不在候选测量中")
        self._emit(
            "conflict_resolved",
            {
                "id": conflict_id,
                "winning_measurement_id": winning_measurement_id,
                "note": note,
            },
            actor,
        )

    # ============================================================== 精品钢批次

    def log_product_batch(
        self, actor, role, batch_id, shift_id, spec_id, amount, passed
    ) -> str:
        validate_role(role)
        validate_id(batch_id, "产品批次")
        shift = self._shift(shift_id)
        if shift.frozen:
            raise LedgerError("班次已冻结，不能再登记产品批次")
        if spec_id not in self.specs:
            raise LedgerError("精品钢规格不存在")
        if amount <= 0:
            raise LedgerError("批次量必须为正")
        self._emit(
            "product_batch_logged",
            {
                "id": batch_id,
                "shift_id": shift_id,
                "spec_id": spec_id,
                "amount": amount,
                "passed": bool(passed),
            },
            actor,
        )
        return batch_id

    # ============================================================== 固废 / 制氢

    def submit_waste_record(
        self,
        actor,
        role,
        record_id,
        shift_id,
        waste_type,
        amount,
        process_id,
        material_batch_ids,
        partner_id,
        destination,
        utilized,
        idempotency_key,
    ) -> str:
        self._require_role(role, ROLE_OPERATOR)
        shift = self._shift(shift_id)
        if shift.frozen:
            raise LedgerError("班次已冻结，不能再提交处理记录")
        if process_id not in self.processes:
            raise LedgerError("工序不存在")
        if partner_id not in self.partners:
            raise LedgerError("合作单位不存在")
        for bid in material_batch_ids:
            if bid not in self.batches:
                raise LedgerError(f"原料批次不存在: {bid}")
        if amount <= 0:
            raise LedgerError("固废量必须为正")
        if idempotency_key in self.idem:
            rid = self.idem[idempotency_key]
            if rid in self.wastes:
                return rid
            raise LedgerError("幂等键已用于其他类型记录")
        validate_id(record_id, "固废记录")
        self._emit(
            "waste_record_submitted",
            {
                "id": record_id,
                "shift_id": shift_id,
                "waste_type": waste_type,
                "amount": amount,
                "process_id": process_id,
                "material_batch_ids": list(material_batch_ids),
                "partner_id": partner_id,
                "destination": destination,
                "utilized": bool(utilized),
                "submitted_by": actor,
                "idempotency_key": idempotency_key,
            },
            actor,
        )
        return record_id

    def submit_hydrogen_record(
        self,
        actor,
        role,
        record_id,
        shift_id,
        source_gas_batch_id,
        volume_nm3,
        process_id,
        partner_id,
        use,
        idempotency_key,
    ) -> str:
        self._require_role(role, ROLE_OPERATOR)
        shift = self._shift(shift_id)
        if shift.frozen:
            raise LedgerError("班次已冻结，不能再提交处理记录")
        if process_id not in self.processes or partner_id not in self.partners:
            raise LedgerError("工序或合作单位不存在")
        if source_gas_batch_id not in self.batches:
            raise LedgerError("焦炉煤气来源批次不存在")
        if volume_nm3 <= 0:
            raise LedgerError("副产氢产量必须为正")
        if idempotency_key in self.idem:
            rid = self.idem[idempotency_key]
            if rid in self.hydrogens:
                return rid
            raise LedgerError("幂等键已用于其他类型记录")
        validate_id(record_id, "制氢记录")
        self._emit(
            "hydrogen_record_submitted",
            {
                "id": record_id,
                "shift_id": shift_id,
                "source_gas_batch_id": source_gas_batch_id,
                "volume_nm3": volume_nm3,
                "process_id": process_id,
                "partner_id": partner_id,
                "use": use,
                "submitted_by": actor,
                "idempotency_key": idempotency_key,
            },
            actor,
        )
        return record_id

    def confirm_record(self, actor: str, role: str, record_id: str) -> None:
        record, kind = self._find_record(record_id)
        if self.shifts[record.shift_id].frozen:
            raise LedgerError("班次已冻结，复核须在冻结前完成")
        required_role = ROLE_ENV if kind == "waste" else ROLE_ENERGY
        self._require_role(role, required_role)
        if actor == record.submitted_by:
            raise LedgerError("提交人不能复核本人提交的记录")
        key = _confirm_key(actor, role)
        if key in record.confirmed_by:
            return
        self._emit(
            "record_confirmed",
            {"id": record_id, "kind": kind, "role": role},
            actor,
        )

    def divert_byproduct(
        self, actor, role, record_id, new_destination, reason, new_utilized=None
    ) -> str:
        """副产物改用途：只触及相关衍生指标与结算，原始记录和已发布报表不动。"""
        validate_role(role)
        record, kind = self._find_record(record_id)
        required_role = ROLE_ENV if kind == "waste" else ROLE_ENERGY
        if role not in (required_role, ROLE_MANAGER):
            raise LedgerError(f"{kind} 改用途须由 {required_role} 或管理人员登记")
        if not new_destination or not reason:
            raise LedgerError("新去向与原因不能为空")
        did = new_id("div")
        self._emit(
            "byproduct_diversion_logged",
            {
                "id": did,
                "record_id": record_id,
                "kind": kind,
                "new_destination": new_destination,
                "new_utilized": new_utilized,
                "reason": reason,
            },
            actor,
        )
        return did

    # ============================================================== 异常工况

    def log_maintenance(self, actor, role, shift_id, process_id, note, affects_codes):
        """登记设备检修：仅在相关指标快照上打标，不影响其他指标。"""
        validate_role(role)
        self._shift(shift_id)
        if process_id not in self.processes:
            raise LedgerError("工序不存在")
        for code in affects_codes:
            validate_metric(code)
        mid = new_id("maint")
        self._emit(
            "maintenance_logged",
            {
                "id": mid,
                "shift_id": shift_id,
                "process_id": process_id,
                "note": note,
                "affects_codes": list(affects_codes),
            },
            actor,
        )
        return mid

    def log_late_inspection(self, actor, role, shift_id, metric_code, reason):
        """登记检测迟到：对应测量需以迟到补录方式提交，且仅影响该指标。"""
        validate_role(role)
        self._shift(shift_id)
        validate_metric(metric_code)
        if metric_code in DERIVED_CODES:
            raise LedgerError("衍生指标不存在检测迟到")
        if (shift_id, metric_code) in self.late_inspections:
            raise LedgerError("该指标已登记检测迟到")
        lid = new_id("late")
        self._emit(
            "late_inspection_logged",
            {"id": lid, "shift_id": shift_id, "metric_code": metric_code, "reason": reason},
            actor,
        )
        return lid

    # ============================================================== 口径与报表

    def publish_caliber(self, actor: str, role: str, changes: dict[str, dict]) -> int:
        """发布新口径版本（版本递增）；不影响任何已发布报表。"""
        self._require_role(role, ROLE_MANAGER)
        for code in changes:
            validate_metric(code)
        new_version = self.current_caliber + 1
        snapshot = copy.deepcopy(self.calibers[self.current_caliber])
        snapshot.update(copy.deepcopy(changes))
        self._emit(
            "caliber_published",
            {"version": new_version, "caliber": snapshot},
            actor,
        )
        return new_version

    def current_snapshot(self, shift_id: str) -> dict[str, Any]:
        """未发布前按当前口径的试算视图；班次冻结后按冻结口径呈现。"""
        shift = self._shift(shift_id)
        version = shift.caliber_version or self.current_caliber
        return self._build_snapshot(shift, version)

    def publish_report(self, actor: str, role: str, shift_id: str) -> str:
        """按班次冻结时的口径版本发布报表；报表一经发布不可修改。"""
        self._require_role(role, ROLE_MANAGER)
        shift = self._shift(shift_id)
        if not shift.frozen:
            raise LedgerError("班次未冻结，不能发布报表")
        snapshot = self._build_snapshot(shift, shift.caliber_version)
        rid = new_id("rpt")
        self._emit(
            "report_published",
            {"id": rid, **snapshot, "published_by": actor},
            actor,
        )
        return rid

    def get_report(self, report_id: str) -> Report:
        report = self.reports.get(report_id)
        if report is None:
            raise LedgerError("报表不存在")
        return report

    def reports_for_shift(self, shift_id: str) -> list[Report]:
        return [
            r for r in sorted(self.reports.values(), key=lambda x: x.published_at)
            if r.shift_id == shift_id
        ]

    def settlements_for_shift(self, shift_id: str) -> list[Settlement]:
        return [
            s
            for s in sorted(self.settlements.values(), key=lambda x: (x.record_id, x.report_id or ""))
            if s.shift_id == shift_id
        ]

    def valid_settlements_for_partner(self, partner_id: str) -> list[Settlement]:
        """合作单位当前有效结算：每条记录取最新发布报表对应的版本。"""
        latest: dict[str, Settlement] = {}
        for s in self.settlements.values():
            if s.partner_id != partner_id or not s.valid:
                continue
            cur = latest.get(s.record_id)
            cur_ts = self.reports[cur.report_id].published_at if cur and cur.report_id else 0.0
            new_ts = self.reports[s.report_id].published_at if s.report_id else 0.0
            if cur is None or new_ts >= cur_ts:
                latest[s.record_id] = s
        return list(latest.values())

    # ============================================================== 追溯

    def trace(self, shift_id: str, metric_code: str) -> dict[str, Any]:
        """从一项指标回溯到测量/记录、班次、工序、原料批次、责任确认与报表。"""
        shift = self._shift(shift_id)
        validate_metric(metric_code)

        nodes: dict[str, dict[str, str]] = {}
        edges: list[dict[str, str]] = []
        confirmations: list[dict[str, str]] = []

        def node(kind: str, nid: str, label: str) -> None:
            nodes[nid] = {"kind": kind, "label": label}

        def edge(src: str, dst: str, rel: str) -> None:
            edges.append({"from": src, "to": dst, "rel": rel})

        line = self.lines[shift.line_id]
        metric_node = f"metric:{metric_code}"
        node("metric", metric_node, METRICS[metric_code][0])
        node("shift", shift.id, f"{shift.date} 第{shift.shift_no}班（冻结口径 v{shift.caliber_version or '-'}）")
        node("line", line.id, line.name)
        edge(metric_node, shift.id, "归属班次")
        edge(shift.id, line.id, "所属产线")

        line_processes = [
            p for p in self.processes.values() if p.line_id == shift.line_id
        ]
        process_ids = {p.id for p in line_processes}
        for p in line_processes:
            node("process", p.id, p.name)
            edge(shift.id, p.id, "包含工序")
        for b in self.batches.values():
            if b.process_id in process_ids:
                node("material", b.id, f"{b.name} {b.amount}{b.unit}（{b.supplier}）")
                edge(b.id, b.process_id, "投入工序")

        if metric_code in DERIVED_CODES:
            for dep in self._dependencies(metric_code):
                node("metric", f"metric:{dep}", METRICS[dep][0])
                edge(metric_node, f"metric:{dep}", "口径计算依赖")
                for m in self.measurements.values():
                    if m.shift_id == shift_id and m.metric_code == dep:
                        self._trace_measurement(m, node, edge, confirmations)
            if metric_code == "QUALITY_PASS_RATE":
                for pb in self.product_batches.values():
                    if pb.shift_id != shift_id:
                        continue
                    node(
                        "product_batch", pb.id,
                        f"精品钢批次（{'合格' if pb.passed else '不合格'} {pb.amount}t）",
                    )
                    edge(pb.id, metric_node, "合格率来源")
                    spec = self.specs.get(pb.spec_id)
                    if spec:
                        node("spec", spec.id, f"{spec.grade} / {spec.standard}")
                        edge(pb.id, spec.id, "执行规格")
            if metric_code == "WASTE_UTIL_RATE":
                for w in self.wastes.values():
                    if w.shift_id == shift_id:
                        self._trace_waste(w, node, edge)
            if metric_code == "HYDROGEN_VOLUME":
                for h in self.hydrogens.values():
                    if h.shift_id == shift_id:
                        self._trace_hydrogen(h, node, edge)
        else:
            for m in self.measurements.values():
                if m.shift_id == shift_id and m.metric_code == metric_code:
                    self._trace_measurement(m, node, edge, confirmations)

        reports = [
            {
                "report_id": r.id,
                "caliber_version": r.caliber_version,
                "value": r.values.get(metric_code),
                "fingerprint": r.fingerprint,
                "published_at": r.published_at,
            }
            for r in self.reports_for_shift(shift_id)
            if metric_code in r.values
        ]
        return {
            "metric": metric_code,
            "shift_id": shift_id,
            "nodes": list(nodes.values()),
            "edges": edges,
            "confirmations": confirmations,
            "reports": reports,
        }

    def _trace_measurement(self, m: Measurement, node, edge, confirmations) -> None:
        node("measurement", m.id, f"{METRICS[m.metric_code][0]}={m.value}{m.unit}")
        edge(m.id, f"metric:{m.metric_code}", "测量支撑")
        node("actor", f"submitter:{m.submitted_by}", f"车间提交：{m.submitted_by}")
        edge(m.id, f"submitter:{m.submitted_by}", "提交责任")
        for ck in m.confirmed_by:
            actor_name, role_name = ck.split("@", 1)
            confirmations.append(
                {"measurement_id": m.id, "actor": actor_name, "role": role_name}
            )
            node("actor", f"confirmer:{ck}", f"复核：{actor_name}（{role_name}）")
            edge(f"confirmer:{ck}", m.id, "复核确认")
        if m.conflict_id:
            node("conflict", m.conflict_id, f"数值冲突核对单（{m.status}）")
            edge(m.id, m.conflict_id, "挂账核对")

    def _trace_waste(self, w: WasteRecord, node, edge) -> None:
        label_dest = w.diversion.new_destination if w.diversion else w.destination
        node(
            "waste", w.id,
            f"{w.waste_type} {w.amount}t → {label_dest}"
            + ("（已改用途）" if w.diversion else ""),
        )
        edge(w.id, "metric:WASTE_UTIL_RATE", "处理记录")
        node("partner", w.partner_id, self.partners[w.partner_id].name)
        edge(w.id, w.partner_id, "去向合作单位")
        edge(w.id, w.process_id, "产废工序")
        for bid in w.material_batch_ids:
            edge(w.id, bid, "来源原料批次")

    def _trace_hydrogen(self, h: HydrogenRecord, node, edge) -> None:
        label_use = h.diversion.new_destination if h.diversion else h.use
        node(
            "hydrogen", h.id,
            f"副产氢 {h.volume_nm3}Nm3 → {label_use}"
            + ("（已改用途）" if h.diversion else ""),
        )
        edge(h.id, "metric:HYDROGEN_VOLUME", "制氢记录")
        node("partner", h.partner_id, self.partners[h.partner_id].name)
        edge(h.id, h.partner_id, "去向合作单位")
        edge(h.id, h.process_id, "制氢工序")
        edge(h.id, h.source_gas_batch_id, "焦炉煤气来源批次")

    # ============================================================== 内部：计算

    def _dependencies(self, code: str) -> tuple[str, ...]:
        return {
            "CO2_REDUCTION": ("ENERGY_INTENSITY", "CRUDE_STEEL_OUTPUT"),
            "WASTE_UTIL_RATE": ("WASTE_AMOUNT",),
            "QUALITY_PASS_RATE": (),
            "HYDROGEN_VOLUME": (),
        }[code]

    def _active_measurement(self, shift_id: str, metric_code: str) -> Measurement | None:
        active = [
            m for m in self.measurements.values()
            if m.shift_id == shift_id
            and m.metric_code == metric_code
            and m.status in MEASUREMENT_ACTIVE
        ]
        return active[0] if active else None

    def _open_conflict(self, shift_id: str, metric_code: str) -> Conflict | None:
        for c in self.conflicts.values():
            if (
                c.shift_id == shift_id
                and c.metric_code == metric_code
                and not c.resolved
            ):
                return c
        return None

    def _measurement(self, mid: str) -> Measurement:
        m = self.measurements.get(mid)
        if m is None:
            raise LedgerError("测量不存在")
        return m

    def _find_record(self, rid: str) -> tuple[WasteRecord | HydrogenRecord, str]:
        if rid in self.wastes:
            return self.wastes[rid], "waste"
        if rid in self.hydrogens:
            return self.hydrogens[rid], "hydrogen"
        raise LedgerError("处理记录不存在")

    def _shift(self, shift_id: str) -> Shift:
        shift = self.shifts.get(shift_id)
        if shift is None:
            raise LedgerError("班次不存在")
        return shift

    def _require_role(self, role: str, expected: str) -> None:
        validate_role(role)
        if role != expected:
            raise LedgerError(f"该操作仅允许 {expected}，当前为 {role}")

    @staticmethod
    def _waste_utilized(w: WasteRecord) -> bool:
        if w.diversion is not None and w.diversion.new_utilized is not None:
            return w.diversion.new_utilized
        return w.utilized

    def _build_snapshot(self, shift: Shift, caliber_version: int) -> dict[str, Any]:
        shift_id = shift.id
        caliber = self.calibers[caliber_version]
        values: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}
        flags: dict[str, list[str]] = {}

        maint_codes = {
            code
            for entry in self.maintenance.get(shift_id, [])
            for code in entry["affects_codes"]
        }

        winners: dict[str, Measurement] = {}
        for m in self.measurements.values():
            if m.shift_id == shift_id and m.status in MEASUREMENT_ACTIVE:
                winners[m.metric_code] = m
        for code, m in winners.items():
            values[code] = m.value
            meta[code] = {
                "kind": "raw",
                "unit": m.unit,
                "sources": [m.id],
                "status": m.status,
            }
            reasons: list[str] = []
            if code in maint_codes:
                reasons.append("设备检修期间测量")
            if m.late_inspection:
                reasons.append("检测迟到补录")
            if reasons:
                flags[code] = reasons

        # 精品钢合格率（按产品批次）
        pbs = [p for p in self.product_batches.values() if p.shift_id == shift_id]
        if pbs:
            total = sum(p.amount for p in pbs)
            passed = sum(p.amount for p in pbs if p.passed)
            values["QUALITY_PASS_RATE"] = round(passed / total * 100, 4)
            meta["QUALITY_PASS_RATE"] = {
                "kind": "derived",
                "unit": "%",
                "sources": [p.id for p in pbs],
                "formula": caliber["QUALITY_PASS_RATE"]["formula"],
            }

        # 碳减排量（能耗 × 产量 × 口径参数）
        if "ENERGY_INTENSITY" in values and "CRUDE_STEEL_OUTPUT" in values:
            params = caliber["CO2_REDUCTION"]["params"]
            delta = params["baseline_kgce_per_t"] - values["ENERGY_INTENSITY"]
            values["CO2_REDUCTION"] = round(
                max(delta, 0.0)
                * values["CRUDE_STEEL_OUTPUT"]
                * params["factor_tco2_per_tce"]
                / 1000.0,
                4,
            )
            meta["CO2_REDUCTION"] = {
                "kind": "derived",
                "unit": "tCO2",
                "sources": [
                    winners["ENERGY_INTENSITY"].id,
                    winners["CRUDE_STEEL_OUTPUT"].id,
                ],
                "formula": caliber["CO2_REDUCTION"]["formula"],
                "params": params,
            }
            propagated = [
                f"上游{METRICS[code][0]}：{reason}"
                for code in ("ENERGY_INTENSITY", "CRUDE_STEEL_OUTPUT")
                for reason in flags.get(code, [])
            ]
            if propagated:
                flags["CO2_REDUCTION"] = propagated

        # 固废资源化利用率（按当前去向汇总：改用途只影响后续快照/报表）
        ws = [w for w in self.wastes.values() if w.shift_id == shift_id]
        if ws:
            total = sum(w.amount for w in ws)
            used = sum(w.amount for w in ws if self._waste_utilized(w))
            values["WASTE_UTIL_RATE"] = round(used / total * 100, 4)
            meta["WASTE_UTIL_RATE"] = {
                "kind": "derived",
                "unit": "%",
                "sources": [w.id for w in ws],
                "formula": caliber["WASTE_UTIL_RATE"]["formula"],
            }

        # 焦炉煤气副产氢
        hs = [h for h in self.hydrogens.values() if h.shift_id == shift_id]
        if hs:
            values["HYDROGEN_VOLUME"] = round(sum(h.volume_nm3 for h in hs), 2)
            meta["HYDROGEN_VOLUME"] = {
                "kind": "derived",
                "unit": "Nm3",
                "sources": [h.id for h in hs],
            }

        fingerprint = self._fingerprint(
            {
                "shift_id": shift_id,
                "caliber_version": caliber_version,
                "values": values,
                "meta": meta,
            }
        )
        return {
            "shift_id": shift_id,
            "caliber_version": caliber_version,
            "values": values,
            "meta": meta,
            "flags": flags,
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # ============================================================== 事件

    def _emit(self, etype: str, payload: dict[str, Any], actor: str) -> Event:
        return self._append(Event(type=etype, payload=payload, actor=actor))

    def _append(self, event: Event) -> Event:
        stored = self.store.append(event)
        self._apply(stored)
        return stored

    # ============================================================== 回放

    def _apply(self, e: Event) -> None:
        p = e.payload
        t = e.type
        if t == "production_line_registered":
            self.lines[p["id"]] = ProductionLine(p["id"], p["name"])
        elif t == "process_registered":
            self.processes[p["id"]] = ProcessStep(
                p["id"], p["line_id"], p["name"], p["order"]
            )
        elif t == "material_batch_registered":
            self.batches[p["id"]] = MaterialBatch(
                p["id"], p["name"], p["supplier"], p["amount"],
                p["unit"], p["process_id"],
            )
        elif t == "product_spec_registered":
            self.specs[p["id"]] = ProductSpec(p["id"], p["grade"], p["standard"])
        elif t == "partner_registered":
            self.partners[p["id"]] = Partner(p["id"], p["name"], p["kind"])
        elif t == "shift_opened":
            self.shifts[p["id"]] = Shift(
                p["id"], p["line_id"], p["date"], p["shift_no"], p["opened_by"]
            )
        elif t == "shift_frozen":
            shift = self.shifts[p["id"]]
            shift.frozen = True
            shift.frozen_at = e.ts or time.time()
            shift.caliber_version = p["caliber_version"]
        elif t == "product_batch_logged":
            self.product_batches[p["id"]] = ProductBatch(
                p["id"], p["spec_id"], p["shift_id"], p["amount"], p["passed"]
            )
            self.shifts[p["shift_id"]].product_batch_ids.append(p["id"])
        elif t == "measurement_submitted":
            if p.get("late_inspection"):
                # 迟到补录消费一次登记，保证回放后状态与提交时一致
                self.late_inspections.pop((p["shift_id"], p["metric_code"]), None)
            self.measurements[p["id"]] = Measurement(
                id=p["id"],
                shift_id=p["shift_id"],
                metric_code=p["metric_code"],
                value=p["value"],
                unit=p["unit"],
                submitted_by=p["submitted_by"],
                submitted_at=e.ts or time.time(),
                idempotency_key=p["idempotency_key"],
                late_inspection=p.get("late_inspection"),
            )
            self.idem[p["idempotency_key"]] = p["id"]
        elif t == "measurement_confirmed":
            m = self.measurements[p["id"]]
            key = _confirm_key(e.actor, p["role"])
            if key not in m.confirmed_by:
                m.confirmed_by.append(key)
            if all(
                any(ck.endswith(f"@{role}") for ck in m.confirmed_by)
                for role in required_reviewers(m.metric_code)
            ):
                m.status = "confirmed"
        elif t == "conflict_raised":
            self.conflicts[p["id"]] = Conflict(
                id=p["id"],
                shift_id=p["shift_id"],
                metric_code=p["metric_code"],
                candidates=list(p["candidates"]),
                values=list(p["values"]),
                raised_by=p["raised_by"],
            )
            for cid in p["candidates"]:
                self.measurements[cid].status = "conflict"
                self.measurements[cid].conflict_id = p["id"]
        elif t == "conflict_resolved":
            c = self.conflicts[p["id"]]
            c.resolved = True
            c.winning_measurement_id = p["winning_measurement_id"]
            c.note = p.get("note", "")
            for cid in c.candidates:
                m = self.measurements[cid]
                if cid == p["winning_measurement_id"]:
                    m.status = "pending"
                    m.conflict_id = None
                    m.confirmed_by = []
                    m.supersedes = c.id
                # 落败者保持 conflict，不再参与计算
        elif t == "waste_record_submitted":
            self.wastes[p["id"]] = WasteRecord(
                id=p["id"],
                shift_id=p["shift_id"],
                waste_type=p["waste_type"],
                amount=p["amount"],
                process_id=p["process_id"],
                material_batch_ids=list(p["material_batch_ids"]),
                partner_id=p["partner_id"],
                destination=p["destination"],
                utilized=p["utilized"],
                submitted_by=p["submitted_by"],
                submitted_at=e.ts or time.time(),
            )
            self.idem[p["idempotency_key"]] = p["id"]
        elif t == "hydrogen_record_submitted":
            self.hydrogens[p["id"]] = HydrogenRecord(
                id=p["id"],
                shift_id=p["shift_id"],
                source_gas_batch_id=p["source_gas_batch_id"],
                volume_nm3=p["volume_nm3"],
                process_id=p["process_id"],
                partner_id=p["partner_id"],
                use=p["use"],
                submitted_by=p["submitted_by"],
                submitted_at=e.ts or time.time(),
            )
            self.idem[p["idempotency_key"]] = p["id"]
        elif t == "record_confirmed":
            pool = self.wastes if p["kind"] == "waste" else self.hydrogens
            r = pool[p["id"]]
            key = _confirm_key(e.actor, p["role"])
            if key not in r.confirmed_by:
                r.confirmed_by.append(key)
            r.status = "confirmed"
        elif t == "byproduct_diversion_logged":
            pool = self.wastes if p["kind"] == "waste" else self.hydrogens
            record = pool[p["record_id"]]
            record.diversion = ByproductDiversion(
                id=p["id"],
                record_id=p["record_id"],
                kind=p["kind"],
                new_destination=p["new_destination"],
                new_utilized=p.get("new_utilized"),
                reason=p["reason"],
                logged_by=e.actor,
                ts=e.ts or time.time(),
            )
            # 已被报表引用的结算只作废、不删改；新结算随下一份报表按新去向重建。
            for st in self.settlements.values():
                if st.record_id == p["record_id"] and st.valid:
                    st.valid = False
        elif t == "maintenance_logged":
            self.maintenance.setdefault(p["shift_id"], []).append(
                {
                    "id": p["id"],
                    "process_id": p["process_id"],
                    "note": p["note"],
                    "affects_codes": list(p["affects_codes"]),
                }
            )
        elif t == "late_inspection_logged":
            self.late_inspections[(p["shift_id"], p["metric_code"])] = {
                "id": p["id"],
                "reason": p["reason"],
            }
        elif t == "caliber_published":
            self.current_caliber = p["version"]
            self.calibers[p["version"]] = p["caliber"]
        elif t == "report_published":
            raw_sources = [
                mid
                for info in p["meta"].values()
                if info["kind"] == "raw"
                for mid in info["sources"]
            ]
            derived_sources = [
                sid
                for info in p["meta"].values()
                if info["kind"] == "derived"
                for sid in info["sources"]
            ]
            self.reports[p["id"]] = Report(
                id=p["id"],
                shift_id=p["shift_id"],
                caliber_version=p["caliber_version"],
                published_by=p["published_by"],
                published_at=e.ts or time.time(),
                values=dict(p["values"]),
                measurement_ids=raw_sources,
                record_ids=derived_sources,
                quality_pass_rate=p["values"].get("QUALITY_PASS_RATE"),
                fingerprint=p["fingerprint"],
            )
            # 报表发布时按快照固化合作单位结算（每份报表一个结算版本）
            for w in self.wastes.values():
                if w.shift_id != p["shift_id"]:
                    continue
                basis = w.diversion.new_destination if w.diversion else w.destination
                utilized = self._waste_utilized(w)
                self.settlements[(w.partner_id, w.id, p["id"])] = Settlement(
                    partner_id=w.partner_id,
                    shift_id=p["shift_id"],
                    record_id=w.id,
                    kind="waste",
                    amount_desc=f"{w.waste_type} {w.amount}t",
                    basis=f"{basis}({'资源化' if utilized else '非资源化'})",
                    report_id=p["id"],
                )
            for h in self.hydrogens.values():
                if h.shift_id != p["shift_id"]:
                    continue
                basis = h.diversion.new_destination if h.diversion else h.use
                self.settlements[(h.partner_id, h.id, p["id"])] = Settlement(
                    partner_id=h.partner_id,
                    shift_id=p["shift_id"],
                    record_id=h.id,
                    kind="hydrogen",
                    amount_desc=f"副产氢 {h.volume_nm3}Nm3",
                    basis=basis,
                    report_id=p["id"],
                )
        else:
            raise LedgerError(f"未知事件类型: {t}")

