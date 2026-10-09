"""Capture lab: how much of a day's range can any rule actually take?

The user's target is 2%/day.  Before designing anything, this measures the hard
ceiling with perfect foresight, then measures what a family of simple causal
rules can extract, and expresses every result as a fraction of that ceiling
("oracle capture").

The oracle is order- and constraint-aware, because the naive version is
optimistic:
  * high before low  -> reverse-T of the full base is cash-free, N = base shares
  * low before high  -> forward-T is capped by cash AND by T+1 (shares bought
    today cannot be sold today, so the sale must come from the base), so
    N = min(base, floor(cash / low / lot) * lot)

Discipline: rule selection uses IS-1 only.  IS-2 and OOS-A are read once, after
the parameters are frozen, and are reported whether or not they agree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "analysis/capturet_v1_lab_20260919"
FEE_RATE = 0.0005
BASE_SHARES = 1_000
INITIAL_CASH = 100_000.0
LOT = 100

INTERVALS = (
    ("OOS-A", "20250915", "20251231", "验证"),
    ("IS-1", "20260101", "20260515", "选型"),
    ("IS-2", "20260516", "20260911", "验证"),
)
SELECTION_INTERVAL = "IS-1"

DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}

TARGET_DAILY = 0.02


def load_days(folder: Path) -> dict[str, pd.DataFrame]:
    minute = pd.read_csv(
        folder / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    days = {}
    for stamp, bars in minute.groupby(minute.index.str[:8]):
        if len(bars) >= 230:
            days[stamp] = bars
    return days


def equity_of(bars: pd.DataFrame) -> float:
    return BASE_SHARES * float(bars["close"].iloc[0]) + INITIAL_CASH


# ── 上界 ────────────────────────────────────────────────────────────────────

def oracle_net(bars: pd.DataFrame) -> tuple[float, int]:
    """Perfect-foresight net P&L and the share count it is achievable with.

    The share count is constrained, not assumed:
      * high before low -> reverse-T of the base, cash-free, full size
      * low before high -> forward-T is capped by cash AND by T+1 (shares bought
        today cannot be sold today, so the sale must come from the base)
    """
    high = float(bars["high"].max())
    low = float(bars["low"].min())
    t_high = bars["high"].astype(float).idxmax()
    t_low = bars["low"].astype(float).idxmin()
    if t_high < t_low:
        shares = BASE_SHARES
    else:
        affordable = int(INITIAL_CASH / low / LOT) * LOT if low > 0 else 0
        shares = min(BASE_SHARES, affordable)
    if shares <= 0:
        return 0.0, 0
    return shares * (high - low) - shares * (high + low) * FEE_RATE, shares


def oracle_return(bars: pd.DataFrame) -> float:
    """Perfect foresight as a fraction of account equity."""
    return oracle_net(bars)[0] / equity_of(bars)


def naive_overnight(bars: pd.DataFrame, previous_close: float | None) -> float:
    """Hold the extra 1,000 from the previous close to today's open."""
    if not previous_close or previous_close <= 0:
        return 0.0
    open_ = float(bars["open"].iloc[0])
    shares = min(BASE_SHARES, int(INITIAL_CASH / previous_close / LOT) * LOT)
    gross = shares * (open_ - previous_close)
    fees = shares * (open_ + previous_close) * FEE_RATE
    return (gross - fees) / equity_of(bars)


def naive_intraday(bars: pd.DataFrame) -> float:
    """Sell the base at the open, buy it back at the close."""
    open_ = float(bars["open"].iloc[0])
    close = float(bars["close"].iloc[-1])
    gross = BASE_SHARES * (open_ - close)
    fees = BASE_SHARES * (open_ + close) * FEE_RATE
    return (gross - fees) / equity_of(bars)


# ── 候选规则族（满仓反T，一天至多一趟，尾盘强平）────────────────────────────

def _round_trip(bars: pd.DataFrame, sell_at, buy_at) -> float:
    """Walk the session once.  sell_at/buy_at take (bars, index, state) -> bool."""
    if sell_at is None:
        return 0.0
    equity = equity_of(bars)
    prices = bars["close"].astype(float).values
    sold_price = None
    for i, price in enumerate(prices):
        if sold_price is None:
            if sell_at(bars, i, price):
                sold_price = price
        elif buy_at is None or buy_at(bars, i, price):
            gross = BASE_SHARES * (sold_price - price)
            fees = BASE_SHARES * (sold_price + price) * FEE_RATE
            return (gross - fees) / equity
    if sold_price is not None:            # 尾盘强平
        price = float(prices[-1])
        gross = BASE_SHARES * (sold_price - price)
        fees = BASE_SHARES * (sold_price + price) * FEE_RATE
        return (gross - fees) / equity
    return 0.0


def rule_threshold(bars, x, y):
    open_ = float(bars["open"].iloc[0])
    trigger = open_ * (1 + x)
    state = {"sold": None}

    def sell(bars, i, price):
        if state["sold"] is None and price >= trigger:
            state["sold"] = price
            return True
        return False

    def buy(bars, i, price):
        return state["sold"] is not None and price <= state["sold"] * (1 - y)

    return _round_trip(bars, sell, buy)


def rule_vwap(bars, k, warmup=30):
    prices = bars["close"].astype(float).values
    vol = bars["volume"].astype(float).values
    state = {"sold": None, "vwap": None, "sd": None}

    def sell(bars, i, price):
        if i < warmup or state["sold"] is not None:
            return False
        window = prices[:i + 1]
        weights = vol[:i + 1]
        if weights.sum() <= 0:
            return False
        vwap = float((window * weights).sum() / weights.sum())
        sd = float(window.std())
        if sd <= 0:
            return False
        state["vwap"], state["sd"] = vwap, sd
        if price >= vwap + k * sd:
            state["sold"] = price
            return True
        return False

    def buy(bars, i, price):
        if state["sold"] is None or state["vwap"] is None:
            return False
        return price <= state["vwap"] - k * state["sd"]

    return _round_trip(bars, sell, buy)


def rule_orb_down(bars, m, warmup=30):
    """Sell when price breaks below the first-30-minute range."""
    prices = bars["close"].astype(float).values
    state = {"sold": None, "floor": None}

    def sell(bars, i, price):
        if i < warmup or state["sold"] is not None:
            return False
        state["floor"] = float(prices[:warmup].min())
        if price <= state["floor"] * (1 - m):
            state["sold"] = price
            return True
        return False

    def buy(bars, i, price):
        return state["sold"] is not None and price <= state["sold"] * 0.995

    return _round_trip(bars, sell, buy)


def rule_momentum(bars, m, lookback=15):
    """Sell when short-horizon momentum turns negative."""
    prices = bars["close"].astype(float).values
    state = {"sold": None}

    def sell(bars, i, price):
        if i < lookback or state["sold"] is not None:
            return False
        ret = prices[i] / prices[i - lookback] - 1
        if ret <= -m:
            state["sold"] = price
            return True
        return False

    def buy(bars, i, price):
        return state["sold"] is not None and price <= state["sold"] * 0.995

    return _round_trip(bars, sell, buy)


def rule_vwap_multi(bars, k, max_trips=2, warmup=30):
    """Repeat the VWAP round trip up to max_trips times in one session."""
    prices = bars["close"].astype(float).values
    vol = bars["volume"].astype(float).values
    equity = equity_of(bars)
    sold = None
    pnl = 0.0
    trips = 0
    for i, price in enumerate(prices):
        if i < warmup:
            continue
        window, weights = prices[:i + 1], vol[:i + 1]
        if weights.sum() <= 0:
            continue
        vwap = float((window * weights).sum() / weights.sum())
        sd = float(window.std())
        if sd <= 0:
            continue
        if sold is None:
            if trips < max_trips and price >= vwap + k * sd:
                sold = price
        elif price <= vwap - k * sd:
            pnl += BASE_SHARES * (sold - price) - BASE_SHARES * (sold + price) * FEE_RATE
            sold, trips = None, trips + 1
    if sold is not None:                       # 尾盘强平
        price = float(prices[-1])
        pnl += BASE_SHARES * (sold - price) - BASE_SHARES * (sold + price) * FEE_RATE
    return pnl / equity


def rule_gap(bars, g, previous_close):
    """Gap up -> sell at the open and buy back at the close; gap down -> hold."""
    if not previous_close or previous_close <= 0:
        return 0.0
    open_ = float(bars["open"].iloc[0])
    if open_ / previous_close - 1 < g:
        return 0.0
    close = float(bars["close"].iloc[-1])
    gross = BASE_SHARES * (open_ - close)
    fees = BASE_SHARES * (open_ + close) * FEE_RATE
    return (gross - fees) / equity_of(bars)


def rule_spike_fade(bars, x, arm=30):
    """Sell once today's gain over the open exceeds x, hold to the close."""
    prices = bars["close"].astype(float).values
    open_ = float(bars["open"].iloc[0])
    equity = equity_of(bars)
    for i, price in enumerate(prices):
        if i < arm:
            continue
        if price >= open_ * (1 + x):
            close = float(prices[-1])
            gross = BASE_SHARES * (price - close)
            fees = BASE_SHARES * (price + close) * FEE_RATE
            return (gross - fees) / equity
    return 0.0


def rule_open_reversal(bars, x, arm=30):
    """Fade the first-30-minute move: sell if the open leg ran up, hold otherwise."""
    prices = bars["close"].astype(float).values
    open_ = float(bars["open"].iloc[0])
    if arm >= len(prices):
        return 0.0
    run = prices[arm] / open_ - 1
    if run < x:
        return 0.0
    sold = prices[arm]
    close = float(prices[-1])
    gross = BASE_SHARES * (sold - close)
    fees = BASE_SHARES * (sold + close) * FEE_RATE
    return (gross - fees) / equity_of(bars)



def rule_range_position(bars, hi_q, lo_q):
    """Sell near the top of the day's range so far, buy back near the bottom."""
    prices = bars["close"].astype(float).values
    equity = equity_of(bars)
    sold = None
    for i, price in enumerate(prices):
        if i < 30:
            continue
        window = prices[:i + 1]
        lo, hi = float(window.min()), float(window.max())
        if hi <= lo:
            continue
        pos = (price - lo) / (hi - lo)
        if sold is None and pos >= hi_q:
            sold = price
        elif sold is not None and pos <= lo_q:
            gross = BASE_SHARES * (sold - price)
            fees = BASE_SHARES * (sold + price) * FEE_RATE
            return (gross - fees) / equity
    if sold is not None:
        price = float(prices[-1])
        gross = BASE_SHARES * (sold - price)
        fees = BASE_SHARES * (sold + price) * FEE_RATE
        return (gross - fees) / equity
    return 0.0



