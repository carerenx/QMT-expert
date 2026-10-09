"""RedisQMT-only 1-minute daily-reset audit for v056, v0561 and v0562.

The strategy instance is deliberately recreated at every session boundary while
cash and the broker position are carried to the next session.  This models a
daily relaunch without pretending that an unfilled leg disappeared from the
account.  It is research-only: no live order interface is called.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from output.analysis.compare_v51_v39_minute import replay

OUT = ROOT / "analysis/dayt_v056_daily_reset_redis_20260101_20260918"
SYMBOL = "601869.SH"
REQUEST_START = "20260101"
REQUEST_END = "20260918"
DAILY_WARMUP_START = "20250701"
INITIAL_CASH = 100_000.0
INITIAL_POSITION_VALUE = 100_000.0
LOT = 100
FEE_RATE = 0.0005
SIZE_OVERRIDES = {"T_POSITION_FRACTION": 0.50, "T_TARGET_VALUE": 1_000_000_000.0}
VARIANTS = {
    "v056": "v056_nomom",
    "v0561": "v0561",
    "v0562": "v0562",
}


def _digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame.to_csv().encode("utf-8")).hexdigest()


def fetch_redis() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch both inputs exclusively from the local RedisQMT bridge."""
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure

    _, data = configure(account_id="8890145315", timeout_seconds=30)
    fields = ["open", "high", "low", "close", "volume", "amount"]
    daily = data.get_market_data_ex(
        fields, [SYMBOL], period="1d", start_time=DAILY_WARMUP_START,
        end_time=REQUEST_END, count=-1, dividend_type="front",
        fill_data=False, timeout_seconds=120,
    )[SYMBOL].sort_index()
    chunks = []
    for month in pd.period_range("2026-01", "2026-09", freq="M"):
        begin = month.start_time.strftime("%Y%m%d")
        end = (month.end_time + pd.Timedelta(days=1)).strftime("%Y%m%d")
        frame = data.get_market_data_ex(
            fields, [SYMBOL], period="1m", start_time=begin, end_time=end,
            count=-1, dividend_type="none", fill_data=False,
            timeout_seconds=120,
        )[SYMBOL]
        chunks.append(frame)
    minute = pd.concat(chunks).loc[lambda x: ~x.index.duplicated(keep="last")]
    minute = minute.sort_index()
    minute = minute.loc[(minute.index.str[:8] >= REQUEST_START) &
                        (minute.index.str[:8] <= REQUEST_END)]
    return daily, minute


def lane(label: str) -> str:
    if label.startswith("REV-T"):
        return "反T"
    if label.startswith("FWD-T"):
        return "正T"
    return "其他"


def cycles_from_fills(fills: list[dict], close: float) -> list[dict]:
    """FIFO-pair T fills within one daily strategy instance.

    A left-over opening leg is explicitly marked to the close.  It remains in
    the carried account position, but is *not* claimed as a completed T.
    """
    queues: dict[str, list[dict]] = {"反T": [], "正T": []}
    rows: list[dict] = []
    for fill in fills:
        kind = lane(fill["label"])
        if kind == "其他":
            continue
        opening = (kind == "反T" and fill["shares"] < 0) or (
            kind == "正T" and fill["shares"] > 0)
        if opening:
            queues[kind].append(dict(fill, remaining=abs(fill["shares"])))
            continue
        remaining = abs(fill["shares"])
        while remaining and queues[kind]:
            opening_fill = queues[kind][0]
            used = min(remaining, opening_fill["remaining"])
            gross = ((opening_fill["price"] - fill["price"]) if kind == "反T"
                     else (fill["price"] - opening_fill["price"])) * used
            fees = (opening_fill["price"] + fill["price"]) * used * FEE_RATE
            rows.append({
                "lane": kind, "entry_time": opening_fill["time"],
                "exit_time": fill["time"], "shares": used,
                "entry_price": opening_fill["price"], "exit_price": fill["price"],
                "gross": gross, "fees": fees, "net": gross - fees,
                "closed": True,
            })
            opening_fill["remaining"] -= used
            remaining -= used
            if opening_fill["remaining"] == 0:
                queues[kind].pop(0)
    for kind, legs in queues.items():
        for opening_fill in legs:
            used = opening_fill["remaining"]
            gross = ((opening_fill["price"] - close) if kind == "反T"
                     else (close - opening_fill["price"])) * used
            fees = opening_fill["price"] * used * FEE_RATE
            rows.append({
                "lane": kind, "entry_time": opening_fill["time"],
                "exit_time": "未闭合（按收盘价标记）", "shares": used,
                "entry_price": opening_fill["price"], "exit_price": close,
                "gross": gross, "fees": fees, "net": gross - fees,
                "closed": False,
            })
    return rows


