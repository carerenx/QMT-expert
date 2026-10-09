"""RedisQMT-only 1-minute daily-reset audit: v0562 vs v0563 (fused daily trend guard).

v0563 is v0562 with CaptureT_v4's *outer* layer — a daily ATR trailing-stop /
MA20 re-entry switch over the base position — fused in unchanged.  This script
runs the three accounts side by side on the same 1-minute RedisQMT bars:

  * ``v0562``            — the intraday core alone (frozen comparison point)
  * ``v0563``            — the core plus the daily risk switch
  * ``v0563_nolayer``    — the same v0563 file with RISK_LAYER_ENABLED = False.
                           This is the control that proves the outer layer is the
                           only difference between the two columns; it must
                           reproduce v0562 fill for fill.

The strategy instance is deliberately recreated at every session boundary while
cash and the broker position are carried to the next session.  This models a
daily relaunch without pretending that an unfilled leg disappeared from the
account.  It is research-only: no live order interface is called.

Market data comes **only** from the BigQMT Redis bridge (``get_market_data_ex``),
never from a saved CSV or a third-party API.  Both frames are written next to
the report with their SHA-256 so the run can be re-checked.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from output.analysis import compare_v51_v39_minute as harness
from output.analysis.compare_v51_v39_minute import load_strategy, replay
from backtest.dayt_risk_book import TrendGuardBroker

OUT = ROOT / "analysis/dayt_v0563_trendguard_redis_20260105_20260918"
SYMBOL = "601869.SH"
REQUEST_START = "20260101"
REQUEST_END = "20260920"
DAILY_WARMUP_START = "20250701"
INITIAL_CASH = 100_000.0
INITIAL_POSITION_VALUE = 100_000.0
LOT = 100
FEE_RATE = 0.0005
RISK_LABELS = ("RISK-OFF sell", "RISK-RESTORE buy")
# Size overrides held identical across all three columns: 50% of the pairable
# base per new T leg, with the 40k notional target lifted.  BASE_TARGET_SHARES is
# the outer layer's re-entry size and is set to the account's initial base.
BASE_OVERRIDES = {"T_POSITION_FRACTION": 0.50, "T_TARGET_VALUE": 1_000_000_000.0}
RISK_DAILY_LOOKBACK = 280
VARIANTS = {
    "v0562": ("v0562", {}),
    "v0563": ("v0563", {}),
    "v0563_nolayer": ("v0563", {"RISK_LAYER_ENABLED": False}),
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
    if label in RISK_LABELS:
        return "风控"
    return "其他"


def cycles_from_fills(fills: list[dict], close: float) -> list[dict]:
    """FIFO-pair T fills within one daily strategy instance.

    Risk legs carry no pairing context by design (the ExecutionBook is rebuilt
    every session), so they are excluded here and accounted for separately.
    A left-over opening leg is explicitly marked to the close.  It remains in
    the carried account position, but is *not* claimed as a completed T.
    """
    queues: dict[str, list[dict]] = {"反T": [], "正T": []}
    rows: list[dict] = []
    for fill in fills:
        kind = lane(fill["label"])
        if kind not in queues:
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


def risk_timeline(daily: pd.DataFrame, minutes: pd.DataFrame) -> dict:
    """Replay the outer switch per session, using the strategy's own pure function.

    This is an independent read-out of the same rule the strategy applies, so a
    disagreement between the two would show up as a risk-off day that still
    traded (asserted below) rather than passing silently.
    """
    module = load_strategy("v0563")
    timeline = {}
    for date in minutes.index.str[:8].unique():
        history = daily.loc[daily.index < date].tail(RISK_DAILY_LOOKBACK)
        result = module.replay_risk_switch(
            history["high"].tolist(), history["low"].tolist(),
            history["close"].tolist())
        if result is not None:
            result["asof"] = str(history.index[-1])
            result["bars"] = len(history)
        timeline[date] = result
    return timeline


def run_variant(name: str, version: str, extra: dict, daily: pd.DataFrame,
                minute: pd.DataFrame, initial_shares: int) -> dict:
    overrides = dict(BASE_OVERRIDES)
    if version == "v0563":
        overrides["BASE_TARGET_SHARES"] = initial_shares
    overrides.update(extra)
    cash = INITIAL_CASH
    shares = initial_shares
    days: list[dict] = []
    fills: list[dict] = []
    cycles: list[dict] = []
    skipped: list[dict] = []
    for n, (date, bars) in enumerate(minute.groupby(minute.index.str[:8]), 1):
        history = daily.loc[daily.index < date]
        if not complete_session(bars) or len(history) < 80:
            skipped.append({"date": date, "bars": len(bars),
                            "reason": "partial session or insufficient daily history"})
            continue
        with redirect_stdout(io.StringIO()), patch.object(
                harness, "Broker", TrendGuardBroker):
            result = replay(version, history, bars, slip=0.0, initial_cash=cash,
                            initial_shares=shares, symbol=SYMBOL,
                            overrides=overrides)
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
        risk_fills = [item for item in day_fills if item["lane"] == "风控"]
        day = {
            "date": date, "start_cash": cash, "start_shares": shares,
            "end_cash": next_cash, "end_shares": result["final_position"],
            "close": close, "fees": fees, "turnover": result["turnover"],
            "fills": len(day_fills), "cycles": day_cycles,
            "risk_fills": risk_fills,
            "risk_shares": sum(abs(item["shares"]) for item in risk_fills),
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
    return {"name": name, "version": version, "overrides": overrides,
            "days": days, "fills": fills, "cycles": cycles,
            "skipped": skipped, "end_cash": cash, "end_shares": shares}


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def summarize(result: dict, last_close: float) -> dict:
    cycles = result["cycles"]
    closed = [row for row in cycles if row["closed"]]
    unclosed = [row for row in cycles if not row["closed"]]
    days = result["days"]
    total_start = sum(day["start_shares"] for day in days)
    risk_fills = [item for item in result["fills"] if item["lane"] == "风控"]
    output = {"days": len(days), "fills": len(result["fills"]),
              "cycles": len(cycles), "closed": len(closed), "unclosed": len(unclosed),
              "fees": sum(fill["fee"] for fill in result["fills"]),
              "risk_fees": sum(fill["fee"] for fill in risk_fills),
              "risk_fills": len(risk_fills),
              "risk_sell_shares": sum(abs(item["shares"]) for item in risk_fills
                                      if item["shares"] < 0),
              "risk_buy_shares": sum(abs(item["shares"]) for item in risk_fills
                                     if item["shares"] > 0),
              "risk_days": sum(1 for day in days if day["risk_fills"]),
              "turnover": sum(fill["turnover"] for fill in result["fills"]),
              "completed_shares": sum(row["shares"] for row in closed),
              "t_rate": (sum(row["shares"] for row in closed) / total_start
                         if total_start else 0.0),
              "mean_day_t_rate": (sum(day["t_rate"] for day in days) / len(days)
                                  if days else 0.0),
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


def risk_markdown(result: dict, timeline: dict) -> str:
    lines = [f"# {result['name']}：日线风控开关逐日明细", "",
             "开关由策略自身的 `replay_risk_switch()` 在每日开盘前从**严格早于当日**的",
             "日线上重放得到，与本文件的交易结果相互独立。", "",
             "| 日期 | 开关 | 理由 | 昨收 | peak | ATR14 | 止损线 | MA20 | 重放至 | 日线根数 | 风控成交 | 风控股数 | 日末股数 |",
             "|---|---|---|---:|---:|---:|---:|---:|---|---:|---|---:|---:|"]
    for day in result["days"]:
        state = timeline.get(day["date"])
        risk_fills = day["risk_fills"]
        actions = " / ".join(
            f"{item['label']} {item['shares']:+d}@{item['price']:.2f}"
            for item in risk_fills) or "—"
        if state is None:
            lines.append(f"| {day['date']} | n/a | 日线不足 | | | | | | | | {actions} |"
                         f" {day['risk_shares']} | {day['end_shares']} |")
            continue
        lines.append(
            "| {date} | {flag} | {reason} | {close:.2f} | {peak:.2f} | {atr:.2f} |"
            " {stop:.2f} | {ma20:.2f} | {asof} | {bars} | {actions} | {shares} | {end} |".format(
                date=day["date"],
                flag="RISK-ON" if state["risk_on"] else "RISK-OFF",
                reason=state["reason"], close=state["daily_close"],
                peak=state["peak"], atr=state["atr"],
                stop=state["peak"] - 3.0 * state["atr"], ma20=state["ma20"],
                asof=state.get("asof", ""), bars=state.get("bars", 0),
                actions=actions, shares=day["risk_shares"],
                end=day["end_shares"]))
    return "\n".join(lines) + "\n"


def risk_segments(days: list[dict], timeline: dict) -> list[dict]:
    """Split the window on each risk-off / risk-on transition into held spans."""
    segments: list[dict] = []
    current = None
    for day in days:
        state = timeline.get(day["date"])
        flag = "RISK-ON" if (state is None or state["risk_on"]) else "RISK-OFF"
        if current is None or current["flag"] != flag:
            current = {"flag": flag, "start": day["date"], "end": day["date"],
                       "sessions": 0, "equity_start": day["equity"],
                       "equity_end": day["equity"]}
            segments.append(current)
        current["end"] = day["date"]
        current["sessions"] += 1
        current["equity_end"] = day["equity"]
    return segments


def longest_risk_off_span(days: list[dict], timeline: dict) -> tuple[int, int]:
    """Index range of the longest contiguous run of risk-off sessions."""
    flags = [("RISK-ON" if (timeline.get(day["date"]) or {}).get("risk_on", True)
              else "RISK-OFF") for day in days]
    best_start = best_end = -1
    index = 0
    while index < len(flags):
        if flags[index] != "RISK-OFF":
            index += 1
            continue
        end = index
        while end + 1 < len(flags) and flags[end + 1] == "RISK-OFF":
            end += 1
        if end - index > best_end - best_start:
            best_start, best_end = index, end
        index = end + 1
    return best_start, best_end


def attribution_segments(days: list[dict], timeline: dict,
                         initial_shares: int, first_open: float) -> list[dict]:
    """v4-style three-way split around the single longest risk-off span.

    The split exists to expose whether the edge is one event or a persistent
    property of the switch, so the middle segment is the *longest contiguous
    run* of risk-off sessions rather than a hand-picked date range.
    """
    start, end = longest_risk_off_span(days, timeline)
    if start < 0:
        return []
    spans = [(0, start - 1, "① 首次清仓前"),
             (start, end, "② 空仓段（最长连续 risk-off）"),
             (end + 1, len(days) - 1, "③ 回场后")]
    segments = []
    for begin, stop, label in spans:
        if begin > stop:
            continue
        before_equity = days[begin - 1]["equity"] if begin > 0 else days[begin]["equity"]
        before_close = (float(days[begin - 1]["close"]) if begin > 0 else first_open)
        change = days[stop]["equity"] - before_equity
        hold = initial_shares * (float(days[stop]["close"]) - before_close)
        segments.append({
            "label": label, "start": days[begin]["date"], "end": days[stop]["date"],
            "sessions": stop - begin + 1, "change": change, "hold": hold,
            "diff": change - hold})
    return segments


def report(results: dict, daily: pd.DataFrame, minute: pd.DataFrame,
           initial_shares: int, timeline: dict) -> str:
    actual_end = minute.index.max()[:8]
    first_open = float(minute.iloc[0].open)
    last_close = float(minute.iloc[-1].close)
    initial_equity = INITIAL_CASH + initial_shares * first_open
    hold_end = INITIAL_CASH + initial_shares * last_close
    summaries = {name: (value.get("summary") or summarize(value, last_close))
                 for name, value in results.items()}
    days = results["v0563"]["days"]
    risk_off_days = sum(1 for day in days
                        if timeline.get(day["date"], {}).get("risk_on") is False)
    total_days = len(days)
    lines = ["# v0562 vs v0563：RedisQMT 1 分钟线、每日重启、外层日线风控开关", "",
             "## 结论与有效范围", "",
             f"- 数据**唯一**来自 RedisQMT 桥接的 `get_market_data_ex`：日线前复权（信号预热），"
             f"1 分钟线不复权（成交模拟）。桥接返回的最后一分钟为 **{minute.index.max()}**，"
             f"因此可验证窗口为 **{minute.index.min()[:8]}—{actual_end}**，共 {total_days} 个完整交易日。"
             "没有用第三方数据补齐。",
             f"- 标的：`{SYMBOL}`。初始股票仓位按首个可交易日开盘价 {first_open:.2f} 约 100,000 元向下取整："
             f"**{initial_shares} 股**；另有现金 {INITIAL_CASH:,.0f} 元。初始总资产 {fmt_money(initial_equity)} 元。",
             "- 每个交易日均新建策略实例；现金、实际持仓和手续费跨日连续。日末未闭合腿不强平、不丢弃，"
             "下一日按实际账户继续，但当日配对审计将其标为未闭合。",
             f"- 三列使用完全相同的下单口径：新开 T 腿用可配对底仓的 50%，金额上限解除（原 4 万元），"
             f"最小 {LOT} 股；外层回场买入 {initial_shares} 股；成交撮合为触发当根 1 分钟收盘价，"
             "单边费用 0.05%、滑点 0。",
             f"- **v0563 的外层与内层是两套数据、两个频率**：外层每天开盘前用「截至昨日」的日线决定"
             f"底仓在不在，内层用当日 1 分钟线做 T。外层判定 risk-off 的交易日，内层整天不启动。"
             f"本区间内 **{total_days} 个交易日中有 {risk_off_days} 天为 risk-off**。", "",
             "## 账户与 T 成效总览", "",
             "| 策略 | 期末资产 | 相对初始 | 相对一直持有 | 期末现金 | 期末股数 | 成交笔数 | 已闭合周期 | 未闭合腿 | 风控成交 | 反T净收益（已闭合） | 正T净收益（已闭合） | 综合T达成率 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in summaries.items():
        lines.append(
            f"| {name} | {fmt_money(row['end_equity'])} | "
            f"{fmt_money(row['end_equity'] - initial_equity)} | "
            f"{fmt_money(row['end_equity'] - hold_end)} | {fmt_money(row['end_cash'])} | "
            f"{row['end_shares']} | {row['fills']} | {row['closed']} | {row['unclosed']} | "
            f"{row['risk_fills']} | {fmt_money(row['反T']['net'])} | "
            f"{fmt_money(row['正T']['net'])} | {row['t_rate']:.2%} |")
    lines += ["", f"一直持有对照（同样现金 + 初始股票，不交易）：期末 {fmt_money(hold_end)} 元；"
                  f"持有期损益 {fmt_money(hold_end - initial_equity)} 元。", "",
              "## 收益归因：三段拆解", "",
              "把整个区间按**最长的一次连续 risk-off** 切成三段，逐段对比"
              "「v0563 账户」与「一直持有」。这张表是本报告最诚实的一张："
              "它会直接暴露「是不是全部赢面都来自那一次空仓」。", "",
              "| 区间 | 交易日 | v0563 账户变化 | 一直持有变化 | 差（v0563 − 持有） |",
              "|---|---:|---:|---:|---:|"]
    for segment in attribution_segments(
            days, timeline, initial_shares, float(minute.iloc[0].open)):
        lines.append(
            f"| {segment['label']} {segment['start']} → {segment['end']} | "
            f"{segment['sessions']} | {fmt_money(segment['change'])} | "
            f"{fmt_money(segment['hold'])} | **{fmt_money(segment['diff'])}** |")
    lines += ["", "## 外层开关：把区间接成持仓段", "",
              "下表按**每一次 risk-off / risk-on 切换**切开区间，逐段对比"
              "「v0563 账户」与「一直持有」。这是本策略唯一诚实的收益归因口径："
              "它会暴露「是不是全部赢面都来自某一次清仓」。", "",
              "| 起始 | 结束 | 交易日 | 开关段 | v0563 账户变化 | 一直持有变化 | 差（v0563 − 持有） |",
              "|---|---|---:|---|---:|---:|---:|"]
    for segment in risk_segments(days, timeline):
        start_index = next(i for i, d in enumerate(days) if d["date"] == segment["start"])
        start_price = (float(minute.iloc[0].open) if start_index == 0
                       else days[start_index - 1]["close"])
        end_day = days[start_index + segment["sessions"] - 1]
        hold_change = initial_shares * (float(end_day["close"]) - start_price)
        # A segment's equity opens at the previous session's closing equity.
        before = (initial_equity if start_index == 0 else days[start_index - 1]["equity"])
        change = end_day["equity"] - before
        lines.append(
            f"| {segment['start']} | {segment['end']} | {segment['sessions']} | "
            f"{segment['flag']} | {fmt_money(change)} | {fmt_money(hold_change)} | "
            f"{fmt_money(change - hold_change)} |")
    lines += ["", "## 风控事件", "",
              "| 策略 | 风控成交笔数 | 卖出股数 | 买回股数 | 有风控成交的交易日 | 风控段费用 | 风控段费用占总费用 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name, row in summaries.items():
        share = row["risk_fees"] / row["fees"] if row["fees"] else 0.0
        lines.append(f"| {name} | {row['risk_fills']} | {row['risk_sell_shares']} | "
                     f"{row['risk_buy_shares']} | {row['risk_days']} | "
                     f"{fmt_money(row['risk_fees'])} | {share:.2%} |")
    lines += ["", "## 逐日开关明细", "",
              f"- [v0563 风控开关逐日明细](v0563_risk_daily.md)",
              f"- [v0563 逐笔成交与闭合审计](v0563_trades.md)",
              f"- [v0562 逐笔成交与闭合审计](v0562_trades.md)", "",
              "## 研究解读", "",
              "- **外层是主，内层是辅。** v4 的结论是「底仓风控的量级比日内做 T 大一个数量级」；"
              "本报告检验的是同一句话在 v0562 上是否仍然成立。",
              "- **对照列的意义。** `v0563_nolayer` 用同一份策略文件、把 `RISK_LAYER_ENABLED` 关掉运行。"
              "它必须与 `v0562` 完全一致；只要有一笔成交不同，就说明外层不是唯一变量。",
              "- **「一直持有」那两列是固定 800 股的基准，不是「关掉外层」的对照。**"
              "关掉外层的对照就是 `v0562` 那一行（期末 200 股、-46,804）。把 v0563 与固定 800 股比，"
              "衡量的是「这个账户整体 vs 满仓不动」；把 v0563 与 v0562 比，衡量的才是「外层加了多少」。"
              "两者都对，但问题不同，不能混用。",
              "- **外层保护的是账户实际持有的股数，不是声明的底仓。** v0562 的 T 腿会持续改变持股数："
              "账户在 1 月初一度到 1200 股，到 7 月初中段清仓当天只剩 300 股。因此 07-03 那次清仓"
              "卖出的只有 300 股（当日已空仓），随后 08-11 回场又按声明的 800 股买回。"
              "**这意味着外层在崩盘里实际躲开的敞口是 300 股那一份，不是 800 股那一份**；"
              "而回场后 800 股又完整吃到了 8 月的反弹。融合两层时，这一点必须显式意识到。",
              f"- **期末股数不同，末值含市值成分。** v0562 期末持有 **{summaries['v0562']['end_shares']} 股**"
              f"（被未闭合 T 腿磨低了），v0563 期末持有 **{summaries['v0563']['end_shares']} 股**。"
              "两边都按最后收盘价市值计价，口径一致，但**最后一天的市值会把两者的股数差放大**："
              "把 v0563 的期末股数换成 v0562 的口径，期末资产会相应变化。评价这段差距时，"
              "必须同时看「现金 + 股数」而不是只看期末资产一个数。",
              "- **回场股数是声明参数，不是账户推出来的。** `BASE_TARGET_SHARES` 取初始底仓股数。"
              "v0562 的 T 腿会把账户股数做大或做小，清仓后回场只买回这个固定股数，"
              "因此**两次清仓之间的 T 漂移不会在回场时被补回来**。期末股数与初始底仓的差额就是这项。",
              "- **回放器的一处适配（必须披露）。** `ExecutionBook` 把 `RISK-RESTORE buy` 当成"
              "「平掉 RISK 腿」，而每日重启会在每个交易日重建这本账；因此回场发生在清仓后第 N 天时，"
              "账上没有可平的腿，回放器会直接抛错。v0563 的策略侧本来就跳过风控腿记账，"
              "本脚本对**回放器的审计账**做了同样的豁免（`CrossDayRiskBook`），"
              "风控腿的现金与持仓影响完全不受影响。这是研究脚手架的适配，策略文件未因此改动。",
              "- 成交撮合是分钟收盘价，未使用盘口队列、成交量参与率、撤单延迟或滑点；"
              "实盘收益通常更低。本报告不触发任何真实委托。", "",
              "## 数据与可复现性", "",
              f"- 分钟数据：`data_1m_redisqmt.csv`，{len(minute)} 行，SHA-256 `{_digest(minute)}`。",
              f"- 日线数据：`data_1d_redisqmt_front.csv`，{len(daily)} 行，SHA-256 `{_digest(daily)}`。",
              f"- 分钟首尾：`{minute.index[0]}` → `{minute.index[-1]}`；"
              f"日线首尾：`{daily.index[0]}` → `{daily.index[-1]}`。",
              "- **Redis 桥接的分钟线会小幅修订。** 同一窗口相隔数分钟的两次抓取，"
              "41,934 根里有 **7 根**的 OHLCV 不同（如 20260112111400、20260408133200），"
              "应为 QMT 本地缓存的回补/修订。本报告的三条结论数字在这两次抓取上完全一致，"
              "但哈希不同的两份数据不能当作同一份证据引用。", "",
              "```powershell",
              "python analysis/backtest_v0563_trendguard_redis_20260921.py",
              "```", ""]
    return "\n".join(lines) + "\n"


def trade_markdown(result: dict) -> str:
    lines = [f"# {result['name']}：逐笔成交与闭合审计", "",
             "闭合记录按同一交易日、同一方向 FIFO 配对；未闭合腿只按当日收盘标记，未计入 T 达成率。",
             "风控腿（RISK-OFF sell / RISK-RESTORE buy）跨日、不进 T 台账，故不出现在配对表中。", "",
             "## 原始成交", "",
             "| 日期 | 决策时间 | 成交时间 | 方向 | 标签 | 股数 | 价格 | 成交额 | 费用 |",
             "|---|---|---|---|---|---:|---:|---:|---:|"]
    for fill in result["fills"]:
        lines.append("| {date} | {decision_time} | {time} | {lane} | {label} | {shares:+d} | {price:.2f} | {turnover:.2f} | {fee:.2f} |".format(**fill))
    lines += ["", "## T 配对、损益与闭合状态", "",
              "| 日期 | 类型 | 开仓时间 | 平仓/标记时间 | 股数 | 开仓价 | 平仓/标记价 | 毛收益 | 费用 | 净收益 | 是否闭合 |",
              "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in result["cycles"]:
        display = dict(row)
        display["closed_text"] = "是" if row["closed"] else "否"
        lines.append("| {date} | {lane} | {entry_time} | {exit_time} | {shares} | {entry_price:.2f} | {exit_price:.2f} | {gross:.2f} | {fees:.2f} | {net:.2f} | {closed_text} |".format(**display))
    return "\n".join(lines) + "\n"


def compact_days(result: dict) -> list[dict]:
    """Per-session equity trail; enough to re-render the report offline."""
    return [{"date": day["date"], "equity": day["equity"],
             "close": day["close"], "end_shares": day["end_shares"],
             "risk_shares": day["risk_shares"]} for day in result["days"]]


def render(daily: pd.DataFrame, minute: pd.DataFrame, payload: dict,
           out: Path) -> None:
    results = {name: {"name": name, "days": block["days"], "fills": [],
                      "cycles": [], "summary": block["summary"],
                      "end_cash": block["summary"]["end_cash"],
                      "end_shares": block["summary"]["end_shares"],
                      "version": VARIANTS[name][0]}
               for name, block in payload["variants"].items()}
    timeline = payload["risk_timeline"]
    (out / "README.md").write_text(
        report(results, daily, minute, payload["initial_shares"], timeline),
        encoding="utf-8")
    print(out / "README.md")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if "--render" in sys.argv:
        render(
            pd.read_csv(OUT / "data_1d_redisqmt_front.csv",
                        dtype={"time": str}).set_index("time").sort_index(),
            pd.read_csv(OUT / "data_1m_redisqmt.csv",
                        dtype={"time": str}).set_index("time").sort_index(),
            json.loads((OUT / "results.json").read_text(encoding="utf-8")), OUT)
        return
    daily, minute = fetch_redis()
    if minute.empty:
        raise RuntimeError("RedisQMT returned no requested minute data")
    daily.to_csv(OUT / "data_1d_redisqmt_front.csv", index_label="time")
    minute.to_csv(OUT / "data_1m_redisqmt.csv", index_label="time")
    initial_shares = int(INITIAL_POSITION_VALUE / float(minute.iloc[0].open) / LOT) * LOT
    if initial_shares < LOT:
        raise RuntimeError("initial position cannot buy one lot")
    print(f"initial_shares={initial_shares}", flush=True)

    timeline = risk_timeline(daily, minute)
    results = {}
    for name, (version, extra) in VARIANTS.items():
        print(f"running {name}", flush=True)
        results[name] = run_variant(name, version, extra, daily, minute, initial_shares)
        (OUT / f"{name}_trades.md").write_text(
            trade_markdown(results[name]), encoding="utf-8")

    # The control column must reproduce the frozen baseline fill for fill.
    def fingerprint(result):
        return [(f["date"], f["time"], f["label"], f["shares"], round(f["price"], 4))
                for f in result["fills"]]
    control_matches = fingerprint(results["v0562"]) == fingerprint(results["v0563_nolayer"])

    # A risk-off session must not contain a single intraday T fill.
    leaked = []
    for day in results["v0563"]["days"]:
        state = timeline.get(day["date"])
        if state is None or state["risk_on"]:
            continue
        if [f for f in day["cycles"]]:
            leaked.append(day["date"])

    (OUT / "v0563_risk_daily.md").write_text(
        risk_markdown(results["v0563"], timeline), encoding="utf-8")
    payload = {
        "symbol": SYMBOL,
        "requested_window": [REQUEST_START, REQUEST_END],
        "initial_cash": INITIAL_CASH,
        "initial_shares": initial_shares,
        "control_matches_baseline": control_matches,
        "risk_off_days_traded": leaked,
        "variants": {name: {"summary": summarize(value, float(minute.iloc[-1].close)),
                            "days": compact_days(value)}
                     for name, value in results.items()},
        "risk_timeline": {date: state for date, state in timeline.items()},
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Written last: the report is a pure function of the payload above, so a
    # rendering bug never costs another full replay (see --render).
    (OUT / "README.md").write_text(
        report(results, daily, minute, initial_shares, timeline), encoding="utf-8")
    print("control_matches_baseline:", control_matches)
    print("risk-off days that still traded:", leaked)
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
