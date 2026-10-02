"""钢铁转型数据服务规则测试。

覆盖：班次冻结与口径版本、幂等上传与冲突人工核对、职责分离会签、
检修/迟到/副产物改用途的局部影响、已发布报表不可重写、绿色指标全链追溯。
"""

from __future__ import annotations

import unittest

from src.steel_ledger import (
    Actor,
    ConflictStatus,
    EnvTarget,
    HydrogenRecord,
    LedgerError,
    MaterialBatch,
    Measurement,
    MetricKind,
    MetricStatus,
    PermissionDenied,
    ProcessStep,
    ProductionLine,
    ProductBatch,
    ProductSpec,
    Role,
    ShiftFrozen,
    SpecError,
    SteelLedger,
    WasteRecord,
)


def build_ledger() -> SteelLedger:
    """构造一条覆盖精品钢、固废、制氢、环保指标的最小数据链。"""
    ledger = SteelLedger()
    for actor in (
        Actor("ws01", "甲班车间", Role.OPERATOR),
        Actor("hb01", "环保复核组", Role.ENV),
        Actor("gy01", "工艺算法组", Role.PROCESS),
        Actor("gl01", "产业管理科", Role.AUDITOR),
        Actor("ny01", "能源管理岗", Role.ENERGY),
        Actor("fw01", "环科固废公司", Role.PARTNER, org="环科固废公司"),
    ):
        ledger.register_actor(actor)

    ledger.register_line(ProductionLine("L1", "迁安精品钢1号线",
                                        ("P-IRON", "P-STEEL", "P-H2")))
    for step in (
        ProcessStep("P-IRON", "炼铁", "L1", 1),
        ProcessStep("P-STEEL", "炼钢", "L1", 2),
        ProcessStep("P-ROLL", "热轧", "L1", 3),
        ProcessStep("P-H2", "焦炉煤气PSA制氢", "L1", 4),
        ProcessStep("P-CEMENT", "矿渣微粉回用", "L1", 5),
    ):
        ledger.register_process(step)

    ledger.register_material(MaterialBatch(
        "MAT-1", "高品位铁精粉", 10000.0, "t", "P-IRON"))
    ledger.register_material(MaterialBatch(
        "MAT-2", "冶金焦", 3000.0, "t", "P-IRON", parent="MAT-1"))
    ledger.register_spec(ProductSpec(
        "SPC-DP01", "DP590", {"碳含量": (0.05, 0.10)}))
    ledger.register_product(ProductBatch(
        "PRD-01", "SPC-DP01", 2000.0, "t", "P-STEEL",
        material_codes=("MAT-1", "MAT-2")))

    ledger.register_waste(WasteRecord(
        "W-SLAG", "高炉矿渣", 3000.0, "t", "P-IRON", "fw01",
        "外委矿渣微粉", reuse_process_code="P-CEMENT"))
    ledger.register_waste(WasteRecord(
        "W-DUST", "炼钢除尘灰", 200.0, "t", "P-STEEL", None,
        "内部冷固回用", reuse_process_code="P-STEEL"))
    ledger.register_hydrogen(HydrogenRecord(
        "H2-01", "焦炉煤气", 50000.0, "Nm3", "P-H2", None, "自用烘烤钢包"))

    for target in (
        EnvTarget("T-SW", "固废利用量", MetricKind.SOLID_WASTE_USED, "t"),
        EnvTarget("T-H2", "工业副产氢产量", MetricKind.H2_OUTPUT, "Nm3"),
        EnvTarget("T-EM", "二氧化硫排放", MetricKind.EMISSION, "kg",
                  higher_is_better=False),
        EnvTarget("T-ES", "吨钢节能量", MetricKind.ENERGY_SAVING, "kgce/t"),
    ):
        ledger.register_target(target)

    ledger.publish_spec_version(1, "转型指标口径v1", {
        "T-SW": "资源化处置量合计",
        "T-H2": "PSA提纯氢气体积",
        "T-EM": "在线监测小时均值",
        "T-ES": "副产物替代与燃氢折算节能",
    })
    return ledger


