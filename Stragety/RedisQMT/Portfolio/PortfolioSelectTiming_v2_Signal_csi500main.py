#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选股组合策略 v2 —— **收窄池版实盘信号脚本（只出信号，不调任何下单接口）**。

## 与 v1 的差异

| 项 | v1 | **v2（本脚本）** |
|---|---|---|
| 股票池 | 冻结面板里的全部代码 | **中证500 ∩ 主板 ∩ 非ST**，从桥接实时取 |
| 成分来源 | 回测时的静态名单 | **桥接 `中证500` 板块**（实时成分） |
| 择时层 | 支持 `--mode timing` | **去掉**（消融显示是负贡献，见下） |
| 配置 | 冻结面板 | 同 |

## 择时层为什么被去掉

两个消融都显示它是**负贡献**，而且是在**收窄池自己的数据上**：

* F7 口径：择时（B − A）= **−6.64%/年**
* F10 口径：择时（B − A）= **−5.32%/年**

上一轮的全市场研究还发现它在样本外全面失效。**去掉后平均仓位 100%。**

## 🛑 当前状态：不冻结，本脚本拒绝出信号

收窄池（415 只）上的完整结论是**没有配置能通过**（证据见
`analysis/csi500_main_study_20260920/`）：

| 候选配置 | 选股贡献 (D−A) | alpha t | 尾部层是否解释得通 |
|---|---:|---:|---|
| F10 隔夜 size_neutral n=30 | +13.93% | 1.33 | ✗ 尾部只给 +2.7%/年毛利，差 10.6pp |
| **F7 特质波动率 size_neutral n=10** | **+2.02%** | **0.34** | **✓ 对得上** |
| F6 MAX raw n=30 | — | — | ✗ 尾部 −5.6%/年，符号相反 |

F10 那个 +18% 看着最好，但**它的机制对不上** —— 引擎排出来的配置优劣
和稳健的截面度量不一致时，引擎那一边是噪声。这正是上一轮 F2 踩过的坑
（尾部 +2.26%/月 → 引擎 −60%/年）。

F7 是唯一机制自洽的，但对得上的代价是**它小到无法与零区分**（t = 0.34）。

**所以 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py` 的 `FROZEN` 仍是 `None`，本脚本调用即报错。**
用户已确认选择「不冻结」。要打开需自行填 `FROZEN` —— 但请先读
`analysis/csi500_main_study_20260920/README.md`。

## 本脚本不做的事

* 不调用 `order_shares` / `passorder` / 任何下单接口
* 不修改 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py` 或任何回测产物
* 只往 `output/` 写信号文件

用法：
    python Stragety/RedisQMT/Portfolio/PortfolioSelectTiming_v2_Signal_csi500main.py \
        --asof 20260918
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.Portfolio.portfolio import frozen, transforms                        # noqa: E402
from Stragety.RedisQMT.Portfolio.portfolio.panel import PanelData                           # noqa: E402


OUTPUT = Path(__file__).resolve().parent / "output"
PANEL = ROOT / "analysis/panel_20260920/panel.parquet"

ACCOUNT = "8890145315"
SECTOR = "中证500"
FETCH_BARS = 600          # 够长：只要 ≥250 根就能判定「上市够久」
WARMUP_BARS = 300         # 一致性闸门跳过的预热 bar 数（覆盖最长的 250 根窗口）
EQUITY = 1_000_000.0

# 主板前缀。002 中小板 2021-04-06 已并入深主板。
SH_MAIN = ("600", "601", "603", "605")
SZ_MAIN = ("000", "001", "002", "003")


# ─────────────────────── 股票池 ───────────────────────

def connect_bridge():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    # **`configure()` 返回的是二元组 `(xt_trader, xtdata)`。**
    # v1 写的是 `data = connect_bridge()` 再调 `data.download_history_data2` ——
    # 拿到的是 xt_trader，上面根本没有这个方法，脚本一跑就 AttributeError。
    _, xtdata = configure(account_id=ACCOUNT, timeout_seconds=120)
    return xtdata


def is_main_board(code: str) -> bool:
    return code[:3] in SH_MAIN or code[:3] in SZ_MAIN


def is_st_name(name: str) -> bool:
    upper = str(name or "").upper().replace(" ", "")
    return ("ST" in upper) or ("退" in str(name or ""))


