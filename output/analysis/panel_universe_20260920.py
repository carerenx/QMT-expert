"""重建「历史上真实存在过」的 A 股股票池 —— 修正幸存者偏差。

## 为什么必须做这件事

`analysis/screener_fetch_universe_20260920.py` 用的是 QMT 桥接的
`get_stock_list_in_sector("沪深A股")`，那是**当前**成分股。实测结果：
5129 只股票里**没有任何一只**最后交易日早于 2026-09-18。
退市股全部缺失，而退市恰恰集中在绩差小盘股 —— 任何「选小盘 / 选活跃」
的策略都会被系统性高估。

## 股票池 = 当前在市 ∪ 已退市

只有这两块。一只股票要么现在还在交易，要么已经退市，没有第三种。
所以：

    universe = akshare 当前 A 股全表
             ∪ akshare 沪市退市名单
             ∪ akshare 深市退市名单

## 为什么不用 baostock 的 query_all_stock

原方案想用 `bs.query_all_stock(day)` 按月采样重建历史在市名单 ——
它确实能返回「该日在市」的证券（实测 `2015-06-30` 含 2015-12 才退市的
sz.000024）。但**实测该接口在本环境会卡死**：单次调用消耗 232 秒 CPU
后仍未返回，200 次采样不可行。

好在「当前在市 ∪ 已退市」在集合论上就是完整股票池，
`query_all_stock` 提供的「逐年名单」只是额外的覆盖率度量，不是必需品。
逐年的覆盖率改由面板自身推导（见 `panel_build_20260920.py` 的 QC）。

## 输出

    analysis/panel_20260920/universe.json
        codes      -- 并集，排好序，`600000.SH` 口径
        by_source  -- 各来源贡献只数
        delisted   -- 退市股及其终止上市日期（QC 要用）

用法：
    python analysis/panel_universe_20260920.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


# Windows 控制台默认 GBK，编不了 emoji。中文本身没问题，所以只需要
# 让 stdout 对无法编码的字符降级而不是抛异常。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/panel_20260920"

# 旧面板的股票名单，作为独立的交叉校验（不是数据源）
LEGACY_META = ROOT / "analysis/screener_universe_20260920/meta.json"

START = "2010-01-01"
END = "2026-09-18"


def is_common_stock(code: str) -> bool:
    """沪深 A 股正股，排除指数、ETF、基金、债券、北交所。

    按**交易所 + 号段**双重限定：
    * `.SH` 只可能是 600/601/603/605/688/689
    * `.SZ` 只可能是 000/001/002/003/300/301

    不能只看数字前缀 —— 上证指数系列也是 `sh.000xxx`
    （000001 上证综指、000300 沪深300、000905 中证500），
    用「00 开头」筛会把它们全部放进来。

    北交所（8/4 开头）排除：±30% 板，与主板不可比。
    """
    num, _, market = code.partition(".")
    if len(num) != 6 or not num.isdigit():
        return False
    if market == "SH":
        return num.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "SZ":
        return num.startswith(("000", "001", "002", "003", "300", "301"))
    return False


def to_qmt_code(raw: str) -> str | None:
    """6 位数字代码 → `600000.SH` 口径。不是正股就返回 None。"""
    num = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(num) != 6:
        return None
    if num.startswith(("600", "601", "603", "605", "688", "689")):
        code = f"{num}.SH"
    elif num.startswith(("000", "001", "002", "003", "300", "301")):
        code = f"{num}.SZ"
    else:
        return None
    return code if is_common_stock(code) else None


def source_current() -> set[str]:
    """akshare 当前的沪深 A 股全表 —— 在市的那一半。"""
    import akshare as ak
    frame = ak.stock_info_a_code_name()
    col = next(c for c in frame.columns
               if "code" in str(c).lower() or "代码" in str(c))
    out = {c for c in (to_qmt_code(v) for v in frame[col]) if c}
    print(f"  akshare 当前 A 股: {len(out)} 只", flush=True)
    return out


def source_delisted() -> dict[str, str]:
    """akshare 退市名单 —— 已退市的那一半。

    返回 `{code: 终止上市日期}`。日期用于 QC（确认这些股票在面板里
    确实终止于那个日期附近，而不是被当成还在交易）。
    """
    import akshare as ak

    out: dict[str, str] = {}
    for label, fetch in (
        ("沪市退市", lambda: ak.stock_info_sh_delist()),
        ("深市退市", lambda: ak.stock_info_sz_delist(symbol="终止上市公司")),
    ):
        try:
            frame = fetch()
        except Exception as error:
            print(f"  [warn] {label} 失败: {type(error).__name__}: {error}",
                  flush=True)
            continue
        code_col = next((c for c in frame.columns
                         if "代码" in str(c) or "code" in str(c).lower()), None)
        date_col = next((c for c in frame.columns
                         if "终止" in str(c) or "暂停" in str(c)), None)
        if code_col is None:
            print(f"  [warn] {label} 找不到代码列: {list(frame.columns)}",
                  flush=True)
            continue
        added = 0
        for _, row in frame.iterrows():
            code = to_qmt_code(row[code_col])
            if code is None:
                continue
            out[code] = str(row[date_col])[:10] if date_col else ""
            added += 1
        print(f"  {label}: {added} 只", flush=True)
    return out


def main() -> None:
    print("[1/3] 取当前在市 A 股 ...", flush=True)
    current = source_current()

    print("[2/3] 取退市名单 ...", flush=True)
    delisted = source_delisted()

    print("[3/3] 合并 ...", flush=True)
    union = current | set(delisted)

    # 与旧面板交叉校验：旧面板是当前在市的一个子集，
    # 差异说明两个「当前在市」口径不完全一致，要如实报告。
    legacy_only: list[str] = []
    if LEGACY_META.exists():
        legacy = set(json.loads(LEGACY_META.read_text(encoding="utf-8"))["codes"])
        legacy_only = sorted(legacy - current)
        print(f"  旧面板 {len(legacy)} 只，其中不在 akshare 当前表的 "
              f"{len(legacy_only)} 只", flush=True)
        union |= legacy          # 两边口径不同的部分都保留，宁可多拉

    if not union:
        raise SystemExit("股票池为空 —— 数据源全部失败")

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "start": START,
        "end": END,
        "codes": sorted(union),
        "by_source": {
            "akshare_current": len(current),
            "akshare_delisted": len(delisted),
            "legacy_only_not_in_current": len(legacy_only),
            "union": len(union),
        },
        "delisted": delisted,
    }
    path = OUT / "universe.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    delisted_in_legacy = len(set(delisted) & set(
        json.loads(LEGACY_META.read_text(encoding="utf-8"))["codes"]
    )) if LEGACY_META.exists() else 0

    print()
    print(f"股票池 {len(union)} 只 → {path}")
    print(f"  当前在市（akshare） {len(current):>6}")
    print(f"  已退市（akshare）   {len(delisted):>6}")
    print(f"  仅见于旧面板        {len(legacy_only):>6}")
    print()
    print(f"[!] 旧面板含退市股 {delisted_in_legacy} 只 —— "
          f"若为 0，则幸存者偏差确认为 100%")
    print()
    print("退市股样本（用于后续 QC 核对终止日期）:")
    for code, date in sorted(delisted.items())[:5]:
        print(f"  {code}  终止 {date}")


if __name__ == "__main__":
    main()