def open_full_shift(ledger: SteelLedger, code: str = "S1") -> dict[str, str]:
    """开班、提交覆盖各指标的测量，返回测点键。"""
    ledger.open_shift(code, "L1", "2026-09-30T08:00", "2026-09-30T16:00")
    measurements = {
        "output": Measurement(
            "m-output", None, code, "粗钢产量", 2000.0, "t", "ws01",
            lineage=("PRD-01", "P-STEEL")),
        "waste": Measurement(
            "m-waste", "T-SW", code, "矿渣资源化量", 3000.0, "t", "ws01",
            lineage=("W-SLAG", "P-CEMENT", "P-IRON")),
        "h2": Measurement(
            "m-h2", "T-H2", code, "副产氢体积", 50000.0, "Nm3", "ws01",
            lineage=("H2-01", "P-H2")),
        "emission": Measurement(
            "m-em", "T-EM", code, "SO2排放", 0.8, "kg", "ws01",
            lineage=("P-STEEL",)),
        "saving": Measurement(
            "m-es", "T-ES", code, "吨钢节能", 12.5, "kgce/t", "ws01",
            lineage=("W-SLAG", "MAT-2", "P-IRON")),
    }
    for measurement in measurements.values():
        assert ledger.submit_measurement("ws01", measurement) == "accepted"
    ledger.derive_metrics_for_shift(code)
    return measurements


def metric_ids_by_kind(ledger: SteelLedger, shift: str,
                       kind: MetricKind) -> list[str]:
    return [e.code for e in ledger.metrics.values()
            if e.shift_code == shift and e.kind == kind
            and e.status != MetricStatus.SUPERSEDED]


class ShiftAndSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build_ledger()

    def test_freeze_locks_data_sources_and_caliber(self) -> None:
        open_full_shift(self.ledger)
        self.ledger.freeze_shift("S1", "ws01")
        shift = self.ledger.shifts["S1"]
        self.assertTrue(shift.frozen)
        self.assertEqual(shift.spec_version, 1)

        # 冻结后新值不得直接写入；完全一致的重放仍幂等通过。
        replay = Measurement(
            "m-h2", "T-H2", "S1", "副产氢体积", 50000.0, "Nm3", "ws01",
            lineage=("H2-01", "P-H2"))
        self.assertEqual(self.ledger.submit_measurement("ws01", replay),
                         "accepted")
        changed = Measurement(
            "m-h2", "T-H2", "S1", "副产氢体积", 49000.0, "Nm3", "ws01",
            lineage=("H2-01", "P-H2"))
        with self.assertRaises(ShiftFrozen):
            self.ledger.submit_measurement("ws01", changed)

    def test_spec_version_only_advances_and_spares_history(self) -> None:
        with self.assertRaises(SpecError):
            self.ledger.publish_spec_version(1, "重复口径", {})
        with self.assertRaises(SpecError):
            self.ledger.publish_spec_version(0, "倒退口径", {})
        open_full_shift(self.ledger)
        self.ledger.freeze_shift("S1", "ws01")
        self.ledger.publish_spec_version(2, "转型指标口径v2",
                                         {"T-ES": "新折算系数"})
        self.ledger.activate_spec(2)
        self.ledger.open_shift("S2", "L1", "2026-09-30T16:00",
                               "2026-10-01T00:00")
        self.assertEqual(self.ledger.shifts["S1"].spec_version, 1)
        self.assertEqual(self.ledger.shifts["S2"].spec_version, 2)


class IdempotencyAndConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build_ledger()

    def test_repeated_upload_is_idempotent(self) -> None:
        open_full_shift(self.ledger)
        again = Measurement(
            "m-waste", "T-SW", "S1", "矿渣资源化量", 3000.0, "t", "ws01",
            lineage=("W-SLAG", "P-CEMENT", "P-IRON"))
        self.assertEqual(self.ledger.submit_measurement("ws01", again),
                         "accepted")
        self.ledger.derive_metrics_for_shift("S1")
        self.assertEqual(
            len(metric_ids_by_kind(self.ledger, "S1",
                                   MetricKind.SOLID_WASTE_USED)),
            1)

    def test_value_conflict_goes_to_manual_review(self) -> None:
        self.ledger.open_shift("SC", "L1", "2026-10-01T00:00",
                               "2026-10-01T08:00")
        first = Measurement("m-x", "T-H2", "SC", "副产氢体积", 50000.0,
                            "Nm3", "ws01", lineage=("H2-01", "P-H2"))
        second = Measurement("m-x", "T-H2", "SC", "副产氢体积", 46000.0,
                             "Nm3", "ws01", lineage=("H2-01", "P-H2"))
        self.assertEqual(self.ledger.submit_measurement("ws01", first),
                         "accepted")
        self.assertEqual(self.ledger.submit_measurement("ws01", second),
                         "conflict")
        self.ledger.derive_metrics_for_shift("SC")
        metric = self.ledger.metrics[
            metric_ids_by_kind(self.ledger, "SC", MetricKind.H2_OUTPUT)[0]]
        self.assertEqual(metric.status, MetricStatus.DISPUTED)

        # 未决冲突不得冻结；车间/环保都不能自行裁决。
        with self.assertRaises(LedgerError):
            self.ledger.freeze_shift("SC", "ws01")
        with self.assertRaises(PermissionDenied):
            self.ledger.resolve_conflict("hb01", "m-x", "incoming", "环保自裁")
        resolved = self.ledger.resolve_conflict(
            "gl01", "m-x", "incoming", "以校准后流量计复测值为准")
        self.assertEqual(resolved.status, ConflictStatus.RESOLVED)
        self.assertEqual(self.ledger.measurements["m-x"].value, 46000.0)
        self.ledger.freeze_shift("SC", "gl01")
        self.assertTrue(self.ledger.shifts["SC"].frozen)

    def test_rejected_conflict_leaves_point_without_metric(self) -> None:
        self.ledger.open_shift("SR", "L1", "2026-10-01T00:00",
                               "2026-10-01T08:00")
        first = Measurement("m-r", "T-SW", "SR", "除尘灰", 200.0, "t",
                            "ws01", lineage=("W-DUST", "P-STEEL"))
        second = Measurement("m-r", "T-SW", "SR", "除尘灰", 380.0, "t",
                             "ws01", lineage=("W-DUST", "P-STEEL"))
        self.ledger.submit_measurement("ws01", first)
        self.ledger.submit_measurement("ws01", second)
        self.ledger.derive_metrics_for_shift("SR")
        self.ledger.reject_conflict("gl01", "m-r", "两次票据均无原始磅单")
        self.assertEqual(
            metric_ids_by_kind(self.ledger, "SR",
                               MetricKind.SOLID_WASTE_USED), [])


class SegregationOfDutiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build_ledger()
        open_full_shift(self.ledger)
        self.ledger.freeze_shift("S1", "ws01")

    def test_energy_saving_requires_process_and_env_dual_sign(self) -> None:
        saving_id = metric_ids_by_kind(
            self.ledger, "S1", MetricKind.ENERGY_SAVING)[0]

        # 车间不能复核任何指标。
        with self.assertRaises(PermissionDenied):
            self.ledger.confirm_metric(saving_id, "ws01")
        # 工艺单方只能进入已复核，不能确认节能成果。
        entry = self.ledger.confirm_metric(saving_id, "gy01", "算法核对")
        self.assertEqual(entry.status, MetricStatus.REVIEWED)
        with self.assertRaises(LedgerError):
            self.ledger.settle_metric(saving_id)
        # 环保补签后才确认；会签可重复调用而不重复计票。
        entry = self.ledger.confirm_metric(saving_id, "hb01", "环保复核")
        self.assertEqual(entry.status, MetricStatus.CONFIRMED)
        self.assertEqual({s.role for s in entry.signatures},
                         {Role.PROCESS, Role.ENV})
        self.ledger.confirm_metric(saving_id, "hb01")
        self.assertEqual(len(entry.signatures), 2)

    def test_environmental_metric_reviewed_by_env_only(self) -> None:
        emission_id = metric_ids_by_kind(
            self.ledger, "S1", MetricKind.EMISSION)[0]
        with self.assertRaises(PermissionDenied):
            self.ledger.confirm_metric(emission_id, "gy01")
        entry = self.ledger.confirm_metric(emission_id, "hb01")
        self.assertEqual(entry.status, MetricStatus.CONFIRMED)

    def test_quality_metric_reviewed_by_process_owner(self) -> None:
        # 质量指标由工艺/算法负责人复核，而非环保。
        ledger = build_ledger()
        ledger.register_target(EnvTarget(
            "T-Q", "成材率", MetricKind.QUALITY, "%"))
        open_full_shift(ledger)
        ledger.submit_measurement("ws01", Measurement(
            "m-q", "T-Q", "S1", "成材率", 98.6, "%", "ws01",
            lineage=("PRD-01", "P-STEEL")))
        ledger.derive_metrics_for_shift("S1")
        ledger.freeze_shift("S1", "ws01")
        quality_id = metric_ids_by_kind(ledger, "S1", MetricKind.QUALITY)[0]
        with self.assertRaises(PermissionDenied):
            ledger.confirm_metric(quality_id, "hb01")
        entry = ledger.confirm_metric(quality_id, "gy01")
        self.assertEqual(entry.status, MetricStatus.CONFIRMED)


class ImpactPropagationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build_ledger()
        open_full_shift(self.ledger)
        self.ledger.freeze_shift("S1", "ws01")
        for kind in (MetricKind.OUTPUT, MetricKind.SOLID_WASTE_USED,
                     MetricKind.H2_OUTPUT, MetricKind.EMISSION):
            mid = metric_ids_by_kind(self.ledger, "S1", kind)[0]
            self.ledger.confirm_metric(mid, "hb01")
        self.saving_id = metric_ids_by_kind(
            self.ledger, "S1", MetricKind.ENERGY_SAVING)[0]
        self.ledger.confirm_metric(self.saving_id, "gy01")
        self.ledger.confirm_metric(self.saving_id, "hb01")
        self.ledger.settle_metric(self.saving_id)
        self.report = self.ledger.publish_report(
            "gl01", "R1", ["S1"])

    def _entry(self, kind: MetricKind) -> object:
        return self.ledger.metrics[
            metric_ids_by_kind(self.ledger, "S1", kind)[0]]

    def test_late_inspection_only_recomputes_related_metric(self) -> None:
        old_saving = self.ledger.metrics[self.saving_id]
        self.ledger.update_measurement_value("ws01", "m-waste", 2950.0)
        new_waste = self._entry(MetricKind.SOLID_WASTE_USED)
        self.assertEqual(new_waste.value, 2950.0)
        self.assertEqual(new_waste.status, MetricStatus.DRAFT)
        self.assertIn("检测迟到", new_waste.tags)

        # 节能、副产氢、排放指标不受检测迟到影响，节能结算保持有效。
        self.assertEqual(old_saving.status, MetricStatus.CONFIRMED)
        self.assertTrue(old_saving.settled)
        self.assertFalse(old_saving.settlement_reversed)
        self.assertEqual(self._entry(MetricKind.H2_OUTPUT).value, 50000.0)
        self.assertEqual(
            self._entry(MetricKind.H2_OUTPUT).status, MetricStatus.CONFIRMED)

    def test_maintenance_event_isolated_to_tagged_points(self) -> None:
        self.ledger.report_event(
            "ws01", "S1", "设备检修", ["m-em"])
        self.assertEqual(
            self._entry(MetricKind.EMISSION).status, MetricStatus.DRAFT)
        self.assertIn("设备检修", self._entry(MetricKind.EMISSION).tags)
        self.assertEqual(
            self._entry(MetricKind.H2_OUTPUT).status, MetricStatus.CONFIRMED)

    def test_byproduct_disposition_change_reverses_only_related_settlement(
            self) -> None:
        self.ledger.change_byproduct_disposition(
            "ws01", "W-SLAG", "改供厂区透水砖骨料", partner_code="fw01")

        # 节能指标与矿渣利用相关：旧版本冲销结算，等待按新去向重算重签。
        old_saving = self.ledger.metrics[self.saving_id]
        self.assertEqual(old_saving.status, MetricStatus.SUPERSEDED)
        self.assertTrue(old_saving.settlement_reversed)
        new_saving = self.ledger.metrics[old_saving.superseded_by]
        self.assertIn("副产物改用途", new_saving.tags)
        self.assertEqual(new_saving.status, MetricStatus.DRAFT)

        # 产量与副产氢去向未变，仍已确认，不被冲销。
        output = self._entry(MetricKind.OUTPUT)
        self.assertEqual(output.status, MetricStatus.CONFIRMED)
        self.assertEqual(self.ledger.waste["W-SLAG"].disposition,
                         "改供厂区透水砖骨料")

    def test_published_report_is_never_rewritten(self) -> None:
        before = {
            "fingerprint": self.report.fingerprint,
            "metrics": [(m["metric"], m["value"]) for m in self.report.metrics],
            "spec_version": self.report.spec_version,
        }
        self.ledger.update_measurement_value("ws01", "m-waste", 2950.0)
        self.ledger.change_byproduct_disposition(
            "ws01", "W-SLAG", "改供透水砖骨料")
        self.ledger.publish_spec_version(2, "转型指标口径v2",
                                         {"T-ES": "新折算系数"})
        self.ledger.activate_spec(2)

        stored = self.ledger.get_report("R1")
        self.assertEqual(stored.fingerprint, before["fingerprint"])
        self.assertEqual(
            [(m["metric"], m["value"]) for m in stored.metrics],
            before["metrics"])
        self.assertEqual(stored.spec_version, 1)
        with self.assertRaises(LedgerError):
            self.ledger.publish_report("gl01", "R1", ["S1"])

    def test_consistency_check_uses_single_source_of_truth(self) -> None:
        values = self.ledger.consistency_check("S1")
        self.assertEqual(values["m-waste"], 3000.0)
        self.ledger.update_measurement_value("ws01", "m-waste", 2950.0)
        values = self.ledger.consistency_check("S1")
        self.assertEqual(values["m-waste"], 2950.0)


class TraceabilityTest(unittest.TestCase):
    def test_green_metric_traces_to_material_process_records_and_signoffs(
            self) -> None:
        ledger = build_ledger()
        open_full_shift(ledger)
        ledger.freeze_shift("S1", "ws01")
        saving_id = metric_ids_by_kind(
            ledger, "S1", MetricKind.ENERGY_SAVING)[0]
        ledger.confirm_metric(saving_id, "gy01", "工艺折算确认")
        ledger.confirm_metric(saving_id, "hb01", "环保绩效复核")

        view = ledger.trace(saving_id)
        self.assertEqual(view["metric"]["kind"], "energy_saving")
        self.assertEqual(view["metric"]["spec_version"], 1)
        types = {node["type"]: node for node in view["lineage"]}
        self.assertIn("material", types)
        self.assertIn("process", types)
        self.assertIn("waste", types)
        # 原料沿 parent 链一路追溯到铁精粉批。
        self.assertEqual(types["material"]["material_chain"],
                         ["MAT-1", "MAT-2"])
        self.assertEqual(types["waste"]["disposition"], "外委矿渣微粉")
        self.assertEqual(types["waste"]["partner"], "fw01")
        self.assertEqual(view["shift"]["frozen"], True)
        self.assertEqual(view["measurement"]["submitted_by"], "ws01")
        self.assertEqual(
            {(s["role"], s["actor"]) for s in view["signatures"]},
            {("process", "gy01"), ("env", "hb01")})


if __name__ == "__main__":
    unittest.main()