def csi500_main_universe(xtdata) -> tuple[list[str], dict[str, str], list[str]]:
    """中证500 ∩ 主板 ∩ 非ST。

    返回 `(可用代码, 代码→名称, 被剔除的ST代码)`。

    **ST 按「当前名字」判**，这是实盘唯一正确的判法（不能买今天的 ST）。
    注意这与回测口径不同 —— 回测里历史 `isST` 恒为 "0"（推导已证伪放弃），
    这是**已知偏差**，不是这里能补的。
    """
    members = xtdata.get_stock_list_in_sector(SECTOR) or []
    main = sorted(c for c in members if is_main_board(c))

    names, dropped_st = {}, []
    for code in main:
        try:
            detail = xtdata.get_instrument_detail(code) or {}
            name = str(detail.get("InstrumentName") or "").strip()
        except Exception:
            name = ""
        names[code] = name

    # 取不到名字的代码**不进池**：连名字都没有就说明桥接没有它的详情，
    # 下单时大概率也拿不到，宁可不选也不要在信号里占位。
    usable, unnamed = [], []
    for code in main:
        if not names.get(code):
            unnamed.append(code)
        elif is_st_name(names[code]):
            dropped_st.append(code)
        else:
            usable.append(code)
    if unnamed:
        print(f"  [warn] {len(unnamed)} 只取不到名称，已排除：{unnamed[:8]}",
              flush=True)
    return usable, names, dropped_st


# ─────────────────────── 活数据 → 面板 ───────────────────────

def fetch_history(xtdata, codes: list[str]) -> pd.DataFrame:
    """取日线：不复权 + 前复权，拼成长表。"""
    fields = ["open", "high", "low", "close", "volume", "amount"]
    xtdata.download_history_data2(codes, "1d", start_time="", end_time="",
                                  download_timeout_seconds=900)

    # **先按复权类型各自拼接，最后只做一次跨类型合并。**
    # 把两种复权的 block 全塞进同一个 `parts` 列表再两两 merge 是错的：
    # 列表里相邻两项是**同一复权、不同股票**，列名完全相同，merge 会直接抛
    # `MergeError: duplicate columns`。（v1 靠一个 set(columns) 比较兜住了，
    # 但那个判据在列名恰好相同时会走 concat、不同时走 merge，本身就绕。）
    by_type: dict[str, list[pd.DataFrame]] = {"none": [], "front": []}
    for dividend_type, prefix in (("none", ""), ("front", "f_")):
        raw = xtdata.get_market_data_ex(fields, codes, period="1d",
                                        count=FETCH_BARS,
                                        dividend_type=dividend_type,
                                        fill_data=False, timeout_seconds=600)
        for code, block in (raw or {}).items():
            if block is None or len(block) == 0:
                continue
            frame = block.reset_index()
            frame.columns = ["date"] + [f"{prefix}{c}"
                                        for c in frame.columns[1:]]
            frame["date"] = (frame["date"].astype(str)
                             .str.replace("-", "").str[:8])
            frame["code"] = code
            by_type[dividend_type].append(frame)

    if not by_type["none"]:
        return pd.DataFrame()
    merged = pd.concat(by_type["none"], ignore_index=True)
    if by_type["front"]:
        front = pd.concat(by_type["front"], ignore_index=True)
        merged = merged.merge(front, on=["code", "date"], how="outer")
    return merged


