"""收窄股票池：中证500 ∩ 主板 ∩ 非ST。

## 为什么这不是「换个筛选条件」

用户要求把择股范围收窄为「中证500 中的股票，非ST，非创业板科创板」。

中证500 成分按板块拆开是：沪主板 239 / 深主板 179 / 创业板 56 / 科创板 26。
剔除创业板与科创板后剩 **418 只**。

关键影响**不是少了 82 只**，而是：

> **±20% 的涨跌停板全部消失。**

研究里唯一走到引擎层的最强因子是 F6（MAX，回避彩票股）。
彩票股正住在创业板/科创板（±20% 板 + 散户投机）。
所以这个收窄会**改变因子的表现**，不能从原 500 只的结果直接继承。
本脚本只负责建池；因子是否还成立由后续的尾部检验与引擎扫描回答。

## 板块划分

| 前缀 | 板块 |
|---|---|
| 600/601/603/605 | 沪主板 |
| 000/001/002/003 | 深主板（002 中小板 2021-04-06 已并入主板） |
| 300/301 | 创业板 → **剔除** |
| 688/689 | 科创板 → **剔除** |

## ST 的两套口径（必须分清）

* **历史 ST**：面板的 `isST` 恒为 "0"（推导召回仅 5%，已证伪放弃）。
  所以**回测里没有建模历史 ST**，这是已知偏差。
* **当前 ST**：本脚本从桥接取 `InstrumentName`，剔除**今天**名字含
  ST/退 的标的。这是实盘信号必须做的（不能买今天的 ST），
  但它**带有轻微前视** —— 今天 ST 的股票三年前未必是 ST。

如实标注，不假装两者是一回事。

用法：
    python analysis/csi500_main_universe_20260920.py
    python analysis/csi500_main_universe_20260920.py --cache-only   # 只用已有名字缓存
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PANEL_DIR = ROOT / "analysis/panel_20260920"
CSI500 = PANEL_DIR / "csi500_universe.json"
OUT = PANEL_DIR / "csi500_main_universe.json"
NAME_CACHE = PANEL_DIR / "instrument_names.json"

ACCOUNT = "8890145315"

SH_MAIN = ("600", "601", "603", "605")
SZ_MAIN = ("000", "001", "002", "003")


def board_of(code: str) -> str:
    prefix = code[:3]
    if prefix in SH_MAIN:
        return "SH_MAIN"
    if prefix in SZ_MAIN:
        return "SZ_MAIN"
    if prefix in ("300", "301"):
        return "CHINEXT"
    if prefix in ("688", "689"):
        return "STAR"
    return "OTHER"


def load_names(codes: list[str], cache_only: bool) -> dict[str, str]:
    """取股票名。有缓存就用缓存，缺的才走桥接（单代码 RPC，所以很慢）。"""
    cached: dict[str, str] = {}
    if NAME_CACHE.exists():
        cached = json.loads(NAME_CACHE.read_text(encoding="utf-8"))

    missing = [c for c in codes if c not in cached]
    if not missing:
        print(f"  名字缓存命中 {len(cached)} 只", flush=True)
        return cached
    if cache_only:
        print(f"  [warn] --cache-only，但缺 {len(missing)} 只名字", flush=True)
        return cached

    print(f"  桥接取名字：{len(missing)} 只（单代码 RPC，约 "
          f"{len(missing) * 0.15:.0f} 秒）...", flush=True)
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure

    _, xtdata = configure(account_id=ACCOUNT, timeout_seconds=60)
    failed = []
    for i, code in enumerate(missing, 1):
        try:
            detail = xtdata.get_instrument_detail(code) or {}
            name = str(detail.get("InstrumentName") or "").strip()
            if name:
                cached[code] = name
            else:
                failed.append(code)
        except Exception as error:
            failed.append(code)
            if len(failed) <= 3:
                print(f"    [fail] {code}: {type(error).__name__}", flush=True)
        if i % 50 == 0:
            print(f"    {i}/{len(missing)}", flush=True)
            NAME_CACHE.write_text(
                json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8")

    NAME_CACHE.write_text(json.dumps(cached, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    if failed:
        print(f"  [warn] {len(failed)} 只取不到名字：{failed[:10]}", flush=True)
    return cached


def is_st_name(name: str) -> bool:
    upper = name.upper().replace(" ", "")
    return ("ST" in upper) or ("退" in name) or ("*" in name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args()

    payload = json.loads(CSI500.read_text(encoding="utf-8"))
    codes = list(payload["codes"])
    print(f"中证500 成分 {len(codes)} 只", flush=True)

    buckets: dict[str, list[str]] = {}
    for code in codes:
        buckets.setdefault(board_of(code), []).append(code)
    print("  板块分布：" + "，".join(
        f"{k} {len(v)}" for k, v in sorted(buckets.items())), flush=True)

    mainboard = sorted(buckets.get("SH_MAIN", []) + buckets.get("SZ_MAIN", []))
    print(f"  剔除创业板/科创板后 {len(mainboard)} 只", flush=True)

    names = load_names(mainboard, args.cache_only)
    st = sorted(c for c in mainboard if is_st_name(names.get(c, "")))
    if st:
        print(f"  当前名字含 ST/退 的 {len(st)} 只：", flush=True)
        for code in st:
            print(f"    {code} {names.get(code, '?')}", flush=True)
    clean = [c for c in mainboard if c not in set(st)]

    covered = [c for c in clean if c in set(payload["covered"])]
    print(f"  最终可研究 {len(clean)} 只，面板已覆盖 {len(covered)} 只", flush=True)

    OUT.write_text(json.dumps({
        "index": payload["index"],
        "asof": payload["asof"],
        "rule": "中证500 ∩ 主板(沪600/601/603/605、深000/001/002/003) ∩ 非当前ST",
        "parent_universe": str(CSI500.relative_to(ROOT)),
        "board_counts": {k: len(v) for k, v in sorted(buckets.items())},
        "excluded_st": st,
        "names": {c: names.get(c, "") for c in clean},
        "codes": clean,
        "covered": covered,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