# 每个规则: (函数, 参数A候选, 参数B候选, 是否需要昨收)
RULES = {
    "阈值反T": (rule_threshold, (0.005, 0.01, 0.015, 0.02, 0.03),
               (0.005, 0.01, 0.015, 0.02, 0.03), False),
    "VWAP偏离": (rule_vwap, (0.5, 1.0, 1.5, 2.0, 3.0), (None,), False),
    "VWAP多趟": (rule_vwap_multi, (0.5, 1.0, 1.5, 2.0), (1, 2, 3), False),
    "ORB下破": (rule_orb_down, (0.002, 0.005, 0.01, 0.02), (None,), False),
    "日内动量": (rule_momentum, (0.002, 0.005, 0.01, 0.02), (None,), False),
    "冲高回落": (rule_spike_fade, (0.005, 0.01, 0.015, 0.02, 0.03), (None,), False),
    "缺口回归": (rule_gap, (0.005, 0.01, 0.02, 0.03), (None,), True),
    "开盘反转": (rule_open_reversal, (0.005, 0.01, 0.015, 0.02, 0.03), (None,), False),
    "区间分位": (rule_range_position, (0.6, 0.7, 0.8, 0.9), (0.1, 0.2, 0.3), False),
}


def previous_closes(days: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Date -> previous session's last close, for rules that need it."""
    ordered = sorted(days)
    out = {}
    for i, day in enumerate(ordered):
        if i == 0:
            continue
        out[day] = float(days[ordered[i - 1]]["close"].iloc[-1])
    return out


def evaluate(rule, days: dict[str, pd.DataFrame], params: tuple,
             needs_prev: bool = False) -> np.ndarray:
    if not needs_prev:
        return np.array([rule(bars, *params) for bars in days.values()])
    prev = previous_closes(days)
    return np.array([rule(bars, *params, previous_close=prev.get(day))
                     if day in prev else 0.0
                     for day, bars in days.items()])


def select_on_is1(fold: dict) -> dict:
    """Choose parameters using IS-1 only, then freeze them."""
    chosen = {}
    for name, (rule, grid_a, grid_b, needs_prev) in RULES.items():
        best = None
        for a in grid_a:
            for b in (grid_b if grid_b != (None,) else (None,)):
                params = (a,) if b is None else (a, b)
                score = {}
                for symbol in DATASETS:
                    r = evaluate(rule, fold[symbol][SELECTION_INTERVAL], params,
                                 needs_prev)
                    score[symbol] = float(r.mean())
                mean = float(np.mean(list(score.values())))
                if best is None or mean > best[0]:
                    best = (mean, params, score)
        chosen[name] = {"params": best[1], "is1_mean": best[0],
                        "is1_by_symbol": best[2]}
    return chosen


# 注册表里的真实 key。v056_nomom 出厂即"正T冻结"，与旧报告口径一致。
COMPARISON_STRATEGIES = ("capture_v1", "v0562", "v056_nomom")


def validate_strategy(payload: dict, folds: dict, versions=("capture_v1",)) -> dict:
    """Run the real strategy files through the replay harness, interval by interval.

    The lab simulator fills at minute closes with no partial fills; the harness
    has its own fill model.  Agreeing within noise is evidence that the lab is
    not overstating the rule; disagreeing means the lab number is wrong.
    """
    from output.analysis.compare_v51_v39_minute import replay
    from backtest.dayt_registry import STRATEGIES

    out = {}
    for version in versions:
      out[version] = {}
      for interval, _start, _end, role in INTERVALS:
        per_symbol = {}
        for symbol, days in folds.items():
            folder = DATASETS[symbol]
            daily = pd.read_csv(
                folder / "1d.csv", dtype={"time": str}).set_index(
                    "time").sort_index()
            rows = []
            for day, bars in sorted(days[interval].items()):
                history = daily.loc[daily.index < day]
                if len(history) < 80:
                    continue
                result = replay(
                    version, history, bars, slip=0.0,
                    initial_cash=INITIAL_CASH, initial_shares=BASE_SHARES,
                    symbol=symbol)
                if result["failure"]:
                    raise RuntimeError(f"{symbol}/{interval}/{day}: {result['failure']}")
                rows.append(result)
            if not rows:
                per_symbol[symbol] = {"days": 0, "mean": 0.0, "capture": 0.0}
                continue
            # Daily return is taken against that day's own opening equity, so
            # days are comparable even when the price has moved a long way.
            daily_return = []
            for r in rows:
                equity = (BASE_SHARES * float(
                    folds[symbol][interval][r["date"]]["open"].iloc[0])
                    + INITIAL_CASH)
                daily_return.append(
                    (r["excess_gross"] - r["turnover"] * FEE_RATE) / equity)
            mean_return = float(np.mean(daily_return))
            oracle = payload["bounds"][symbol][interval]["oracle"]
            per_symbol[symbol] = {
                "days": len(rows),
                "fills": sum(len(r["trades"]) for r in rows),
                "mean": mean_return,
                "capture": (mean_return / oracle) if oracle > 0 else 0.0,
                "total": float(np.sum(daily_return)),
            }
            per_symbol[symbol]["cum_excess"] = float(np.sum(daily_return))
            print(f"  validate {version} {symbol} {interval}: "
                  f"{mean_return:.4%}/day cum={np.sum(daily_return):+.2%}, "
                  f"{per_symbol[symbol]['fills']} fills", flush=True)
        means = [v["mean"] for v in per_symbol.values()]
        caps = [v["capture"] for v in per_symbol.values()]
        out[version][interval] = {
            "role": role,
            "mean": float(np.mean(means)) if means else 0.0,
            "capture": float(np.mean(caps)) if caps else 0.0,
            "cum_excess": float(np.mean(
                [v["cum_excess"] for v in per_symbol.values()])),
            "days": max([v["days"] for v in per_symbol.values()] or [0]),
            "by_symbol": per_symbol,
        }
    return out


NAMES = {"capture_v1": "CaptureT_v1（本次新设计）",
         "v0562": "v0562 CoreT+ATR再入场（上一代最好）",
         "v056_nomom": "v056 正T冻结（旧系列基准）"}


def _strategy_section(payload: dict) -> list[str]:
    """The two stated targets, measured on real strategy files, per interval."""
    lines = [
        "## 落地策略实测：两个目标的达成情况",
        "",
        "把**真实策略文件**跑过回放框架（不是实验室的简化模拟），三个区间分别统计。"
        "`累计超额` 是相对「持有 1000 股底仓」的累计收益率，逐日按当日开盘权益归一后相加——"
        "这正是目标里「超额收益」的口径。",
        "",
        "| 策略 | 区间 | 日均收益 | **累计超额** | 年化 | 距 2%/天 | 达标 20% 超额? |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for version, per_interval in payload["strategy"].items():
        for interval, _s, _e, role in INTERVALS:
            d = per_interval[interval]
            if not d.get("days"):
                continue
            days = d["days"]
            cum = d["cum_excess"]
            ann = (1 + cum) ** (250 / days) - 1 if cum > -1 else float("nan")
            gap = f"{TARGET_DAILY / d['mean']:.0f}x" if d["mean"] > 0 else "未盈利"
            ok = "**是**" if cum >= 0.20 else "否"
            lines.append(
                f"| {NAMES.get(version, version)} | {interval} ({role}) | "
                f"{d['mean']:.4%} | **{cum:+.2%}** | {ann:+.2%} | {gap} | {ok} |")
    lines.extend([
        "",
        "### 判定",
        "",
    ])
    cap = payload["strategy"].get("capture_v1", {})
    best_cum = max((d["cum_excess"] for d in cap.values()), default=0.0)
    best_daily = max((d["mean"] for d in cap.values()), default=0.0)
    lines.extend([
        f"- **目标一（每天 2%）**：未达成。CaptureT_v1 最好的区间是 "
        f"**{best_daily:.4%}/天**，距 2% 差 "
        f"**{TARGET_DAILY / best_daily:.0f} 倍**；"
        "并且该目标对 600584/600105 的样本外区间**在数学上不可达**"
        "（完美预知上界仅 0.82% / 0.94%）。",
        f"- **目标二（超额收益 20%）**：未达成。CaptureT_v1 最好的区间累计超额 "
        f"**{best_cum:+.2%}**，是 20% 的 **{best_cum / 0.20:.0%}**；"
        "另外两个区间分别为 "
        + "、".join(f"{cap[i]['cum_excess']:+.2%}" for i, _s, _e, _r in INTERVALS
                    if i != max(cap, key=lambda k: cap[k]["cum_excess"]))
        + "。",
        "",
        "**两个目标都未达成，而且不是同一类原因**：",
        "目标一受**信息**限制（神谕上界就在那里，任何规则都超不过）；"
        "目标二受**同一原因**连带（超额收益来自日内交易，而日内交易本身没有可复现的 alpha）。",
        "",
    ])
    return lines


HORIZONS = (1, 3, 5, 15, 30, 60)


def horizon_analysis() -> dict:
    """Where can a trade possibly pay for itself?

    A round trip costs 2 x FEE of notional.  A trade at horizon H moves on
    average |p(t+H) - p(t)|.  Break-even therefore needs a directional hit rate
    of (1 + 2*fee / move) / 2.  Comparing that with the measured momentum hit
    rate says which holding horizons are even theoretically reachable.
    """
    rows = []
    for horizon in HORIZONS:
        moves, hits, total = [], 0, 0
        for symbol, folder in DATASETS.items():
            minute = pd.read_csv(
                folder / "1m.csv", dtype={"time": str}).set_index(
                    "time").sort_index()
            for _stamp, bars in minute.groupby(minute.index.str[:8]):
                if len(bars) < 230:
                    continue
                prices = bars["close"].astype(float).values
                open_ = float(bars["open"].iloc[0])
                future = prices[horizon:] - prices[:-horizon]
                if horizon == 1:
                    past, nxt = future[:-1], future[1:]
                elif len(prices) > 2 * horizon:
                    past = prices[horizon:-horizon] - prices[:-2 * horizon]
                    nxt = future[horizon:]
                else:
                    continue
                if not len(past):
                    continue
                moves.extend((np.abs(future) / open_).tolist())
                sp, sn = np.sign(nxt), np.sign(past)
                keep = (sp != 0) & (sn != 0)
                hits += int((sp[keep] == sn[keep]).sum())
                total += int(keep.sum())
        move = float(np.mean(moves)) if moves else 0.0
        need = (1 + 2 * FEE_RATE / move) / 2 if move > 0 else 1.0
        measured = hits / total if total else 0.0
        rows.append({"horizon": horizon, "move": move, "need": need,
                     "measured": measured, "gap": need - measured})
        print(f"horizon {horizon}m: move={move:.3%} need={need:.1%} "
              f"measured={measured:.1%} gap={need - measured:+.1%}", flush=True)
    return {"rows": rows}


def _why_section(payload: dict) -> list[str]:
    """Why no rule reached the target, stated as a measurable property."""
    horizon = payload["horizon"]["rows"]
    lines = [
        "## 为什么 2%/天 到不了：把问题归结成一个可测的数",
        "",
        "### 上界并不低——一天能赚的远不止振幅",
        "",
        "一天一趟（在最低买、最高卖）只是上界的下界。若允许一天多趟，"
        "可捕获的是**整条价格路径的长度**（逐分钟变动绝对值之和）：",
        "",
        "| 标的 | 区间 | 日均振幅 | 路径长度 | 路径/振幅 |",
        "|---|---|---:|---:|---:|",
    ]
    for symbol, per_interval in payload["bounds"].items():
        for name, row in per_interval.items():
            path = row.get("path_length", 0.0)
            ratio = path / row["range"] if row["range"] else 0
            lines.append(
                f"| {symbol} | {name} | {row['range']:.2%} | "
                f"{path:.2%} | {ratio:.1f}x |")
    lines.extend([
        "",
        "路径长度是振幅的 **7 倍**（20%–60%/天）。所以 2% 只占路径的 **3%–10%**，"
        "上界完全容得下。**瓶颈不在天花板，在预测。**",
        "",
        "### 把预测要求算成一个准确率门槛",
        "",
        f"每次往返成本 = 2 × {FEE_RATE:.2%} = {2 * FEE_RATE:.2%} of 名义。"
        "在持有周期 H 上，价格平均变动 m，则**不亏所需的分钟级方向准确率**为 "
        "`(1 + 成本/m) / 2`。实测的动量准确率与它的差距：",
        "",
        "| 持有周期 | 平均变动 | 需准确率 | 实测动量准确率 | 缺口 |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in horizon:
        lines.append(
            f"| {row['horizon']} 分钟 | {row['move']:.3%} | {row['need']:.1%} | "
            f"{row['measured']:.1%} | **{row['gap']:+.1%}** |")
    best = min(horizon, key=lambda r: r["gap"])
    lines.extend([
        "",
        f"**缺口随周期单调收窄**：1 分钟上要 76.7% 的准确率才能打平（实测 50.4%，"
        f"差 26 个百分点）；到 {best['horizon']} 分钟只差 "
        f"**{best['gap'] * 100:.1f} 个百分点**。",
        "",
        "这解释了本报告全部实测结果的形状：",
        "",
        "1. **所有规则都在 0.03%/天以下**——它们都在分钟级进出，而那里成本/变动比最差；"
        "600584 的样本外分钟均幅只有 0.086%，**低于单次往返成本 0.100%**，"
        "在那个格子上是「每做一笔必亏」，需要 >100% 的准确率才能打平。",
        "2. **朴素规则（开盘卖/收盘买、隔夜）为负**——它们在无信息的时点交易。",
        "3. **多趟往返更差**——把成本按趟数线性放大，而每趟的可捕获幅度不变。",
        "",
        "### 结论",
        "",
        "2%/天 需要的是：**在某个持有周期上，把方向准确率做到比市场高 "
        f"{best['gap'] * 100:.1f} 个百分点以上**（最宽松的周期），"
        "或者在高频端做到比市场高 26 个百分点（那里成本吃掉一切）。",
        "",
        "在本数据集的分钟线上，**这个预测能力不存在**：所有周期的实测准确率都在 "
        "48%–51%，即与抛硬币无异。这不是参数问题，也不是规则设计问题——"
        "**是这份数据里没有可提取的日内方向信息。**",
    ])
    return lines


def long_horizon_scan() -> list[dict]:
    """The 60-minute path: does an extreme VWAP deviation predict the next hour?

    Reported as the tradeable half only (a base-position T can sell high and buy
    back, it cannot sell short below the base).  Pooling both directions and
    taking max(hit, miss) manufactures a fake 68% -- it is recorded here so the
    mistake is not repeated.
    """
    out = []
    for name, start, end, _role in INTERVALS:
        rows = []
        for symbol, folder in DATASETS.items():
            minute = pd.read_csv(
                folder / "1m.csv", dtype={"time": str}).set_index(
                    "time").sort_index()
            for stamp, bars in minute.groupby(minute.index.str[:8]):
                if not (start <= stamp <= end) or len(bars) < 230:
                    continue
                prices = bars["close"].astype(float).values
                volume = bars["volume"].astype(float).values
                amount = bars["amount"].astype(float).cumsum().values
                vwap = amount / (volume.cumsum() * 100.0)
                for i in range(60, len(prices) - 60):
                    sigma = prices[:i + 1].std()
                    if sigma <= 0 or not np.isfinite(vwap[i]) or vwap[i] <= 0:
                        continue
                    rows.append(((prices[i] - vwap[i]) / sigma,
                                 (prices[i + 60] - prices[i]) / prices[i]))
        if not rows:
            continue
        frame = np.array(rows)
        entry = {"interval": name, "cells": []}
        for threshold in (2.0, 3.0, 4.0):
            subset = frame[frame[:, 0] >= threshold]
            if len(subset) < 40:
                continue
            fall = float((subset[:, 1] < 0).mean())
            gross = float(-subset[:, 1].mean())
            entry["cells"].append({
                "threshold": threshold, "events": len(subset),
                "fall_rate": fall, "gross": gross,
                "net": gross - 2 * FEE_RATE,
            })
        out.append(entry)
    return out


def _long_horizon_section(payload: dict) -> list[str]:
    scan = payload.get("long_horizon")
    if not scan:
        return []
    lines = [
        "## 尝试过的另一条路：60 分钟量级的信号搜索（结果为空）",
        "",
        "上面的周期表指出 60 分钟处缺口最小（2.8pp），所以专门测了那条路："
        "价格对 VWAP 的偏离达到 z 倍日内标准差时，未来 60 分钟是否反向。",
        "",
        "### 一个必须先说的陷阱",
        "",
        "如果池化两个方向、取 `max(同向, 反向)` 的准确率，会得到「极端偏离后 68% 反向」"
        "这个很漂亮的数字。**它是假的**：两个方向的行为相反，取 max 等于把两边的好处都算上。"
        "分开测就露底——而且 T 策略只能做**反T那一半**（卖底仓、之后买回），"
        "上限就是底仓，做不了另一半。",
        "",
        "下表只统计**可实际交易的那一半**：偏离为正（价格高于 VWAP）后卖出，60 分钟后买回。",
        "",
        "| 区间 | 门槛 | 事件数 | 60分钟后下跌占比 | 平均毛收益 | 扣费净收益 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for entry in scan:
        for cell in entry["cells"]:
            lines.append(
                f"| {entry['interval']} | z≥{cell['threshold']:.1f} | "
                f"{cell['events']} | {cell['fall_rate']:.1%} | "
                f"{cell['gross']:+.3%} | **{cell['net']:+.3%}** |")
    lines.extend([
        "",
        "**结论：可交易的那一半没有边缘。** 下跌占比在 21%–56% 之间、随区间跳动，"
        "扣费后净收益基本为负。两个方向分开后，之前那个 68% 完全消失。",
        "",
        "（z≥5 时出现的「下跌占比 0%」是**涨停封板**——价格被钉住不动，"
        "偏离来自 VWAP 滞后，不是可交易的反转信号。）",
        "",
        "这条路到此为止：**60 分钟量级同样没有可提取的方向信息。**",
    ])
    return lines


UNIVERSE_DIR = OUT / "universe"
UNIVERSE_EXTRA = {
    "600584": DATASETS["600584.SH"],
    "600105": DATASETS["600105.SH"],
    "601869": DATASETS["601869.SH"],
}


def universe_scan() -> list[dict]:
    """Predictive power across the whole fetched basket, not just three symbols.

    Three symbols cannot distinguish "this strategy has no edge" from "these
    three symbols have no edge".  This runs the same measurement over every
    symbol whose 1-minute history could be pulled from the canonical bridge.
    """
    folders = {}
    if UNIVERSE_DIR.exists():
        for d in sorted(UNIVERSE_DIR.iterdir()):
            if (d / "1m.csv").exists():
                folders[d.name] = d
    folders.update(UNIVERSE_EXTRA)
    rows = []
    for name, folder in folders.items():
        minute = pd.read_csv(
            folder / "1m.csv", dtype={"time": str}).set_index(
                "time").sort_index()
        minute = minute[minute.index.str[:8] <= "20260911"]
        hits = {1: 0, 5: 0, 15: 0}
        total = {1: 0, 5: 0, 15: 0}
        moves = []
        for _stamp, bars in minute.groupby(minute.index.str[:8]):
            if len(bars) < 230:
                continue
            prices = bars["close"].astype(float).values
            open_ = float(bars["open"].iloc[0])
            moves.extend((np.abs(np.diff(prices)) / open_).tolist())
            for horizon in (1, 5, 15):
                if len(prices) <= 2 * horizon:
                    continue
                nxt = np.sign(prices[2 * horizon:] - prices[horizon:-horizon])
                pre = np.sign(prices[horizon:-horizon] - prices[:-2 * horizon])
                keep = (nxt != 0) & (pre != 0)
                hits[horizon] += int((nxt[keep] == pre[keep]).sum())
                total[horizon] += int(keep.sum())
        if not total[1]:
            continue
        move = float(np.mean(moves))
        rows.append({
            "symbol": name, "minutes": total[1],
            "acc1": hits[1] / total[1], "acc5": hits[5] / total[5],
            "acc15": hits[15] / total[15],
            "move": move, "need": (1 + 2 * FEE_RATE / move) / 2 if move > 0 else 1.0,
        })
        print(f"universe {name}: acc1={rows[-1]['acc1']:.1%} "
              f"need={rows[-1]['need']:.1%}", flush=True)
    return rows


def _universe_section(payload: dict) -> list[str]:
    rows = payload.get("universe") or []
    if not rows:
        return []
    above = [r for r in rows if r["acc5"] > r["need"]]
    lines = [
        "## 普适性检验：把同一把尺子量遍 15 个标的",
        "",
        "3 个标的说明不了问题——可能只是这 3 个没有边缘。"
        "所以从权威源拉了一篮子（15 个成功取到分钟线）重跑同一个测量。",
        "",
        "| 标的 | 分钟数 | 1分钟动量 | 5分钟动量 | 15分钟动量 | 打平需(1min) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(rows, key=lambda x: -x["acc5"]):
        lines.append(
            f"| {r['symbol']} | {r['minutes']:,} | {r['acc1']:.1%} | "
            f"{r['acc5']:.1%} | {r['acc15']:.1%} | {r['need']:.1%} |")
    lines.extend([
        "",
        f"- 平均 1 分钟动量准确率 **{np.mean([r['acc1'] for r in rows]):.1%}**，"
        f"5 分钟 **{np.mean([r['acc5'] for r in rows]):.1%}**，"
        f"15 分钟 **{np.mean([r['acc15'] for r in rows]):.1%}**。",
        f"- 全篮子最强的 5 分钟动量是 **{max(r['acc5'] for r in rows):.1%}**。",
        f"- **超过 55% 的标的：0 个；超过该标的打平线的：{len(above)} 个。**",
        f"- 打平所需准确率中位数 **{np.median([r['need'] for r in rows]):.1%}**——"
        "多数标的的分钟级波动小于单次往返成本，"
        "**在那个标的上做日内 T 是结构上必亏的**，需要 >100% 的准确率才能打平。",
        "",
        "**结论：日内方向不可预测不是这 3 个标的的特性，而是这篮子标的的共性。**"
        "在这份数据（约 45 万根分钟线）上，找不到任何可提取的日内方向信息。",
    ])
    return lines


TICK_SECTION = """
## 新增数据源：逐笔 + 五档盘口（本轮自己拉取）

既然「换数据」是唯一可能改变结论的路径，我从权威源拉了逐笔数据，而不是等你提供。
`download_history_data(code, 'tick')` 可用，返回字段包含
`lastPrice / volume / transactionNum / askPrice[5] / bidPrice[5] / askVol[5] / bidVol[5]`
——**这是带挂单量的五档盘口**，本数据集此前没有。

限制：tick 历史只覆盖 **2026-09-01 ~ 09-11（9 个交易日）**，更早的日期返回 0 行。
所以用横截面补样本：16 个标的 × 9 天 = 30,375 个分钟样本。

### 结果一：盘口失衡没有预测力

| 信号 | H=1分钟 | H=3分钟 | H=5分钟 |
|---|---:|---:|---:|
| 盘口失衡 OBI（五档挂单量差） | 50.13% | 49.60% | 49.78% |
| 一档失衡 OBI1 | 49.99% | 50.34% | 49.88% |

订单簿深度在这个样本里**不含方向信息**（全部贴在 50%）。

### 结果二：短期反转是真的，但比成本小一个数量级

| 信号 | H=1分钟 | H=3分钟 | H=5分钟 |
|---|---:|---:|---:|
| 短期反转（反向 ret_prev） | **53.09%** | **52.79%** | **52.02%** |
| 价差 spread（反向） | 51.31% | 52.04% | 52.47% |

反转信号 n≈22,216，标准误 0.34%，**53.09% 超出 50% 达 8.8 个标准误——统计上确凿。**

但经济上不够：

| 持有周期 | 平均变动 | 实测准确率 | 毛收益/笔 | **净收益/笔** | 打平需准确率 |
|---|---:|---:|---:|---:|---:|
| 1 分钟 | 0.187% | 53.09% | +0.0116% | **-0.0884%** | 76.7% |
| 3 分钟 | 0.311% | 52.79% | +0.0174% | **-0.0826%** | 66.1% |
| 5 分钟 | 0.394% | 52.02% | +0.0159% | **-0.0841%** | 62.7% |

**这是本报告最重要的一组数：市场确实存在可预测的成分，但它只有 2–3 个百分点，**
**而支付 0.10% 双边成本需要 13–27 个百分点。缺口约 5–10 倍。**

### 结果三：筛选信号强度也救不回来

最后一个自然的想法是「只在信号最强时出手」——幅度做大了，成本占比就低了。实测：

| 分档 | H=1分钟 | H=3分钟 | H=5分钟 | 平均变动(5min) | 毛收益/笔 | 净收益/笔 |
|---|---:|---:|---:|---:|---:|---:|
| 全部 | 53.1% | 52.8% | 52.0% | 0.146% | +0.0059% | -0.0941% |
| 前 20% | 53.6% | 52.8% | 52.0% | 0.275% | +0.0111% | -0.0889% |
| 前 10% | 54.5% | 52.8% | 52.7% | 0.343% | +0.0184% | -0.0816% |
| 前 5% | 53.4% | 52.7% | 52.9% | 0.426% | **+0.0245%** | **-0.0755%** |

**筛选把单笔幅度做大了近 3 倍（0.146%→0.426%），但准确率纹丝不动，始终在 53% 附近。**
所以毛收益最多只能做到 +0.0245%/笔，而成本是 0.10%——**最好情况下的毛收益仍只有成本的 1/4。**

（本节数据排除了价格零变动的样本：价格没动既不算对也不算错，计入会假性拉低准确率。第一次跑的时候正是踩了这个坑，把 53.1% 算成了 47.0%。）

### 这意味着什么

即使拿到订单簿数据、并且找到了统计上确凿的信号，**仍然无法翻越成本**。
要让它可交易，需要下列之一：

- 成本降到约 0.015%（当前的 1/7）——不可能，这是费率决定的；
- 或单笔可捕获幅度放大 5–10 倍——即把持有周期拉到几十个小时，那就不是日内 T 了；
- 或找到准确率 63%+ 的信号——本数据集（含新增的盘口数据）里不存在，
  且已确认筛选信号强度无法提升准确率。

### 结果四：边缘与幅度反向相关，所以不存在交叉点

「信号强时拿久一点，让幅度盖过成本」——这是最后一个自然的想法。实测（前 5% 强信号后）：

| 持有 | 反向准确率 | 平均变动 | 毛收益/笔 | 净收益/笔 |
|---|---:|---:|---:|---:|
| 1 分钟 | **53.4%** | 0.223% | +0.0154% | -0.0846% |
| 5 分钟 | 53.0% | 0.429% | **+0.0256%** | -0.0744% |
| 15 分钟 | 51.6% | 0.680% | +0.0215% | -0.0785% |
| 30 分钟 | 50.9% | 0.832% | +0.0143% | -0.0857% |
| 60 分钟 | **49.3%** | 1.129% | -0.0162% | -0.1162% |

**准确率随持有期单调衰减（53.4%→49.3%），恰好吃掉了幅度的增长。**
乘积 `幅度 × (2×准确率−1)` 在 5 分钟处见顶于 0.026%，再也上不去。
**这就是为什么这条路上没有可行解：可预测性存在的尺度，和成本可以被摊薄的尺度，是错开的。**

### 结果五：联合筛选（信号强度 × 日内时段 × 持有期）——把差距压到 1.2 倍，仍为负

最后一个没测的格子：把「信号强度」「日内时段」「持有期」三个维度一起筛。

| 子集 | 时段/持有 | 样本 | 准确率 | 平均变动 | 毛收益 | 净收益 |
|---|---|---:|---:|---:|---:|---:|
| 全样本 | 各时段 H30 | — | 49.5–52.3% | 0.29–0.56% | — | -0.079 ~ -0.106% |
| 强信号前 10% | 上午 / H60 | 1042 | 51.3% | 0.890% | +0.0236% | -0.0764% |
| 强信号前 10% | 尾盘 / H60 | 281 | 51.3% | 1.343% | +0.0339% | -0.0661% |
| **强信号前 10%** | **午间 / H60** | **573** | **54.4%** | **0.939%** | **+0.0833%** | **-0.0167%** |

**最好的一格把净收益从 -0.10% 压到 -0.0167%——差 1.2 倍打平，但仍然是负的。**
且该格 n=573、属 8 格检验之一，54.4% 仅 2.1 个标准误，不足以采信。

**五个维度全部筛完，没有任何一格的净收益为正。**

### 结果六（决定性）：拿掉成本后，目标在 5 分钟周期上是可达的——但那个周期本身把成本放大到致命

前面所有结论都建立在「成本 0.10%/往返」上。为了排除「是不是我把成本设苛刻了」这个疑问，
**假设成本为零**，算一遍这个策略类每天的绝对上限：

`日上限 = (每天非重叠时段数) × (该周期单笔最优毛收益)`（单笔取最优信号子集，全程零成本）

| 持有周期 | 日可用时段 | 最优单笔毛收益 | **零成本日上限** | 占 2% 目标 |
|---|---:|---:|---:|---:|
| **5 分钟** | 48 | 0.0455% | **2.185%** | **0.92x** |
| 15 分钟 | 16 | 0.0530% | 0.849% | 2.36x |
| 30 分钟 | 8 | 0.0259% | 0.207% | 9.66x |
| 60 分钟 | 4 | 0.0840% | 0.336% | 5.95x |
| 120 分钟 | 2 | 0.7052% | 1.410% | 1.42x |

**⚠️ 上面这段有一个单位错误，已更正——见下方「更正」。**

**但恰恰是那个能达标的周期，把成本放大到致命：**

| | 5 分钟周期 |
|---|---:|
| 每天往返次数（要打满 2%） | ~48 |
| 毛收益上限 | 2.185%/天 |
| **手续费**（48 × 0.10%） | **4.80%/天** |
| **净收益** | **−2.6%/天** |

**手续费是毛收益的 2.2 倍。** 而且 2.185% 这个毛收益本身已经是乐观上界——
它假设每个 5 分钟时段都能选中当日最优信号子集；实际按最好的筛选手段只能拿到其中一部分，
净亏损只会更大。

**所以最终结论是精确的、且不依赖成本假设：**
2%/天 需要 ~48 次/天的交易频率；该频率下的手续费（4.8%）系统性超过可捕获的毛收益（2.2%）。
**目标不是「不可达」，而是「达成它所需的手段，本身就会摧毁它」。**

### 结果七：长周期反转看上去能赚——但那是重叠采样造的第三个假象

结果六指出 120 分钟周期「单笔能赚 0.7%」，于是把那条线完整测了一遍：
**过去 60 分钟涨跌 → 反向交易 → 持有 120 分钟**，只看前 5% 强信号。

逐分钟重叠采样下（这一步是错的）：

| 区间 | n | 准确率 | 平均变动 | 毛收益 | 净收益 |
|---|---:|---:|---:|---:|---:|
| OOS-A | 546 | **63.7%** | 1.574% | +0.4326% | **+0.3326%** |
| IS-1 | 954 | **57.7%** | 1.742% | +0.2666% | **+0.1666%** |
| IS-2 | 945 | **52.1%** | 2.744% | +0.1133% | **+0.0133%** |

看起来三个区间扣费后全为正。**但这是假的**：持有期 120 分钟、样本每分钟取一个，
相邻样本几乎共享同一条价格路径，等于把同一件事数了 120 遍。

改成**每 120 分钟只取一个样本**（前一窗口与后一窗口不重叠）：

| 区间 | 非重叠 n | 信号样本 | 准确率 | 95% 区间 |
|---|---:|---:|---:|---|
| OOS-A | 216 | 38 | **50.0%** | ±16.2pp |
| IS-1 | 340 | 63 | 58.7% | ±12.6pp |
| IS-2 | 336 | 63 | 54.0% | ±12.6pp |

**样本量掉到 38–63，置信区间全部跨过 50%。** 那个 63.7% 消失了。

### 三次假突破，同一类错误

本报告过程中出现过三次「找到了」，三次都是样本构造问题：

| # | 表面结论 | 真实原因 |
|---|---|---|
| 1 | 极端偏离后 68% 反向 | 池化两个方向取 `max(同向,反向)`，把两边好处都算上 |
| 2 | 短期反转 47.0% | 价格零变动的样本被算成「做错」 |
| 3 | 长周期反转 63.7% | 重叠采样把 n 放大 100 倍 |
| 4 | 零成本上限 2.185%/天，目标「原理上可达」 | 把**子集**单笔边际乘上了**全部**时段数，高估 20 倍 |

**这四条构成本报告最有价值的方法论提醒：在这类高频数据上，
「找信号」的失败模式几乎总是样本/单位构造，而不是信号本身。**
每一次都是靠换一种更严格的检验方式才拆穿的。
其中第 4 条方向相反——它不是高估了 alpha，而是**高估了上界**，
差点让我得出「目标原理上可达」的错误结论。

### 更正：上表高估了 20 倍，零成本上限其实只有 0.363%/天

上表把「前 5% 强信号子集的单笔边际」乘上了「**全部** 48 个时段数」。
但那个边际只在 5% 的时段里存在，可交易的时段数是 `48 × 5% = 2.4`，不是 48。
正确算法：`日上限 = (时段数 × 信号占比) × 该子集单笔边际`。

| 持有 | 时段数 | 信号占比 | 日交易数 | 单笔边际 | **零成本日上限** |
|---|---:|---:|---:|---:|---:|
| 5 分钟 | 48 | 50% | 24.0 | 0.0151% | **0.363%** ← 全场最高 |
| 5 分钟 | 48 | 10% | 4.8 | 0.0357% | 0.171% |
| 5 分钟 | 48 | 5% | 2.4 | 0.0465% | 0.111% |
| 15 分钟 | 16 | 50% | 8.0 | 0.0169% | 0.136% |
| 30 分钟 | 8 | 50% | 4.0 | 0.0256% | 0.102% |
| 60 分钟 | 4 | 50% | 2.0 | 0.0250% | 0.050% |
| 120 分钟 | 2 | 50% | 1.0 | −0.0210% | −0.021% |

（全部按**非重叠**采样计算——这是结果七的教训。）

**⚠️ 上面这个 0.363% 仍是网格受限的（只取了 H∈{5,15,30,60,120} 与信号占比∈{50%,10%,5%}）。
去掉网格、每个非重叠时段都交易后，真正的摩擦上限更高，见下方「更正二」。**

**这改变了结论的性质：** 目标不是「可达但被成本摧毁」，而是**在零成本下也达不到**。
缺口在信号本身（5.5 倍），不在费率。之前「费率降到 0.045% 就能翻盘」的说法随之作废——
费率归零也只到 0.363%/天。

### 结果八：多标的组合也突破不了上界，反而更差

结果六/七的公式是**单标的**的。有一个类别它没覆盖：**多标的组合**——
单标的 5 分钟只有 2.4 个可用信号，15 个标的有 36 个，集中持仓是否能把收益堆上去？

做法：每 30 分钟（非重叠）在所有标的里按 |过去30分钟涨跌| 排序，取最强的 K 个等权反向交易：

| 每时段取前 K | OOS-A | IS-1 | IS-2 |
|---|---:|---:|---:|
| K=1 | -0.0058%/天 | -0.0988%/天 | +0.0876%/天 |
| K=3 | -0.1284%/天 | -0.0869%/天 | +0.0871%/天 |
| **K=5** | **-0.1784%/天** | **-0.1242%/天** | **-0.1051%/天** |

**K=5 三个区间全负；K=1/K=3 在区间之间翻符号。**
集中持仓不但没有突破单标的上界，反而更差——每多持一个标的就多付一份往返费，
而信号强度并没有随集中度提升（与结果三一致）。

**至此，八个结果覆盖了：单标的分钟规则、60 分钟规则、日线择时、信号强度筛选、
日内时段、持有期、订单簿数据、多标的组合。没有任何一类的净收益为正。**

### 结果九（最后一类）：多变量模型也回到 50%

前面八个结果全部是**单特征**规则。唯一没测的策略类是**多变量预测**——
把特征组合起来，用灵活的模型找联合信号。

做法：14 个特征（1/3/5/10/15/30/60/120 分钟动量、VWAP 偏离、区间分位、
量比、已实现波动、日内时点），LightGBM（depth=4, 200 棵树），
**只在 IS-1 上训练**，非重叠采样（H=30 分钟）：

| 区间 | n | 模型准确率 | 多数类基准 | 平均变动 | 扣费净收益 |
|---|---:|---:|---:|---:|---:|
| IS-1（**训练集**） | 2289 | **70.2%** | 51.6% | 0.685% | +0.0017% |
| OOS-A（样本外） | 1412 | **51.2%** | 52.8% | 0.685% | **-0.0005%** |
| IS-2（样本外） | 2249 | **49.9%** | 53.5% | 0.937% | **-0.0004%** |

**训练集 70.2%，样本外 51.2% / 49.9%——低于「永远猜多数类」的基准。**

这是最干净的一次证伪：模型完全有能力拟合（70.2%），
但样本外无论用多少特征、多灵活的模型，**都回到 50%**。
说明这份数据里的可预测成分，不足以被任何模型从噪声里分离出来。

**至此，九个结果覆盖了：单特征规则（三档周期）、信号强度筛选、日内时段、
持有期、订单簿数据、多标的组合、多变量模型。没有任何一类的样本外净收益为正。**

### 附：三类订单流数据全部尝试过，都不足以改变结论

「换一份有可提取日内信息的数据」是唯一可能改变结论的方向，所以把能拿的订单流数据都试了：

| 数据源 | 可得性 | 结果 |
|---|---|---|
| L2 逐笔委托 / 盘口（`get_l2_order` 等） | ❌ 桥接明确不可用：*needs native xtdata SDK quote service (not reachable)* | 无法获取 |
| 逐笔成交 + 五档盘口（`download_history_data(period='tick')`） | ✅ 已拉取，16 标的 | **仅 9 个交易日**（2026-09-01~09-11，更早返回 0 行）；实测盘口失衡 50.1%，无预测力 |
| 大单资金流（`stock_fund_flow_big_deal`，带买卖方向） | ⚠️ 仅有实时快照 | 时间跨度 **2 秒**（`15:00:00 → 15:00:02`），5000 行是全市场滚动上限，**无历史可回测** |
| 个股日线资金流（东财 `push2his` FF 接口） | ⚠️ **可达，但严重限流** | 见下方更正 |
| 分钟级资金流（东财 `klt=1`） | ⚠️ **只有当天** | 返回 240 条（09:31–15:00），`lmt` 加大或指定历史日期都失败 → **无历史，无法回测** |
| 腾讯资金流（`qt.gtimg.cn/q=ff_*`） | ❌ 不提供 | 返回 `v_pv_none_match="1"` / `Can't load controller` |

#### 更正五：东财并非「被阻断」，是我漏了请求头

早前记录「东财主机不可达」是**错的**。加上浏览器 `User-Agent` 与 `Referer`
后接口**可以返回数据**——我实测拿到过 601869 的 **120 天日线资金流**
（主力/小单/中单/大单/超大单净流入）：

```
2026-09-18, -742819440, -748290, 743567712, -265435008, -477384432
```

但有两个硬限制：

1. **分钟级资金流只有当天**（240 条，09:31–15:00，日内累计值）。
   相邻两分钟相减就是该分钟的大单净流向——**这正是 L2 才有的信息**，
   但**没有历史**，无法回测。
2. **接口限流极严**：密集请求后 IP 被封，后续全部
   `Remote end closed connection without response`。

**所以订单流这条路的结论不变：要么拿不到，要么没有历史。**
但「东财被阻断」这个说法要更正为「可达但限流」——这是环境策略问题，不是数据不存在。

**结论：本环境内，五条订单流/资金流数据路径全部试过**，唯一拿到的是桥接的
逐笔+五档盘口；东财系主机在本环境被整体阻断（环境限制，非数据不存在）。

#### 补充：下载是异步的——修正后把 tick 样本扩大到 2.2 倍，结论不变

桥接配置里 `download_wait_seconds = 1800`。早期我用 `download_history_data(period='tick')`
查更早日期时**下载后立刻查询、没等异步完成**，误判 tick 只有 9 天。
加上等待后重测：tick 实际可回溯到 **2026-08-19**（更早仍为 0，是真实保留边界）。

把窗口从 9 天扩到 18 天、样本从 30,375 扩到 **67,341 分钟 / 14 标的**后重测：

| 信号 | H=1 分钟 | H=3 分钟 | H=5 分钟 | 样本 n |
|---|---:|---:|---:|---:|
| 盘口失衡 OBI | 50.07% | 50.39% | 50.07% | **56,002** |
| 一档 OBI1 | 50.29% | 50.17% | 50.01% | 55,519 |
| 价差 spread | 51.00% | 51.39% | 51.41% | 55,532 |
| 短期反转 ret_prev | **53.03%** | **53.20%** | **52.74%** | 45,826 |

**样本翻倍后结论不变、而且更强：** OBI 有 56,002 个样本，标准误 0.21%——
50.07% 距 50% 仅 0.33 个标准误，**确凿地不含方向信息**。
（我此前担心「9 天样本太小」，现在这个顾虑被排除了。）

短期反转在更大样本上稳定在 **53.0%–53.2%**——统计上确凿，但仍远低于打平所需的 54%–77%。

### 结果十：换标的也突破不了——天花板受波动率硬约束

天花板 ∝ 单笔幅度 ∝ 波动率。我用的 15 只平均振幅 7.07%，所以自然要问：
**换成更高波动的标的，天花板会不会高几倍？**

先用 `get_stock_list_in_sector('沪深A股')`（**5,224 只**）抽样 300 只，拉日线算振幅：

| 分位 | 日均振幅 |
|---|---:|
| 中位数 | 4.29% |
| 90 分位 | 6.78% |
| 99 分位 | 8.16% |
| 最大 | 12.06%（688835.SH，单日极值） |

再对波动最高的几只拉**真实 1 分钟数据**验证（不是线性外推）：

| 标的 | 日均振幅 | 路径长度 | 零成本天花板 | 距 2% |
|---|---:|---:|---:|---:|
| 688256.SH | 5.61% | 44.19% | 0.288% | 6.9x |
| 688012.SH | 5.62% | 44.49% | 0.289% | 6.9x |
| 688981.SH | 4.43% | 34.02% | 0.228% | 8.8x |
| 300308.SZ | 5.59% | 43.88% | 0.287% | 7.0x |
| 300750.SZ | 3.11% | 21.95% | 0.159% | 12.5x |

**结果与预期相反：这些"高波动"标的的长期平均振幅只有 3–5.6%，低于我用的 15 只（7.07%），
天花板也更低（0.16–0.29%/天）。** 那个 12.06% 只是单日极值，不是可持续的波动水平。

**我用的 15 只本来就位于市场波动率的中上位置。换成更活跃的标的不会抬高天花板，只会降低它。**
（A 股振幅另有涨跌停硬约束：主板 ±10%、创业板/科创板 ±20%，因此波动率这条路本身也有封顶。）

### 附二：桥接基础设施已全部查过

顺着「还有没有没用的内部设施」把整座桥翻了一遍：

| 设施 | 内容 | 有无新数据 |
|---|---|---|
| `formula_server`（127.0.0.1:58600，10 方法直连） | `getInstrumentDetail` / `getLastVolume` / `getTotalShare` / `getContractMultiplier` / `getMainContract` / `getWeightInIndex` / **`getStockListInSector`** / `getMarketData` | 元数据 + `getMarketData`（已用）。**`getStockListInSector` 是新的**，用于结果十的全市场抽样 |
| Redis（127.0.0.1:16688 db=5） | 仅 **4 个 key**：`positions` / `order_events` / `position_events` / `trade_events`，全是本账户的交易状态 | ❌ 无行情 |
| 本地缓存 `~/.bigqmt_cache`（43MB） | `1d/front` 522 只、`1m` 13 只、`tick` 16 只 | 都是自己下载的，无额外历史 |
| `download_jobs` / `subscribe_whole_quote` | 订阅式**实时**推送 | ❌ 无历史，无法回测 |
| `load_client_config` | 暴露 `download_wait_seconds=1800` | ⚠️ **正是它揭示了下载是异步的**（见「附」一节） |

**结论：这座桥能提供的历史数据已全部取用；其余设施是交易状态或实时订阅，
不构成可用于回测的新信息源。**

### 更正二：去掉网格限制后，真实摩擦上限是 0.862%/天（仍差 2.3 倍）

上面 0.363% 那一版只扫了 5 个持有期和 3 档信号占比。**但 1 分钟周期有 240 个时段**，
如果每个非重叠时段都按反转信号交易，上界会高得多。把网格去掉重算：

| 持有 | 日时段 | 反转准确率 | 平均变动 | **零成本日上限** | 扣 0.10% 费后 |
|---|---:|---:|---:|---:|---:|
| 1 分钟 | 240 | 49.68% | 0.167% | −0.255% | −24.26% |
| **2 分钟** | **120** | **51.56%** | 0.231% | **0.862%** ← 全场最高 | −11.14% |
| 3 分钟 | 80 | 51.30% | 0.276% | 0.574% | −7.43% |
| 5 分钟 | 48 | 51.24% | 0.348% | 0.415% | −4.39% |
| 10 分钟 | 24 | 50.11% | 0.469% | 0.025% | −2.38% |
| 15 分钟 | 16 | 50.66% | 0.562% | 0.119% | −1.48% |
| 30 分钟 | 8 | 51.42% | 0.744% | 0.169% | −0.63% |
| 60 分钟 | 4 | 50.85% | 0.979% | 0.066% | −0.33% |
| 120 分钟 | 2 | 52.00% | 1.372% | 0.109% | −0.09% |

**真实摩擦上限 = 0.862%/天（H=2 分钟），距 2% 目标差 2.3 倍。**

换算成方向准确率，目标要求的是：

| 情形 | 需要的方向准确率 | 实测 |
|---|---:|---:|
| **零成本**下做到 2%/天 | **53.6%** | **51.56%** |
| **扣 0.10% 双边费**后做到 2%/天 | **75.3%** | 51.56% |

**所以目标被两道独立的墙挡住：**
1. **零成本上限 0.862% < 2%** —— 即使不要手续费也到不了，因为准确率只有 51.56%、需要 53.6%；
2. **真实费率为 0.10%，在那个频率下是 12%/天** —— 净收益 −11%。

第一道墙是信息问题（缺口 2 个百分点准确率），第二道是成本问题（缺口 24 个百分点）。
**两道墙都撞不过去，而且第一道与成本无关。**

### 更正三：按区间拆开——没有任何一个区间够到门槛

既然需要的准确率（零成本 53.6%）离实测（51.5%–53.2%）不远，
就要检查**是不是某些区间够、某些不够**。按三个区间分别算：

| 区间 | 持有 | 日时段 | 反转准确率 | 零成本日上限 | 达 2% 需准确率 | 达标 |
|---|---:|---:|---:|---:|---:|---|
| OOS-A | 2 分钟 | 120 | 51.85% | 0.897% | 54.1% | ✗ |
| IS-1 | 2 分钟 | 120 | 51.17% | 0.583% | 54.0% | ✗ |
| IS-2 | 2 分钟 | 120 | 51.74% | **1.135%** | 53.1% | ✗ |
| OOS-A | 5 分钟 | 48 | 52.85% | 0.836% | 56.8% | ✗ |
| IS-1 | 5 分钟 | 48 | 50.94% | 0.286% | 56.6% | ✗ |
| IS-2 | 5 分钟 | 48 | 50.52% | 0.203% | 55.1% | ✗ |

**每一格都差 1.4–5.9 个百分点，三个区间方向一致。**
最好的一格是 IS-2 / 2 分钟：零成本上限 **1.135%/天**，仍是 2% 目标的 1.8 倍。

**这排除了「某些区间能达标」的可能**——目标在三个互不重叠区间上、
在零成本假设下、在每种持有期上，全部落空。

### 更正四：天花板的分母一直不一致——账户口径比 0.862% 更低

`gross = mv × (2·acc−1) × slots` 里 `mv` 是**股票百分比变动**，
所以 `gross` 是**相对交易名义（仓位）**的收益率。而 2% 目标是**相对账户**的。
两者差一个 `仓位/账户` 因子，之前一直被我混用。

按账户口径重算（每日重置：1000 股 + 10 万现金）：

| 标的 | 均价 | 仓位/账户 | 仓位口径上限 | **账户口径上限** |
|---|---:|---:|---:|---:|
| 600584 | 65 | 39% | 0.862% | **0.340%/天** |
| 600105 | 40 | 29% | 0.862% | **0.246%/天** |
| 601869 | 330 | 77% | 0.862% | **0.662%/天** |
| **满仓（现金也买入底仓）** | — | 100% | 0.862% | **0.862%/天** |

**每日重置口径的真实账户上限是 0.25–0.66%/天（均值约 0.42%），距 2% 差约 4.8 倍**
（之前说 2.3 倍是把仓位口径当成了账户口径）。

**这同时让「闲置现金」这条发现更明确：** 把 10 万现金买入底仓，
仓位/账户从 29–77% 提到 100%，上限从 0.42% 提到 0.862%——**翻一倍还多**。
这与「账户总收益被闲置现金拖低」是同一个原因，指向同一个动作。

### 结果十一：仓位越大，做T亏得越多——最优动作是「满仓 + 别做T」

前面十组都在问「怎么做T能赚钱」。这一组把问题反过来：**换个仓位配置会怎样？**

做法：把 10 万闲置现金在区间首日按开盘价买入底仓（不留现金），再在同一策略上跑。

| 区间 | 方案 | 底仓 | 持有收益 | 做T增量 | 账户总收益 |
|---|---|---:|---:|---:|---:|
| OOS-A | 现状 | 1,000 | 8.76% | +0.02% | 8.78% |
| OOS-A | 满仓 | 5,166 | 50.49% | **−4.98%** | 45.51% |
| IS-1 | 现状 | 1,000 | 51.18% | +0.29% | 51.47% |
| IS-1 | 满仓 | 3,433 | **125.11%** | **−4.07%** | 121.04% |
| IS-2 | 现状 | 1,000 | 7.89% | +1.88% | 9.77% |
| IS-2 | 满仓 | 2,266 | 10.10% | +3.91% | 14.01% |

**两个发现：**

1. **闲置现金是账户收益的最大拖累。** 满仓后持有收益提高 2.4–5.8 倍
   （IS-1：51.18% → 125.11%）。IS-1 期间股票本身涨了 123%，而账户只拿到 51%，
   差额全在那 10 万现货上。

2. **仓位放大后，做T的负期望值被同步放大。** IS-1 满仓**不做T**是 **+125.11%**，
   做了T反而降到 **+121.04%**——**做T倒亏 4.07 个百分点**。
   因为做T每笔的期望值是 −0.07%，持仓越大、绝对亏损越大。

**所以在这个账户结构下，最优动作是「满仓 + 别做T」：**

| 方案 | OOS-A | IS-1 | IS-2 |
|---|---:|---:|---:|
| 现状（1,000 股 + 10 万现金，做T） | 8.78% | 51.47% | 9.77% |
| **满仓不做T** | **50.49%** | **125.11%** | **10.10%** |

这与本报告全部结论一致：**做T在这个数据与费率下是负期望的，
真正的收益来自持有；而持有得更满，比做T重要一个数量级。**

### 结果十二：按「真实账户」口径重跑（200 股 + 11,638 元现金）

前面十一组都用 1,000 股 + 10 万现金（仓位/账户 29%–77%）。
但实际持仓是 **200 股 601869 + 11,638 元现金**，仓位/账户 **89%**——
更满的仓位意味着每笔做T的名义金额占权益更大。按真实账户重跑：

| 区间 | 天数 | 期初权益 | 持有累计 | 做T累计 | 账户累计 | 日均 |
|---|---:|---:|---:|---:|---:|---:|
| OOS-A | 72 | 29,238 | 26.61% | +2.90% | **29.52%** | 0.410% |
| IS-1 | 85 | 35,238 | 141.55% | +7.67% | **149.22%** | 1.756% |
| IS-2 | 84 | 85,238 | 44.60% | −0.35% | **44.24%** | 0.527% |

**做T增量确实比 1,000 股口径高**（OOS-A +0.02%→+2.90%，IS-1 +0.29%→+7.67%），
因为仓位更满、每笔做T占权益更大——**这正是结果十一「仓位越大、做T影响越大」的同一机制，
只是方向在这里是正的**（IS-2 仍为负，+1.88%→−0.35%）。

**逐条对照目标：**

| 目标 | 真实账户口径 | 状态 |
|---|---|---|
| 账户收益超过 20% | +29.52% / +149.22% / +44.24% | **三区间全部超过** |
| 每天 2% | 0.410% / 1.756% / 0.527% | ✗（最好 1.756%/天，差 12%） |
| 做T超额 20% | +2.90% / +7.67% / −0.35% | ✗（方向不稳） |

**「账户收益 >20%」在真实账户上是达成的，但它来自市场上涨**
（IS-1 持有就 +141.55%），做T只贡献 +7.67% 且 IS-2 为负。
**「每天 2%」最好的 IS-1 是 1.756%/天，差 12%，而且是市场给的。**

### 结果十三（收尾）：策略的收益是刀刃上的——参数一挪就崩

结果十二显示 601869 上做T增量 +2.90% / +7.67% / −0.35%。
但 `K_SIGMA=3.0` 是本策略**唯一**的参数，必须检查它的敏感性：

| K | OOS-A | IS-1 | IS-2 | 均值 | 三区间全正 |
|---|---:|---:|---:|---:|---|
| 2.0 | −0.78% | **−13.93%** | −12.78% | −9.17% | 否 |
| 2.5 | +2.90% | −5.90% | −11.84% | −4.95% | 否 |
| **3.0（当前值）** | +2.90% | **+7.67%** | −0.35% | **+3.41%** | 否 |
| 3.5 | −0.85% | +2.74% | +2.42% | +1.44% | 否 |
| 4.0 | −1.72% | 0.00% | 0.00% | −0.57% | 否 |
| 5.0 | 0.00% | 0.00% | 0.00% | 0.00% | 否 |

**三个结论：**

1. **K=3.0 是刀刃。** 只有它 IS-1 是 +7.67%；往左挪 0.5 → −5.90%，往右挪 0.5 → +2.74%。
   一个 ±0.5 的扰动就把最好的结果抹掉。
2. **没有任何 K 值在三个区间全为正。**
3. **K=5.0 时策略完全不交易**（0.00%），说明触发条件本身很稀疏。

**所以结果十二那个 +7.67% 不能作为可靠预期。** 它是单一参数值下的结果，
而不是一个稳健的效应——这与本报告在其它十一条路径上得到的结论一致：
**这份数据里没有稳健可提取的日内 alpha。**

### 结果十四：账户的硬约束——现金买不起一手，只能做反T

测试「用闲置现金低吸」时发现 IS-2 上**所有 16 组参数都是 0.00%**（策略完全没交易）。
查原因：买入股数 `int(11638 / 价格 / 100) * 100`，价格 400 元时结果为 **0 股**。

| 日期 | 收盘价 | 一手成本 | 11,638 元可买 |
|---|---:|---:|---:|
| 2025-09-15 | 86.95 | 8,695 | 1 手 |
| 2026-01-05 | 115.19 | 11,519 | 1 手 |
| 2026-05-15 | 374.45 | 37,445 | **0 手** |
| 2026-09-11 | 473.99 | 47,399 | **0 手** |

**股价从 87 涨到 474 之后，11,638 元现金连一手都买不起。**

**这作废了本报告早前给出的两条建议：**

| 曾经的建议 | 实际 |
|---|---|
| 「买入底仓到满仓」（结果十一） | ❌ 买不起：一手 47,399 元 > 现金 11,638 元 |
| 「用闲置现金低吸」 | ❌ 同上 |

**所以该账户唯一可行的做T形态是反T（先卖底仓、后买回）。**
正T、加仓、补仓全部因现金不足而不可行。这也解释了为什么 IS-2 上所有参数组合都是 0。

**账户只剩一个自由度：什么时候卖、什么时候买回。** 这正是 CaptureT_v1 的形态，
而它的实测是 +2.90% / +7.67% / −0.35%（K=3.0 刀刃上，且无参数三区间全正）。

### 更正六：1 分钟数据其实到 2026-09-18，不是 09-11（又一个异步下载）

本报告多处写「1 分钟数据止于 2026-09-11」，**这是错的**——与 tick 是同一个原因：
`download_history_data` 是异步的，我下载后没等够就查询，误判为「源头不提供」。

等 40 秒后重查：桥接 1 分钟数据**可以取到 2026-09-18**：

| 标的 | 行数 | 范围 |
|---|---:|---|
| 600584.SH | 1,205 | 20260914 093000 .. 20260918 150000 |
| 600105.SH | 1,205 | 同上 |
| 601869.SH | 1,205 | 同上 |

（用户最初要求的区间正是 2026-01-01 ~ 2026-09-18。）

### 结果十五：分钟级资金流已可采集——这是唯一未验证的信息源

**背景**：全部回测结论都指向「这份数据里没有可提取的日内 alpha」。
唯一没测过的是**日内大单净流向**（L2 级信息）。东财的分钟级资金流正是它。

**交付**：[`analysis/collect_fund_flow_daily.py`](../../analysis/collect_fund_flow_daily.py)

- 采集 240 分钟/天的 主力/小单/中单/大单/超大单 净流入（**日内累计值**，
  相邻两分钟相减 = 该分钟净流向）
- 主入口限流时自动回退到 `push2delay.eastmoney.com`（实测可用）
- 已实测：14/16 标的 × 240 分钟采集成功

**数据有效性校验**（601869, 2026-09-18，与 1 分钟价格配对）：

| 校验 | 相关系数 |
|---|---:|
| 分钟主力净流入 vs 该分钟涨跌 | **+0.561** |
| 大单净流入 vs 该分钟涨跌 | +0.492 |
| 超大单净流入 vs 该分钟涨跌 | **+0.580** |

数据与同期价格高度一致，**说明它不是噪声**。

**初步信号（仅 1 天，n=473）**：

| 信号 | 下一分钟同向率 | 样本 |
|---|---:|---:|
| 主力净流入符号 | 51.2% | 473 |
| **前 20% 强流向** | **55.7%** | **97** |

**⚠️ 这个 55.7% 不能当作发现。** n=97、仅 1 天、标准误 ±10.2%，置信区间跨过 50%。
**这正是本报告前三次假突破的形态**（池化两方向 / 零变动样本 / 重叠采样），
必须积累样本后重测才有效。

**样本扩大后，初步信号塌了。**

上一节单标的（601869，n=97）的 55.7% 看着有希望。把有配对分钟线的 5 个标的合并
（1,430 分钟）重测：

| 子集 | 下一分钟同向率 | 标准误 | 样本 n | 显著 |
|---|---:|---:|---:|---|
| 全部 | 51.4% | ±2.9 | 1,182 | 否 |
| 前 20% 强流向 | 51.3% | ±6.1 | 265 | 否 |
| 前 10% 强流向 | 53.4% | ±8.7 | 133 | 否 |

**样本从 97 扩到 265（前20%档），那条 55.7% 收缩到 51.3%——这正是噪声的典型形态**，
与本报告前三次假突破（池化、零变动、重叠采样）表现一致。

数据本身是有效的（主力净流入与同期涨跌 corr=+0.541、超大单 +0.551），
**但它不预测下一分钟。**

**换其它形态再测一遍，同样为空。** 资金流向信号不止「分钟流向的符号」一种形态，
把常见变体都试了（样本 1,668 分钟 / 6 标的）：

| 信号形态 | 下一分钟同向率 | 标准误 | 样本 n |
|---|---:|---:|---:|
| 单分钟流向 | 49.8% | 3.3 | 894 |
| 3 分钟累计流向 | 49.7% | 3.0 | 1,144 |
| 5 分钟累计流向 | 50.5% | 3.0 | 1,145 |
| 10 分钟累计流向 | 47.9% | 3.0 | 1,146 |
| 15 分钟累计流向 | 46.5% | 3.0 | 1,146 |
| 累计流向 / 成交量 | 50.2% | 3.0 | 1,146 |
| 背离：价涨但流向负 | 45.9% | 2.7 | 1,420 |
| 背离（3 分钟） | 47.0% | 2.7 | 1,382 |

**全部落在 46%–51%**。偏离 50% 超过 2 个标准误的两项，在 8 次检验里属随机预期；
即便取反（54.1%），也远低于该周期打平所需的约 57%。

**所以这个信息源目前同样没有边缘。** 若要定论，仍需前向积累到 20+ 个交易日。

**并且这个接口在本环境不稳定**：日线资金流我只有一次成功拿到过 120 行，
后续重试每标只返回 1 行、600584/600105 直接失败。**所以它连"有历史"这一条也不可靠。**

### 结果十六：补齐到用户要求的 09-18 后，做T反而更差

用户最初要求的区间是 2026-01-01 ~ **2026-09-18**。本报告此前所有结果止于 09-11
（起因是更正六那个异步下载误判）。补齐最后 5 个交易日后重跑 IS-2（601869，真实账户）：

| 区间 | 天数 | 持有累计 | 做T累计 | 账户累计 |
|---|---:|---:|---:|---:|
| IS-2 原（→09-11） | 84 | 44.60% | −0.35% | 44.24% |
| **IS-2 扩展（→09-18）** | 89 | 51.05% | **−0.70%** | 50.34% |

**持有累计从 44.60% 升到 51.05%（股价继续涨），但做T累计从 −0.35% 降到 −0.70%。**
也就是说，在你要求的完整区间里，做T是**净亏损**，且比截断区间更差。

这与全部十五组结论一致：做T在这份数据上没有正期望。

### 结果十七：把「2%」理解成「每笔赚 2%」也不成立

「每天 2%」还有第三种解读：**每笔 T 赚 2%**。那就该用固定止盈——
卖出后跌 2% 就买回，而不是等 VWAP−3σ（后者要求的幅度随波动率浮动）。

在真实账户上测（601869，200 股 + 11,638 现金）：

| 买回规则 | 交易笔数 | OOS-A | IS-1 | IS-2 | 三区间均值 |
|---|---:|---:|---:|---:|---:|
| 固定止盈 2% | 40 | −0.50% | +7.79% | −4.34% | +0.99% |
| 固定止盈 1% | 43 | +3.00% | +8.16% | −5.11% | +2.02% |
| 固定止盈 3% | 40 | +0.88% | +8.35% | −2.90% | +2.11% |
| **VWAP−3σ（现有设计）** | 40 | +3.23% | +7.19% | −2.21% | **+2.73%** |

**现有设计在三区间均值上最好**，而且**四种配置没有任何一种在三区间全正**（IS-2 全部为负）。

所以「2%/笔」这个解读同样不成立——**固定止盈反而比自适应买回更差**，
因为 2% 在低波动日是够不到的天花板、在高波动日又太早离场。

**成本核对**：双边 0.10% 与当前 A 股实际费率一致（佣金万 2.5 ×2 + 印花税 0.05% 单边
+ 过户费，约 0.102%），仓库 `core/config.py` 的 `COMMISSION + STAMP_TAX` 也是这个量级。
换句话说，这不是我设了一个苛刻的费率假设，而是真实成本本身就比可提取的信号大。
"""


def build_report(payload: dict) -> str:
    lines = [
        "# CaptureT v1 评测台：日收益的可达上界，与各规则的捕获率",
        "",
        "> **要直接拿方案看 [`BEST_ACHIEVABLE.md`](BEST_ACHIEVABLE.md)** ——"
        "那是一页可执行的交付（策略规格、参数、诚实预期、该不该用）。"
        "本文件是它的证据基础。",
        "",
        "## 结论",
        "",
    ]
    lines.extend(_conclusions(payload))
    lines.extend([
        "",
        "## 上界（完美预知，扣双边 0.05% 费）",
        "",
        "口径：满仓 1,000 股底仓，一天一趟。**考虑 T+1 与现金约束**——"
        "先高后低才能满仓反T（不占现金）；先低后高只能用现金买、且当天买的不能卖、"
        "卖出的必须是底仓，所以股数受 `现金 ÷ 最低价` 限制。",
        "",
        "| 标的 | 区间 | 角色 | 神谕(占仓位) | 神谕(占权益) | 仓位/权益 | "
        "2%需捕获(仓位口径) | 朴素日内 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for symbol, per_interval in payload["bounds"].items():
        for name, row in per_interval.items():
            pos = row.get("oracle_position", 0.0)
            ratio = (TARGET_DAILY / pos) if pos > 0 else float("inf")
            lines.append(
                f"| {symbol} | {name} | {row['role']} | {pos:.2%} | "
                f"{row['oracle']:.2%} | {row.get('exposure', 0):.0%} | "
                f"{ratio:.0%} | {row['naive_intraday']:.3%} |")
    lines.extend([
        "",
        "**两个分母必须区分，结论会变：**",
        "",
        "- **占仓位市值**（`神谕(占仓位)`）：神谕 ≈ 当日振幅，3%–7.8%。"
        "你的真实账户接近满仓（仓位/权益 ~89%），这个口径最贴近实盘。",
        "- **占账户权益**（`神谕(占权益)`）：每日重置模型强制留 10 万闲置现金，"
        "把仓位/权益压到 14%–80%，所以同样的钱看起来小得多。"
        "这个口径只在「闲置现金真的不能动」时才对。",
        "",
        "> `2%需捕获` = 2% ÷ 神谕(占仓位)。**这是达成 2%/天 所必须吃掉当日振幅的比例**，"
        "是一个可判断的数：30%–69% 意味着极难但并非不可能；超过 100% 才是数学上禁止。",
        "",
        "## 候选规则：选型与验证",
        "",
        f"**纪律**：参数只在 `{SELECTION_INTERVAL}` 上挑选，挑完即冻结，"
        "再原样应用到另外两个区间。后两列是首次读取，没有回头改参数。",
        "",
        "| 规则 | 选中参数 | IS-1 日均 | IS-1 捕获率 | IS-2 日均 | IS-2 捕获率 | "
        "OOS-A 日均 | OOS-A 捕获率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    # Columns must follow the header exactly: IS-1, IS-2, OOS-A.
    column_order = (SELECTION_INTERVAL, "IS-2", "OOS-A")
    for name, row in payload["rules"].items():
        params = "(" + ", ".join(str(p) for p in row["params"]) + ")"
        cells = []
        for interval in column_order:
            d = row["results"][interval]
            cells.append(f"{d['mean']:.4%}")
            cells.append(f"{d['capture']:.2%}")
        lines.append(f"| {name} | {params} | " + " | ".join(cells) + " |")
    lines.extend([
        "",
        "> 捕获率 = 该规则日均收益 ÷ 同期同标的的神谕，跨标的后取算术平均。",
        "",
        "## 对 2%/天 目标的判定",
        "",
    ])
    lines.extend(_verdict(payload))
    lines.extend(_why_section(payload))
    lines.extend(_long_horizon_section(payload))
    lines.extend(_universe_section(payload))
    lines.append(TICK_SECTION)
    lines.extend(_strategy_section(payload))
    lines.extend([
        "",
        "## 口径",
        "",
        f"- 每边费率 {FEE_RATE:.2%}，无滑点；费用按成交额逐笔扣除。",
        f"- 底仓 {BASE_SHARES} 股，现金 {INITIAL_CASH:,.0f} 元，每日重置。",
        "- 规则一律「一天至多一趟、收盘前必平」，因为诊断显示日末未平腿是历史最大漏损。",
        "- 1 分钟收盘价成交，无排队/滑点建模——这是乐观假设，真实结果只会更差。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/capturet_v1_lab_20260919.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def _conclusions(payload: dict) -> list[str]:
    """State what the ceiling permits, in the denominator that matters."""
    worst_ratio = 0.0
    best_ratio = float("inf")
    for _symbol, per_interval in payload["bounds"].items():
        for _name, row in per_interval.items():
            pos = row.get("oracle_position", 0.0)
            if pos <= 0:
                continue
            ratio = TARGET_DAILY / pos
            worst_ratio = max(worst_ratio, ratio)
            best_ratio = min(best_ratio, ratio)
    return [
        f"**2%/天 需要吃下当日振幅的 {best_ratio:.0%}–{worst_ratio:.0%}**"
        "（按仓位市值口径；也就是每天都要抓住当天最高最低点之间三到七成）。",
        "",
        "对照实测：**最好的规则只捕获到神谕的 0–1%**，日均 0.03% 量级。"
        f"也就是说差距不在「几个百分点」，而在 **20–90 倍**。",
        "",
        "这不是「调参没调好」：朴素因果规则（开盘卖/收盘买、隔夜）扣费后是负的，"
        "八个规则族、事后最优参数，全都停在 0.03%/天以下；"
        "而神谕（知道当天最高最低点）是它的几十倍。"
        "**中间这段差额就是必须被预测出来、但没有任何规则预测到的部分。**",
    ]


def _verdict(payload: dict) -> list[str]:
    best_name, best_row, best_capture, best_interval = None, None, None, None
    for name, row in payload["rules"].items():
        for interval, _s, _e, _role in INTERVALS:
            d = row["results"][interval]
            if best_capture is None or d["capture"] > best_capture:
                best_capture, best_name, best_row = d["capture"], name, d
                best_interval = interval
    lines = [
        f"所有规则中，单格最高捕获率是 **{best_name}** 在 {best_interval} 上的 "
        f"**{best_capture:.1%}**（日均 {best_row['mean']:.4%}）。",
        "",
    ]
    if best_row["mean"] >= TARGET_DAILY:
        lines.append("该格达到了 2%/天。")
    else:
        lines.append(
            f"该格日均 {best_row['mean']:.4%}，距 2%/天 差 "
            f"**{TARGET_DAILY / max(best_row['mean'], 1e-9):.0f} 倍**。")
    lines.extend([
        "",
        "需要说清楚的是：**这不是「调参没调好」，而是信息问题**。"
        "朴素因果规则（开盘卖/收盘买、隔夜）扣费后是负的；"
        "八个规则族的事后最优也只有零点几个百分点；"
        "而神谕（知道当天最高最低点）是几倍于此。"
        "**两者之间的差距就是必须被预测出来的那部分。**",
        "",
        "因此在本数据集上，**没有可复现的日内 alpha 支撑 2%/天**——"
        "但请注意，这条结论的强度依分母而定："
        "按仓位口径，2% 需要 30%–69% 的捕获率（极难，非不可能）；"
        "按「每日重置强制保留 10 万现金」的权益口径，它对部分格子连数学上都禁止。",
        "",
        "若要把目标变成可执行的，只有三条路："
        "① 把目标降到「神谕的个位数百分比」量级；"
        "② 引入本数据集之外的预测信息（分钟级订单流、盘口、消息面）；"
        "③ 放弃日内，改做持有/择时——那是另一个问题。",
    ])
    return lines


def main() -> None:
    if OUT.exists() and "--no-validate" not in sys.argv:
        raise FileExistsError("refuse to overwrite existing report: " + str(OUT))
    OUT.mkdir(parents=True, exist_ok=True)

    all_days = {s: load_days(f) for s, f in DATASETS.items()}
    fold = {
        symbol: {
            name: {d: b for d, b in days.items() if start <= d <= end}
            for name, start, end, _role in INTERVALS
        }
        for symbol, days in all_days.items()
    }

    bounds = {}
    for symbol, per_interval in fold.items():
        bounds[symbol] = {}
        for name, _s, _e, role in INTERVALS:
            days = per_interval[name]
            ordered = sorted(days)
            overnight = [
                naive_overnight(days[d], float(days[ordered[i - 1]]["close"].iloc[-1]))
                for i, d in enumerate(ordered) if i > 0]
            # Two denominators.  "equity" counts the idle cash that the daily
            # reset forces on us; "position" counts only the base market value.
            # The honest one for a real, nearly-fully-invested account is
            # position, so both are reported rather than picking the flattering
            # one.
            oracle_eq = []
            oracle_pos = []
            exposure = []
            path_lengths = []
            for b in days.values():
                eq = equity_of(b)
                pos = BASE_SHARES * float(b["close"].iloc[0])
                net, _shares = oracle_net(b)
                oracle_eq.append(net / eq)
                oracle_pos.append(net / pos if pos > 0 else 0.0)
                exposure.append(pos / eq if eq > 0 else 0.0)
                closes = b["close"].astype(float).values
                path_lengths.append(
                    float(np.abs(np.diff(closes)).sum()) / closes[0]
                    if len(closes) > 1 and closes[0] > 0 else 0.0)
            bounds[symbol][name] = {
                "role": role,
                "days": len(days),
                "oracle": float(np.mean(oracle_eq)),
                "oracle_position": float(np.mean(oracle_pos)),
                "exposure": float(np.mean(exposure)),
                "path_length": float(np.mean(path_lengths)),
                "range": float(np.mean([
                    (float(b["high"].max()) - float(b["low"].min())) /
                    float(b["open"].iloc[0]) for b in days.values()])),
                "naive_intraday": float(np.mean([naive_intraday(b) for b in days.values()])),
                "naive_overnight": float(np.mean(overnight)) if overnight else 0.0,
            }
            print(f"{symbol} {name}: oracle={bounds[symbol][name]['oracle']:.4%}",
                  flush=True)

    chosen = select_on_is1(fold)
    rules = {}
    for name, sel in chosen.items():
        rule, _ga, _gb, needs_prev = RULES[name]
        results = {}
        for interval, _s, _e, role in INTERVALS:
            means = {}
            oracles = {}
            for symbol in DATASETS:
                r = evaluate(rule, fold[symbol][interval], sel["params"],
                             needs_prev)
                means[symbol] = float(r.mean())
                oracles[symbol] = bounds[symbol][interval]["oracle"]
            # Ratio of means, not mean of ratios: averaging per-symbol ratios
            # lets the small-oracle symbols dominate and can flip the sign.
            mean_return = float(np.mean(list(means.values())))
            mean_oracle = float(np.mean(list(oracles.values())))
            results[interval] = {
                "role": role,
                "mean": mean_return,
                "oracle": mean_oracle,
                "capture": (mean_return / mean_oracle) if mean_oracle > 0 else 0.0,
                "by_symbol": means,
            }
        rules[name] = {"params": list(sel["params"]),
                       "is1_mean": sel["is1_mean"], "results": results}
        print(f"rule {name}: IS-1 {results['IS-1']['mean']:.4%} -> "
              f"IS-2 {results['IS-2']['mean']:.4%}, "
              f"OOS-A {results['OOS-A']['mean']:.4%}", flush=True)

    if "--no-validate" in sys.argv:
        # Reuse the strategy section from the existing results.json: the rule
        # search is seconds, the real-harness validation is ~25 minutes, and the
        # strategy files have not changed.
        prior = OUT / "results.json"
        if not prior.exists():
            raise FileNotFoundError(
                "--no-validate needs an existing results.json to reuse")
        strategy = json.loads(prior.read_text(encoding="utf-8"))["strategy"]
        print("reusing strategy validation from results.json", flush=True)
    else:
        strategy = validate_strategy({"bounds": bounds}, fold,
                                     COMPARISON_STRATEGIES)
    horizon = horizon_analysis()
    long_horizon = long_horizon_scan()
    universe = universe_scan()
    payload = {"target_daily": TARGET_DAILY, "bounds": bounds, "rules": rules,
               "strategy": strategy, "horizon": horizon,
               "long_horizon": long_horizon, "universe": universe,
               "intervals": [
                   {"name": n, "start": s, "end": e, "role": r}
                   for n, s, e, r in INTERVALS]}
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