def complete_session(bars: pd.DataFrame) -> bool:
    clocks = set(bars.index.str[8:12])
    return len(bars) >= 240 and "0930" in clocks and "1500" in clocks


def run_variant(name: str, version: str, daily: pd.DataFrame,
                minute: pd.DataFrame, initial_shares: int) -> dict:
    cash = INITIAL_CASH
    shares = initial_shares
    days: list[dict] = []
    fills: list[dict] = []
    cycles: list[dict] = []
    skipped: list[dict] = []
    for n, (date, bars) in enumerate(minute.groupby(minute.index.str[:8]), 1):
        history = daily.loc[daily.index < date]
        if not complete_session(bars) or len(history) < 80:
            skipped.append({"date": date, "bars": len(bars), "reason": "partial session or insufficient daily history"})
            continue
        with redirect_stdout(io.StringIO()):
            result = replay(version, history, bars, slip=0.0, initial_cash=cash,
                            initial_shares=shares, symbol=SYMBOL,
                            overrides=SIZE_OVERRIDES)
        if result["failure"]:
            raise RuntimeError(f"{name} {date}: {result['failure']}")
        close = float(bars.iloc[-1].close)
        day_fills = []
        for raw in result["trades"]:
            item = dict(raw)
            item["date"] = date
            item["lane"] = lane(item["label"])
            item["fee"] = item["turnover"] * FEE_RATE
            day_fills.append(item)
            fills.append(item)
        day_cycles = cycles_from_fills(day_fills, close)
        for item in day_cycles:
            item["date"] = date
            cycles.append(item)
        fees = sum(item["fee"] for item in day_fills)
        pre_fee_cash = result["final_equity"] - result["final_position"] * close
        next_cash = pre_fee_cash - fees
        done = sum(item["shares"] for item in day_cycles if item["closed"])
        day = {
            "date": date, "start_cash": cash, "start_shares": shares,
            "end_cash": next_cash, "end_shares": result["final_position"],
            "close": close, "fees": fees, "turnover": result["turnover"],
            "fills": len(day_fills), "cycles": day_cycles,
            "completed_shares": done,
            "t_rate": done / shares if shares else 0.0,
            "equity": next_cash + result["final_position"] * close,
            "state": result["state"],
        }
        days.append(day)
        cash = next_cash
        shares = result["final_position"]
        if n % 30 == 0:
            print(f"{name}: {n} sessions loaded", flush=True)
    return {"name": name, "version": version, "days": days, "fills": fills,
            "cycles": cycles, "skipped": skipped, "end_cash": cash, "end_shares": shares}


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def summarize(result: dict, last_close: float) -> dict:
    cycles = result["cycles"]
    closed = [row for row in cycles if row["closed"]]
    unclosed = [row for row in cycles if not row["closed"]]
    days = result["days"]
    total_start = sum(day["start_shares"] for day in days)
    output = {"days": len(days), "fills": len(result["fills"]),
              "cycles": len(cycles), "closed": len(closed), "unclosed": len(unclosed),
              "fees": sum(fill["fee"] for fill in result["fills"]),
              "turnover": sum(fill["turnover"] for fill in result["fills"]),
              "completed_shares": sum(row["shares"] for row in closed),
              "t_rate": (sum(row["shares"] for row in closed) / total_start if total_start else 0.0),
              "mean_day_t_rate": (sum(day["t_rate"] for day in days) / len(days) if days else 0.0),
              "end_equity": result["end_cash"] + result["end_shares"] * last_close,
              "end_cash": result["end_cash"], "end_shares": result["end_shares"]}
    for category in ("反T", "正T"):
        c = [row for row in closed if row["lane"] == category]
        u = [row for row in unclosed if row["lane"] == category]
        output[category] = {
            "closed": len(c), "unclosed": len(u),
            "shares": sum(row["shares"] for row in c),
            "gross": sum(row["gross"] for row in c), "net": sum(row["net"] for row in c),
            "marked_net": sum(row["net"] for row in u),
            "wins": sum(row["net"] > 0 for row in c),
        }
    return output


