# -*- coding: utf-8 -*-
"""Generate a trade-by-trade attribution report for the A144 RedisQMT study."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "analysis" / "a144_redisqmt_20260923"
NAME_PATH = ROOT / "analysis" / "panel_20260920" / "instrument_names.json"
SELECTED_PATH = RESULT_DIR / "selected_trades.csv"
BASELINE_PATH = RESULT_DIR / "baseline_trades.csv"
EQUITY_PATH = RESULT_DIR / "equity.csv"
SUMMARY_PATH = RESULT_DIR / "summary.json"
REPORT_PATH = RESULT_DIR / "每笔盈亏深度分析.md"
LEDGER_CSV_PATH = RESULT_DIR / "逐笔交易明细.csv"
LEDGER_MD_PATH = RESULT_DIR / "逐笔交易明细.md"


def pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


def money(value: float) -> str:
    return f"{value:,.0f}元"


def markdown_table(frame: pd.DataFrame) -> str:
    columns = [str(column) for column in frame.columns]
    lines = ["| " + " | ".join(columns) + " |"]
    lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
    for row in frame.itertuples(index=False, name=None):
        values = [str(value).replace("|", "\\|") for value in row]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def classify_return(value: float) -> str:
    if value <= -0.15:
        return "≤-15%"
    if value <= -0.10:
        return "(-15%,-10%]"
    if value <= -0.05:
        return "(-10%,-5%]"
    if value < 0:
        return "(-5%,0)"
    if value < 0.05:
        return "[0,5%)"
    if value < 0.10:
        return "[5%,10%)"
    if value < 0.20:
        return "[10%,20%)"
    return "≥20%"


def holding_bucket(value: int) -> str:
    if value <= 3:
        return "1-3日"
    if value <= 7:
        return "4-7日"
    if value <= 14:
        return "8-14日"
    if value <= 19:
        return "15-19日"
    return "20日"


def diagnose_trade(row: pd.Series) -> str:
    trade_return = float(row["return"])
    reason = str(row["reason"])
    hold_days = int(row["hold_days"])
    if reason == "stop":
        if hold_days <= 3 and trade_return < -0.12:
            return "早期止损；T+1开盘成交穿越-12%阈值"
        if trade_return < -0.18:
            return "硬止损；开盘跳空使亏损穿越-18%阈值"
        return "止损退出"
    if reason == "market":
        if trade_return >= 0.10:
            return "市场转弱退出，仍保留大幅盈利"
        if trade_return > 0:
            return "市场转弱退出并锁定盈利"
        if trade_return <= -0.10:
            return "市场转弱批量退出，形成较大亏损"
        return "市场转弱退出，小中幅亏损"
    if trade_return >= 0.20:
        return "持满20日，大幅盈利兑现"
    if trade_return > 0:
        return "持满20日，盈利兑现"
    if trade_return <= -0.10:
        return "持满20日仍未修复，较大亏损"
    return "持满20日仍未修复，小中幅亏损"


def longest_streak(values: list[bool], target: bool) -> int:
    longest = 0
    current = 0
    for value in values:
        if value is target:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def grouped_summary(frame: pd.DataFrame, group_column: str) -> pd.DataFrame:
    grouped = frame.groupby(group_column, observed=True)
    result = grouped.agg(
        笔数=("return", "size"),
        胜率=("is_win", "mean"),
        平均收益=("return", "mean"),
        中位收益=("return", "median"),
        收益金额=("pnl_amount", "sum"),
        平均持有日=("hold_days", "mean"),
    ).reset_index()
    result["胜率"] = result["胜率"].map(pct)
    result["平均收益"] = result["平均收益"].map(pct)
    result["中位收益"] = result["中位收益"].map(pct)
    result["收益金额"] = result["收益金额"].map(money)
    result["平均持有日"] = result["平均持有日"].map(lambda value: f"{value:.1f}")
    return result


def drawdown_episode(equity: pd.DataFrame, column: str) -> dict[str, object]:
    values = equity[column].astype(float)
    running_peak = values.cummax()
    drawdown = values / running_peak - 1.0
    trough_index = int(drawdown.idxmin())
    peak_value = float(running_peak.iloc[trough_index])
    peak_candidates = equity.index[: trough_index + 1][values.iloc[: trough_index + 1] == peak_value]
    peak_index = int(peak_candidates[-1])
    recovery = equity.index[trough_index + 1 :][values.iloc[trough_index + 1 :] >= peak_value]
    recovery_date = "截至样本末未恢复"
    if len(recovery):
        recovery_date = str(equity.loc[int(recovery[0]), "date"])
    return {
        "peak_date": str(equity.loc[peak_index, "date"]),
        "trough_date": str(equity.loc[trough_index, "date"]),
        "recovery_date": recovery_date,
        "drawdown": float(drawdown.iloc[trough_index]),
    }


def main() -> None:
    selected = pd.read_csv(SELECTED_PATH, dtype={"code": str, "entry_date": str, "exit_date": str})
    baseline = pd.read_csv(BASELINE_PATH, dtype={"code": str, "entry_date": str, "exit_date": str})
    equity = pd.read_csv(EQUITY_PATH, dtype={"date": str})
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    names = json.loads(NAME_PATH.read_text(encoding="utf-8"))

    identity_columns = ["code", "entry_date", "exit_date", "reason", "hold_days"]
    selected = selected.sort_values(identity_columns).reset_index(drop=True)
    baseline = baseline.sort_values(identity_columns).reset_index(drop=True)
    if not selected[identity_columns].equals(baseline[identity_columns]):
        raise RuntimeError("Candidate and baseline trade identities differ; attribution is invalid.")
    if not np.allclose(selected["return"], baseline["return"], rtol=0.0, atol=1e-12):
        raise RuntimeError("Candidate and baseline per-trade returns differ; attribution is invalid.")

    selected["name"] = selected["code"].map(names).fillna("")
    selected["is_win"] = selected["return"] > 0
    selected["return_bucket"] = selected["return"].map(classify_return)
    selected["holding_bucket"] = selected["hold_days"].map(holding_bucket)
    selected["exit_year"] = selected["exit_date"].str[:4]
    selected["diagnosis"] = selected.apply(diagnose_trade, axis=1)
    selected["baseline_capital_in"] = baseline["capital_in"]
    selected["baseline_pnl_amount"] = baseline["pnl_amount"]
    selected["allocation_ratio"] = selected["capital_in"] / baseline["capital_in"]
    selected["incremental_pnl"] = selected["pnl_amount"] - baseline["pnl_amount"]

    wins = selected[selected["is_win"]]
    losses = selected[~selected["is_win"]]
    gross_profit = float(wins["pnl_amount"].sum())
    gross_loss = float(losses["pnl_amount"].sum())
    realized_pnl = float(selected["pnl_amount"].sum())
    baseline_realized_pnl = float(baseline["pnl_amount"].sum())
    average_win = float(wins["return"].mean())
    average_loss = float(losses["return"].mean())
    payoff_ratio = average_win / abs(average_loss)
    profit_factor = gross_profit / abs(gross_loss)
    breakeven_win_rate = abs(average_loss) / (average_win + abs(average_loss))
    final_gain = float(summary["selected_full"]["final_equity"]) - 1_000_000.0
    open_mark_to_market = final_gain - realized_pnl

    ranked_wins = wins.sort_values("pnl_amount", ascending=False)
    ranked_losses = losses.sort_values("pnl_amount")
    top_1_profit = float(ranked_wins.head(1)["pnl_amount"].sum())
    top_5_profit = float(ranked_wins.head(5)["pnl_amount"].sum())
    top_10_profit = float(ranked_wins.head(10)["pnl_amount"].sum())
    bottom_5_loss = float(ranked_losses.head(5)["pnl_amount"].sum())
    bottom_10_loss = float(ranked_losses.head(10)["pnl_amount"].sum())

    chronological = selected.sort_values(["exit_date", "entry_date", "code"]).reset_index(drop=True)
    win_flags = [bool(value) for value in chronological["is_win"].tolist()]
    max_win_streak = longest_streak(win_flags, True)
    max_loss_streak = longest_streak(win_flags, False)
    loss_streak_start = 0
    loss_streak_end = 0
    current_start = None
    for index, is_win in enumerate(win_flags):
        if not is_win and current_start is None:
            current_start = index
        if is_win and current_start is not None:
            if index - current_start > loss_streak_end - loss_streak_start + 1:
                loss_streak_start = current_start
                loss_streak_end = index - 1
            current_start = None
    if current_start is not None and len(win_flags) - current_start > loss_streak_end - loss_streak_start + 1:
        loss_streak_start = current_start
        loss_streak_end = len(win_flags) - 1
    longest_loss_slice = chronological.iloc[loss_streak_start : loss_streak_end + 1]
    loss_streak_first_date = str(longest_loss_slice.iloc[0]["exit_date"])
    loss_streak_last_date = str(longest_loss_slice.iloc[-1]["exit_date"])
    loss_streak_pnl = float(longest_loss_slice["pnl_amount"].sum())

    reason_summary = grouped_summary(selected, "reason")
    reason_summary = reason_summary.rename(columns={"reason": "退出原因"})
    holding_order = ["1-3日", "4-7日", "8-14日", "15-19日", "20日"]
    selected["holding_bucket"] = pd.Categorical(selected["holding_bucket"], holding_order, ordered=True)
    holding_summary = grouped_summary(selected, "holding_bucket")
    holding_summary = holding_summary.rename(columns={"holding_bucket": "持有期"})
    year_summary = grouped_summary(selected, "exit_year")
    year_summary = year_summary.rename(columns={"exit_year": "退出年份"})
    return_order = ["≤-15%", "(-15%,-10%]", "(-10%,-5%]", "(-5%,0)", "[0,5%)", "[5%,10%)", "[10%,20%)", "≥20%"]
    selected["return_bucket"] = pd.Categorical(selected["return_bucket"], return_order, ordered=True)
    distribution = selected.groupby("return_bucket", observed=True).agg(
        笔数=("return", "size"),
        收益金额=("pnl_amount", "sum"),
    ).reindex(return_order).fillna(0).reset_index()
    distribution = distribution.rename(columns={"return_bucket": "单笔收益区间"})
    distribution["笔数"] = distribution["笔数"].astype(int)
    distribution["收益金额"] = distribution["收益金额"].map(money)

    symbol_summary = selected.groupby(["code", "name"]).agg(
        笔数=("return", "size"),
        胜率=("is_win", "mean"),
        平均收益=("return", "mean"),
        收益金额=("pnl_amount", "sum"),
    ).reset_index()
    symbol_summary["胜率"] = symbol_summary["胜率"].map(pct)
    symbol_summary["平均收益"] = symbol_summary["平均收益"].map(pct)
    best_symbols = symbol_summary.sort_values("收益金额", ascending=False).head(10).copy()
    worst_symbols = symbol_summary.sort_values("收益金额").head(10).copy()
    best_symbols["收益金额"] = best_symbols["收益金额"].map(money)
    worst_symbols["收益金额"] = worst_symbols["收益金额"].map(money)

    top_columns = ["code", "name", "entry_date", "exit_date", "hold_days", "reason", "return", "pnl_amount"]
    top_trades = ranked_wins.head(15)[top_columns].copy()
    worst_trades = ranked_losses.head(15)[top_columns].copy()
    for frame in [top_trades, worst_trades]:
        frame["return"] = frame["return"].map(pct)
        frame["pnl_amount"] = frame["pnl_amount"].map(money)
        frame.columns = ["代码", "名称", "买入日", "卖出日", "持有日", "原因", "收益率", "盈亏金额"]

    stop_trades = selected[selected["reason"] == "stop"][top_columns + ["diagnosis"]].copy()
    stop_trades["return"] = stop_trades["return"].map(pct)
    stop_trades["pnl_amount"] = stop_trades["pnl_amount"].map(money)
    stop_trades.columns = ["代码", "名称", "买入日", "卖出日", "持有日", "原因", "收益率", "盈亏金额", "解释"]

    allocation_by_outcome = selected.groupby("is_win").agg(
        笔数=("return", "size"),
        候选平均投入=("capital_in", "mean"),
        基准平均投入=("baseline_capital_in", "mean"),
        平均投入倍数=("allocation_ratio", "mean"),
        增量盈亏=("incremental_pnl", "sum"),
    ).reset_index()
    allocation_by_outcome["结果"] = allocation_by_outcome["is_win"].map({True: "盈利交易", False: "亏损交易"})
    allocation_by_outcome = allocation_by_outcome.drop(columns="is_win")
    allocation_by_outcome["候选平均投入"] = allocation_by_outcome["候选平均投入"].map(money)
    allocation_by_outcome["基准平均投入"] = allocation_by_outcome["基准平均投入"].map(money)
    allocation_by_outcome["平均投入倍数"] = allocation_by_outcome["平均投入倍数"].map(lambda value: f"{value:.3f}×")
    allocation_by_outcome["增量盈亏"] = allocation_by_outcome["增量盈亏"].map(money)
    allocation_by_outcome = allocation_by_outcome[["结果", "笔数", "候选平均投入", "基准平均投入", "平均投入倍数", "增量盈亏"]]
    allocation_return_corr = float(selected["allocation_ratio"].corr(selected["return"], method="spearman"))

    market_exits = selected[selected["reason"] == "market"]
    market_batches = market_exits.groupby("exit_date").size().sort_values(ascending=False)
    multi_market_exit_count = int(market_batches[market_batches >= 2].sum())
    market_loss_amount = float(market_exits.loc[~market_exits["is_win"], "pnl_amount"].sum())
    market_batch_summary = market_exits.groupby("exit_date").agg(
        退出笔数=("code", "size"),
        批次盈亏=("pnl_amount", "sum"),
        平均收益=("return", "mean"),
    ).reset_index()
    market_batch_summary = market_batch_summary[market_batch_summary["退出笔数"] >= 2]
    market_batch_summary = market_batch_summary.sort_values(["退出笔数", "批次盈亏"], ascending=[False, True]).head(12)
    market_batch_summary = market_batch_summary.rename(columns={"exit_date": "退出日期"})
    market_batch_summary["批次盈亏"] = market_batch_summary["批次盈亏"].map(money)
    market_batch_summary["平均收益"] = market_batch_summary["平均收益"].map(pct)
    max_hold_losses = selected[(selected["reason"] == "max-hold") & (~selected["is_win"])]
    max_hold_loss_amount = float(max_hold_losses["pnl_amount"].sum())

    selected_episode = drawdown_episode(equity, "v1_normalized_inverse_atr_budget")
    baseline_episode = drawdown_episode(equity, "v5_reconstructed")

    ledger = selected.sort_values(["exit_date", "entry_date", "code"]).reset_index(drop=True).copy()
    ledger.insert(0, "序号", np.arange(1, len(ledger) + 1))
    ledger_output = pd.DataFrame(
        {
            "序号": ledger["序号"],
            "代码": ledger["code"],
            "名称": ledger["name"],
            "买入日": ledger["entry_date"],
            "卖出日": ledger["exit_date"],
            "持有交易日": ledger["hold_days"],
            "退出原因": ledger["reason"],
            "买入价": ledger["entry_price"].round(4),
            "卖出价": ledger["exit_price"].round(4),
            "单笔收益率": ledger["return"],
            "候选投入": ledger["capital_in"].round(2),
            "候选盈亏": ledger["pnl_amount"].round(2),
            "基准投入": ledger["baseline_capital_in"].round(2),
            "基准盈亏": ledger["baseline_pnl_amount"].round(2),
            "仓位倍数": ledger["allocation_ratio"].round(4),
            "相对基准增量盈亏": ledger["incremental_pnl"].round(2),
            "单笔归因": ledger["diagnosis"],
        }
    )
    ledger_output.to_csv(LEDGER_CSV_PATH, index=False, encoding="utf-8-sig")

    ledger_md_frame = ledger_output.copy()
    ledger_md_frame["单笔收益率"] = ledger_md_frame["单笔收益率"].map(pct)
    for column in ["候选投入", "候选盈亏", "基准投入", "基准盈亏", "相对基准增量盈亏"]:
        ledger_md_frame[column] = ledger_md_frame[column].map(money)
    ledger_md_frame["仓位倍数"] = ledger_md_frame["仓位倍数"].map(lambda value: f"{value:.3f}×")
    ledger_md_frame = ledger_md_frame.drop(columns=["买入价", "卖出价"])
    ledger_text = "# Alpha144 v1 逐笔交易明细\n\n"
    ledger_text += "共 226 笔已平仓交易；按卖出日、买入日、证券代码排序。金额已计入回测设定的双边交易成本。\n\n"
    ledger_text += markdown_table(ledger_md_frame)
    ledger_text += "\n"
    LEDGER_MD_PATH.write_text(ledger_text, encoding="utf-8")

    report_lines = [
        "# Alpha144 v1 每笔盈亏深度分析",
        "",
        "> 口径：2022-01-04 至 2026-09-18；日线收盘生成信号，下一可交易日开盘成交；初始资金 100 万元；已计入回测设定的买卖费用。本文分析 226 笔已平仓交易，期末未平仓头寸单独列示。",
        "",
        "## 核心结论",
        "",
        f"1. 这是一个**低胜率、依赖右尾大赢家**的策略：胜率 {pct(len(wins) / len(selected))}，但平均盈利 {pct(average_win)}、平均亏损 {pct(average_loss)}，盈亏比 {payoff_ratio:.2f}，高于对应的盈亏平衡胜率 {pct(breakeven_win_rate)}。",
        f"2. 226 笔中盈利 {len(wins)} 笔、亏损 {len(losses)} 笔；中位单笔收益 {pct(float(selected['return'].median()))}，说明“典型交易”其实小亏，组合收益由少数强趋势股抬升。",
        f"3. 前 1/5/10 笔盈利交易贡献 {money(top_1_profit)} / {money(top_5_profit)} / {money(top_10_profit)}，分别占全部已实现毛利润的 {pct(top_1_profit / gross_profit)} / {pct(top_5_profit / gross_profit)} / {pct(top_10_profit / gross_profit)}。去掉前 5 笔赢家后，已实现净盈亏为 {money(realized_pnl - top_5_profit)}。",
        f"4. 优化版与 v5 的证券、进出日期、退出原因和逐笔收益率完全一致。优化版已实现盈亏 {money(realized_pnl)}，基准为 {money(baseline_realized_pnl)}；差额 {money(realized_pnl - baseline_realized_pnl)} 全部来自仓位大小与随后复利路径，不是信号命中率改善。",
        f"5. 最大问题不是 5 笔显式止损，而是大量失败交易拖到 20 日或由市场过滤批量退出：20日到期亏损 {len(max_hold_losses)} 笔，合计 {money(max_hold_loss_amount)}；市场退出中的亏损合计 {money(market_loss_amount)}。",
        f"6. 优化版最大回撤 {pct(float(selected_episode['drawdown']))}，从 {selected_episode['peak_date']} 到 {selected_episode['trough_date']}，恢复日为 {selected_episode['recovery_date']}；基准同期极值为 {pct(float(baseline_episode['drawdown']))}。收益放大的同时，亏损簇也被放大。",
        "",
        "## 总体盈亏画像",
        "",
        f"- 毛盈利：{money(gross_profit)}；毛亏损：{money(gross_loss)}；已实现净盈亏：{money(realized_pnl)}；利润因子：{profit_factor:.2f}。",
        f"- 全部交易平均收益 {pct(float(selected['return'].mean()))}，中位收益 {pct(float(selected['return'].median()))}；最大单笔收益 {pct(float(selected['return'].max()))}，最大单笔亏损 {pct(float(selected['return'].min()))}。",
        f"- 最长连续盈利 {max_win_streak} 笔；最长连续亏损 {max_loss_streak} 笔，退出日期横跨 {loss_streak_first_date} 至 {loss_streak_last_date}，合计亏损 {money(loss_streak_pnl)}。连续亏损是该策略的常态风险，不能用单笔胜率判断是否失效。",
        f"- 期末权益较初始资金增加 {money(final_gain)}，其中已平仓净盈亏 {money(realized_pnl)}，差额约 {money(open_mark_to_market)} 来自期末仍持有头寸的市值变化（回测没有在样本末强制平仓）。",
        "",
        "### 单笔收益分布",
        "",
        markdown_table(distribution),
        "",
        "## 退出机制归因",
        "",
        markdown_table(reason_summary),
        "",
        f"市场退出共 {len(market_exits)} 笔，其中 {multi_market_exit_count} 笔发生在同日退出至少两只股票的批次中。它更像组合级风险开关：能同步降仓，但也会把同一市场冲击下的相关亏损一次性确认。",
        "",
        "### 规模最大的市场退出批次",
        "",
        markdown_table(market_batch_summary),
        "",
        "### 5 笔显式止损",
        "",
        markdown_table(stop_trades),
        "",
        "止损阈值不是成交价格保证。策略在收盘识别止损，下一可交易日开盘成交；若隔夜跳空，实际亏损会穿越 -12% 或 -18% 阈值。",
        "",
        "## 持有期规律",
        "",
        markdown_table(holding_summary),
        "",
        "20 日组同时装着最强趋势赢家和大量未修复亏损。固定持有上限承担了事实上的主要退出职责，因此下一轮优化应优先研究 8–20 日间的弱势退出，而不是继续微调少量 stop 交易。",
        "",
        "## 年份与市场阶段",
        "",
        markdown_table(year_summary),
        "",
        "年度结果高度不均匀。训练段收益很弱、2025 年后明显增强，表明策略收益依赖特定市场阶段；这与当前成分股快照带来的生存者偏差叠加，不能把近两年的强表现直接外推。",
        "",
        "## 仓位优化究竟做了什么",
        "",
        markdown_table(allocation_by_outcome),
        "",
        f"仓位倍数与单笔收益率的 Spearman 相关系数为 {allocation_return_corr:.3f}。该数值只能描述本样本中的同向程度，不能证明逆 ATR 能预测赢家；而且倍数还混入了账户复利差异。当前证据支持的结论是“更充分地使用资金放大了原信号”，而不是“风险预算改善了选股质量”。",
        "",
        "## 最大盈利交易（前15）",
        "",
        markdown_table(top_trades),
        "",
        "## 最大亏损交易（前15）",
        "",
        markdown_table(worst_trades),
        "",
        f"前 5 笔最大亏损合计 {money(bottom_5_loss)}，前 10 笔合计 {money(bottom_10_loss)}。损失尾部没有盈利尾部那么长，但亏损出现频率更高，因此实际体验通常是长时间小亏、偶尔靠大赢家修复。",
        "",
        "## 标的重复性",
        "",
        f"226 笔交易覆盖 {selected['code'].nunique()} 只股票。以下分别为累计贡献最高和最低的 10 只；多次交易同一股票时，累计结果比单次极值更能反映信号适配性。",
        "",
        "### 累计贡献最高",
        "",
        markdown_table(best_symbols),
        "",
        "### 累计贡献最低",
        "",
        markdown_table(worst_symbols),
        "",
        "## 潜在规律与可验证建议",
        "",
        "1. **保留右尾，不宜设置紧利润封顶。** 收益由少数 20% 以上、甚至翻倍的交易驱动；固定止盈会直接破坏策略的正期望结构。若增加退出规则，应采用趋势转弱或回撤式保护，而不是目标价止盈。",
        "2. **优化重点放在失败交易的时间止损。** 对持有 8–19 日仍处于亏损、相对强度持续走弱、量价冲击未修复的头寸做独立消融测试；目标是减少 20 日到期亏损，同时确认不会误杀后程大赢家。",
        "3. **市场退出应从全开全关改为分级降险候选。** 先验证减半、禁止新开仓、仅退出弱势持仓三种处理，避免相关持仓同日集中兑现亏损。此处只是研究建议，尚未证明优于现机制。",
        "4. **逆 ATR 仓位必须增加组合层风险约束。** 当前绝对收益提高但最大回撤扩大。建议测试单行业上限、相关性簇上限和组合波动目标，并以样本外 Calmar/最大回撤为主指标，而不是继续追求全样本收益。",
        "5. **对极端赢家做稳健性压力测试。** 至少报告剔除前 1、前 5、前 10 大赢家后的净收益，并进行刷新相位集合测试；若多数相位必须依赖极少数股票才赚钱，不应实盘放大。",
        "6. **先修数据偏差，再继续调参。** 使用历史中证500成分和历史行业分类重跑；当前股票池是 2026-09-18 快照，逐笔规律包含生存者偏差。",
        "",
        "## 文件说明",
        "",
        "- `逐笔交易明细.csv`：便于筛选、排序和二次统计，含候选/基准仓位与增量盈亏。",
        "- `逐笔交易明细.md`：226 笔逐笔可读清单，每笔附简短归因。",
        "- `selected_trades.csv`、`baseline_trades.csv`：回测原始成交输出。",
        "",
        "以上规律均为历史回测中的相关性与结构性观察，不构成未来收益保证。",
    ]
    REPORT_PATH.write_text("\n".join(report_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
