"""钢铁转型数据台账服务的规则测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ledger import GreenSteelLedger, LedgerError
from src.metrics import (
    ROLE_ENERGY,
    ROLE_ENV,
    ROLE_MANAGER,
    ROLE_OPERATOR,
    ROLE_PROCESS,
)


class LedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.lg = GreenSteelLedger(self.path)
        self._seed()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        lg = self.lg
        lg.register_line("张管理", ROLE_MANAGER, "L1", "精品钢1号线")
        lg.register_process("张管理", ROLE_MANAGER, "P1", "L1", "高炉炼铁", 1)
        lg.register_process("张管理", ROLE_MANAGER, "P2", "L1", "焦炉煤气制氢", 4)
        lg.register_material_batch("王车间", ROLE_OPERATOR, "B1", "烧结矿", "本地矿业", 2000, "t", "P1")
        lg.register_material_batch("王车间", ROLE_OPERATOR, "B2", "焦炉煤气", "焦化厂", 50000, "Nm3", "P2")
        lg.register_product_spec("张管理", ROLE_MANAGER, "S1", "DC01", "GB/T 5213")
        lg.register_partner("张管理", ROLE_MANAGER, "PT1", "环科固废公司", "waste")
        lg.register_partner("张管理", ROLE_MANAGER, "PT2", "冀能氢能公司", "hydrogen")
        lg.open_shift("王车间", ROLE_OPERATOR, "SH1", "L1", "2026-10-01", 1)

    # -- 便捷构造 -----------------------------------------------------------

    def _raw_shift(self, shift_id="SH1", output=1200.0, energy=545.0):
        """提交并复核产量与能耗，返回两个测量 id。"""
        lg = self.lg
        m_out = lg.submit_measurement("王车间", ROLE_OPERATOR, shift_id, "CRUDE_STEEL_OUTPUT", output, f"{shift_id}-out")
        m_en = lg.submit_measurement("王车间", ROLE_OPERATOR, shift_id, "ENERGY_INTENSITY", energy, f"{shift_id}-en")
        lg.confirm_measurement("张管理", ROLE_MANAGER, m_out)
        lg.confirm_measurement("赵环保", ROLE_ENV, m_en)
        lg.confirm_measurement("孙能源", ROLE_ENERGY, m_en)
        return m_out, m_en

    def _freeze_publish(self, shift_id="SH1"):
        self.lg.freeze_shift("张管理", ROLE_MANAGER, shift_id)
        rid = self.lg.publish_report("张管理", ROLE_MANAGER, shift_id)
        return rid


class IdempotencyTest(LedgerTestBase):
    def test_same_key_is_idempotent(self):
        lg = self.lg
        a = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        b = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        self.assertEqual(a, b)

    def test_same_content_new_key_is_idempotent(self):
        lg = self.lg
        a = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        b = lg.submit_measurement("李车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k2")
        self.assertEqual(a, b)

    def test_same_key_different_value_is_rejected(self):
        lg = self.lg
        lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        with self.assertRaisesRegex(LedgerError, "幂等键已使用但提交内容不一致"):
            lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1300.0, "k1")

    def test_waste_record_repeat_upload_is_idempotent(self):
        lg = self.lg
        args = ("W1", "SH1", "钢渣", 100.0, "P1", ["B1"], "PT1", "资源化利用", True, "wk1")
        self.assertEqual(lg.submit_waste_record("王车间", ROLE_OPERATOR, *args), "W1")
        self.assertEqual(lg.submit_waste_record("王车间", ROLE_OPERATOR, *args), "W1")
        self.assertEqual(len(lg.wastes), 1)

    def test_confirmation_is_idempotent(self):
        lg = self.lg
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "SO2_EMISSION", 30.0, "k")
        lg.confirm_measurement("赵环保", ROLE_ENV, mid)
        lg.confirm_measurement("赵环保", ROLE_ENV, mid)  # 不报错、不重复
        self.assertEqual(lg.measurements[mid].confirmed_by, ["赵环保@环保人员"])


class ConflictTest(LedgerTestBase):
    def test_conflicting_values_raise_manual_ticket(self):
        lg = self.lg
        a = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        b = lg.submit_measurement("李车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1210.0, "k2")
        self.assertEqual(len(lg.conflicts), 1)
        cid = next(iter(lg.conflicts))
        self.assertEqual(lg.measurements[a].status, "conflict")
        self.assertEqual(lg.measurements[b].status, "conflict")
        # 冲突未决不能复核
        with self.assertRaisesRegex(LedgerError, "冲突"):
            lg.confirm_measurement("张管理", ROLE_MANAGER, a)
        # 冲突未决不能冻结
        with self.assertRaisesRegex(LedgerError, "人工核对"):
            lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")
        # 工艺/算法人员无权处理冲突
        with self.assertRaisesRegex(LedgerError, "管理人员"):
            lg.resolve_conflict("钱算法", ROLE_PROCESS, cid, a)
        lg.resolve_conflict("张管理", ROLE_MANAGER, cid, a, "以校准值为准")
        self.assertEqual(lg.measurements[a].status, "pending")
        self.assertEqual(lg.measurements[b].status, "conflict")  # 落败者作废
        # 选定值需重新完整复核
        lg.confirm_measurement("张管理", ROLE_MANAGER, a)
        snap = lg.current_snapshot("SH1")
        self.assertEqual(snap["values"]["CRUDE_STEEL_OUTPUT"], 1200.0)

    def test_third_value_blocked_while_conflict_open(self):
        lg = self.lg
        lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k1")
        lg.submit_measurement("李车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1210.0, "k2")
        with self.assertRaisesRegex(LedgerError, "冲突尚未核对"):
            lg.submit_measurement("周车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1195.0, "k3")


class SeparationOfDutiesTest(LedgerTestBase):
    def test_operator_cannot_confirm(self):
        lg = self.lg
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "SO2_EMISSION", 30.0, "k")
        with self.assertRaisesRegex(LedgerError, "环保人员"):
            lg.confirm_measurement("王车间", ROLE_OPERATOR, mid)

    def test_submitter_cannot_self_review(self):
        lg = self.lg
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k")
        with self.assertRaisesRegex(LedgerError, "不能复核本人"):
            # 管理人员恰好也是提交人时仍需回避
            lg.confirm_measurement("王车间", ROLE_MANAGER, mid)

    def test_process_role_cannot_confirm_energy_saving(self):
        lg = self.lg
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "ENERGY_INTENSITY", 545.0, "k")
        with self.assertRaisesRegex(LedgerError, "无权确认"):
            lg.confirm_measurement("钱算法", ROLE_PROCESS, mid)

    def test_env_cannot_confirm_production(self):
        lg = self.lg
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "k")
        with self.assertRaisesRegex(LedgerError, "无权确认"):
            lg.confirm_measurement("赵环保", ROLE_ENV, mid)

    def test_energy_metric_needs_env_and_energy_dual_confirmation(self):
        lg = self.lg
        m_out = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CRUDE_STEEL_OUTPUT", 1200.0, "o")
        m_en = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "ENERGY_INTENSITY", 545.0, "e")
        lg.confirm_measurement("张管理", ROLE_MANAGER, m_out)
        lg.confirm_measurement("赵环保", ROLE_ENV, m_en)
        with self.assertRaisesRegex(LedgerError, "未完成复核"):
            lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")
        lg.confirm_measurement("孙能源", ROLE_ENERGY, m_en)
        lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")  # 不再报错

    def test_hydrogen_record_reviewed_by_energy_not_env(self):
        lg = self.lg
        rid = lg.submit_hydrogen_record(
            "王车间", ROLE_OPERATOR, "H1", "SH1", "B2", 12000.0, "P2", "PT2", "外供", "hk"
        )
        with self.assertRaisesRegex(LedgerError, "能源管理人员"):
            lg.confirm_record("赵环保", ROLE_ENV, rid)
        lg.confirm_record("孙能源", ROLE_ENERGY, rid)
        self.assertEqual(lg.hydrogens[rid].status, "confirmed")


class FreezeAndCaliberTest(LedgerTestBase):
    def test_freeze_blocks_raw_changes_and_pins_caliber(self):
        lg = self.lg
        self._raw_shift()
        lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")
        with self.assertRaisesRegex(LedgerError, "已冻结"):
            lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "SO2_EMISSION", 30.0, "late")
        with self.assertRaisesRegex(LedgerError, "已冻结"):
            lg.log_product_batch("王车间", ROLE_OPERATOR, "PB1", "SH1", "S1", 10.0, True)
        self.assertEqual(lg.shifts["SH1"].caliber_version, 1)

    def test_freeze_requires_crude_output(self):
        lg = self.lg
        with self.assertRaisesRegex(LedgerError, "粗钢产量"):
            lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")

    def test_new_caliber_does_not_rewrite_published_report(self):
        lg = self.lg
        self._raw_shift()
        rid = self._freeze_publish()
        old = lg.get_report(rid).values["CO2_REDUCTION"]
        lg.publish_caliber(
            "张管理", ROLE_MANAGER,
            {"CO2_REDUCTION": {
                "definition": "新基准", "formula": "x",
                "params": {"baseline_kgce_per_t": 500.0, "factor_tco2_per_tce": 2.5},
            }},
        )
        # 已发布报表保持 v1 口径结果
        self.assertEqual(lg.get_report(rid).values["CO2_REDUCTION"], old)
        # 后续班次使用 v2
        lg.open_shift("王车间", ROLE_OPERATOR, "SH2", "L1", "2026-10-02", 1)
        self._raw_shift("SH2")
        rid2 = self._freeze_publish("SH2")
        r2 = lg.get_report(rid2)
        self.assertEqual(r2.caliber_version, 2)
        self.assertEqual(r2.values["CO2_REDUCTION"], 0.0)  # 能耗高于新基准 → 0

    def test_report_fingerprint_is_stable_across_replay(self):
        lg = self.lg
        self._raw_shift()
        rid = self._freeze_publish()
        fp = lg.get_report(rid).fingerprint
        replayed = GreenSteelLedger(self.path)
        self.assertEqual(replayed.get_report(rid).fingerprint, fp)


class ExceptionScopeTest(LedgerTestBase):
    def test_late_inspection_only_flags_that_metric(self):
        lg = self.lg
        self._raw_shift()
        with self.assertRaisesRegex(LedgerError, "检测迟到"):
            lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "SO2_EMISSION", 35.0, "s", late=True)
        lg.log_late_inspection("赵环保", ROLE_ENV, "SH1", "SO2_EMISSION", "仪表送检")
        mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "SO2_EMISSION", 35.0, "s", late=True)
        lg.confirm_measurement("赵环保", ROLE_ENV, mid)
        # 登记一次性消费：再次迟到提交无可用登记
        with self.assertRaisesRegex(LedgerError, "检测迟到"):
            lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "DUST_EMISSION", 8.0, "d", late=True)
        snap = lg.current_snapshot("SH1")
        self.assertEqual(snap["flags"].get("SO2_EMISSION"), ["检测迟到补录"])
        self.assertNotIn("CRUDE_STEEL_OUTPUT", snap["flags"])

    def test_unconsumed_late_registration_blocks_freeze(self):
        lg = self.lg
        self._raw_shift()
        lg.log_late_inspection("赵环保", ROLE_ENV, "SH1", "SO2_EMISSION", "仪表送检")
        with self.assertRaisesRegex(LedgerError, "检测迟到已登记"):
            lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")

    def test_maintenance_flags_only_affected_metric_and_propagates_to_co2(self):
        lg = self.lg
        self._raw_shift()
        lg.log_maintenance("周设备", ROLE_OPERATOR, "SH1", "P1", "TRT检修", ["ENERGY_INTENSITY"])
        snap = lg.current_snapshot("SH1")
        self.assertIn("设备检修期间测量", snap["flags"]["ENERGY_INTENSITY"])
        self.assertTrue(snap["flags"]["CO2_REDUCTION"])  # 传播到碳减排
        self.assertNotIn("CRUDE_STEEL_OUTPUT", snap["flags"])  # 产量不受影响

    def test_diversion_after_report_changes_only_future_view_and_settlement(self):
        lg = self.lg
        self._raw_shift()
        lg.submit_waste_record("王车间", ROLE_OPERATOR, "W1", "SH1", "钢渣", 100.0, "P1", ["B1"], "PT1", "资源化利用", True, "w1")
        lg.submit_waste_record("王车间", ROLE_OPERATOR, "W2", "SH1", "除尘灰", 20.0, "P1", ["B1"], "PT1", "贮存", False, "w2")
        lg.confirm_record("赵环保", ROLE_ENV, "W1")
        lg.confirm_record("赵环保", ROLE_ENV, "W2")
        rid = self._freeze_publish()
        self.assertAlmostEqual(lg.get_report(rid).values["WASTE_UTIL_RATE"], 83.3333, places=3)

        # 改用途：贮存 → 资源化
        with self.assertRaisesRegex(LedgerError, "环保人员"):
            lg.divert_byproduct("王车间", ROLE_OPERATOR, "W2", "资源化", "新用途", True)
        lg.divert_byproduct("赵环保", ROLE_ENV, "W2", "钢渣微粉配料", "下游新增", True)

        # 已发布报表不变
        self.assertAlmostEqual(lg.get_report(rid).values["WASTE_UTIL_RATE"], 83.3333, places=3)
        # 旧结算作废
        valid = lg.valid_settlements_for_partner("PT1")
        self.assertTrue(all(s.record_id != "W2" for s in valid))
        self.assertTrue(any(not s.valid for s in lg.settlements_for_shift("SH1") if s.record_id == "W2"))
        # 当前视图反映新去向
        self.assertEqual(lg.current_snapshot("SH1")["values"]["WASTE_UTIL_RATE"], 100.0)

        # 重新发布：新报表反映新去向并重建结算，旧报表仍保留
        rid2 = lg.publish_report("张管理", ROLE_MANAGER, "SH1")
        self.assertNotEqual(rid, rid2)
        self.assertEqual(lg.get_report(rid2).values["WASTE_UTIL_RATE"], 100.0)
        self.assertAlmostEqual(lg.get_report(rid).values["WASTE_UTIL_RATE"], 83.3333, places=3)
        self.assertTrue(
            any(s.valid and s.record_id == "W2" and s.report_id == rid2
                for s in lg.settlements_for_shift("SH1"))
        )


class TraceabilityTest(LedgerTestBase):
    def test_trace_waste_metric_covers_chain(self):
        lg = self.lg
        self._raw_shift()
        lg.submit_waste_record("王车间", ROLE_OPERATOR, "W1", "SH1", "钢渣", 100.0, "P1", ["B1"], "PT1", "资源化利用", True, "w1")
        lg.confirm_record("赵环保", ROLE_ENV, "W1")
        rid = self._freeze_publish()
        trace = lg.trace("SH1", "WASTE_UTIL_RATE")
        kinds = {n["kind"] for n in trace["nodes"]}
        self.assertTrue({"metric", "shift", "line", "process", "material", "waste", "partner"} <= kinds)
        self.assertTrue(any(e["rel"] == "来源原料批次" for e in trace["edges"]))
        self.assertEqual(trace["reports"][0]["report_id"], rid)

    def test_trace_co2_lists_responsibility_confirmations(self):
        lg = self.lg
        self._raw_shift()
        rid = self._freeze_publish()
        trace = lg.trace("SH1", "CO2_REDUCTION")
        roles = {c["role"] for c in trace["confirmations"]}
        self.assertEqual(roles, {ROLE_MANAGER, ROLE_ENV, ROLE_ENERGY})
        self.assertTrue(any(r["report_id"] == rid for r in trace["reports"]))
        # 依赖边指向产量与能耗
        deps = {e["to"] for e in trace["edges"] if e["rel"] == "口径计算依赖"}
        self.assertEqual(deps, {"metric:ENERGY_INTENSITY", "metric:CRUDE_STEEL_OUTPUT"})

    def test_trace_quality_includes_product_batches_and_spec(self):
        lg = self.lg
        self._raw_shift()
        lg.log_product_batch("王车间", ROLE_OPERATOR, "PB1", "SH1", "S1", 90.0, True)
        lg.log_product_batch("王车间", ROLE_OPERATOR, "PB2", "SH1", "S1", 10.0, False)
        lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")
        trace = lg.trace("SH1", "QUALITY_PASS_RATE")
        kinds = {n["kind"] for n in trace["nodes"]}
        self.assertIn("product_batch", kinds)
        self.assertIn("spec", kinds)


class IntegrityTest(LedgerTestBase):
    def test_event_store_rebuilds_identical_state(self):
        lg = self.lg
        self._raw_shift()
        lg.submit_waste_record("王车间", ROLE_OPERATOR, "W1", "SH1", "钢渣", 100.0, "P1", ["B1"], "PT1", "资源化利用", True, "w1")
        lg.confirm_record("赵环保", ROLE_ENV, "W1")
        lg.submit_hydrogen_record("王车间", ROLE_OPERATOR, "H1", "SH1", "B2", 12000.0, "P2", "PT2", "外供", "h1")
        lg.confirm_record("孙能源", ROLE_ENERGY, "H1")
        rid = self._freeze_publish()
        fp_before = lg.get_report(rid).fingerprint

        replayed = GreenSteelLedger(self.path)
        self.assertEqual(replayed.get_report(rid).fingerprint, fp_before)
        self.assertEqual(replayed.current_snapshot("SH1")["values"], lg.current_snapshot("SH1")["values"])
        self.assertEqual(len(replayed.settlements), len(lg.settlements))

    def test_tampering_history_is_detected(self):
        lg = self.lg
        self._raw_shift()
        self._freeze_publish()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[2])
        row["payload"]["value"] = 9999.0
        lines[2] = json.dumps(row, ensure_ascii=False)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "哈希|断裂"):
            GreenSteelLedger(self.path)

    def test_derived_metric_cannot_be_submitted(self):
        with self.assertRaisesRegex(LedgerError, "衍生指标"):
            self.lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1", "CO2_REDUCTION", 10.0, "x")


if __name__ == "__main__":
    unittest.main()
