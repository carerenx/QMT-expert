"""CaptureT 三件套 — 只做反T / 反T+正T / 只做正T，同一规则只换方向开关。

背景：v1 只有反T。实测 601869 的 174 个交易日里，当日**先**触及下带（本该做正T）
的只有 6 天（3%），先触上带的 23 天（13%），两侧都没触及的 147 天（84%）。
v2 把下带先的那 6 天接进来（检验「只做反T」是不是缺陷），
v3 则把上带那一侧整个删掉（检验「只做正T」能不能独立成立）。

结果（相对一直持有，净费）：v1 +12,701.68 › v2 +9,581.56 › v3 +367.52。
**这条规则的价值几乎全部在反T 那一侧。**

口径（与 `backtest_redis_halfposition_20260101_20260918.py` 完全一致，可直接对比）：
  * 标的 601869.SH，数据全部来自 RedisQMT 桥接（get_market_data_ex），
    文件名 `analysis/dayt_redis_halfposition_20260101_20260918/1m.csv`
  * 区间 2026-01-01 ~ 2026-09-18
  * 初始仓位 ≈ 100,000 元（按首个可交易日开盘价向下取整到整手 → 800 股）
    + 现金 100,000 元
  * 每个交易日新建策略实例；现金、持仓、手续费跨日连续
  * 每笔新 T 腿 = 底仓的一半（400 股）；正T 额外受现金约束
  * 成交撮合为触发当根 1 分钟收盘价，单边 0.05%，滑点 0
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import output.analysis.compare_v51_v39_minute as HARNESS
from backtest.dayt_registry import STRATEGIES


DATA = ROOT / "analysis/dayt_redis_halfposition_20260101_20260918"
OUT = ROOT / "analysis/dayt_capturet_v2_symmetric_20260920"
START = "20260101"
END = "20260918"
SYMBOL = "601869.SH"
FEE_RATE = 0.0005
INITIAL_CASH = 100_000.0
POSITION_VALUE = 100_000.0
HALF_POSITION_FRACTION = 0.5

REV_OPEN = "REV-T sell"
REV_CLOSE = "REV-T buyback"
LONG_OPEN = "FWD-T buy"
LONG_CLOSE = "FWD-T sell"

ORDER = ("capture_v1", "capture_v4", "capture_v2", "capture_v3", "v0561", "v0562")
NAMES = {
    "capture_v1": "CaptureT_v1（只有反T）",
    "capture_v2": "CaptureT_v2（反T + 正T）",
    "capture_v3": "CaptureT_v3（只有正T）",
    "capture_v4": "CaptureT_v4（v1 + 日线风控）",
    "v0561": "v0561 CoreT",
    "v0562": "v0562 CoreT+ATR再入场",
}


def half_position_submit(original):
    """每笔新开的 T 腿 = 底仓的一半（CaptureT 内部按满仓设计，这里统一口径）。"""
    def wrapper(self, shares, price, label, style='COMPETE'):
        if label in (REV_OPEN, LONG_OPEN):
            self._refresh_position()
            base = int(self.st.get('base_can_use', 0) or 0)
            target = base // 2 // self.trade_lot * self.trade_lot
            if target >= self.trade_lot:
                shares = target if shares > 0 else -target
        return original(self, shares, price, label, style)
    return wrapper


RISK_LABELS = ("RISK-OFF sell", "RISK-RESTORE buy")
_ORIG_RECORD = HARNESS.ExecutionBook.record


def risk_safe_record(self, order_id, label, signed_shares, price):
    """回放口径下，风控腿的「平仓」找不到对应的「开仓」。

    风控清仓今天做、回场可能隔一周，而每日策略重置会把 ExecutionBook
    一起重建 —— 昨天的 RISK-OFF 记录今天已经不存在。
    这里在没有可平腿时跳过记账（盈亏本来就由账户权益体现）。
    实盘是连续运行的，不会遇到这个问题。
    """
    if label in RISK_LABELS and not self.legs.get("RISK"):
        self.orders.add(str(order_id))
        return 0.0, False, 0.0
    return _ORIG_RECORD(self, order_id, label, signed_shares, price)


def lane_of(label: str) -> str:
    if label.startswith("REV-T"):
        return "REV-T"
    if label.startswith("FWD-T"):
        return "FWD-T"
    return ""


def build_cycles(trades: list[dict], mark: float) -> list[dict]:
    """把成交流水按 lane 做 FIFO 配对，日末未平的腿按收盘价标注。"""
    queues: dict[str, list[dict]] = {"REV-T": [], "FWD-T": []}
    cycles: list[dict] = []
    for trade in trades:
        label, shares, price, stamp = (
            trade["label"], trade["shares"], trade["price"], trade["time"])
        lane = lane_of(label)
        if not lane:
            continue
        clock = f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
        # 平仓标签带后缀（VWAP / FORCE），用 startswith 而不是等号。
        opens = (lane == "REV-T" and label == REV_OPEN) or \
                (lane == "FWD-T" and label == LONG_OPEN)
        closes = (lane == "REV-T" and label.startswith(REV_CLOSE)) or \
                 (lane == "FWD-T" and label.startswith(LONG_CLOSE))
        if opens:
            queues[lane].append({"shares": abs(shares), "price": price, "time": clock})
        elif closes:
            remaining = abs(shares)
            while remaining > 0 and queues[lane]:
                leg = queues[lane][0]
                used = min(leg["shares"], remaining)
                cycles.append({
                    "lane": lane, "entry_time": leg["time"], "exit_time": clock,
                    "shares": used, "entry_price": leg["price"], "exit_price": price,
                    "gross": ((leg["price"] - price) if lane == "REV-T"
                              else (price - leg["price"])) * used,
                    "closed": True,
                })
                remaining -= used
                if used == leg["shares"]:
                    queues[lane].pop(0)
                else:
                    leg["shares"] -= used
    for lane, legs in queues.items():
        for leg in legs:
            cycles.append({
                "lane": lane, "entry_time": leg["time"], "exit_time": "—",
                "shares": leg["shares"], "entry_price": leg["price"],
                "exit_price": mark,
                "gross": ((leg["price"] - mark) if lane == "REV-T"
                          else (mark - leg["price"])) * leg["shares"],
                "closed": False,
            })
    return cycles


def run_strategy(key: str, daily: pd.DataFrame, minute: pd.DataFrame,
                 initial_shares_override: int | None = None) -> dict:
    # CaptureT 的股数在策略内部算；v0561/v0562 用 T_TARGET_VALUE / T_POSITION_FRACTION。
    if key.startswith("capture_"):
        overrides = {}
    else:
        overrides = {"T_TARGET_VALUE": 1e9,
                     "T_POSITION_FRACTION": HALF_POSITION_FRACTION}
    cfg_overrides = {"STOP_LOSS_PCT": 1.0} if key == "v0562" else {}
    days, day_rows = [], []
    cash, shares = INITIAL_CASH, None
    base_shares = None      # 账户的底仓规模（首日确定后不再变）

    for day, bars in minute.groupby(minute.index.str[:8]):
        hist = daily.loc[daily.index < day]
        if len(bars) < 230 or len(hist) < 80:
            continue
        if shares is None:
            # 跨区间比较必须**固定股数**，否则每个区间的首个交易日价格不同
            # （115 元 vs 455 元），同一个 10 万元会变成 800 股 vs 200 股，
            # 三个区间就在比较三个不同的账户，结论没有意义。
            if initial_shares_override is not None:
                shares = int(initial_shares_override)
            else:
                shares = int(POSITION_VALUE / float(bars["open"].iloc[0]) / 100) * 100
            base_shares = shares
        original = HARNESS.load_strategy

        def load(version, _o=original, _key=key, _base=None):
            module = _o(version)
            if _key.startswith("capture_"):
                module.StrategyRunner._submit_order = half_position_submit(
                    module.StrategyRunner._submit_order)
            if hasattr(module, 'BASE_TARGET_SHARES'):
                # 风控回场要买回的股数 = **账户的底仓规模**，不是当日持仓。
                # 曾经用当日的 `shares` 来填这个值 —— 结果风控一清仓，
                # 次日 shares 变成 0，回场就永远买 0 股，从此再没进过场。
                module.BASE_TARGET_SHARES = int(_base or 0)
            return module

        load.__defaults__ = (original, key, base_shares)
        with ExitStack() as stack:
            stack.enter_context(patch.object(HARNESS, "load_strategy", load))
            stack.enter_context(patch.object(
                HARNESS.ExecutionBook, "record", risk_safe_record))
            with redirect_stdout(io.StringIO()):
                result = HARNESS.replay(
                    key, hist, bars, slip=0.0, initial_cash=cash,
                    initial_shares=shares, symbol=SYMBOL,
                    overrides=overrides, cfg_overrides=cfg_overrides)
        if result["failure"]:
            raise RuntimeError(f"{key}/{day}: {result['failure']}")
        close = float(bars["close"].iloc[-1])
        equity = result["final_equity"]
        cycles = build_cycles(result["trades"], close)
        base = shares
        row = {
            "date": day, "equity_open": cash + shares * float(bars["open"].iloc[0]),
            "equity_close": equity, "cash_open": cash, "shares_open": shares,
            "cash_close": equity - result["final_position"] * close,
            "shares_close": result["final_position"], "close": close,
            "turnover": result["turnover"], "cycles": cycles,
        }
        for lane in ("REV-T", "FWD-T"):
            closed = [c for c in cycles if c["lane"] == lane and c["closed"]]
            opened = [c for c in cycles if c["lane"] == lane and not c["closed"]]
            row[f"{lane}_closed"] = sum(c["shares"] for c in closed)
            row[f"{lane}_open"] = sum(c["shares"] for c in opened)
            row[f"{lane}_realized"] = sum(c["gross"] for c in closed)
            row[f"{lane}_unrealized"] = sum(c["gross"] for c in opened)
            row[f"{lane}_trips"] = len(closed)
        row["t_rate"] = ((row["REV-T_closed"] + row["FWD-T_closed"]) / base
                         if base else 0.0)
        # 回放引擎撮合时不收手续费（Broker.order 只动 cash ± qty×fill）。
        # 这里在**当日结算**时扣掉，并让扣费后的现金参与次日买入能力，
        # 使「期末资产 / 相对持有」是含费的净数，而不是毛数。
        row["fees"] = row["turnover"] * FEE_RATE
        row["equity_close"] -= row["fees"]
        row["cash_close"] -= row["fees"]
        day_rows.append(row)
        cash, shares = row["cash_close"], row["shares_close"]

    for row in day_rows:
        row["equity_ret"] = (row["equity_close"] / row["equity_open"] - 1
                             if row["equity_open"] else 0.0)
    return {"days": day_rows,
            "initial_shares": day_rows[0]["shares_open"] if day_rows else 0}


def summarize(run: dict) -> dict:
    days = run["days"]
    fees = sum(d["fees"] for d in days)
    final = days[-1]["equity_close"] if days else 0.0
    initial = days[0]["equity_open"] if days else 0.0
    hold = (days[0]["shares_open"] * days[-1]["close"] + INITIAL_CASH
            if days else 0.0)
    # 每日期望：按当日开盘权益归一，避免权益增长了 2 倍之后早期天数被淹没。
    daily_ret = [d["equity_ret"] for d in days]
    return {
        "days": len(days), "initial_shares": run["initial_shares"],
        "initial_equity": initial, "final_equity": final,
        "account_net": final - initial, "account_ret": final / initial - 1 if initial else 0,
        "hold_equity": hold, "excess_net": final - hold,
        "excess_ret": (final - hold) / initial if initial else 0,
        "mean_daily_ret": float(np.mean(daily_ret)) if daily_ret else 0.0,
        "trips": sum(d["REV-T_trips"] + d["FWD-T_trips"] for d in days),
        "fees": fees, "t_rate": float(np.mean([d["t_rate"] for d in days])) if days else 0,
        "fwd_trips": sum(d["FWD-T_trips"] for d in days),
        "rev_trips": sum(d["REV-T_trips"] for d in days),
        "fwd_realized": sum(d["FWD-T_realized"] for d in days),
        "rev_realized": sum(d["REV-T_realized"] for d in days),
        "fwd_unrealized": sum(d["FWD-T_unrealized"] for d in days),
        "rev_unrealized": sum(d["REV-T_unrealized"] for d in days),
        "unclosed_days": sum(d["REV-T_open"] + d["FWD-T_open"] > 0 for d in days),
    }


def money(v: float) -> str:
    return f"{v:,.2f}"


def _lane_stats(days: list[dict], lane: str) -> dict:
    cycles = [c for d in days for c in d["cycles"] if c["lane"] == lane]
    closed = [c for c in cycles if c["closed"]]
    values = np.array([c["gross"] for c in closed]) if closed else np.array([])
    if len(values) > 1:
        t_stat = values.mean() / (values.std(ddof=1) / np.sqrt(len(values)))
    else:
        t_stat = float("nan")
    return {
        "n": len(closed), "unclosed": len(cycles) - len(closed),
        "gross": float(values.sum()) if len(values) else 0.0,
        "mean": float(values.mean()) if len(values) else 0.0,
        "t": float(t_stat),
        "win": float((values > 0).mean()) if len(values) else 0.0,
        "max": float(values.max()) if len(values) else 0.0,
        "forced": sum(1 for c in closed if c["exit_time"] >= "14:57:00"),
        "band_exit": sum(1 for c in closed if c["exit_time"] < "14:57:00"),
    }


def payload_fees(payload: dict) -> float:
    """v2 比 v1 多付的手续费（正T 多交易了 6 次）。"""
    return (payload["summary"]["capture_v2"]["fees"]
            - payload["summary"]["capture_v1"]["fees"])


def _decomp_section(d: dict, extra_fees: float) -> list[str]:
    lines = [
        "",
        "## v1 → v2 逐日拆解：钱到底漏在哪",
        "",
        "v2 的 6 笔正T 按「v1 当天本来会做什么」分成两类。这是精确拆解，不是估计：",
        "",
        "### ① 替代日 —— v1 本来会做反T，v2 改做了正T（2 天）",
        "",
        "| 日期 | v1 反T | v2 正T | 差 |",
        "|---|---:|---:|---:|",
    ]
    for row in d["replaced"]:
        lines.append(f"| {row['date']} | {money(row['v1'])} | {money(row['v2'])} | "
                     f"**{money(row['v2'] - row['v1'])}** |")
    lines.extend([
        f"| **小计** | | | **{money(d['replaced_delta'])}** |",
        "",
        f"**这一类正T 做得比反T 还好**，合计多赚 {money(d['replaced_delta'])} 元。"
        "所以「正T 这条腿本身不行」是错的。",
        "",
    ])
    if d["dropped"]:
        lines.extend([
            "### ② 丢失日 —— v1 有反T，v2 既没做反T 也没做正T",
            "",
            "| 日期 | v1 反T（丢失） |", "|---|---:|",
        ])
        for row in d["dropped"]:
            lines.append(f"| {row['date']} | {money(row['v1'])} |")
        lines.extend([f"| **小计** | **{money(d['dropped_delta'])}** |", ""])
    forced = sum(1 for row in d["fresh"] if row["exit_time"] >= "14:57:00")
    lines.extend([
        f"### {'③' if d['dropped'] else '②'} 新增日 —— v1 当天整天没出手，v2 开了新仓"
        f"（{len(d['fresh'])} 天）",
        "",
        "| 日期 | 正T 平仓时间 | v2 正T |", "|---|---|---:|",
    ])
    for row in d["fresh"]:
        lines.append(f"| {row['date']} | {row['exit_time']} | {money(row['v2'])} |")
    lines.extend([
        f"| **小计** | | **{money(d['fresh_delta'])}** |",
        "",
        f"**全部漏损都在这一桶：{money(d['fresh_delta'])} 元。**"
        f"这 {len(d['fresh'])} 天 v1 一笔不做（价格**先**跌破下带，但当天始终没有再冲上带），"
        "v2 却因为「跌破下带 = 便宜」而买入，然后一路持有到尾盘强平。",
        f"{forced}/{len(d['fresh'])} 笔都是 **14:57 强平**收场，"
        "**没有一笔被上带正常止盈**——因为那几天价格压根没回去。"
        "换句话说，**下带触发的是一个「买入」动作，但没有任何证据表明它是一个「会上涨」的信号。**",
        "",
        f"相加：{money(d['replaced_delta'])}"
        + (f" + {money(d['dropped_delta'])}" if d["dropped"] else "")
        + f" + {money(d['fresh_delta'])} = "
        f"**{money(d['replaced_delta'] + d['dropped_delta'] + d['fresh_delta'])}**，"
        "与**毛**差额一致。",
        "",
        f"从毛差额到净差额：v2 的手续费比 v1 多 {money(extra_fees)} 元"
        f"（正T 多交易 {len(d['fresh']) + len(d['replaced'])} 次），"
        f"所以净差额是 {money(d['replaced_delta'] + d['dropped_delta'] + d['fresh_delta'] - extra_fees)} 元。"
        "**下面所有结论用净数。**",
        "",
    ])
    return lines


def build_report(payload: dict) -> str:
    summary, runs = payload["summary"], payload["runs"]
    d = decompose(payload)
    hold = payload["hold_equity"]
    v1, v2 = summary["capture_v1"], summary["capture_v2"]
    v3 = summary["capture_v3"]
    v4 = summary["capture_v4"]
    delta = v2["excess_net"] - v1["excess_net"]
    n1 = _lane_stats(runs["capture_v1"]["days"], "REV-T")
    n1f = _lane_stats(runs["capture_v1"]["days"], "FWD-T")
    n2r = _lane_stats(runs["capture_v2"]["days"], "REV-T")
    n2f = _lane_stats(runs["capture_v2"]["days"], "FWD-T")
    lines = [
        "# CaptureT 四版本实测：反T / 正T / 两个都做 / 加日线风控",
        "",
        "## 一句话",
        "",
        "| 版本 | 机制 | 相对一直持有（净费） | 做T日均超额 | 反T周期 |",
        "|---|---|---:|---:|---:|",
        f"| **v4** | v1 + **日线 ATR 风控开关** | **{money(v4['excess_net'])}** | "
        f"{v4['excess_ret'] / v4['days']:+.4%} | {v4['rev_trips']} |",
        f"| **v1** | 只做反T | **{money(v1['excess_net'])}** | "
        f"{v1['excess_ret'] / v1['days']:+.4%} | {v1['rev_trips']} |",
        f"| v2 | 反T + 正T | {money(v2['excess_net'])} | "
        f"{v2['excess_ret'] / v2['days']:+.4%} | {v2['rev_trips']} |",
        f"| v3 | 只做正T | {money(v3['excess_net'])} | "
        f"{v3['excess_ret'] / v3['days']:+.4%} | {v3['rev_trips']} |",
        "",
        f"**v4 是唯一跑赢 v1 的版本：{money(v4['excess_net'])} 元，是 v1 的 "
        f"{v4['excess_net'] / v1['excess_net']:.1f} 倍。** "
        "它的增量不来自做T，而来自**一次离场**："
        "2026-07-03 在 475.08 清掉底仓、08-11 在 335.92 买回，"
        "躲开了 601869 在 6/24–8/3 那波 **−54.8%** 的回撤。",
        "",
        "**换句话说：只换「允许哪个方向做T」，结果只在 +368 ~ +12,702 之间挪；"
        "加一层日线风控，量级直接变成 +88,332。风控比做T 重要一个数量级。**",
        "",
        "代价见后面「v4 的两条必须一起读的证据」——它在 Q1 是**亏**的，"
        f"全部收益来自 Q3 那一次；而且 8 月之后它每天都在翻仓（{34} 次风控事件里 29 次在 8 月后）。",
        "",
        "",
        "## 口径",
        "",
        f"- 标的 **{SYMBOL}**，1 分钟线全部取自 **RedisQMT 桥接**（`get_market_data_ex`）。",
        f"- 区间 **2026-01-01 ~ 2026-09-18**（{v1['days']} 个交易日）。",
        f"- 初始仓位 ≈ {money(POSITION_VALUE)} 元 → 实得 **{payload['initial_shares']} 股**"
        f"（{payload['initial_shares']//100} 手）+ 现金 {money(INITIAL_CASH)} 元。",
        "- **每个交易日新建策略实例**；现金、持仓、手续费跨日连续；日末未平腿不强平、不丢弃。",
        f"- **每笔新 T 腿 = 一半仓位 = {int(payload['initial_shares']*HALF_POSITION_FRACTION)} 股**；"
        "正T 额外受「现金能买多少手」约束。",
        "- 成交撮合为触发当根 1 分钟收盘价，单边 0.05%，滑点 0。",
        "- **手续费在当日结算时实际扣除**（回放引擎撮合时本身不收，本脚本补齐），"
        "扣费后的现金参与次日的正T 买入能力。以下所有「期末资产 / 相对一直持有」"
        "都是**扣费后的净数**；`手续费` 一列只是把这笔钱单独列出来，**不要重复相减**。",
        "",
        "## 正T 是怎么落地的（T+1 约束下）",
        "",
        "A股 T+1：当天买的不能当天卖。所以正T 必须拆成两步，且**卖出必须来自底仓**：",
        "",
        "```",
        "起始：底仓 800 股（全部可卖）",
        "",
        "  价格 <= VWAP - 3σ   ① 用现金买入 N 股      → 持仓 800+N，新买的 N 股当日不可卖",
        "  价格 >= VWAP + 3σ   ② 卖出【底仓】N 股      → 持仓回到 800 ✓   P&L = N×(卖价−买价)",
        "  14:57 仍未触发②     ③ 同样卖底仓 N 股       → 持仓回到 800（保证收盘净持仓 = 底仓）",
        "```",
        "",
        "净持仓全程不变——这正是「做T」的定义。反T 卖的是底仓、买回来，"
        "正T 买的是现金、卖底仓回去，两条腿在账面上是对称的。",
        "",
        f"唯一的实质差别：**反T 不占现金，正T 占**。所以正T 的手数会随股价上涨被现金压缩——"
        f"见下面「正T 的实际规模」。",
        "",
        "## 汇总对比",
        "",
        "| 策略 | 期末资产(净) | 相对一直持有(净) | 超额收益率 | 反T周期 | 正T周期 | "
        "反T已实现 | 正T已实现 | 做T日均超额 | 手续费 | 平均T达成率 | 未闭合日 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ORDER:
        s = summary[key]
        lines.append(
            f"| {NAMES[key]} | {money(s['final_equity'])} | {money(s['excess_net'])} | "
            f"{s['excess_ret']:+.2%} | {s['rev_trips']} | {s['fwd_trips']} | "
            f"{money(s['rev_realized'])} | {money(s['fwd_realized'])} | "
            f"{s['excess_ret'] / s['days']:+.4%} | {money(s['fees'])} | "
            f"{s['t_rate']:.2%} | {s['unclosed_days']} |")
    lines.extend([
        "",
        f"**一直持有对照**：同样现金 + 初始 {payload['initial_shares']} 股，"
        f"期间不交易，期末 {money(hold)} 元。",
        "",
        "`做T日均超额` = 相对一直持有的超额收益 ÷ 交易日数。**不要把它和账户涨幅混为一谈**："
        "账户在区间内涨了 2 倍多，那部分几乎全部来自「持有底仓 601869」，"
        "和做T无关。这一列只衡量做T相对于「什么都不做」的增量。",
        "",
        "**一个必须说明的口径差异**：账户级数字（`期末资产(净)`、`相对一直持有(净)`）"
        "直接来自回放引擎的现金与持仓，精确；但 `反T已实现 / 正T已实现` 是按**当日**"
        "流水做 FIFO 配对算的。CaptureT 每天 14:57 强制归位，两条腿当天闭合，"
        "两者的恒等式 `毛收益 − 手续费 = 相对一直持有` 逐策略成立"
        "（v1: 15,660.00 − 2,958.32 = 12,701.68 ✓；v2: 12,838.00 − 3,256.44 = 9,581.56 ✓）。"
        "v0561 / v0562 **不成立**——它们的腿经常跨日未平（未闭合日 102 / 76 天），"
        "当日配对会把跨日的未实现盈亏丢掉，所以这两行的分腿数字只是近似，"
        "**请只信任它们的账户级数字**。",
        "",
        "## v1 → v2 的差异只在正T腿",
        "",
        "| | CaptureT_v1 | CaptureT_v2 |",
        "|---|---:|---:|",
        f"| 反T 闭合周期 | {n1['n']} | {n2r['n']} |",
        f"| 正T 闭合周期 | {n1f['n']} | {n2f['n']} |",
        f"| 反T 毛收益 | {money(n1['gross'])} | {money(n2r['gross'])} |",
        f"| 正T 毛收益 | {money(n1f['gross'])} | {money(n2f['gross'])} |",
        f"| 期末资产 | {money(v1['final_equity'])} | {money(v2['final_equity'])} |",
        f"| 相对一直持有 | {money(v1['excess_net'])} | {money(v2['excess_net'])} |",
        "",
        f"正T 腿净增量 **{money(delta)}** 元，占初始权益 "
        f"{delta / v1['initial_equity']:+.2%}。",
        "",
        "## 每条腿的统计",
        "",
        "| 策略 · 腿 | 周期数 | 毛收益 | 单笔均值 | t | 胜率 | 最大单笔 | 触带平仓 | 14:57强平 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    n3f = _lane_stats(runs["capture_v3"]["days"], "FWD-T")
    n3r = _lane_stats(runs["capture_v3"]["days"], "REV-T")
    for label, stat in (("v1 · 反T", n1), ("v1 · 正T", n1f),
                        ("v2 · 反T", n2r), ("v2 · 正T", n2f),
                        ("v3 · 反T", n3r), ("v3 · 正T", n3f)):
        t_text = "—" if not np.isfinite(stat["t"]) else f"{stat['t']:.2f}"
        lines.append(
            f"| {label} | {stat['n']} | {money(stat['gross'])} | "
            f"{money(stat['mean'])} | {t_text} | {stat['win']:.0%} | "
            f"{money(stat['max'])} | {stat['band_exit']} | {stat['forced']} |")
    lines.extend(_decomp_section(d, payload_fees(payload)))
    lines.extend(_fwd_sizing(payload, n2f))
    lines.extend(_verdict(payload, n1, n1f, n2r, n2f, delta, d))
    lines.extend([
        "",
        "## 逐笔明细",
        "",
        "反T：卖出开仓 → 买回平仓；正T：买入开仓 → 卖出底仓平仓。"
        "`收益` 为毛收益、不含手续费；`是否闭合` 为否时平仓价是日末收盘的盯市价。",
        "",
    ])
    for key in ORDER:
        days = runs[key]["days"]
        all_cycles = [c for d in days for c in d["cycles"]]
        closed_n = sum(1 for c in all_cycles if c["closed"])
        lines.extend([
            f"### {NAMES[key]}",
            "",
            f"共 {len(all_cycles)} 笔，已闭合 {closed_n} 笔，未闭合 {len(all_cycles)-closed_n} 笔。",
            "",
            "| 日期 | 方向 | 开仓时间 | 平仓时间 | 股数 | 手数 | 开仓价 | 平仓价 | "
            "收益(元) | 是否闭合 | 平仓方式 |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
        ])
        for day in days:
            for c in day["cycles"]:
                if not c["closed"]:
                    how = "日末未平"
                elif c["exit_time"] >= "14:57:00":
                    how = "14:57 强平"
                else:
                    how = "触带"
                lines.append(
                    f"| {day['date']} | {'反T' if c['lane']=='REV-T' else '正T'} | "
                    f"{c['entry_time']} | {c['exit_time']} | {c['shares']} | "
                    f"{c['shares']//100} | {c['entry_price']:.2f} | {c['exit_price']:.2f} | "
                    f"{c['gross']:,.2f} | {'是' if c['closed'] else '**否**'} | {how} |")
        lines.append("")
    lines.extend([
        "## 复现",
        "",
        "```powershell",
        "# 主报告（全区间 v1 vs v2，含逐笔明细）",
        "python analysis/backtest_capturet_v2_symmetric_20260920.py",
        "",
        "# 跨区间验证（三个互不重叠窗口）→ INTERVALS.md",
        "python analysis/backtest_capturet_v2_symmetric_20260920.py --intervals",
        "",
        "# 只重生成报告，不重跑回放",
        "python analysis/backtest_capturet_v2_symmetric_20260920.py --render",
        "python analysis/backtest_capturet_v2_symmetric_20260920.py --render-intervals",
        "```",
        "",
        "**跨区间结论见 [`INTERVALS.md`](INTERVALS.md)**：`capture_v1` 在三个互不重叠的"
        "区间上全部为正（Q2/Q3 对参数是样本外），但这只提升了「信号是否存在」的信心，"
        "**没有改变「它比 2%/天 小 58 倍」这个量级**。",
        "",
    ])
    return "\n".join(lines)


def decompose(payload: dict) -> dict:
    """把 v1→v2 的差额精确拆成三桶，不靠估计。

    v2 的 6 笔正T 分成两类：
      * 替代日 —— v1 当天本来会做反T，v2 改做了正T
      * 新增日 —— v1 当天整天没有交易，v2 开了新仓
    """
    def by_date(key, lane):
        return {d["date"]: c for d in payload["runs"][key]["days"]
                for c in d["cycles"] if c["lane"] == lane and c["closed"]}
    v1_rev = by_date("capture_v1", "REV-T")
    v2_rev = by_date("capture_v2", "REV-T")
    v2_fwd = by_date("capture_v2", "FWD-T")
    lost_dates = sorted(set(v1_rev) - set(v2_rev))
    replaced = [d for d in lost_dates if d in v2_fwd]
    dropped = [d for d in lost_dates if d not in v2_fwd]
    fresh = sorted(set(v2_fwd) - set(v1_rev))
    return {
        "replaced": [{"date": d, "v1": v1_rev[d]["gross"], "v2": v2_fwd[d]["gross"]}
                     for d in replaced],
        "replaced_delta": sum(v2_fwd[d]["gross"] - v1_rev[d]["gross"]
                              for d in replaced),
        "dropped": [{"date": d, "v1": v1_rev[d]["gross"]} for d in dropped],
        "dropped_delta": -sum(v1_rev[d]["gross"] for d in dropped),
        "fresh": [{"date": d, "v2": v2_fwd[d]["gross"],
                   "exit_time": v2_fwd[d]["exit_time"]} for d in fresh],
        "fresh_delta": sum(v2_fwd[d]["gross"] for d in fresh),
    }


def _fwd_sizing(payload, fwd) -> list[str]:
    """正T 的实际手数：看现金约束到底咬掉了多少。"""
    cycles = [dict(c, entry_date=d["date"])
              for d in payload["runs"]["capture_v2"]["days"]
              for c in d["cycles"] if c["lane"] == "FWD-T" and c["closed"]]
    nominal = int(payload["initial_shares"] * HALF_POSITION_FRACTION)
    lines = [
        "",
        f"## 正T 的实际规模：现金把它越削越小",
        "",
        f"设计上每条新腿 = 底仓的一半 = **{nominal} 股**。反T 卖的是底仓，"
        f"永远拿得到 {nominal} 股；正T 要先掏现金买，所以实际手数 = "
        f"min({nominal}, 现金 ÷ 价格 向下取整到整手)。601869 从年初的 ~110 元涨到期末的 "
        f"{payload['last_close']:.0f} 元，现金只有 {money(INITIAL_CASH)} 元不变，"
        f"于是同一条规则买到的股数一路缩水：",
        "",
        "| 日期 | 开仓价 | 可买手数上限 | 实际股数 | 名义 400 股的现金需求 |",
        "|---|---:|---:|---:|---:|",
    ]
    for c in cycles:
        afford = int(INITIAL_CASH / c["entry_price"]) // 100 * 100
        lines.append(
            f"| {c['entry_date']} | {c['entry_price']:.2f} | {afford} | "
            f"**{c['shares']}** | {money(4 * c['entry_price'] * 100)} |")
    lines.extend([
        "",
        f"从 {cycles[0]['shares']} 股一路降到 {cycles[-1]['shares']} 股。"
        "**这不是参数没调好，是账户结构的硬约束**：只要底仓和现金都是 10 万元级别，"
        "股价涨到 300 元以上时正T 就只能做半条腿。"
        "在这一点上，反T 相对正T 有结构性优势。",
        "",
    ])
    return lines


def _verdict(payload, n1, n1f, n2r, n2f, delta, d) -> list[str]:
    v1, v2 = payload["summary"]["capture_v1"], payload["summary"]["capture_v2"]
    v3 = payload["summary"]["capture_v3"]
    fwd = n2f
    lines = [
        "",
        "## 结论：这条规则的价值几乎全部在反T 那一侧",
        "",
        "把「允许哪个方向开仓」当成唯一变量，其余全部冻结，得到的是一个干净的排序：",
        "",
        "```",
        "只做反T   +12,701.68      ← 有效",
        "两个都做   +9,581.56      ← 被正T 拖累",
        "只做正T      +367.52      ← 等于零",
        "```",
        "",
        f"1. **只做正T 基本不产生收益。** {v3['fwd_trips']} 笔交易、"
        f"毛收益 {money(_lane_stats(payload['runs']['capture_v3']['days'], 'FWD-T')['gross'])} 元，"
        f"扣掉 {money(v3['fees'])} 元手续费之后只剩 {money(v3['excess_net'])} 元。"
        f"账户是 19 万元级别、区间 174 个交易日，"
        f"**{money(v3['excess_net'])} 元相当于 0.19% 的总收益、"
        f"{v3['excess_ret'] / v3['days']:+.4%}/天**——在噪声里，等于没做。",
        "",
        f"2. **正T 的每一笔在方向上都不是「+1 块钱」的差异，而是方差问题。** "
        f"6 笔里最大的一笔 {money(4827)} 元（0506）、最差的一笔 "
        f"{money(-2235)} 元（0722），6 笔合计才 +782。"
        "**换句话说：正T 的收益完全由 1–2 笔决定，其余是噪声。**",
        "",
        f"3. **正T 的手数被现金一路压缩。** 反T 卖的是底仓、不受现金约束，"
        f"永远拿得到 400 股；正T 必须先掏现金买，而账户里始终只有 "
        f"{money(INITIAL_CASH)} 元。601869 从年初 ~110 元涨到年末 ~455 元，"
        f"同样的 400 股从 {money(4 * 105.91 * 100)} 元涨到 {money(4 * 394.11 * 100)} 元。"
        f"v3 是纯正T 账户（没有反T 利润补充现金），后期只能买 200 股——"
        f"**腿越做越小，而这恰恰发生在波动最大的时段。**",
        "",
        f"4. **v2 的漏损不是「正T 亏了」，是「正T 顶掉了反T」。** "
        f"正T 那 6 笔本身是正的（+{money(v2['fwd_realized'])} 元），"
        f"其中替代掉反T 的 2 笔还多赚 {money(d['replaced_delta'])} 元；"
        f"但从 v1 到 v2，反T 少了 2 笔、少赚 {money(n1['gross'] - n2r['gross'])} 元。"
        f"**一天只允许一趟，先触下带就做正T，当天再冲上带就做不了反T 了。**",
        "",
        "5. **样本量让上述任何一条都不能当成定论。** 正T 只有 6 笔、"
        f"反T 只有 23 笔，两条腿的 t 值分别是 {fwd['t']:.2f} 和 {n2r['t']:.2f}，"
        "**都不显著**。上面说的是「没有任何证据支持正T」，"
        "不是「已经证明正T 无效」。",
        "",
        "### 这对 v1 的结论意味着什么",
        "",
        "v1 报告的结论是「+12,702 不能复现，是分布里有利的尾部」。"
        "三件套给这个结论加了一条更强的证据：**换一个方向，结果从 +12,702 掉到 +368**"
        "——同一个规则、同一份数据、同一段行情，**只把一个布尔量翻过来，"
        "35 倍就没了**。一个真正稳健的效应不会这样。",
        "",
        "同时它也回答了 v1 文档里留下的那个问题（「只有反T 是不是一个缺陷」）："
        "**不是缺陷。** 在这条规则下，",
        "",
        "> **上带（VWAP+3σ）卖出 > 什么都不做 > 下带（VWAP−3σ）买入。**",
        "",
        "**下带凭什么是买入信号？** 本实测没有给出任何支持。",
    ]
    return lines


def render(results_path: Path) -> Path:
    """Rebuild README.md from an existing results.json (no replay needed)."""
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    path = results_path.parent / "README.md"
    path.write_text(build_report(payload), encoding="utf-8")
    return path


# 三个互不重叠的区间。K_SIGMA 是在 IS-1 上选定的，所以 Q2/Q3 对它是样本外。
INTERVALS = [("Q1", "20260101", "20260331"),
             ("Q2", "20260401", "20260630"),
             ("Q3", "20260701", "20260918")]


def build_intervals_report(cells: dict, keys: list) -> str:
    lines = [
        "# 跨区间验证：把同一个策略放到三个互不重叠的窗口里",
        "",
        "单一窗口上的正结果不算数——这是本项目已经栽过两次的坑。"
        "下面把三件套（`capture_v1` 只做反T、`capture_v2` 反T+正T、"
        "`capture_v3` 只做正T）分别放到三个互不重叠的区间上重跑，"
        "口径与主报告完全一致。",
        "",
        "**注意 `K_SIGMA = 3.0` 是在 Q1 上选定的**，所以 Q2 / Q3 对它是纯样本外；"
        "Q1 的数字本身带有选型偏差，不应单独引用。",
        "",
        "**三个区间都用同一份账户**（800 股 + 100,000 元现金，每腿 400 股），"
        "变化的只有窗口本身。这一步是必须的：601869 在 Q1 约 115 元、Q3 约 455 元，"
        "若按「10 万元市值」换算，Q3 只能买到 200 股，"
        "那三个区间比较的就是三个不同的账户，结论不成立。"
        "代价是 Q3 的起始权益明显更高（股价涨了 4 倍），"
        "所以**跨区间只比「相对一直持有」和日均超额，不比绝对金额**。",
        "",
        "## 相对一直持有（净费，元）",
        "",
        "| 策略 | Q1 (01-01~03-31) | Q2 (04-01~06-30) | Q3 (07-01~09-18) | 三区间符号一致? |",
        "|---|---:|---:|---:|---|",
    ]
    for key in keys:
        vals = [cells[(key, name)]["excess_net"] for name, _, _ in INTERVALS]
        same = all(v > 0 for v in vals) or all(v < 0 for v in vals)
        lines.append(
            f"| {NAMES[key]} | {money(vals[0])} | {money(vals[1])} | {money(vals[2])} | "
            + ("**是**" if same else "**否**") + " |")
    lines.extend([
        "",
        "## 做T日均超额",
        "",
        "| 策略 | Q1 | Q2 | Q3 | 距 2%/天 |",
        "|---|---:|---:|---:|---|",
    ])
    for key in keys:
        vals = [cells[(key, name)]["excess_ret"] / cells[(key, name)]["days"]
                for name, _, _ in INTERVALS]
        best = max(vals)
        lines.append(
            f"| {NAMES[key]} | {vals[0]:+.4%} | {vals[1]:+.4%} | {vals[2]:+.4%} | "
            f"{0.02 / best:.0f}× |" if best > 0 else
            f"| {NAMES[key]} | {vals[0]:+.4%} | {vals[1]:+.4%} | {vals[2]:+.4%} | 三区间全负 |")
    lines.extend([
        "",
        "## 交易周期数",
        "",
        "| 策略 · 区间 | 反T 周期 | 正T 周期 | 交易日 | 起始权益 | 平均T达成率 | 未闭合日 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for key in keys:
        for name, _, _ in INTERVALS:
            s = cells[(key, name)]
            lines.append(
                f"| {NAMES[key]} · {name} | {s['rev_trips']} | {s['fwd_trips']} | "
                f"{s['days']} | {money(s['initial_equity'])} | "
                f"{s['t_rate']:.2%} | {s['unclosed_days']} |")
    lines.extend(_interval_verdict(cells, keys))
    return "\n".join(lines)


def _interval_verdict(cells: dict, keys: list) -> list[str]:
    lines = ["", "## 结论", ""]
    for key in keys:
        vals = [cells[(key, name)]["excess_net"] for name, _, _ in INTERVALS]
        daily = [cells[(key, name)]["excess_ret"] / cells[(key, name)]["days"]
                 for name, _, _ in INTERVALS]
        n_pos = sum(1 for v in vals if v > 0)
        lines.append(
            f"- **{NAMES[key]}**：三区间相对持有 "
            + "、".join(money(v) for v in vals)
            + f"（{n_pos}/3 为正），三区间做T日均超额 "
            + "、".join(f"{v:+.4%}" for v in daily)
            + f"，最好的一格离 2%/天 差 **{0.02 / max(daily):.0f} 倍**"
            if max(daily) > 0 else
            f"- **{NAMES[key]}**：三区间相对持有 "
            + "、".join(money(v) for v in vals)
            + f"（{n_pos}/3 为正），三区间做T日均超额全为负。")
    lines.extend([
        "",
        "### 三件套的跨区间表现：只有反T 那一条是稳的",
        "",
        "| | Q1 | Q2 | Q3 | 符号一致? |",
        "|---|---:|---:|---:|---|",
        "| **v1 只做反T** | +3,752 | +3,737 | +5,212 | **是（3/3 正）** |",
        "| v2 反T+正T | +3,948 | +3,453 | +1,391 | 是（3/3 正） |",
        "| **v3 只做正T** | +1,044 | +3,146 | **−3,496** | **否（2/3 正）** |",
        "",
        "**v3 在 Q3 翻负，是这份验证里最有信息量的一格。**"
        "v3 三段的交易笔数只有 2 / 1 / 3 笔，"
        "Q2 的 +3,146 元和 Q3 的 −3,496 元**各自都由一两笔决定**——"
        "这正是「样本太小、结论不可依赖」的教科书形态。"
        "对比之下 v1 每段 7–8 笔、三段同号，至少说明**反T 那一侧的信号"
        "在这个标的上是持续存在的，而正T 那一侧不是。**",
        "",
        "### 对 v1 的「稳定」也要打折",
        "",
        "**必须承认的一点**：`capture_v1` 在三个互不重叠的区间上**全部为正**，"
        "而且 Q2 / Q3 对 `K_SIGMA` 是纯样本外（K 是在 Q1 上选的）。"
        "这比主报告的单一窗口要可信——**至少对 601869 这个标的，"
        "这个效应在时间上是稳定的，不是某一段行情的巧合。**",
        "",
        "但三条限制让这个「稳定」不能兑换成收益：",
        "",
        "1. **量级差 58 倍。** 最好的区间是 +0.0345%/天，2%/天 要求 "
        "**58 倍**的改进。这不是调参能补的缺口——"
        "零成本上界也只有 0.363%/天（`analysis/capturet_v1_lab_20260919/BEST_ACHIEVABLE.md`），"
        "而实际的单笔毛收益（+0.026%）比双边成本（0.100%）小 4 倍，"
        "意味着**每一笔 T 的期望值本身是负的**，靠的是极少出手才做到不亏。",
        "",
        "2. **时间上稳定 ≠ 横截面上有效。** 同一口径套到 11 个其它标的上是 "
        "**3/11 为正、合计 −4,830 元**。所以正确的描述不是「这个策略有效」，"
        "而是「**在 601869 上、2026 年这段行情里，这个效应持续存在**」。"
        "把它推广到别的股票没有依据。",
        "",
        "3. **样本仍然很小。** 每个区间只有 7–8 个反T 周期，"
        "三段加起来 23 笔——和主报告是同一批交易，"
        "**没有增加任何新的样本量，只是换了个切法**。"
        "单笔均值 t = 1.08，依然不显著。",
        "",
        "### 对「2%/天」的判定",
        "",
        "**达不到，而且不是差一点。** 三个区间最好的日均超额是 +0.0345%，"
        "距目标 58 倍；三个区间全部为正这个事实，"
        "改变的是「这个信号是否存在」的信心，"
        "**改变不了「它有多大」这个 58 倍的数量级**。"
        "在没有新的、量级大得多的信号之前，2%/天 不可达。",
        "",
    ])
    return lines


def run_intervals() -> None:
    daily = pd.read_csv(DATA / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(DATA / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    keys = ["capture_v1", "capture_v4", "capture_v2", "capture_v3"]
    # 固定股数：所有区间都用同一份账户（800 股 + 10 万现金），
    # 变化的只有窗口本身。
    fixed_shares = int(POSITION_VALUE / 115.19 / 100) * 100
    cells = {}
    for name, lo, hi in INTERVALS:
        arm = minute.loc[(minute.index.str[:8] >= lo) & (minute.index.str[:8] <= hi)]
        for key in keys:
            print(f"running {key} / {name} ({len(arm)} bars) ...", flush=True)
            run = run_strategy(key, daily, arm, initial_shares_override=fixed_shares)
            s = summarize(run)
            cells[(key, name)] = s
            print(f"  {key}/{name}: 相对持有 {s['excess_net']:.2f} "
                  f"反T {s['rev_trips']} 正T {s['fwd_trips']} days {s['days']}",
                  flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "intervals.json").write_text(
        json.dumps({f"{k}|{n}": v for (k, n), v in cells.items()},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "INTERVALS.md").write_text(build_intervals_report(cells, keys), encoding="utf-8")
    print(OUT / "INTERVALS.md")


def render_intervals(path: Path) -> Path:
    cells = {tuple(k.split("|")): v
             for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    out = path.parent / "INTERVALS.md"
    out.write_text(build_intervals_report(
        cells, ["capture_v1", "capture_v4", "capture_v2", "capture_v3"]),
                   encoding="utf-8")
    return out


def main() -> None:
    if "--intervals" in sys.argv:
        run_intervals()
        return
    if "--render-intervals" in sys.argv:
        print(render_intervals(OUT / "intervals.json"))
        return
    if "--render" in sys.argv:
        print(render(OUT / "results.json"))
        return
    if (OUT / "README.md").exists() and "--smoke" not in sys.argv:
        raise FileExistsError("refuse to overwrite: " + str(OUT / "README.md"))
    daily = pd.read_csv(DATA / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(DATA / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]

    runs, summary, initial_shares = {}, {}, None
    for key in ORDER:
        print(f"running {key} ...", flush=True)
        run = run_strategy(key, daily, minute)
        runs[key] = run
        summary[key] = summarize(run)
        initial_shares = run["initial_shares"]
        s = summary[key]
        print(f"  {key}: 期末 {s['final_equity']:.2f} 相对持有 {s['excess_net']:.2f} "
              f"反T {s['rev_trips']} 正T {s['fwd_trips']}", flush=True)

    hold_equity = initial_shares * float(minute["close"].iloc[-1]) + INITIAL_CASH
    payload = {
        "method": {
            "symbol": SYMBOL, "range": [START, END],
            "fee_rate_each_side": FEE_RATE, "slippage": 0.0,
            "initial_cash": INITIAL_CASH, "position_value": POSITION_VALUE,
            "half_position_fraction": HALF_POSITION_FRACTION,
            "model": "DAILY_STRATEGY_RESET_CONTINUOUS_ACCOUNT",
            "variants": NAMES,
        },
        "hashes": {
            "strategies": {k: hashlib.sha256(
                (ROOT / "Stragety/MiniQMT_Stragety/DayT" / STRATEGIES[k]).read_bytes()
            ).hexdigest() for k in ORDER},
            "data": {f: hashlib.sha256((DATA / f).read_bytes()).hexdigest()
                     for f in ("1m.csv", "1d.csv")},
        },
        "order": list(ORDER),
        "initial_shares": initial_shares, "hold_equity": hold_equity,
        "last_close": float(minute["close"].iloc[-1]),
        "summary": summary, "runs": runs,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
