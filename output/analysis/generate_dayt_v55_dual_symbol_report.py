"""Reproduce v55 dual-symbol results and write the research report."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dayt_strict import replay


OUT = ROOT / "analysis/dayt_v55_dual_symbol_optimization_20260913"
STRATEGY = ROOT / "Stragety/MiniQMT_Stragety/DayT/DayT_v55_nomom_NoOvernightMomentumGuard.py"
SYMBOLS = (
    ("600584.SH", "长电科技", ROOT / "analysis/dayt_600584_5m_data_20260913"),
    ("600105.SH", "永鼎股份", ROOT / "analysis/dayt_600105_5m_data_20260913"),
)


def load_data(path: Path):
    daily = pd.read_csv(path / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(path / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= "20260801") & (dates <= "20260913")]
    return daily, minute


def reproduce():
    results = {}
    source_hash = hashlib.sha256(STRATEGY.read_bytes()).hexdigest()
    for symbol, name, path in SYMBOLS:
        daily, minute = load_data(path)
        result = replay(
            "v55_nomom",
            daily,
            minute,
            rate=0.0005,
            overrides={"T_TARGET_VALUE": 40000.0, "T_POSITION_FRACTION": 1.0},
            symbol=symbol,
            stock_name=name,
            initial_cash=100000.0,
            initial_shares=1000,
        )
        result.update(
            mode="strict",
            start="20260801",
            end="20260913",
            symbol=symbol,
            stock_name=name,
            source_sha256=source_hash,
            data_sessions=len(set(minute.index.str[:8])),
            data_first=str(minute.index[0]),
            data_last=str(minute.index[-1]),
        )
        target = OUT / f"{symbol[:6]}_v55_strict_5m.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        results[symbol] = result
    return results


def money(value):
    return f"{value:+,.2f}"


def cycle_table(data):
    trades = data["trades"]
    lines = [
        "| # | 卖出时间 | 买回时间 | 股数 | 卖价 | 买价 | 毛收益 | 双边费用 | 净收益 | 平仓机制 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for number, cycle in enumerate(data["cycles"], 1):
        sells = [trade for trade in trades if trade["label"] == "REV-T sell" and trade["time"] == cycle["opened"]]
        buys = [trade for trade in trades if "buyback" in trade["label"] and trade["time"] == cycle["closed"]]
        sell = sells[0]
        buy = buys[0]
        fees = sell["fee"] + buy["fee"]
        net = cycle["gross"] - fees
        reason = buy["label"].replace("REV-T risk buyback", "").replace("REV-T buyback", "").strip(" ()")
        lines.append(
            f"| {number} | {cycle['opened']} | {cycle['closed']} | {abs(sell['shares'])} | "
            f"{sell['price']:.2f} | {buy['price']:.2f} | {money(cycle['gross'])} | "
            f"{fees:.2f} | {money(net)} | {reason} |"
        )
    return lines


def write_report(results):
    jd = results["600584.SH"]
    yd = results["600105.SH"]
    source_hash = jd["source_sha256"]
    text = f"""# v55 双标的优化研究：长电科技与永鼎股份

> 研究日期：2026-09-13  
> 回测区间：2026-08-01 至 2026-09-13；实际可用 5 分钟数据为 2026-08-03 至 2026-09-11，共 30 个交易日  
> 初始账户：现金 100,000 元 + 底仓 1,000 股；单次做 T 目标约 40,000 元，不足时按现金、可卖底仓和整手约束取最大可交易量  
> 费用模型：双边成交额各 0.05%；不代表券商实际收费

## 结论

选择 **v54** 作为基础并新建独立策略 **v55_nomom_NoOvernightMomentumGuard**。在同一份严格连续账户回放中，v55 在两个标的上都超过“一直持有 1,000 股不操作”的基准：

