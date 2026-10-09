"""冒烟测试：实盘脚本 v2 的管线能不能跑通（**不改 frozen.py**）。

`portfolio/frozen.py` 的 `FROZEN` 是 `None`，所以实盘脚本会拒绝出信号 ——
这是设计正确的行为。但「拒绝出信号」不能说明「管线是通的」，
本脚本在**内存里**临时挂一个配置来验证全链路：

    桥接 → 中证500 成分 → 主板/非ST 过滤 → 取日线 → 构造面板
         → 一致性闸门（因子秩相关）→ 目标权重 → 信号输出

**只读，不写 `portfolio/`，不改任何回测产物。**

用法：
    python analysis/smoke_live_v2_20260920.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import frozen                                    # noqa: E402

SIGNAL = (ROOT / "Stragety/RedisQMT/Portfolio"
          / "PortfolioSelectTiming_v2_Signal_csi500main.py")

# 用 F7（唯一机制自洽的配置）做冒烟。**这不是冻结** ——
# 只在本进程内存在，进程退出即消失，`frozen.py` 文件不变。
SMOKE_CONFIG = {
    "factor": "F7_ivol",
    "treatment": "size_neutral",
    "horizon_days": 21,
    "n_holdings": 10,
    "rebalance_rule": "M",
    "buffer_entry": 0.20,
    "buffer_exit": 0.40,
    "weight_scheme": "equal",
    "min_listed_days": 250,
    "max_participation": 0.01,
    "timing_variant": None,
    "timing_index": "000300.SH",
    "selection": "冒烟测试用，未冻结",
}


def load_module():
    spec = importlib.util.spec_from_file_location("live_v2", SIGNAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    live = load_module()

    print("1) 拒绝逻辑（FROZEN 为 None）...", flush=True)
    try:
        frozen.require_frozen()
        print("   [FAIL] 居然没报错")
    except RuntimeError:
        print("   [OK] 正确拒绝", flush=True)

    print("\n2) 在内存里挂配置（不改文件）...", flush=True)
    frozen.FROZEN = SMOKE_CONFIG
    print(f"   config_hash = {frozen.config_hash()}", flush=True)

    print("\n3) 桥接 + 股票池 ...", flush=True)
    xtdata = live.connect_bridge()
    codes, names, dropped = live.csi500_main_universe(xtdata)
    print(f"   {len(codes)} 只可用，剔除 ST {len(dropped)} 只", flush=True)
    assert len(codes) > 300, "股票池太小，桥接可能没给全"
    st_leak = [c for c in codes if live.is_st_name(names.get(c, ""))]
    assert not st_leak, f"ST 泄漏：{st_leak[:5]}"
    print("   [OK] 无 ST 泄漏", flush=True)

    print("\n4) 取日线 + 构造面板 ...", flush=True)
    raw = live.fetch_history(xtdata, codes)
    assert not raw.empty, "桥接返回空"
    asof = str(raw["date"].max())
    raw = raw[raw["date"] <= asof]
    panel = live.build_live_panel(raw, names)
    print(f"   asof {asof}，{len(panel.long):,} 行，{len(panel.codes)} 只",
          flush=True)

    print("\n5) 一致性闸门（因子秩相关）...", flush=True)
    live.assert_live_matches_research(panel, asof, SMOKE_CONFIG["factor"])

    print("\n6) 目标权重 ...", flush=True)
    weights = live.build_signal(panel, asof, {})["weights"]
    print(f"   {len(weights)} 只，权重和 {float(weights.sum()):.4f}", flush=True)
    assert abs(float(weights.sum()) - 1.0) < 0.02, "权重和不为 1"
    sample = panel.cross_section(asof)
    missing = [c for c in weights.index if c not in sample.index]
    assert not missing, f"权重里有当日无数据的代码：{missing[:5]}"
    print("   [OK] 权重落在当日横截面内", flush=True)

    print("\n7) 渲染 ...", flush=True)
    lines = live.render({"gross_exposure": 1.0, "weights": weights},
                        panel, asof, names, {})
    print("\n".join(lines[:14]), flush=True)

    frozen.FROZEN = None       # 还原
    print("\n全部通过。frozen.py 未被修改。", flush=True)


if __name__ == "__main__":
    main()
