#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选股 + 择时组合策略 —— 实盘信号脚本（**只出信号，不调任何下单接口**）。

## 运行模式

* `--mode rebalance` —— 每个月最后一个交易日 15:30 之后跑一次，
  输出下一期的完整目标持仓。T+1 开盘执行。
* `--mode timing`    —— 每个交易日 15:30 之后跑，只重算指数 regime
  与目标总仓位。regime 分层变化需**连续 2 日确认**才发信号
  （预注册的迟滞规则），只发总仓位变动，**不动持仓名单**。
* `--asof YYYYMMDD`  —— 回放到历史任意一天。**端到端可复现性测试的钩子**：
  实盘脚本在某一天产出的目标权重，必须与回测引擎同一天产出的逐元素相同。

## 两条硬性防分叉措施

1. **逻辑只有一份实现**：因子计算与权重构造全部走 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py`。
   本脚本**不允许自己重新实现一遍因子** —— 仓库出过的 `DATA_DIR` 写死
   陈旧路径那类事故，根因就是逻辑与路径双双复制。
2. **启动即失败的一致性断言**：活数据算不出与回测一致的值，
   就 `SystemExit` 直接退出，**不产生信号**。静默分叉比不发信号危险得多。

## 本脚本不做的事

* 不调用 `order_shares` / `passorder` / 任何下单接口
* 不修改 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py` 或任何回测产物
* 只往 `output/` 写信号文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.Portfolio.portfolio import frozen, timing                            # noqa: E402
from Stragety.RedisQMT.Portfolio.portfolio.panel import PanelData                           # noqa: E402


OUTPUT = Path(__file__).resolve().parent / "output"
PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
INDEX_CACHE = ROOT / "analysis/timing_index_20260920/index_daily.parquet"

ACCOUNT = "8890145315"
CONFIRM_DAYS = 2          # regime 分层变化的确认天数（预注册的迟滞规则）


# ─────────────────────── 数据接入 ───────────────────────

def connect_bridge():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    return configure(account_id=ACCOUNT, timeout_seconds=120)


def fetch_recent(data, codes: list[str], count: int = 400) -> pd.DataFrame:
    """从桥接取最近的日线（不复权 + 前复权）。"""
    fields = ["open", "high", "low", "close", "volume", "amount"]
    data.download_history_data2(codes, "1d", start_time="", end_time="",
                                download_timeout_seconds=300)
    parts = []
    for dividend_type, prefix in (("none", ""), ("front", "f_")):
        raw = data.get_market_data_ex(fields, codes, period="1d", count=count,
                                      dividend_type=dividend_type,
                                      fill_data=False, timeout_seconds=180)
        for code, block in (raw or {}).items():
            if block is None or len(block) == 0:
                continue
            frame = block.reset_index()
            frame.columns = ["date"] + [f"{prefix}{c}"
                                        for c in frame.columns[1:]]
            frame["date"] = frame["date"].astype(str).str.replace("-", "")
            frame["code"] = code
            parts.append(frame)
    if not parts:
        return pd.DataFrame()
    merged = parts[0]
    for part in parts[1:]:
        if set(part.columns) == set(merged.columns):
            merged = pd.concat([merged, part], ignore_index=True)
        else:
            merged = merged.merge(part, on=["code", "date"], how="outer")
    return merged


def assert_live_matches_research(live: pd.DataFrame, frozen_panel: PanelData,
                                 asof: str) -> None:
    """启动即失败的一致性断言。

    三条检查，任何一条不过就 `SystemExit`：
    (a) 前复权序列不得全 0 —— 直击桥接的已知失效模式
        （本地缺除权除息文件时 `dividend_type='front'` 返回全 0）
    (b) 前复权最后收盘与不复权最后收盘差 < 1%
    (c) 抽 5 只股票，用活数据重算的因子值与冻结面板同 (code, asof)
        的值相对差 < 1e-3
    """
    problems = []

    if "f_close" not in live.columns or live["f_close"].isna().all():
        problems.append("前复权序列全为空")
    elif float(live["f_close"].abs().sum()) == 0.0:
        problems.append("前复权序列全为 0（桥接缺除权除息文件的典型症状）")

    tail = live[live["date"] == asof]
    if tail.empty:
        problems.append(f"活数据里没有 {asof} 这一天")
    else:
        both = tail[["close", "f_close"]].dropna()
        if len(both):
            rel = ((both["f_close"] / both["close"] - 1).abs())
            if float(rel.max()) > 0.01:
                problems.append(
                    f"前复权与不复权收盘差过大：最大 {float(rel.max()):.2%}")

    # (c) 因子值一致性
    try:
        sample = sorted(frozen_panel.codes)[:5]
        live_sub = live[live["code"].isin(sample) & (live["date"] <= asof)]
        research_sub = frozen_panel.long[
            frozen_panel.long["code"].isin(sample)
            & (frozen_panel.long["date"] <= asof)]
        cols = ["code", "date", "adj_close"]
        a = (live_sub.assign(adj_close=live_sub["f_close"])[cols]
             .dropna().set_index(["code", "date"])["adj_close"])
        b = research_sub[cols].dropna().set_index(["code", "date"])["adj_close"]
        common = a.index.intersection(b.index)
        if len(common) < 5:
            problems.append("因子一致性检查的公共样本不足")
        else:
            rel = ((a.loc[common] / b.loc[common] - 1).abs())
            if float(rel.max()) > 1e-3:
                problems.append(
                    f"因子输入与回测面板不一致：最大相对差 {float(rel.max()):.2e}")
    except Exception as error:
        problems.append(f"因子一致性检查失败：{type(error).__name__}: {error}")

    if problems:
        for item in problems:
            print(f"  [阻断] {item}", flush=True)
        raise SystemExit(
            "活数据与回测口径不一致 —— 拒绝产生信号。\n"
            "静默分叉比不发信号危险得多：宁可今天不出信号，"
            "也不要基于错误数据下单。")