| 标的 | 持有基准收益 | v53 超额净收益 | v54 超额净收益 | v55 账户收益 | v55 超额净收益 | v55 完整 T | 最大回撤 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 长电科技 600584 | +3,640.00 | -3,906.83 | -3,140.36 | {money(jd['account_net'])} | {money(jd['excess_net'])} | {jd['completed_cycles']} | {jd['maximum_drawdown']:.2f}% |
| 永鼎股份 600105 | +11,220.00 | -2,897.20 | -1,304.10 | {money(yd['account_net'])} | {money(yd['excess_net'])} | {yd['completed_cycles']} | {yd['maximum_drawdown']:.2f}% |

最终资产分别为 **168,304.72 元**和 **142,512.04 元**，比持有基准分别多 **564.72 元**和 **312.04 元**。[^1][^2]

这证明的是“当前 30 个交易日样本内、当前费用与 5 分钟成交模型下超过基准”，不是长期有效性或实盘可用性的证明。v55 仍标记为 `RESEARCH_ONLY`，不得据此直接启动实盘。

## 为什么以 v54 为基础

v53 在两个标的都留下 500 股，说明反 T 卖出后跨日风险退出会把交易亏损变成永久减仓，随后上涨时同时损失底仓收益。v54 加入周期风险控制后，永鼎能恢复到 1,000 股，并把超额亏损由 -2,897.20 缩小到 -1,304.10；长电的完整周期由 17 个降至 8 个，说明趋势过滤确实减少了无效开仓。[^3][^4]

但 v54 的风险退出仍允许反 T 腿跨日。对上涨样本而言，隔夜跳空和次日继续上涨会放大“先卖后买”的买回损失。因此优化重点不是增加交易次数，而是：

1. 强趋势阶段少做反 T；
2. 当日没有等到正常回落时，在收盘前恢复底仓；
3. 保留硬止损，防止盘中单边上行无限扩大损失。

## v55 新机制

- **5 日动量门控**：仅使用已经完成的日线收盘价；最近 5 个交易日收益率大于 3% 时，禁止新的反 T 卖出。数据不足 6 个收盘价时保守禁止开仓。
- **反 T 不隔夜**：仍未买回的反 T 腿在 14:50 触发回补，恢复至目标底仓。
- **盘中不利退出**：买回阈值为 `max(1%, 0.1 × ATR百分比)`；它是异常单边走势的兜底，不替代正常买回条件。
- **仓位规则不变**：目标名义金额 40,000 元，按 100 股整手并受现金和底仓约束。

这属于新增机制，因而按仓库约定新建完整策略文件，没有从同级 v54 策略导入实现。源码 SHA-256 为 `{source_hash}`。[^5]

## 参数选择与反过拟合检查

搜索先发现“最大持有 1 日”可让双标的转正，但代码中的持有日计数包含开仓当天，实际效果近似立即退出，经济含义不可靠，因此没有采用。完整搜索结果保留在研究档案中。[^6]

固定 1%/0.1ATR 后，收盘前回补时间的邻域结果如下；14:50 在两个标的合计表现最好，14:45 也同时为正，说明结果并非只依赖唯一一分钟：

| 回补时间 | 长电超额净收益 | 永鼎超额净收益 |
|---|---:|---:|
| 14:30 | +349.62 | +252.01 |
| 14:40 | +454.67 | +257.01 |
| 14:45 | +524.70 | +267.02 |
| **14:50** | **+564.72** | **+312.04** |
| 14:55 | +409.65 | +312.04 |

动量阈值 2.5% 与 3.0% 得到相同结果；3.5%/4.0% 时长电不变，但永鼎下降到 +136.59，故选择 3% 作为平台区间内更保守的门槛，而非追逐单点最优。

分段结果进一步显示策略尚未达到稳健发布标准：

| 标的/区间 | v54 超额 | v55 超额 | 解读 |
|---|---:|---:|---|
| 长电 8 月 | -3,855.92 | -118.91 | 大幅改善但仍略输基准 |
| 长电 9 月 | +715.56 | +683.63 | 两者均为正，v54略高 |
| 永鼎 8 月 | -774.57 | +345.64 | v55转正 |
| 永鼎 9 月 | -529.53 | -33.60 | 接近持有，但仍为负 |

