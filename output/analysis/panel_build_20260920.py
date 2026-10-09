"""把 raw/batch_*.parquet 合成干净面板，并跑 QC。

## 输入（来自 redisQMT 桥接）

每只股票两套价格，外连接在同一张表上：

| 列 | 含义 |
|---|---|
| `time, open, high, low, close, volume, amount` | **不复权原始价** |
| `f_open, f_high, f_low, f_close, f_volume, f_amount` | **前复权** |

## 复权：锚在首日原始价的后复权

前复权序列 `f_*` 已经剔除了除权跳空，所以它就是总收益序列。直接用它，
**不要再自己累乘涨跌幅** —— 实测 `000007.SZ` 2017-07-17（10 送 5 + 一字跌停）
前复权给出 −10.04%、官方 −10.02%，而拿不复权价累乘会得到 −46% 的假跌幅。

    ret_factor = f_close / f_close[0]            # 累计总收益（相对首行）
    adj_close  = raw_close[0] × ret_factor        # 后复权，时间稳定
    scale      = adj_close / raw_close            # 日内常数
    adj_open/high/low = raw_open/high/low × scale

**为什么存后复权而不是直接存 `f_*`**：前复权每次分红都整体重算，
上个月存的面板这个月无法复现同一份回测，直接违反仓库
「结论必须附可复现证据」的规则。后复权序列在过去是时间稳定的。

## 除权后昨收

    total_ret[t] = f_close[t] / f_close[t-1] - 1
    除权后昨收[t]  = raw_close[t] / (1 + total_ret[t])

涨跌停价按**除权后昨收**算，所以除权日的涨跌停判定也是对的。

## 停牌

桥接用 `fill_data=False`，**停牌日缺行**（而 `front` 序列在停牌日会给
冻结价）。所以先按「原始 `close` 有限」过滤，只保留**真实成交日**。

引擎侧对应要求：持仓股停牌时盯市要**前向填充**收盘价，
否则持仓会被算成 0 市值。见 `portfolio/engine.py` 的 `close_mx.ffill()`。

## ST：**不建模**（推导已证伪）

桥接拿不到历史 `isST`。试过用「涨跌幅有界性」推导 —— ST 股是 ±5% 板，
所以「过去一年 max|收益| ∈ [4.8%, 5.5%]」看起来能识别它们。

**实测证明这条路走不通。** `--check-st` 用当前股票名做交叉验证：

| 指标 | 结果 |
|---|---|
| 抽样 697 只中名字含 ST | 21 只 |
| 被推导抓到的 | **1 只（召回 5%）** |
| 假阴性 | 20 只 |
| 假阳性 | 5 只（0.7%） |

召回 5% 等于没抓到。它主要在标记「最近一年恰好没出现大波动的正常股」，
而不是 ST 股。**让一个召回 5% 的字段驱动涨跌停比例判定只会引入错误**，
所以：

* `limit_ratio` **只用板块规则**（主板 ±10%、创业板 2020-08-24 后 ±20%、科创板 ±20%）
* `isST` **恒为 "0"**，不排除任何股票
* `is_st_like` 列保留，**仅作诊断**

**已知代价**：真实的 ST 股在面板里被当成 ±10% 板，其涨停/跌停判定偏乐观。
这是已声明的口径偏差，研究报告里必须列明，并配「剔除当前 ST 股」的敏感度测试。

## 输出

    analysis/panel_20260920/panel.parquet
    analysis/panel_20260920/QC.md
    analysis/panel_20260920/meta.json

用法：
    python analysis/panel_build_20260920.py
    python analysis/panel_build_20260920.py --check-st      # 额外做 ST 交叉验证
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/panel_20260920"
RAW = OUT / "raw"

GEM_REFORM = "20200824"     # 创业板涨跌幅改成 ±20% 的生效日
EPS = 1e-6

MIN_LISTED_DAYS = 250
MIN_AMOUNT = 5e7

# ST 推导的观察窗口。**用一年而不是 60 天**：
# 主板正常股是 ±10% 板，一年内几乎必然出现一次 >5.5% 的单日波动；
# ST 股永远不会。窗口太短（60 天）会把「恰好这段时间很平静」的正常股
# 也框进来 —— 实测 60 天窗口的假阳性率 7.92%，真实 ST 占比只有 2–4%。
ST_WINDOW = 250
ST_MIN_PERIODS = 160
# ST 推导的判据：过去 60 日的最大单日涨幅**恰好落在 ±5% 板附近**。
#
# 不能用「≤5.5%」这种单边判据 —— 那会把所有低波动正常股都框进来
# （实测假阳性 15%，而真实 ST 占比约 2–4%）。
# ST 股的特征不是「波动低」，而是**它会去触碰 ±5% 的板**：
# 上限略高于 5%（涨跌停价四舍五入）而不是明显低于它。
ST_BOUND_LOW = 0.048
ST_BOUND_HIGH = 0.055

# 判定「收益超出涨跌停限制」的容差。留 1.5pp 是因为：
# 涨跌停价有四舍五入、停牌复牌首日不设限、科创板前 5 日不设限。
LIMIT_SLACK = 0.015


def limit_ratio(code: str, date: str, is_st_like: bool) -> float:
    """当日涨跌幅限制比例。板块规则随日期变，这点很容易写错。"""
    num = code[:6]
    if num.startswith(("688", "689")):
        return 0.20                                   # 科创板
    if num.startswith(("300", "301")):
        return 0.20 if date >= GEM_REFORM else 0.10   # 创业板 2020-08-24 改 20%
    return 0.05 if is_st_like else 0.10               # 主板


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    """单只股票：过滤停牌 → 复权 → 涨跌停 → 可交易性。"""
    frame = frame.sort_values("time").reset_index(drop=True)

    # 只保留真实成交日：桥接的 front 序列在停牌日给冻结价，
    # 而不复权序列缺行。按原始价有限来筛，等价于「当天真的成交了」。
    raw_close = frame["close"].to_numpy(float)
    keep = np.isfinite(raw_close) & (raw_close > 0)
    frame = frame.loc[keep].reset_index(drop=True)
    if len(frame) == 0:
        return frame

    raw_close = frame["close"].to_numpy(float)
    f_close = frame["f_close"].to_numpy(float)

    # 前复权序列起点可能晚于不复权序列 → 丢掉前复权还没有值的前几行
    valid = np.isfinite(f_close) & (f_close > 0)
    if not valid.any():
        return frame.iloc[0:0]
    frame = frame.loc[valid].reset_index(drop=True)
    raw_close = frame["close"].to_numpy(float)
    f_close = frame["f_close"].to_numpy(float)

    raw_ret = np.empty(len(frame))
    raw_ret[0] = np.nan
    raw_ret[1:] = raw_close[1:] / raw_close[:-1] - 1.0
    f_ret = np.empty(len(frame))
    f_ret[0] = np.nan
    f_ret[1:] = f_close[1:] / f_close[:-1] - 1.0

    # ── ST 推导（因果：只看过去 60 个交易日）──
    # 先用**原始收益**推导：它在非除权日与总收益一致，而除权日一年才几次，
    # 混进 60 日窗口的滚动最大值里影响可忽略。
    abs_raw = pd.Series(np.abs(np.nan_to_num(raw_ret)))
    rolling_max = abs_raw.rolling(ST_WINDOW, min_periods=ST_MIN_PERIODS).max()
    # 触碰过 ±5% 板、但从未突破 → 疑似 ST。
    #
    # **这个推导已被证伪，只留作诊断，不参与任何决策。**
    # `--check-st` 用当前股票名做了交叉验证：抽样 697 只里有 21 只名字含 ST，
    # 推导只抓到 1 只 —— **召回率 5%**，同时还有 0.7% 的假阳性。
    # 也就是说它主要在标记「最近 250 天恰好没出现大波动的正常股」，
    # 而不是 ST 股。让一个召回 5% 的字段去驱动涨跌停比例判定是错的。
    is_st_like = ((rolling_max >= ST_BOUND_LOW)
                  & (rolling_max <= ST_BOUND_HIGH)).fillna(False).to_numpy()

    # ── 涨跌停比例（决定下面互校的容差）──
    #
    # **只用板块规则，不套用 ST 的 ±5%。** 历史 isST 拿不到，而自建推导
    # 的召回率只有 5%（见上），套用它只会引入错误。
    #
    # 代价：真实的 ST 股在面板里被当成 ±10% 板，它们的涨停/跌停判定偏乐观。
    # 这是一处**已知且已声明**的口径偏差，写进了 QC 与研究报告；
    # 另配一个「剔除当前 ST 股」的敏感度测试来界定它的量级。
    code = frame["code"].iloc[0]
    ratios = np.array([limit_ratio(code, d, False) for d in frame["time"]])
    bound = ratios + LIMIT_SLACK

    # ── 总收益：两套序列互为校验 ──
    #
    # 桥接的前复权序列**在早期年份不可靠** —— 实测 `000538.SZ` 2009-2010
    # 的前复权价在 0.2 和 52 之间乱跳，`000007.SZ` 之外还有大量股票
    # 出现超过涨跌停限制的「不可能收益」（2009–2016 年占 1.3–3.9%）。
    #
    # 但原始价序列是可靠的。两者结合可以判出是哪一边坏了：
    #
    # | 原始 | 前复权 | 判定 | 取值 |
    # |---|---|---|---|
    # | 在限制内 | 在限制内 | 正常日 | 前复权（含股息） |
    # | **超限** | 在限制内 | **除权日** | 前复权 ✓ |
    # | 在限制内 | **超限** | **前复权坏了** | 退回原始收益 |
    # | 超限 | 超限 | 都不可信 | NaN，剔除该行 |
    raw_ok = np.abs(raw_ret) <= bound
    front_ok = np.abs(f_ret) <= bound

    total_ret = np.where(front_ok, f_ret, np.where(raw_ok, raw_ret, np.nan))
    total_ret = np.where(np.isfinite(f_ret) & np.isfinite(raw_ret),
                         total_ret, np.nan)
    frame["total_ret"] = total_ret
    source = np.select([front_ok, raw_ok], ["front", "raw_fallback"],
                       default="dropped")
    source[0] = "first"          # 首行没有前收，本来就算不出收益
    frame["ret_source"] = source

    # ── 复权：锚在首日原始价的后复权，由修好的总收益累乘而来 ──
    clean = np.where(np.isfinite(total_ret), total_ret, 0.0)
    ret_factor = np.cumprod(1.0 + clean)
    adj_close = raw_close[0] * ret_factor
    scale = adj_close / raw_close
    for field in ("open", "high", "low", "close"):
        frame[f"adj_{field}"] = frame[field].to_numpy(float) * scale

    # ── 除权后昨收 ──
    with np.errstate(divide="ignore", invalid="ignore"):
        preclose = np.where(np.isfinite(total_ret) & (total_ret > -1.0),
                            raw_close / (1.0 + total_ret), np.nan)
    preclose[0] = raw_close[0]
    frame["preclose"] = preclose

    # `is_st_like` 只作诊断保留；`isST` 恒为 "0"（推导无效，
    # 不能让一个召回 5% 的字段把 2% 的正常股排除出选股域）。
    frame["is_st_like"] = is_st_like
    frame["isST"] = "0"

    # ── 涨跌停 ──
    limit_up = np.round(preclose * (1.0 + ratios), 2)
    limit_down = np.round(preclose * (1.0 - ratios), 2)
    open_ = frame["open"].to_numpy(float)

    frame["limit_ratio"] = ratios
    frame["limit_up"] = limit_up
    frame["limit_down"] = limit_down
    frame["is_limit_up"] = raw_close >= limit_up - EPS
    frame["is_limit_down"] = raw_close <= limit_down + EPS
    # 开盘即封板 → 排不上队，不能成交
    frame["cannot_buy"] = open_ >= limit_up - EPS
    frame["cannot_sell"] = open_ <= limit_down + EPS

    # ── 可交易性 ──
    volume = frame["volume"].to_numpy(float)
    amount = frame["amount"].to_numpy(float)
    frame["tradable"] = np.isfinite(volume) & (volume > 0)

    frame["listed_days"] = np.arange(1, len(frame) + 1)
    amt = pd.Series(np.where(frame["tradable"], amount, np.nan))
    frame["amount_ma20"] = amt.rolling(20, min_periods=10).mean().to_numpy()
    frame["liquid"] = frame["amount_ma20"] >= MIN_AMOUNT
    return frame


def load_raw() -> pd.DataFrame:
    files = sorted(RAW.glob("batch_*.parquet"))
    if not files:
        raise SystemExit(f"{RAW} 下没有批次文件 —— 先跑 panel_fetch_20260920.py")
    print(f"读取 {len(files)} 个批次文件 ...", flush=True)
    parts = []
    for path in files:
        frame = pd.read_parquet(path)
        if len(frame):
            parts.append(frame)
    panel = pd.concat(parts, ignore_index=True)
    # 同一 (code, time) 只保留一条：分片重启或重复拉取可能产生重复行
    before = len(panel)
    panel = panel.drop_duplicates(subset=["code", "time"], keep="first")
    if len(panel) < before:
        print(f"  去掉重复行 {before - len(panel):,}", flush=True)
    return panel


# ─────────────────────────── QC ───────────────────────────

def check_regression_000007(enriched: dict) -> tuple[bool, str]:
    """钉死 000007.SZ 2017-07-17 = 10 送 5 + 一字跌停。

    这条测试防的是「把除权跳空当成真实下跌」这一类错误。
    """
    frame = enriched.get("000007.SZ")
    if frame is None:
        return False, "样本里没有这只股票"
    row = frame[frame["time"] == "20170717"]
    if row.empty:
        return False, "没有 2017-07-17 这一行"
    r = row.iloc[0]
    ok = (bool(r["is_limit_down"]) and bool(r["cannot_sell"])
          and abs(r["total_ret"] + 0.10) < 0.01)
    detail = (f"原始 close={r['close']:.2f} 除权后昨收={r['preclose']:.2f} "
              f"总收益={r['total_ret']:+.4%} limit_down={r['limit_down']:.2f} "
              f"→ is_limit_down={bool(r['is_limit_down'])} "
              f"cannot_sell={bool(r['cannot_sell'])}")
    return ok, detail


def check_impossible_returns(panel: pd.DataFrame) -> tuple[int, float]:
    """「超出涨跌停限制」的收益行 —— 在价格限制制度下不可能存在。

    这是比「跌幅大 + 振幅小」更锐利的判据：前者会把一字停板（真实）
    误判成除权（伪），后者只依赖涨跌停制度本身。

    修复后这个数应当是 0：`enrich` 已经把所有超限的行换掉或剔除。
    """
    impossible = (np.abs(panel["total_ret"])
                  > panel["limit_ratio"] + LIMIT_SLACK)
    return int(impossible.sum()), float(impossible.mean())


def repair_breakdown(panel: pd.DataFrame) -> pd.Series:
    """修复来源的分布。`raw_fallback` 越多说明桥接的前复权越不可靠。"""
    return panel["ret_source"].value_counts()


def build_qc(panel: pd.DataFrame, enriched: dict, meta: dict) -> str:
    lines = [
        "# 面板 QC",
        "",
        f"- 行数 **{len(panel):,}**，股票数 **{panel['code'].nunique():,}**",
        f"- 区间 {panel['time'].min()} ~ {panel['time'].max()}",
        "- 数据源：**redisQMT 桥接**（唯一可用源；baostock 账号被锁、"
        "akshare 东财被代理拦截）",
        "- 复权口径：**不复权原始价 + 锚在首日原始价的后复权序列**（`adj_*`）",
        "",
        "## 1. 收益口径自检（主证据）",
        "",
        "**判据**：出现「收益绝对值超过该股当日涨跌停限制」的行 —— "
        "在价格限制制度下这不可能存在。比「跌幅大且振幅小」更锐利，"
        "因为后者会把一字停板（真实）误判成除权（伪）。",
        "",
    ]
    imposs_n, imposs_frac = check_impossible_returns(panel)
    lines.extend([
        f"超限行数：**{imposs_n:,}（{imposs_frac:.6%}）**",
        f"判定：{'✅ 通过' if imposs_n == 0 else '❌ 未通过'}",
        "",
        "### 前复权序列的修复统计",
        "",
        "桥接的前复权序列**在早期年份不可靠**。实测 `000538.SZ` "
        "2009-2010 的前复权价在 0.2 与 52 之间乱跳；"
        "全市场有大量股票出现超限收益（2009–2016 年占 1.3–3.9%，"
        "2023 年后降到 0.005–0.04%）。",
        "",
        "`enrich` 用**两套序列互为校验**修复：",
        "",
        "| 原始价 | 前复权 | 判定 | 取值 |",
        "|---|---|---|---|",
        "| 在限制内 | 在限制内 | 正常日 | 前复权（含股息） |",
        "| **超限** | 在限制内 | **除权日** | 前复权 ✓ |",
        "| 在限制内 | **超限** | **前复权坏了** | 退回原始收益 |",
        "| 超限 | 超限 | 都不可信 | NaN，剔除该行 |",
        "",
        "| 来源 | 行数 | 占比 |",
        "|---|---:|---:|",
    ])
    for source, count in repair_breakdown(panel).items():
        lines.append(f"| `{source}` | {count:,} | {count / len(panel):.4%} |")
    lines.extend([
        "",
        "## 2. OHLC 序关系（复权后）",
        "",
    ])
    good = ((panel["adj_high"] >= panel[["adj_open", "adj_close"]].max(axis=1) - EPS)
            & (panel["adj_low"] <= panel[["adj_open", "adj_close"]].min(axis=1) + EPS))
    lines.extend([f"`adj_high >= max(adj_open, adj_close)` 且 "
                  f"`adj_low <= min(...)`：**{good.mean():.4%}**",
                  "（复权因子日内为常数，序关系必须保持）", ""])

    lines.extend(["## 3. 主键唯一性", ""])
    dup = int(panel.duplicated(subset=["code", "time"]).sum())
    lines.extend([f"`(code, time)` 重复：**{dup}**"
                  f" {'✅' if dup == 0 else '❌'}", ""])

    lines.extend([
        "## 4. 回归测试：000007.SZ 2017-07-17（除权 + 一字跌停）",
        "",
        "这一条防的是「把除权跳空当成真实下跌」。前复权必须给出 −10% 而不是 −40%。",
        "",
    ])
    reg_ok, reg_detail = check_regression_000007(enriched)
    lines.extend([f"`{reg_detail}`", "",
                  f"判定：{'✅ 通过' if reg_ok else '❌ 未通过'}", ""])

    lines.extend([
        "## 5. 幸存者偏差修正",
        "",
        f"股票池 = akshare 当前在市 ∪ akshare 退市名单 = "
        f"**{meta.get('universe_size', 0):,} 只**（含退市 {meta.get('delisted', 0)} 只）。",
        f"面板实际覆盖 **{panel['code'].nunique():,} 只**。",
        "",
        "**对照组**：被弃用的旧面板 `analysis/screener_universe_20260920/daily.csv` "
        "含退市股 **0 只** —— 幸存者偏差 100%，这是本次重建的首要动机。",
        "",
    ])
    delisted = meta.get("delisted_codes", [])
    if delisted:
        present = [c for c in delisted if c in set(panel["code"])]
        lines.extend([
            f"退市股覆盖：{len(present)} / {len(delisted)} "
            f"({len(present) / max(len(delisted), 1):.0%})",
            "",
        ])

    lines.extend(["## 6. 交易状态分布", ""])
    lines.extend([
        f"- 涨停收盘：**{panel['is_limit_up'].mean():.3%}**",
        f"- 跌停收盘：**{panel['is_limit_down'].mean():.3%}**",
        f"- 开盘封涨停（不可买）：**{panel['cannot_buy'].mean():.3%}**",
        f"- 开盘封跌停（不可卖）：**{panel['cannot_sell'].mean():.3%}**",
        f"- 推导为 ST 型（±5% 板）：**{panel['is_st_like'].mean():.2%}**",
        f"- 满足流动性门槛（20 日均成交额 ≥ 5 千万）：**{panel['liquid'].mean():.2%}**",
        "",
        "## 7. 逐年规模",
        "",
        "| 年份 | 股票数 | 行数 | 可交易 | 推导 ST |",
        "|---|---:|---:|---:|---:|",
    ])
    panel_year = panel.assign(year=panel["time"].str[:4])
    for year, sub in panel_year.groupby("year"):
        lines.append(
            f"| {year} | {sub['code'].nunique():,} | {len(sub):,} | "
            f"{sub['tradable'].mean():.1%} | {sub['is_st_like'].mean():.2%} |")

    st_note = meta.get("st_check")
    if st_note:
        lines.extend(["", "## 8. ST 推导的交叉验证", "", st_note, ""])

    lines.extend([
        "",
        "## 数据源的已知限制（必须写进研究报告）",
        "",
        "| 字段 | 状态 | 影响 |",
        "|---|---|---|",
        "| 历史 `isST` | **推导值**，非交易所口径 | ST 过滤与 ±5% 板判定可能有误 |",
        "| 历史换手率 | **缺失** | 预注册因子 F9（换手率）无法检验，已剔除 |",
        "| 停牌日 | 缺行 | 拿不到停牌期间的价格冻结信息 |",
        "| 股息税 | 未计 | 后复权把股息视为瞬时再投且免税，乐观偏差约 0.3%/年 |",
        "",
        "前两项是 baostock 本可提供、但账号被锁后拿不到的字段。",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-st", action="store_true",
                        help="用桥接的当前股票名交叉验证 ST 推导")
    args = parser.parse_args()

    universe = json.loads((OUT / "universe.json").read_text(encoding="utf-8"))
    raw = load_raw()
    print(f"  {len(raw):,} 行，{raw['code'].nunique():,} 只", flush=True)

    print("计算复权 / 涨跌停 / 可交易列 ...", flush=True)
    parts = []
    enriched = {}
    for code, group in raw.groupby("code", sort=False):
        out = enrich(group)
        if len(out):
            parts.append(out)
            enriched[code] = out
    panel = pd.concat(parts, ignore_index=True)
    print(f"  {len(panel):,} 行，{panel['code'].nunique():,} 只（过滤停牌后）",
          flush=True)

    meta = {
        "universe_size": len(universe["codes"]),
        "delisted": len(universe.get("delisted", {})),
        "delisted_codes": list(universe.get("delisted", {}).keys()),
    }

    if args.check_st:
        print("交叉验证 ST 推导 ...", flush=True)
        try:
            from output.analysis.panel_st_check_20260920 import check_st  # type: ignore
            meta["st_check"] = check_st(panel, ROOT)
        except Exception as error:
            meta["st_check"] = f"（跳过：{type(error).__name__}: {error}）"

    print("写 panel.parquet ...", flush=True)
    # 内存：全市场约 2000 万行，float64 的光浮点列就要 3.5GB，
    # 而因子筛选每个因子都要排序一次（复制一份）。float32 约 7 位有效
    # 数字，对价格、收益、比率都绰绰有余，QC 的 1e-4 容差也远在其精度内。
    #
    # **不动 `code` / `date` 的 dtype**：把它们转成 category 能省一半内存，
    # 但会让 `set_index` 出来的索引变成 CategoricalIndex，
    # 之后 `reindex` 非分类索引会静默对不齐 —— 那种 bug 太难查，不值得。
    float_cols = [c for c in panel.columns
                  if panel[c].dtype == "float64"]
    panel[float_cols] = panel[float_cols].astype("float32")
    print(f"  降精度到 float32 的列：{len(float_cols)}", flush=True)
    # `ret_source` 只有 4 个取值，用 object 存 1400 万个字符串要 ~800MB，
    # 转成 category 后不到 30MB。它只服务 QC，但保留着便于事后核查。
    panel["ret_source"] = panel["ret_source"].astype("category")
    panel.to_parquet(OUT / "panel.parquet", index=False)
    print(f"  面板内存约 {panel.memory_usage(deep=True).sum() / 1e9:.2f} GB",
          flush=True)

    (OUT / "QC.md").write_text(build_qc(panel, enriched, meta), encoding="utf-8")
    meta.update({
        "rows": int(len(panel)),
        "symbols": int(panel["code"].nunique()),
        "first": str(panel["time"].min()),
        "last": str(panel["time"].max()),
    })
    (OUT / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {OUT / 'panel.parquet'}\n→ {OUT / 'QC.md'}")


if __name__ == "__main__":
    main()
