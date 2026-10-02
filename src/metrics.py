"""指标目录、复核策略与计算口径。

指标分为原始指标（由车间测量或固废/制氢记录产生）与衍生指标（报表发布时
按当前口径由原始值计算）。口径可以发布新版本，但已发布报表保留当时快照。
"""

from __future__ import annotations

from typing import Any

# ---- 角色 -----------------------------------------------------------------

ROLE_MANAGER = "管理人员"
ROLE_OPERATOR = "车间操作人员"
ROLE_ENV = "环保人员"
ROLE_ENERGY = "能源管理人员"
ROLE_PROCESS = "工艺算法人员"
ROLE_WASTE_PARTNER = "固废处理单位"
ROLE_HYDROGEN_PARTNER = "氢能利用单位"

ALL_ROLES = frozenset(
    {
        ROLE_MANAGER,
        ROLE_OPERATOR,
        ROLE_ENV,
        ROLE_ENERGY,
        ROLE_PROCESS,
        ROLE_WASTE_PARTNER,
        ROLE_HYDROGEN_PARTNER,
    }
)

# ---- 指标目录 -------------------------------------------------------------
# code -> (名称, 单位, 类别, 是否衍生)
METRICS: dict[str, tuple[str, str, str, bool]] = {
    "CRUDE_STEEL_OUTPUT": ("粗钢产量", "t", "production", False),
    "QUALITY_PASS_RATE": ("精品钢合格率", "%", "quality", True),
    "ENERGY_INTENSITY": ("吨钢综合能耗", "kgce/t", "energy", False),
    "CO2_REDUCTION": ("碳减排量（较基准）", "tCO2", "energy", True),
    "SO2_EMISSION": ("二氧化硫排放浓度", "mg/m3", "env", False),
    "DUST_EMISSION": ("颗粒物排放浓度", "mg/m3", "env", False),
    "WASTE_AMOUNT": ("工业固废产生量", "t", "waste", False),
    "WASTE_UTIL_RATE": ("固废资源化利用率", "%", "waste", True),
    "HYDROGEN_VOLUME": ("焦炉煤气副产氢产量", "Nm3", "energy", True),
}

DERIVED_CODES = frozenset(code for code, (_, _, _, derived) in METRICS.items() if derived)

# 各类指标所需的复核角色：节能成果必须由环保与能源两条线共同确认，
# 工艺/算法人员不出现在确认角色中，因此不能独自确认节能成果。
REVIEW_POLICY: dict[str, tuple[str, ...]] = {
    "production": (ROLE_MANAGER,),
    "quality": (ROLE_MANAGER,),
    "env": (ROLE_ENV,),
    "waste": (ROLE_ENV,),
    "energy": (ROLE_ENV, ROLE_ENERGY),
}

WASTE_TYPES = ("钢渣", "高炉渣", "除尘灰", "粉煤灰", "脱硫石膏")


def metric_name(code: str) -> str:
    return METRICS[code][0]


def metric_unit(code: str) -> str:
    return METRICS[code][1]


def category_of(code: str) -> str:
    return METRICS[code][2]


def required_reviewers(code: str) -> tuple[str, ...]:
    return REVIEW_POLICY[category_of(code)]


# ---- 口径版本 -------------------------------------------------------------

INITIAL_CALIBER_VERSION = 1

DEFAULT_CALIBER: dict[str, dict[str, Any]] = {
    "ENERGY_INTENSITY": {
        "definition": "班次综合能源消耗量（折标煤）除以粗钢产量",
        "formula": "sum(折标煤消耗 kgce) / 粗钢产量 t",
    },
    "CO2_REDUCTION": {
        "definition": "按基准吨钢综合能耗与实际值之差、粗钢产量和标煤排放因子计算",
        "formula": "(baseline_kgce_per_t - ENERGY_INTENSITY) * CRUDE_STEEL_OUTPUT * factor_tco2_per_tce / 1000",
        "params": {"baseline_kgce_per_t": 560.0, "factor_tco2_per_tce": 2.5},
    },
    "WASTE_UTIL_RATE": {
        "definition": "资源化利用固废量占固废产生总量的比例，按固废处理记录的当前去向汇总",
        "formula": "sum(amount where utilized) / sum(amount) * 100",
    },
    "QUALITY_PASS_RATE": {
        "definition": "精品钢检验合格批次量占检验批次总量的比例",
        "formula": "合格量 / 检验总量 * 100",
    },
}
