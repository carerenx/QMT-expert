"""冻结配置 —— **回测与实盘共用的唯一一份逻辑**。

## 为什么这个模块必须存在

仓库里出过的事故：`Stragety/StockPickingStrategy/*/backtest_v*.py` 的
`DATA_DIR` 被写死成 `d:\\02Project\\QMT-expert\\strategy_v6_final\\data`
（在本机根本不存在），而研究结论却引用着那些脚本产出的文件。
根因是**逻辑与路径双双复制**。

对策有两条，都在这里：

1. **逻辑只有一份实现**：因子计算与权重构造都在本模块的
   `build_target_weights()` 里，回测引擎和实盘信号脚本都调它。
   **实盘脚本不允许自己重新实现一遍因子。**
2. **配置可审计**：`config_hash()` 把冻结配置变成一串 sha256，
   实盘信号文件里会带上它。日后追查「实盘跑的是哪一版」时，
   比对这串哈希即可。

## 冻结流程（对应预注册第七节）

1. Stage 4 在 IS 上按预注册规则选出**唯一**配置
2. 把参数填进下面的 `FROZEN`
3. **提交**，记下 `config_hash()`
4. 提交之后**恰好跑一次** OOS-2，看完不许改

在 `FROZEN` 被填上之前调用 `build_target_weights()` 会**直接抛错** ——
这是故意的。空配置静默跑出结果，比报错危险得多。
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from Stragety.RedisQMT.Portfolio.portfolio import factors as factor_lib
from Stragety.RedisQMT.Portfolio.portfolio import transforms
from Stragety.RedisQMT.Portfolio.portfolio.construct import select_holdings, target_weights
from Stragety.RedisQMT.Portfolio.portfolio.panel import PanelData


# ─────────────────────── 冻结配置 ───────────────────────
# Stage 4 结束前保持 None。填的时候连同选它的依据一起写进注释。
FROZEN: dict | None = None

# 期望的形状（填 FROZEN 时按这个键名填）：
# {
#   "factor":        "F2_reversal_1m",      # Stragety/RedisQMT/Portfolio/portfolio/factors.py 的键
#   "treatment":     "size_neutral",        # raw | size_neutral
#   "horizon_days":  20,                    # 只用于文档：因子本身的持有期口径
#   "n_holdings":    30,
#   "rebalance_rule": "M",                  # "M" = 月末；或形如 "20" 的交易日数
#   "buffer_entry":  0.20,
#   "buffer_exit":   0.40,
#   "weight_scheme": "equal",
#   "min_listed_days": 250,
#   "max_participation": 0.01,
#   "timing_variant": None,                 # None = 不做择时；或 as_is/rescaled/binary
#   "timing_index":  "000300.SH",
#   "selection":     "IS 2011-2018 净 Sharpe 最高（预注册规则）",
# }


def is_frozen() -> bool:
    return FROZEN is not None


def require_frozen() -> dict:
    if FROZEN is None:
        raise RuntimeError(
            "配置尚未冻结。按预注册第七节，Stage 4 结束、在 IS 上选出唯一配置后"
            "才能填 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py` 的 FROZEN，提交后再跑 OOS-2。\n"
            "在此之前调用本模块会直接报错 —— 空配置静默产出结果比报错危险得多。")
    return FROZEN


def config_hash(config: dict | None = None) -> str:
    payload = config if config is not None else require_frozen()
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ─────────────────────── 共享逻辑 ───────────────────────

def build_alpha(panel: PanelData, config: dict | None = None) -> pd.DataFrame:
    """算因子并做截面预处理。返回 `(code, date, alpha)`。

    **回测与实盘共用这一个函数。**
    """
    config = config or require_frozen()
    raw = panel.long
    base = factor_lib.compute(raw, config["factor"])
    if base.empty:
        raise RuntimeError(f"因子 {config['factor']} 产出为空")

    size = None
    if config["treatment"] == "size_neutral":
        size = transforms.size_proxy(raw)
    return transforms.preprocess(
        base, size=size, neutralize=(config["treatment"] == "size_neutral"))


def build_target_weights(panel: PanelData, signal_date: str,
                         current_weights: dict[str, float],
                         equity: float,
                         gross_exposure: float = 1.0,
                         config: dict | None = None) -> pd.Series:
    """给定信号日，算目标权重。**回测与实盘共用这一个函数。**

    时序：只用 `panel` 里 `date <= signal_date` 的数据；
    调用方负责在 T+1 用开盘价执行。
    """
    config = config or require_frozen()
    alpha = build_alpha(panel, config)
    day = alpha[alpha["date"] == signal_date].set_index("code")["alpha"]

    max_price = None
    if config.get("n_holdings"):
        max_price = equity / config["n_holdings"] / 100.0

    eligible = panel.eligible(
        signal_date,
        min_listed_days=config.get("min_listed_days", 250),
        require_liquid=True,
        max_price=max_price)

    ranked = day.reindex(eligible).dropna().sort_values(ascending=False)
    held = [c for c in current_weights if current_weights[c] > 0]
    selected = select_holdings(ranked, held, config["n_holdings"],
                               config["buffer_entry"], config["buffer_exit"])
    weights = target_weights(ranked, selected, config["weight_scheme"])
    return weights * gross_exposure


def alpha_fn(panel: PanelData):
    """给引擎用的闭包 —— 引擎要求 `alpha_fn(panel) -> 长表`。"""
    config = require_frozen()
    return lambda p: build_alpha(p, config)