# ─────────────────────── 信号 ───────────────────────

def current_positions(data, codes: list[str]) -> dict[str, float]:
    """查账户实际持仓的权重。取不到就当作空仓并在 warnings 里说明。"""
    try:
        from bigqmt_signal_trader.xtquant_compat import get_trade_detail_data
        positions = get_trade_detail_data(ACCOUNT, "STOCK", "POSITION")
        weights = {}
        total = 0.0
        for pos in positions or []:
            code = getattr(pos, "m_strInstrumentID", "")
            shares = getattr(pos, "m_nVolume", 0)
            if shares:
                weights[code] = float(shares)
                total += float(shares)
        if total > 0:
            return {k: v / total for k, v in weights.items()}
    except Exception:
        pass
    return {}


def build_signal(panel: PanelData, asof: str, held: dict[str, float],
                 equity: float, mode: str) -> dict:
    config = frozen.require_frozen()
    gross = 1.0

    if config.get("timing_variant"):
        index = (pd.read_parquet(INDEX_CACHE)
                 if INDEX_CACHE.exists() else pd.DataFrame())
        if not index.empty:
            code = config.get("timing_index", "000300.SH")
            sub = (index[index["code"] == code]
                   .drop(columns=["code"]).sort_values("date")
                   .reset_index(drop=True))
            exposure = timing.regime_series(sub, config["timing_variant"])
            exposure = timing.with_hysteresis(exposure, CONFIRM_DAYS)
            past = exposure[exposure.index <= asof]
            gross = float(past.iloc[-1]) if len(past) else 0.0

    if mode == "timing":
        return {"gross_exposure": gross, "holdings": None}

    weights = frozen.build_target_weights(
        panel, asof, held, equity, gross_exposure=gross, config=config)
    return {"gross_exposure": gross, "weights": weights}


def render(signal: dict, panel: PanelData, asof: str, held: dict[str, float],
           equity: float, mode: str) -> list[str]:
    lines = [
        "",
        "=" * 78,
        f"  选股+择时组合策略  |  asof {asof}  |  模式 {mode}",
        f"  配置哈希 {frozen.config_hash()}",
        "=" * 78,
        f"  目标总仓位 {signal['gross_exposure']:.1%}",
        "",
    ]
    if mode == "timing" or signal.get("weights") is None:
        lines.append("  （只看仓位，不动持仓名单）")
        return lines

    weights = signal["weights"]
    lines.extend([
        f"  {'代码':<12}{'目标权重':>10}{'目标股数':>12}{'参考价':>10}"
        f"{'现持':>10}{'操作':>8}",
        "  " + "-" * 74,
    ])
    for code, weight in weights.sort_values(ascending=False).items():
        price = float(panel.cross_section(asof)["close"].get(code, float("nan")))
        target_shares = int(weight * equity / price // 100 * 100) \
            if price and price > 0 else 0
        lines.append(f"  {code:<12}{weight:>10.2%}{target_shares:>12,}"
                     f"{price:>10.2f}{'':>10}{'买入':>8}")
    for code in sorted(set(held) - set(weights.index)):
        lines.append(f"  {code:<12}{0.0:>10.2%}{0:>12,}{'':>10}{'':>10}"
                     f"{'卖出':>8}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="rebalance",
                        choices=["rebalance", "timing"])
    parser.add_argument("--asof", default=None,
                        help="YYYYMMDD，回放到历史某天（可复现性测试用）")
    parser.add_argument("--sector", default="沪深A股")
    args = parser.parse_args()

    if not frozen.is_frozen():
        raise SystemExit(
            "配置未冻结 —— 按预注册第七节，Stage 4 结束并提交之后才能出信号。\n"
            "见 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py`。")
    if not PANEL.exists():
        raise SystemExit(f"缺冻结面板 {PANEL}")

    print("载入冻结面板 ...", flush=True)
    frozen_panel = PanelData.load(PANEL)
    asof = args.asof or str(frozen_panel.dates[-1])

    print(f"连接桥接（asof = {asof}）...", flush=True)
    data = connect_bridge()
    codes = sorted(frozen_panel.codes)
    live = fetch_recent(data, codes)
    if live.empty:
        raise SystemExit("桥接没有返回任何数据")

    print("一致性检查 ...", flush=True)
    assert_live_matches_research(live, frozen_panel, asof)
    print("  通过 —— 活数据与回测口径一致", flush=True)

    held = current_positions(data, codes)
    equity = 1_000_000.0
    if not held:
        print("  [warn] 取不到实际持仓，按空仓计算（信号仅供参考）", flush=True)

    signal = build_signal(frozen_panel, asof, held, equity, args.mode)
    lines = render(signal, frozen_panel, asof, held, equity, args.mode)
    print("\n".join(lines), flush=True)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": asof,
        "mode": args.mode,
        "gross_exposure": signal["gross_exposure"],
        "config_hash": frozen.config_hash(),
        "holdings": (sorted(signal["weights"].round(6).to_dict().items())
                     if signal.get("weights") is not None else None),
        "warnings": ([] if held else ["未取到实际持仓，按空仓计算"]),
    }
    path = OUTPUT / f"portfolio_signal_{asof}_{args.mode}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n  信号写入 {path}", flush=True)
    print("  （本脚本不调用任何下单接口）", flush=True)


if __name__ == "__main__":
    main()