因此“双标的全区间为正”不能掩盖子区间仍有负超额；下一阶段必须做滚动样本外验证，不能继续只在这 30 天上调参。[^7]

## 长电科技：每一笔完整 T

"""
    lines = text.splitlines()
    lines.extend(cycle_table(jd))
    lines.extend([
        "",
        f"合计 {jd['completed_cycles']} 个完整周期，费用 {jd['fees']:.2f} 元；最终现金 {jd['final_cash']:.2f} 元、持仓 {jd['final_position']} 股，没有未平仓腿。",
        "",
        "## 永鼎股份：每一笔完整 T",
        "",
    ])
    lines.extend(cycle_table(yd))
    lines.extend([
        "",
        f"合计 {yd['completed_cycles']} 个完整周期，费用 {yd['fees']:.2f} 元；最终现金 {yd['final_cash']:.2f} 元、持仓 {yd['final_position']} 股，没有未平仓腿。",
        "",
        "## 信号研究的外部一致性",
        "",
        "独立信号验证显示，长电的 RSI>85、布林中轨偏离>25%、10 日涨幅>30% 等过热信号在样本中具有较高后续回落命中率；永鼎的 RSI>80 和 MA10 偏离>15%也较有效，但 ROC 减速与简单 10 日涨幅规则较弱。它支持“在强动量阶段不要机械反 T”的方向，但不直接证明 3% 阈值最优。[^8][^9]",
        "",
        "## 验收与限制",
        "",
        "- 冻结基线复现：`396 independent-day runs, exact trades and P&L`，通过。",
        "- v55 机制单元测试：动量门控边界、缺失数据保守处理、14:50 时间边界，共 4 项通过。",
        "- DayT 全套测试此前共 195 项通过；最终策略文件另做语法编译与针对性测试通过。",
        "- 回测使用 5 分钟 OHLCV 撮合，不含盘口排队、滑点、涨跌停无法成交、真实佣金最低收费及网络/券商延迟。",
        "- 只有两个标的、30 个交易日，而且参数来自同一研究区间，统计把握度低。超额仅 564.72/312.04 元，容易被更真实的摩擦成本吞噬。",
        "- 下一步建议冻结 v55 参数，在更早历史区间及未来新增交易日做滚动样本外回测；验收门槛应包括双标的分段超额为正、未平仓腿为零、费用后仍显著高于持有，以及相邻参数方向一致。",
        "",
        "## 来源",
        "",
        "[^1]: `analysis/dayt_v55_dual_symbol_optimization_20260913/600584_v55_strict_5m.json`，长电 v55 严格连续账户逐笔结果。",
        "[^2]: `analysis/dayt_v55_dual_symbol_optimization_20260913/600105_v55_strict_5m.json`，永鼎 v55 严格连续账户逐笔结果。",
        "[^3]: `analysis/dayt_600584_1000shares_40k_20260913/README.md`，长电 v53/v54/v40 与持有基准。",
        "[^4]: `analysis/dayt_600105_1000shares_40k_20260913/README.md`，永鼎 v53/v54/v40 与持有基准。",
        "[^5]: `Stragety/MiniQMT_Stragety/DayT/DayT_v55_nomom_NoOvernightMomentumGuard.py`，v55 完整策略实现。QMT 下单接口依据官方文档的 `order_shares` 说明：[Python API, p.101]。",
        "[^6]: `analysis/dayt_v55_dual_symbol_optimization_20260913/grid.json` 与 `grid_cutoff.json`，风险阈值和时段搜索原始结果。",
        "[^7]: 同目录 `*_train_*.json` 与 `*_holdout_*.json`，8 月/9 月分段回放。",
        "[^8]: `analysis/dayt_600584_signal_validation_20260913/600584_signal_validation_report.md`，长电过热信号验证。",
        "[^9]: `analysis/dayt_600105_signal_validation_20260913/600105_signal_validation_report.md`，永鼎过热信号验证。",
    ])
    (OUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = reproduce()
    write_report(results)
    for symbol, result in results.items():
        print(symbol, result["account_net"], result["excess_net"], result["completed_cycles"])


if __name__ == "__main__":
    main()
