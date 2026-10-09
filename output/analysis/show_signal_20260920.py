"""把 Portfolio 策略「如果冻结了」会输出的信号完整跑出来（**只读**）。

## 为什么需要这个脚本

`portfolio/frozen.py` 的 `FROZEN` 是 `None`，实盘脚本会**拒绝出信号** ——
这是设计正确的行为，用户已确认选择「不冻结」。

但「拒绝」不能回答「它到底会买什么」。本脚本在**内存里**挂配置，
走与实盘脚本**完全相同**的代码路径（`frozen.build_target_weights`），
把输出打出来。

> **这不是冻结。** `frozen.py` 文件不变，进程退出即消失。
> 下方输出**不构成买入建议** —— 这两个配置的研究结论都是「未通过」。

## 跑两个配置，因为答案取决于选哪个

| 配置 | 引擎选股贡献 | alpha t | 机制是否解释得通 |
|---|---:|---:|---|
| F7 特质波动率 s_n n=10 | +2.02%/年 | 0.34 | ✓ 尾部层对得上 |
| F10 隔夜 s_n n=30 | +13.93%/年 | 1.33 | ✗ 尾部层只支持 +0.4% |

**两个都是「未通过」**，给出两份是为了让你看到：
**换一个配置，要买的股票完全不同。** 这本身就是结论的一部分。

用法：
    python analysis/show_signal_20260920.py
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

CONFIGS = [
    ("F7_ivol", "size_neutral", 10, "机制自洽（尾部层对得上）"),
    ("F10_overnight_intraday", "size_neutral", 30, "引擎数字最好（机制对不上）"),
]


def base_config() -> dict:
    return {
        "factor": None,
        "treatment": None,
        "horizon_days": 21,
        "n_holdings": None,
        "rebalance_rule": "M",
        "buffer_entry": 0.20,
        "buffer_exit": 0.40,
        "weight_scheme": "equal",
        "min_listed_days": 250,
        "max_participation": 0.01,
        "timing_variant": None,
        "timing_index": "000300.SH",
        "selection": "未冻结 —— 本脚本仅供查看，不构成买入建议",
    }


def load_module():
    spec = importlib.util.spec_from_file_location("live_v2", SIGNAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    live = load_module()

    print("连接桥接 ...", flush=True)
    xtdata = live.connect_bridge()
    codes, names, dropped = live.csi500_main_universe(xtdata)
    print(f"股票池：中证500 ∩ 主板 ∩ 非ST = {len(codes)} 只"
          f"（剔除当前 ST {len(dropped)} 只）", flush=True)

    print(f"取日线（{len(codes)} 只 × 2 种复权）...", flush=True)
    raw = live.fetch_history(xtdata, codes)
    asof = str(raw["date"].max())
    raw = raw[raw["date"] <= asof]
    panel = live.build_live_panel(raw, names)
    print(f"  asof {asof}，{len(panel.long):,} 行，{len(panel.codes)} 只\n",
          flush=True)

    for key, treatment, n_holdings, note in CONFIGS:
        config = base_config()
        config.update({"factor": key, "treatment": treatment,
                       "n_holdings": n_holdings})
        frozen.FROZEN = config

        print("=" * 92)
        print(f"  配置：{key} / {treatment} / n={n_holdings}    （{note}）")
        print(f"  配置哈希 {frozen.config_hash()}")
        print("=" * 92, flush=True)

        print("  一致性闸门 ...", flush=True)
        live.assert_live_matches_research(panel, asof, key)

        weights = frozen.build_target_weights(panel, asof, {}, live.EQUITY,
                                              gross_exposure=1.0,
                                              config=config)
        # 展示用：列出等权目标股数。**注意 `target_order` 不在本脚本的
        # 职责内 —— 这里只是把目标权重换算成整手股数方便看。**
        print("\n" + "\n".join(
            live.render({"gross_exposure": 1.0, "weights": weights},
                        panel, asof, names, {})))

        section = panel.cross_section(asof)
        print(f"\n  这只/些股票凭什么入选（{key} 的截面排序前 {n_holdings}）：")
        alpha = frozen.build_alpha(panel, config)
        day = alpha[alpha["date"] == asof].set_index("code")["alpha"]
        eligible = panel.eligible(asof, min_listed_days=250,
                                  require_liquid=True,
                                  max_price=live.EQUITY / n_holdings / 100.0)
        day = day.reindex(eligible).dropna().sort_values(ascending=False)
        for rank, (code, value) in enumerate(day.head(n_holdings).items(), 1):
            # F7 取负（低波动得高分）；F10 是隔夜减日内
            print(f"    {rank:2d}. {code}  {names.get(code, ''):<8}"
                  f"分数 {value:+.3f}  收盘 "
                  f"{float(section['close'].get(code, float('nan'))):.2f}")
        print(flush=True)

    frozen.FROZEN = None
    print("（本脚本未修改 `portfolio/frozen.py`；上述输出不构成买入建议）")


if __name__ == "__main__":
    main()