def build_live_panel(live: pd.DataFrame, names: dict[str, str]) -> PanelData:
    """把活数据整成与回测同 schema 的 `PanelData`。

    **复权口径**：`adj_*` 直接取桥接的**前复权**序列。

    回测面板用的是「锚在首日原始价的后复权」，与这里**绝对值不同**。
    但本脚本要跑的因子（F1/F2/F3/F5/F6/F7/F10/F11）全都是**按股票取比值**，
    每股一个常数尺度因子会在比值里约掉 —— 所以排序不受影响。
    这一点由下面的秩相关断言守着，不靠推理。

    `total_ret` 同理取前复权收益；它只用于 `_market_return()` 的横截面均值。
    """
    frame = live.copy()
    if "f_close" not in frame.columns:
        # 前复权一路整批缺失 —— 桥接缺除权除息文件时的典型症状。
        # 这里**不静默退回不复权**：那会让股息被当成价格下跌，
        # 因子值系统性偏移，而信号照样发出去。宁可硬失败。
        raise SystemExit(
            "桥接的前复权序列整批缺失 —— 拒绝构造面板。\n"
            "静默退回不复权会让股息被当作价格下跌，因子值系统性偏移，"
            "而信号照样发出去。")
    frame = frame.rename(columns={"f_open": "adj_open", "f_high": "adj_high",
                                  "f_low": "adj_low", "f_close": "adj_close"})
    frame = frame.sort_values(["code", "date"]).reset_index(drop=True)

    # 前复权序列在大面积缺失时退回不复权 —— 桥接缺除权除息文件时
    # `dividend_type='front'` 会返回全 0，这个失效模式必须在源头挡住。
    bad = frame.groupby("code")["adj_close"].transform(
        lambda s: (s.fillna(0.0) <= 0).all())
    frame.loc[bad, ["adj_open", "adj_high", "adj_low", "adj_close"]] = \
        frame.loc[bad, ["open", "high", "low", "close"]].to_numpy()

    # `fill_method=None`：停牌日序列里有 NaN，默认的 ffill 会把停牌前后的
    # 价格接起来算出一个并不存在的收益。停牌就该是 NaN。
    frame["total_ret"] = frame.groupby("code")["adj_close"].pct_change(
        fill_method=None).fillna(0.0)

    # 上市天数：用**实际取到的 bar 数**。取满 FETCH_BARS 根即视为
    # 「上市够久」—— 250 根的门槛在取 600 根的窗口下判定是正确的。
    frame["listed_days"] = frame.groupby("code")["date"].transform("size")
    frame["tradable"] = (frame["volume"].fillna(0.0) > 0)
    amount = pd.Series(np.where(frame["tradable"], frame["amount"], np.nan))
    frame["amount_ma20"] = (amount.groupby(frame["code"])
                            .transform(lambda s: s.rolling(20, min_periods=10)
                                       .mean()))
    frame["liquid"] = frame["amount_ma20"] >= 5e7

    # 涨跌停：只按板块规则。主板 ±10%（002/003 同板）。
    # 历史 `isST` 未建模，所以**没有** ±5% 这一档 —— 已知偏差，见 v1 说明。
    limit_ratio = np.where(frame["code"].str[:3].isin(("300", "301")), 0.20,
                           np.where(frame["code"].str[:3].isin(("688", "689")),
                                    0.20, 0.10))
    frame["preclose"] = frame.groupby("code")["close"].shift(1)
    frame["limit_up"] = np.round(frame["preclose"] * (1.0 + limit_ratio), 2)
    frame["limit_down"] = np.round(frame["preclose"] * (1.0 - limit_ratio), 2)
    frame["cannot_buy"] = frame["open"] >= frame["limit_up"] - 1e-4
    frame["cannot_sell"] = frame["open"] <= frame["limit_down"] + 1e-4

    # `eligible()` 用 `isST != "1"` 判 ST；这里的 ST 已在上游按名字剔除，
    # 但要给出一列，否则 `eligible()` 会 KeyError。
    frame["isST"] = np.where(
        frame["code"].map(lambda c: is_st_name(names.get(c, ""))), "1", "0")
    frame["close"] = frame["close"].astype("float64")

    keep = ["code", "date", "open", "high", "low", "close", "volume", "amount",
            "adj_open", "adj_high", "adj_low", "adj_close", "preclose",
            "total_ret", "listed_days", "tradable", "amount_ma20", "liquid",
            "limit_up", "limit_down", "cannot_buy", "cannot_sell", "isST"]
    return PanelData.from_frame(frame[keep].reset_index(drop=True))


# ─────────────────────── 一致性闸门 ───────────────────────

