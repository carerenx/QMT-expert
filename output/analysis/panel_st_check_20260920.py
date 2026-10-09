"""交叉验证 ST 推导是否可信。

## 为什么需要这个检查

桥接拿不到历史 `isST`，`panel_build` 用「过去 60 日 |总收益| ≤ 5.5%」
推导 ST 型（主板 ST 是 ±5% 板）。这是推导值，必须验证它到底对不对，
否则 ST 过滤和 ±5% 板判定都是空的。

## 做法

桥接的 `get_instrument_detail` 给出**当前股票名**。名字里含 `ST` 的
就是当前 ST 股。这些股票在**样本末尾**必须被判为 `is_st_like=True`。

反过来，`is_st_like=True` 但名字不含 ST 的是**假阳性** ——
通常是长期极低波动的非 ST 股。

两个方向都要报，因为误差方向不同、影响也不同：
* 假阴性（ST 没被抓到）→ 会误买 ST 股，且涨跌停判成 ±10% 偏乐观
* 假阳性（非 ST 被抓到）→ 只是少买几只有效标的，偏保守
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def _names(data, codes: list[str], verbose: bool = False) -> dict[str, str]:
    """逐只取当前股票名。

    **必须用 `configure()` 返回的第二个对象（数据连接）** ——
    `get_instrument_detail` 在数据连接上，不是交易连接上。
    用错对象会静默返回空字典（`.get()` 拿到 None，被 `or {}` 吞掉）。
    """
    out = {}
    for i, code in enumerate(codes):
        try:
            detail = data.get_instrument_detail(code) or {}
        except Exception:
            continue
        name = detail.get("InstrumentName")
        if name:
            out[code] = str(name)
        if verbose and (i + 1) % 100 == 0:
            print(f"    {i + 1}/{len(codes)} 只，取到名字 {len(out)}", flush=True)
    return out


def check_st(panel: pd.DataFrame, root: Path, sample: int = 600) -> str:
    """`sample` 别调太大：`get_instrument_detail` 是**单只**调用，
    600 只已经约 3 分钟，1200 只就是 6 分钟。抽查的目的是估召回率，
    600 只的置信区间已经够窄。"""
    sys.path.insert(0, str(root / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure

    _, data = configure(account_id="8890145315", timeout_seconds=60)

    last_date = panel["time"].max()
    tail = panel[panel["time"] >= last_date]
    tail_codes = sorted(tail["code"].unique())

    # 只抽查一部分 —— get_instrument_detail 是单只调用，全量太慢
    import numpy as np
    rng = np.random.default_rng(20260920)
    if len(tail_codes) > sample:
        picked = sorted(rng.choice(tail_codes, size=sample, replace=False))
    else:
        picked = tail_codes

    names = _names(data, picked)
    if not names:
        return "（拿不到任何股票名，跳过）"

    flag = (tail[tail["code"].isin(names)]
            .groupby("code")["is_st_like"].last().to_dict())

    named_st = {c for c, n in names.items() if "ST" in n.upper()}
    flagged = {c for c, v in flag.items() if v}

    if not named_st:
        return (f"取到 {len(names)} 只股票名，其中**没有**名字含 ST 的 —— "
                f"无法做正向验证（可能当前市场确实无 ST，或接口返回的名称不含标记）")

    hit = named_st & flagged
    miss = named_st - flagged
    false_pos = {c for c in flagged if c not in named_st}

    lines = [
        f"抽查 {len(names)} 只股票名（样本末尾 {last_date} 的横截面）。",
        "",
        f"- 名字含 ST（当前 ST 股）：**{len(named_st)} 只**",
        f"- 其中被推导为 `is_st_like` 的：**{len(hit)} 只**"
        f"（召回 {len(hit) / len(named_st):.0%}）",
        f"- **假阴性**（是 ST 但没抓出来）：{len(miss)} 只"
        + ("  ← 会误买 ST 股" if miss else ""),
        f"- **假阳性**（抓成 ST 但名字不含 ST）：{len(false_pos)} 只"
        f"（占抽查 {len(false_pos) / len(flag):.1%}）"
        + ("  ← 只是少买，偏保守" if false_pos else ""),
        "",
    ]
    if miss:
        lines.append("假阴性样本：" + "、".join(sorted(miss)[:10]))
    if false_pos:
        lines.append("假阳性样本：" + "、".join(sorted(false_pos)[:10]))
    return "\n".join(lines)