def trade_markdown(result: dict) -> str:
    lines = [f"# {result['name']}：逐笔成交与闭合审计", "",
             "闭合记录按同一交易日、同一方向 FIFO 配对；未闭合腿只按当日收盘标记，未计入 T 达成率。", "",
             "## 原始成交", "",
             "| 日期 | 决策时间 | 成交时间 | 方向 | 标签 | 股数 | 价格 | 成交额 | 费用 |", "|---|---|---|---|---|---:|---:|---:|---:|"]
    for fill in result["fills"]:
        lines.append("| {date} | {decision_time} | {time} | {lane} | {label} | {shares:+d} | {price:.2f} | {turnover:.2f} | {fee:.2f} |".format(**fill))
    lines += ["", "## T 配对、损益与闭合状态", "",
              "| 日期 | 类型 | 开仓时间 | 平仓/标记时间 | 股数 | 开仓价 | 平仓/标记价 | 毛收益 | 费用 | 净收益 | 是否闭合 |",
              "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in result["cycles"]:
        closed = "是" if row["closed"] else "否"
        display = dict(row)
        display["closed_text"] = closed
        lines.append("| {date} | {lane} | {entry_time} | {exit_time} | {shares} | {entry_price:.2f} | {exit_price:.2f} | {gross:.2f} | {fees:.2f} | {net:.2f} | {closed_text} |".format(**display))
    return "\n".join(lines) + "\n"


def report(results: dict, daily: pd.DataFrame, minute: pd.DataFrame,
           initial_shares: int) -> str:
    actual_end = minute.index.max()[:8]
    first_open = float(minute.iloc[0].open)
    last_close = float(minute.iloc[-1].close)
    initial_equity = INITIAL_CASH + initial_shares * first_open
    hold_end = INITIAL_CASH + initial_shares * last_close
    summaries = {name: summarize(value, last_close) for name, value in results.items()}
    lines = ["# v056 / v0561 / v0562：RedisQMT 分钟线、每日重启回测", "",
             "## 结论与有效范围", "",
             f"- 请求窗口为 {REQUEST_START}—{REQUEST_END}；RedisQMT 桥接返回的最后一分钟为 **{minute.index.max()}**，因此可验证窗口实际止于 **{actual_end}**。09-18 没有用第三方数据补齐。",
             f"- 标的：`{SYMBOL}`。初始股票仓位按首个可交易日开盘价 {first_open:.2f} 约 100,000 元向下取整：**{initial_shares} 股（{initial_shares // LOT} 手）**；另有现金 100,000 元。初始总资产 {fmt_money(initial_equity)} 元。",
             "- 每个交易日均新建策略实例；现金、实际持仓和手续费跨日连续。日末未闭合腿不强平、不丢弃，下一日按实际账户继续，但当日配对审计将其标为未闭合。",
             "- 新开 T 腿使用可配对底仓的 50%，并将金额上限解除，确保不是默认 4 万元上限在缩小仓位。最小交易单位为 100 股。",
             "- 成交撮合为触发当根 1 分钟收盘价，单边费用 0.05%、滑点 0；这是可重复研究口径，不代表真实盘口成交。", "",
             "## 账户与 T 成效总览", "",
             "| 策略 | 期末资产 | 相对初始 | 相对一直持有 | 期末现金 | 期末股数 | 成交笔数 | 已闭合周期 | 未闭合腿 | 反T净收益（已闭合） | 正T净收益（已闭合） | 未闭合标记净损益 | 综合T达成率 | 日均达成率 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in summaries.items():
        marked = row["反T"]["marked_net"] + row["正T"]["marked_net"]
        lines.append(f"| {name} | {fmt_money(row['end_equity'])} | {fmt_money(row['end_equity'] - initial_equity)} | {fmt_money(row['end_equity'] - hold_end)} | {fmt_money(row['end_cash'])} | {row['end_shares']} | {row['fills']} | {row['closed']} | {row['unclosed']} | {fmt_money(row['反T']['net'])} | {fmt_money(row['正T']['net'])} | {fmt_money(marked)} | {row['t_rate']:.2%} | {row['mean_day_t_rate']:.2%} |")
    lines += ["", f"一直持有对照（同样现金 + 初始股票，不交易）：期末 {fmt_money(hold_end)} 元；持有期损益 {fmt_money(hold_end - initial_equity)} 元。", "",
              "## 达成率和盈亏的定义", "",
              "- 单日某方向 T 达成率 = 当天该方向已完成配对的股数 ÷ 当天开盘前实际持股。例如当天有 1,000 股，卖出再买回 1,000 股，则反T达成率为 100%。",
              "- 综合T达成率 = 全部已闭合的反T、正T配对股数 ÷ 各交易日开盘前实际持股数之和。正反方向均完成时可以超过单方向的 100%，这是周转量而不是收益率。",
              "- 已闭合收益扣除了开仓和平仓两侧费用；未闭合腿仅扣实际已成交一侧费用，并按当日收盘价标记，绝不混入“已完成做T收益”。", "",
              "## 分方向统计", "",
              "| 策略 | 方向 | 已闭合周期 | 胜率 | 已闭合股数 | 毛收益 | 净收益 | 未闭合腿 | 未闭合标记净损益 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in summaries.items():
        for side in ("反T", "正T"):
            detail = row[side]
            win = detail["wins"] / detail["closed"] if detail["closed"] else 0.0
            lines.append(f"| {name} | {side} | {detail['closed']} | {win:.2%} | {detail['shares']} | {fmt_money(detail['gross'])} | {fmt_money(detail['net'])} | {detail['unclosed']} | {fmt_money(detail['marked_net'])} |")
    lines += ["", "## 研究解读", ""]
    winner = max(summaries, key=lambda key: summaries[key]["end_equity"])
    lines.append(f"- 在该一条样本、该成本假设和截至 {actual_end} 的分钟数据中，期末账户最高的是 **{winner}**；这只是样本内排序，不等同于实盘推荐。")
    for name, row in summaries.items():
        total_net = row["反T"]["net"] + row["正T"]["net"]
        marked = row["反T"]["marked_net"] + row["正T"]["marked_net"]
        lines.append(f"- **{name}**：已闭合T净收益 {fmt_money(total_net)} 元，未闭合腿的日末标记净损益 {fmt_money(marked)} 元；累计费用 {fmt_money(row['fees'])} 元，说明结果必须同时看闭合质量、跨日残留与成本，而不能只看触发次数。")
    lines += ["", "## 局限与下一步", "",
              "1. 数据唯一来自 RedisQMT 桥接，09-18 缺失导致请求窗口未能完整验证；补齐后必须从同一桥接重跑，而不是拼接数据源。",
              "2. 当前是分钟收盘价撮合，未使用盘口队列、成交量参与率、撤单延迟或滑点；实盘收益通常会更低。",
              "3. 每日策略重启会失去未闭合腿的配对上下文。账户没有丢仓，但策略必须具备启动时识别和管理残余腿的持久账本，才能避免将跨日风险误认为新的底仓。",
              "4. 该报告不触发真实委托。QMT实盘下单接口仍应使用成交回报确认；本报告的离线回放只验证策略逻辑。", "",
              "## 数据与可复现性", "",
              f"- 分钟数据：`data_1m_redisqmt.csv`，{len(minute)} 行，SHA-256 `{_digest(minute)}`。",
              f"- 日线数据：`data_1d_redisqmt_front.csv`，{len(daily)} 行，SHA-256 `{_digest(daily)}`。",
              "- 每笔成交、时间、费用、闭合状态和配对损益分别见下列文件：", ""]
    for name in results:
        lines.append(f"  - [{name} 逐笔审计]({name}_trades.md)")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    daily, minute = fetch_redis()
    if minute.empty:
        raise RuntimeError("RedisQMT returned no requested minute data")
    daily.to_csv(OUT / "data_1d_redisqmt_front.csv", index_label="time")
    minute.to_csv(OUT / "data_1m_redisqmt.csv", index_label="time")
    initial_shares = int(INITIAL_POSITION_VALUE / float(minute.iloc[0].open) / LOT) * LOT
    if initial_shares < LOT:
        raise RuntimeError("initial position cannot buy one lot")
    results = {}
    for name, version in VARIANTS.items():
        print(f"running {name}", flush=True)
        results[name] = run_variant(name, version, daily, minute, initial_shares)
        (OUT / f"{name}_trades.md").write_text(trade_markdown(results[name]), encoding="utf-8")
    (OUT / "README.md").write_text(report(results, daily, minute, initial_shares), encoding="utf-8")
    payload = {
        "symbol": SYMBOL,
        "requested_window": [REQUEST_START, REQUEST_END],
        "size_overrides": SIZE_OVERRIDES,
        "initial_cash": INITIAL_CASH,
        "initial_shares": initial_shares,
        "variants": {
            name: summarize(value, float(minute.iloc[-1].close))
            for name, value in results.items()
        },
    }
    (OUT / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