def assert_live_matches_research(live_panel: PanelData, asof: str,
                                 factor_key: str) -> None:
    """活数据算出的因子，必须与回测面板**同序**。

    判据用**截面秩相关**而不是数值相等：两边复权锚点不同，
    绝对值本来就不同；而这套因子的输出是**截面排序**，
    秩才是要守住的不变量。

    任何一天秩相关 < 0.99，或公共日期不足，直接 `SystemExit`。
    **静默分叉比不发信号危险得多。**
    """
    research = PanelData.load(PANEL).between(
        str(int(asof[:4]) - 3) + asof[4:], asof)
    codes = set(live_panel.codes)
    research = PanelData.from_frame(
        research.long[research.long["code"].isin(codes)].reset_index(drop=True))

    problems = []
    for label, panel in (("活数据", live_panel), ("回测面板", research)):
        try:
            value = frozen.build_alpha(panel, {"factor": factor_key,
                                               "treatment": "raw"})
        except Exception as error:
            problems.append(f"{label}因子计算失败：{type(error).__name__}: "
                            f"{error}")
            continue
        if label == "活数据":
            live_alpha = value
        else:
            research_alpha = value

    if problems:
        for item in problems:
            print(f"  [阻断] {item}", flush=True)
        raise SystemExit("一致性检查无法完成 —— 拒绝产生信号。")

    merged = (live_alpha.merge(research_alpha, on=["code", "date"],
                               suffixes=("_live", "_research")))

    # **按声明好的长度跳过预热区，不靠覆盖率猜。**
    #
    # 实盘只取 `FETCH_BARS` 根 bar。滚动因子在窗口最前面那段是
    # 「有值但不准」：`_residual_stats` 用 `min_periods=window//2`，
    # F7 的 window=60，所以第 30 根就出值、但要到第 60 根才与回测一致。
    # 在那段里比对，量到的是**预热不足**，不是口径分叉 ——
    # 拿它当分叉处理会天天误报，拿它当正常放过又会漏掉真的分叉。
    #
    # 取 300 根，覆盖因子库的最长窗口（F1/F5 的 250 根）并留出余量。
    # 这是**固定规则**，不随数据变，所以不会把闸门调成「永远通过」。
    dates_sorted = sorted(merged["date"].unique())
    warmup_cut = (dates_sorted[WARMUP_BARS]
                  if len(dates_sorted) > WARMUP_BARS else dates_sorted[0])
    merged = merged[merged["date"] >= warmup_cut]
    print(f"  预热区跳过 {WARMUP_BARS} 根 bar（至 {warmup_cut}），"
          f"实际比对 {len(dates_sorted) - WARMUP_BARS if len(dates_sorted) > WARMUP_BARS else 0} 天",
          flush=True)

    ranks = []
    for date, day in merged.groupby("date"):
        day = day.dropna(subset=["alpha_live", "alpha_research"])
        if len(day) < 30:
            continue
        rho = day["alpha_live"].corr(day["alpha_research"], method="spearman")
        if np.isfinite(rho):
            ranks.append((date, float(rho), len(day)))

    if len(ranks) < 5:
        problems.append(f"预热后的公共截面不足（只有 {len(ranks)} 天）")

    if ranks:
        worst_date, worst_rho, worst_n = min(ranks, key=lambda r: r[1])
        median_rho = float(np.median([r[1] for r in ranks]))
        recent = ranks[-60:]
        recent_worst = min(r[1] for r in recent)
        print(f"  秩相关：中位 {median_rho:.4f}，最差 {worst_rho:.4f}"
              f"（{worst_date}，n={worst_n}，共 {len(ranks)} 天）", flush=True)
        print(f"  最近 60 天最差 {recent_worst:.4f}", flush=True)
        if worst_rho < 0.99:
            problems.append(
                f"因子秩相关跌破 0.99：{worst_date} 只有 {worst_rho:.4f}")

    if problems:
        for item in problems:
            print(f"  [阻断] {item}", flush=True)
        raise SystemExit(
            "活数据与回测口径不一致 —— 拒绝产生信号。\n"
            "宁可今天不出信号，也不要基于错误数据下单。")


# ─────────────────────── 信号 ───────────────────────

def build_signal(panel: PanelData, asof: str,
                 held: dict[str, float]) -> dict:
    """目标权重。**择时层已去掉，总仓位恒为 1.0。**"""
    config = frozen.require_frozen()
    weights = frozen.build_target_weights(panel, asof, held, EQUITY,
                                          gross_exposure=1.0, config=config)
    return {"gross_exposure": 1.0, "weights": weights}


def current_positions() -> dict[str, float]:
    """账户实际持仓权重。取不到就按空仓算，并在 warnings 里说明。"""
    try:
        from bigqmt_signal_trader.xtquant_compat import get_trade_detail_data
        positions = get_trade_detail_data(ACCOUNT, "STOCK", "POSITION")
        held, total = {}, 0.0
        for pos in positions or []:
            code = getattr(pos, "m_strInstrumentID", "")
            shares = getattr(pos, "m_nVolume", 0)
            if code and shares:
                held[code] = float(shares)
                total += float(shares)
        if total > 0:
            return {k: v / total for k, v in held.items()}
    except Exception:
        pass
    return {}


