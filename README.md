# 钢铁绿色转型指标与副产物闭环

贯通精品钢生产、固废资源化、焦炉煤气制氢和环保绩效的可追溯数据服务。
同一批班次数据同时支撑产品质量、碳减排和副产物去向，且彼此不再矛盾：
指标全部从冻结的数据来源与口径计算，冲突走人工核对，历史报表不可重写。

## 数据链

```
产线 → 工序 → 原料批次
             ↓
班次（测量 / 精品钢批次 / 固废记录 / 制氢记录 / 异常工况登记）
             ↓
       冻结口径版本 → 指标快照 → 报表（指纹）
                             ↓
                     合作单位结算
```

## 核心规则

- **班次冻结**：冻结数据来源集合与计算口径版本；冻结后原始数据只读。
- **幂等上传**：同一班次数据重复提交返回同一结果；换键的内容重复同样幂等。
- **冲突挂账**：同班次同指标数值不一致时双方挂起，生成冲突单交管理人员核对，
  选定值重新复核，落败值作废。
- **职责分离**：车间可提交测量，环保人员/能源管理人员按指标类别复核，
  固废记录由环保人员复核、制氢记录由能源管理人员复核；工艺算法人员不能
  确认任何节能成果，提交人不能自审。
- **局部影响**：设备检修、检测迟到只标记相关指标（能耗标记传播至碳减排）；
  副产物改用途只改变相关衍生指标的后续视图并作废旧结算。
- **历史不可变**：口径只能递增发布；已发布报表带内容指纹，口径变更和
  改用途都不重写它，需要时发布新报表，旧报表与旧结算留痕。
- **可追溯**：从任一指标可回溯到原料批次、工序、处理记录、合作单位和
  每一步责任确认（`GreenSteelLedger.trace`）。
- **仅追加存储**：JSONL 事件带序号与哈希链，改写历史事件在回放时即被发现。

## 代码结构

| 路径 | 说明 |
| --- | --- |
| `src/metrics.py` | 角色、指标目录、复核策略与计算口径 |
| `src/models.py` | 事件信封与领域实体 |
| `src/store.py` | 仅追加、带哈希链的 JSONL 事件存储 |
| `src/ledger.py` | `GreenSteelLedger`：全部业务命令、快照计算、报表与追溯 |
| `src/steel_context.py` | 领域资料（参与方/事实/约束）读取校验 |
| `contracts/context.schema.json` | 领域资料结构 |
| `fixtures/context.json` | 不含真实身份信息的领域资料示例 |
| `examples/shift_workflow.py` | 一个班次的端到端演示 |
| `tests/` | 29 个规则测试（台账）+ 领域资料测试 |

## 快速开始

```python
from src.ledger import GreenSteelLedger
from src.metrics import ROLE_ENV, ROLE_ENERGY, ROLE_MANAGER, ROLE_OPERATOR

lg = GreenSteelLedger("data/events.jsonl")

lg.register_line("张管理", ROLE_MANAGER, "L1", "精品钢1号线")
lg.register_process("张管理", ROLE_MANAGER, "P1", "L1", "高炉炼铁", 1)
lg.open_shift("王车间", ROLE_OPERATOR, "SH1", "L1", "2026-10-01", 1)

mid = lg.submit_measurement("王车间", ROLE_OPERATOR, "SH1",
                            "ENERGY_INTENSITY", 545.0, "upload-002")
lg.confirm_measurement("赵环保", ROLE_ENV, mid)       # 环保复核
lg.confirm_measurement("孙能源", ROLE_ENERGY, mid)   # 能源复核（节能成果双线确认）

lg.freeze_shift("张管理", ROLE_MANAGER, "SH1")
report_id = lg.publish_report("张管理", ROLE_MANAGER, "SH1")

trace = lg.trace("SH1", "CO2_REDUCTION")  # 指标 → 测量 → 班次 → 工序 → 原料 → 责任人 → 报表
```

端到端演示：

```bash
python3 examples/shift_workflow.py
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests examples
```

两条命令只读写仓库内文件（测试使用临时目录），不需要连接外部业务系统。
