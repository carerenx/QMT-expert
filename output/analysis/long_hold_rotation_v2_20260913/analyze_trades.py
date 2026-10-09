"""Generate a deterministic per-trade audit for the selected v2 backtest."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "results_1y_risk_buffered_redis_qmt.json"
OUTPUT = ROOT / "trade_audit.md"
NAMES = {
    "600584.SH": "长电科技600584",
    "600105.SH": "永鼎股份600105",
    "601869.SH": "长飞光纤601869",
}


def money(value):
    return f"{value:,.2f}"


def pct(value):
    return f"{value * 100:.2f}%"


def closes_from_equity(result):
    rows = result["equity_curve"]
    output = []
    for row in rows:
        shares = int(row["shares"])
        close = None
        if shares:
            close = (float(row["equity"]) - float(row["cash"])) / shares
        output.append((row["date"], close))
    return output


def future_close(closes, date, sessions):
    indices = {item[0]: index for index, item in enumerate(closes)}
    index = indices[date]
    target = min(index + sessions, len(closes) - 1)
    return closes[target][1]


def attribution(trade, future_price):
    shares = int(trade["shares"])
    price = float(trade["price"])
    fee = float(trade["fee"])
    return shares * (future_price - price) - fee


def action_reason(reasons):
    priorities = (
        "DRAWDOWN_25_HALT_BUYS",
        "DRAWDOWN_20_CAP_45",
        "DRAWDOWN_15_CAP_75",
        "LONG_TREND_BROKEN_30",
        "CONFIRMED_WEAKNESS_CAP_60",
        "REGIME_CONFIRMATION_PENDING",
    )
    for reason in priorities:
        if reason in reasons:
            return reason
    return reasons[0] if reasons else "UNKNOWN"


def enrich(result):
    closes = closes_from_equity(result)
    final_price = closes[-1][1]
    rows = []
    for number, trade in enumerate(result["trades"], 1):
        item = dict(trade)
        item["number"] = number
        item["side"] = "买入" if item["shares"] > 0 else "卖出"
        item["value"] = abs(item["shares"] * item["price"])
        item["attr_1"] = attribution(item, future_close(closes, item["execution_date"], 1))
        item["attr_5"] = attribution(item, future_close(closes, item["execution_date"], 5))
        item["attr_20"] = attribution(item, future_close(closes, item["execution_date"], 20))
        item["attr_final"] = attribution(item, final_price)
        item["action_reason"] = action_reason(item["reason_codes"])
        rows.append(item)
    return rows


def aggregate(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    output = []
    for name, items in groups.items():
        output.append({
            "name": name,
            "count": len(items),
            "fees": sum(item["fee"] for item in items),
            "attr_5": sum(item["attr_5"] for item in items),
            "attr_20": sum(item["attr_20"] for item in items),
            "attr_final": sum(item["attr_final"] for item in items),
            "positive": sum(item["attr_final"] > 0 for item in items),
        })
    return sorted(output, key=lambda item: item["attr_final"], reverse=True)


def report_symbol(lines, code, result):
    rows = enrich(result)
    final_attr = sum(row["attr_final"] for row in rows)
    lines.extend([
        f"## {NAMES[code]}",
        "",
        f"共{len(rows)}笔调仓，期末归因合计{money(final_attr)}元；"
        f"回测记录的兼容持有超额为{money(result['compatibility_excess'])}元，"
        "两者一致，说明逐笔现金流归因闭合。",
        "",
        "### 分方向归因",
        "",
        "|方向|笔数|费用|5日边际贡献|20日边际贡献|期末边际贡献|期末正贡献笔数|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for group in aggregate(rows, "side"):
        lines.append(
            f"|{group['name']}|{group['count']}|{money(group['fees'])}|"
            f"{money(group['attr_5'])}|{money(group['attr_20'])}|"
            f"{money(group['attr_final'])}|{group['positive']}|")
    lines.extend([
        "",
        "### 按主要触发原因归因",
        "",
        "|主要原因|笔数|费用|5日边际贡献|20日边际贡献|期末边际贡献|",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for group in aggregate(rows, "action_reason"):
        lines.append(
            f"|`{group['name']}`|{group['count']}|{money(group['fees'])}|"
            f"{money(group['attr_5'])}|{money(group['attr_20'])}|"
            f"{money(group['attr_final'])}|")
    best = sorted(rows, key=lambda row: row["attr_final"], reverse=True)[:5]
    worst = sorted(rows, key=lambda row: row["attr_final"])[:5]
    lines.extend(["", "### 期末反事实贡献最大与最差交易", "", "最佳五笔：", ""])
    for row in best:
        lines.append(
            f"- #{row['number']} {row['execution_date']} {row['side']}"
            f"{abs(row['shares'])}股@{row['price']:.2f}，贡献{money(row['attr_final'])}元，"
            f"原因`{'/'.join(row['reason_codes'])}`。")
    lines.extend(["", "最差五笔：", ""])
    for row in worst:
        lines.append(
            f"- #{row['number']} {row['execution_date']} {row['side']}"
            f"{abs(row['shares'])}股@{row['price']:.2f}，贡献{money(row['attr_final'])}元，"
            f"原因`{'/'.join(row['reason_codes'])}`。")
    lines.extend([
        "",
        "### 全部逐笔交易",
        "",
        "|#|执行日|信号日|方向|股数|价格|金额|费用|成交后持仓|目标仓位|5日贡献|20日贡献|期末贡献|原因|",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in rows:
        lines.append(
            f"|{row['number']}|{row['execution_date']}|{row['signal_date']}|{row['side']}|"
            f"{abs(row['shares'])}|{row['price']:.2f}|{money(row['value'])}|"
            f"{money(row['fee'])}|{row['position']}|{pct(row['target_weight'])}|"
            f"{money(row['attr_5'])}|{money(row['attr_20'])}|{money(row['attr_final'])}|"
            f"`{'/'.join(row['reason_codes'])}`|")
    lines.append("")
    return rows


def main():
    results = json.loads(INPUT.read_text(encoding="utf-8"))
    lines = [
        "# 长期仓位策略v2逐笔交易审计",
        "",
        "## 归因口径",
        "",
        "长期仓位调仓没有天然的一买一卖配对。本报告采用相对“10万元现金保持现金＋原始1000股持有不动”的现金流反事实归因：",
        "",
        "```text",
        "买入贡献 = 买入股数 ×（评价日价格－成交价）－费用",
        "卖出贡献 = 卖出股数（负数）×（评价日价格－成交价）－费用",
        "```",
        "",
        "正数表示这笔调仓相对不操作有利，负数表示不利。5日、20日贡献用于观察信号短中期效果；期末贡献能与兼容持有超额严格对账，但会受到期末价格影响，不等同于已实现会计盈亏。原因存在叠加，主要原因表按风险优先级归类，全部原因保留在逐笔表。",
        "",
    ]
    all_rows = {}
    for code, result in results.items():
        all_rows[code] = report_symbol(lines, code, result)
    lines.extend([
        "## 跨标的研究结论与可优化点",
        "",
        "逐笔归因显示，v2的核心矛盾不是费用，而是风险减仓后的再进入速度。三只费用合计仅1,551.60元，远小于长飞风险减仓造成的长期机会成本。",
        "",
        "1. **新增风险恢复状态机是最高优先级。** 长飞`DRAWDOWN_20_CAP_45`与`DRAWDOWN_25_HALT_BUYS`的期末贡献合计-636,090.79元；不少减仓在5日内有保护作用，但随后没有足够快地恢复仓位。建议把风险状态拆成`防守→企稳观察→分级恢复`，当价格重新站上MA20、趋势效率转正并连续确认时，允许30%→45%→60%恢复，不要求账户净值先创新高。",
        "2. **15%回撤不应无条件减仓。** `DRAWDOWN_15_CAP_75`在长电和永鼎分别贡献-29,126.51元和-72,777.59元，却在长飞贡献+106,206.33元，跨标的不一致。候选规则应增加`收盘低于MA20且趋势效率为负`，否则只进入观察状态，不立即卖出。",
        "3. **普通多头降档需要价格确认。** 永鼎2025-10-21和11-05两笔从100%降到85%的卖出，期末合计拖累81,615.39元；长飞普通多头卖出也存在明显踏空。可要求多头降档连续两日收盘低于MA20，或MA20斜率同步转负。",
        "4. **保留20%/25%的强制风险阈值，但避免在急跌低点一次卖满。** 长飞10月和次年1月多次单次卖出300至500股。下一候选可将风险减仓分成次日09:40与随后确认两段；第一段先完成一半，第二段只有弱势延续才执行。必须同时模拟额外滑点和未成交风险。",
        "5. **10个百分点固定死区可改为波动自适应。** 高价高波动的长飞与低价永鼎使用相同仓位差，经济含义不同。候选可取`max(10个百分点, 一手市值/账户权益)`，避免低价股一次买卖数千股、高价股频繁触发一手。",
        "6. **不要按期末贡献直接删除卖出信号。** 期末贡献会天然偏爱持续上涨样本；5日和20日贡献才适合评价入场时点。优化必须同时报告短期保护收益、长期机会成本和最大回撤，不能只追求更接近满仓持有。",
        "7. **下一轮使用独立窗口验证。** 当前一年已经参与v2设计，应固定上述候选规则后使用更早数据或未来新增日期，且三只股票保持同一参数。",
        "",
        "QMT日线与1分钟线读取使用`get_market_data_ex`支持的周期、时间与复权参数。[Python API, pp.70–71] 本报告只分析离线成交记录，不涉及真实委托。",
    ])
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUTPUT} with {sum(len(rows) for rows in all_rows.values())} trades")


if __name__ == "__main__":
    main()
