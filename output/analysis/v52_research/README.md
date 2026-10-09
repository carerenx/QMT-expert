# v52回放产物索引

**本次交付结论只引用 `release/`。**

- `release/development/`：源码哈希一致的前69天有限消融及`selection.json`。
- `release/v39_0.0005.json`、`v51_0.0005.json`：修正盘前生命周期适配后的严格旧版结果；均停止/无效，不参与上线收益排名。
- `release/v52_*.json`：默认研究组合的连续99天回放及3/5/10bp敏感性。
- `release/fixed_v52.json`：黄金复现通过后运行的候选固定比较。
- `release/exports/`：25份逐笔、权益、委托、周期与未平明细CSV。

根目录其他JSON、`development_01/02/03`、`development_final/`及`final/`均为工程调试过程中保留的中间结果，可能使用盘前维护修正前的适配器，或在代码变化期间中止。即使目录名含`final`也不是本次交付基准，不可与`release`混合比较。

原v39/v51黄金档案始终位于`backtest/dayt_golden_20260910`，没有被这些调试产物覆盖。
