# 收益优先融合研究：数据验收未通过

> 后续修正与进展见 [FOLLOWUP.md](FOLLOWUP.md)：已定位分红查询日期语义问题并获取历史因子；首日行情重试仍未补齐。下文保留首轮审计记录，不代表最新因子获取状态。

研究区间：2025-09-12 至 2026-09-11。全部属于样本内研究。

当前没有有效收益排名，不代表策略无收益或已提升收益。

## 数据检查

| 标的 | 阻碍 |
|---|---|
| 600584.SH | 20250912: incomplete minute session；corporate action adjustment requires verified cash/share entitlement: 20250926,20260623 |
| 600105.SH | 20250912: incomplete minute session；corporate action adjustment requires verified cash/share entitlement: 20251014 |
| 601869.SH | 20250912: incomplete minute session；corporate action adjustment requires verified cash/share entitlement: 20260821 |

原始 CSV、逐日检查和 SHA256 见 `data_manifest.json`。Redis 分红因子为空，但前复权/不复权比例出现变化；不得把除权直接当作普通涨跌。

## 已实现范围

- 独立研究策略、四组日线决策、统一单账户下一分钟撮合入口。
- 现金、整手、可卖股份、T+1、部分成交、订单最低费用、独占订单预留、显式日终撤单。
- 两次重定价限制；不采用同分钟成交或15:00强行平仓。
- 分钟/日终净值、损益恒等式、订单和成交记录。

## 尚未完成的验收与交付

- 完整起始日分钟数据及可核验的分红、送转与除权权益处理。
- 四组有效收益、双基准、逐笔归因、固定数量执行对照、阶段与成本敏感性结果。
- 持久化重启恢复、全面停牌/涨跌停识别、成交量单位独立核验。
- 波段状态目前按信号维护，尚需与实际减仓成交同步后才能作为验收版本。
- 注册表仅登记研究入口，未适配旧版日重置接口；不得声称通过严格连续策略注册验收。

## 本次验证记录（2026-09-14）

- DayT 黄金回放：396 次独立日回放全部通过，成交及损益精确一致。
- 新增撮合测试 4 项、既有 v2 测试 6 项全部通过；这不是全面安全验收或收益验证。
- 三只股票起始日均仅164根分钟线，首根10:47，末根15:00。

## 复现

```powershell
python -m unittest tests.test_profit_priority -v
python backtest/dayt_benchmark.py replay
python backtest/profit_priority_research.py --output analysis/profit_priority_fusion_20260914 --cached
```

缓存读取先核验 SHA256。数据无效时阻断收益发布。未启动任何实盘委托。
