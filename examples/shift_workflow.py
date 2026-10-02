"""迁安钢铁转型数据服务：一个班次的端到端示例。

运行：python3 examples/shift_workflow.py
数据写入临时文件，仅用于演示，不含真实身份信息。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ledger import GreenSteelLedger
from src.metrics import ROLE_ENERGY, ROLE_ENV, ROLE_MANAGER, ROLE_OPERATOR


def main() -> None:
    store = Path(tempfile.mkdtemp()) / "qianan-demo.jsonl"
    lg = GreenSteelLedger(store)

    # 1) 产线、工序、原料批次、精品钢规格与合作单位
    lg.register_line("张管理", ROLE_MANAGER, "L1", "迁安精品钢1号线")
    lg.register_process("张管理", ROLE_MANAGER, "P1", "L1", "高炉炼铁", 1)
    lg.register_process("张管理", ROLE_MANAGER, "P4", "L1", "焦炉煤气制氢", 4)
    lg.register_material_batch("王车间", ROLE_OPERATOR, "B-ORE", "烧结矿", "本地矿业", 2000, "t", "P1")
    lg.register_material_batch("王车间", ROLE_OPERATOR, "B-GAS", "焦炉煤气", "焦化厂", 50000, "Nm3", "P4")
    lg.register_product_spec("张管理", ROLE_MANAGER, "SP-DC01", "DC01 冷轧基料", "GB/T 5213")
    lg.register_partner("张管理", ROLE_MANAGER, "PT-W", "环科固废公司", "waste")
    lg.register_partner("张管理", ROLE_MANAGER, "PT-H", "冀能氢能公司", "hydrogen")

    # 2) 开班
    lg.open_shift("王车间", ROLE_OPERATOR, "SH-1001-1", "L1", "2026-10-01", 1)
    sid = "SH-1001-1"

    # 3) 车间提交测量（重复上传幂等）
    m_out = lg.submit_measurement("王车间", ROLE_OPERATOR, sid, "CRUDE_STEEL_OUTPUT", 1200.0, "upload-001")
    lg.submit_measurement("王车间", ROLE_OPERATOR, sid, "CRUDE_STEEL_OUTPUT", 1200.0, "upload-001")  # 同键重试
    m_en = lg.submit_measurement("王车间", ROLE_OPERATOR, sid, "ENERGY_INTENSITY", 545.0, "upload-002")

    # 4) 精品钢批次
    lg.log_product_batch("王车间", ROLE_OPERATOR, "PB-1", sid, "SP-DC01", 1100.0, True)
    lg.log_product_batch("王车间", ROLE_OPERATOR, "PB-2", sid, "SP-DC01", 100.0, False)

    # 5) 固废与焦炉煤气制氢记录，环保/能源分别复核
    lg.submit_waste_record("王车间", ROLE_OPERATOR, "W-1", sid, "钢渣", 100.0, "P1", ["B-ORE"], "PT-W", "资源化利用", True, "waste-001")
    lg.confirm_record("赵环保", ROLE_ENV, "W-1")
    lg.submit_hydrogen_record("王车间", ROLE_OPERATOR, "H-1", sid, "B-GAS", 12000.0, "P4", "PT-H", "外供", "h2-001")
    lg.confirm_record("孙能源", ROLE_ENERGY, "H-1")

    # 6) 产量管理人员复核；能耗需环保+能源双线确认
    lg.confirm_measurement("张管理", ROLE_MANAGER, m_out)
    lg.confirm_measurement("赵环保", ROLE_ENV, m_en)
    lg.confirm_measurement("孙能源", ROLE_ENERGY, m_en)

    # 7) 冻结班次并发布报表（口径 v1）
    lg.freeze_shift("张管理", ROLE_MANAGER, sid)
    report_id = lg.publish_report("张管理", ROLE_MANAGER, sid)
    report = lg.get_report(report_id)
    print("已发布报表", report_id, "口径版本 v%s" % report.caliber_version)
    print(json.dumps(report.values, ensure_ascii=False, indent=2))

    # 8) 从碳减排指标追溯到原料、工序、处理记录和责任确认
    trace = lg.trace(sid, "CO2_REDUCTION")
    print("\n追溯：碳减排 →", len(trace["nodes"]), "个节点，责任确认：")
    for c in trace["confirmations"]:
        print("  -", c["actor"], "（%s）" % c["role"], "确认", c["measurement_id"])


if __name__ == "__main__":
    main()