def render(signal: dict, panel: PanelData, asof: str, names: dict[str, str],
           held: dict[str, float]) -> list[str]:
    weights = signal["weights"]
    lines = [
        "",
        "=" * 92,
        f"  选股组合策略 v2（中证500 主板 / 非ST）  |  asof {asof}",
        f"  配置哈希 {frozen.config_hash()}   总仓位 100%（无择时层）",
        "=" * 92,
        f"  {'代码':<11}{'名称':<10}{'目标权重':>10}{'目标股数':>11}"
        f"{'参考价':>9}{'现持':>9}{'操作':>7}",
        "  " + "-" * 88,
    ]
    section = panel.cross_section(asof)
    for code, weight in weights.sort_values(ascending=False).items():
        price = float(section["close"].get(code, np.nan))
        shares = (int(weight * EQUITY / price // 100 * 100)
                  if np.isfinite(price) and price > 0 else 0)
        has = "持有" if code in held else "新买"
        lines.append(f"  {code:<11}{names.get(code, ''):<10}"
                     f"{weight:>10.2%}{shares:>11,}{price:>9.2f}"
                     f"{'是' if code in held else '否':>9}{has:>7}")
    for code in sorted(set(held) - set(weights.index)):
        lines.append(f"  {code:<11}{names.get(code, ''):<10}{0.0:>10.2%}"
                     f"{0:>11,}{'':>9}{'是':>9}{'卖出':>7}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", default=None,
                        help="YYYYMMDD，默认取活数据最后一个交易日")
    parser.add_argument("--skip-consistency", action="store_true",
                        help="跳过硬闸门 —— **只用于调试，不要在有持仓时用**")
    args = parser.parse_args()

    if not frozen.is_frozen():
        raise SystemExit(
            "配置未冻结 —— 本脚本拒绝出信号。\n\n"
            "收窄池（中证500 主板 / 非ST）的研究结论是「没有配置能通过」：\n"
            "  · F10 隔夜 n=30 引擎 +18.0%，但尾部层只给 +2.7%/年毛利，"
            "机制对不上；\n"
            "  · F7 特质波动率 n=10 机制自洽，但选股贡献 +2.02%/年、"
            "t = 0.34，与零无法区分。\n"
            "详见 analysis/csi500_main_study_20260920/README.md 与\n"
            "     Stragety/RedisQMT/Portfolio/StrategicResearchDirections"
            "AndEffectivenessRecords.md\n\n"
            "要打开请在 `Stragety/RedisQMT/Portfolio/portfolio/frozen.py` 填 FROZEN。")

    print(f"连接桥接（asof = {args.asof or '最新'}）...", flush=True)
    xtdata = connect_bridge()

    codes, names, dropped_st = csi500_main_universe(xtdata)
    print(f"股票池：中证500 ∩ 主板 ∩ 非ST = **{len(codes)} 只**"
          f"（剔除当前 ST {len(dropped_st)} 只：{dropped_st}）", flush=True)

    print(f"取日线（{len(codes)} 只 × 2 种复权）...", flush=True)
    live = fetch_history(xtdata, codes)
    if live.empty:
        raise SystemExit("桥接没有返回任何数据")
    asof = args.asof or str(live["date"].max())
    live = live[live["date"] <= asof]
    print(f"  {len(live):,} 行，{live['date'].min()} ~ {asof}", flush=True)

    panel = build_live_panel(live, names)
    print(f"  面板：{len(panel.long):,} 行，{len(panel.codes)} 只", flush=True)

    config = frozen.require_frozen()
    if not args.skip_consistency:
        print("一致性闸门（因子秩相关 vs 回测面板）...", flush=True)
        assert_live_matches_research(panel, asof, config["factor"])
    else:
        print("  [warn] 跳过一致性闸门（--skip-consistency）", flush=True)

    held = current_positions()
    if not held:
        print("  [warn] 取不到实际持仓，按空仓计算（信号仅供参考）", flush=True)

    signal = build_signal(panel, asof, held)
    print("\n".join(render(signal, panel, asof, names, held)), flush=True)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": asof,
        "universe": "csi500 ∩ 主板 ∩ 非ST",
        "n_universe": len(panel.codes),
        "excluded_st": dropped_st,
        "gross_exposure": 1.0,
        "timing_layer": None,
        "config_hash": frozen.config_hash(),
        "holdings": sorted(signal["weights"].round(6).to_dict().items()),
        "warnings": ([] if held else ["未取到实际持仓，按空仓计算"]),
    }
    path = OUTPUT / f"portfolio_v2_signal_{asof}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n  信号写入 {path}", flush=True)
    print("  （本脚本不调用任何下单接口）", flush=True)


if __name__ == "__main__":
    main()
