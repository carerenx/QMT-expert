# A144 v17 月度动量与商品趋势延续

入口：`A144_v17_MonthMomentumTrendContinuation.py`。这是独立策略文件，只导入共享core，不导入同级旧版本。

## 目标与当前范围

用户确认目标是：在原三个月回测口径上，收益率增加至少10个百分点。基准v12收益12.9002471%，验收目标22.9002471%，9万元期末资产至少110610.22元。

最终同口径结果：v17收益23.62571746%，期末111263.15元，增加10.72547036个百分点；0.3%不利滑点压力情景23.384255%。逐日完整选股流程、全部20笔成交、每日权益及独立费用现金核验均通过，相关59项测试通过。[完整报告](../../../output/analysis/a144_v13_optimization_20261104/report.md)及`acceptance.json`保留验收证据。本窗口目标已完成，独立样本外效果仍未验证。

## 三处变化

1. 商品ETF使用标准20交易日动量，代替60交易日动量，作为正动量资格和领先者排名；仍要求站上MA20、20日均成交额至少3000万元，商品新买仍要求指数弱。股票主策略、7只ETF池和回归ETF资格均不变。
2. 商品浮盈达到6%后，不立刻固定止盈，改为记录已完成分钟收盘峰值；收盘从峰值回落3%时触发退出，下一可执行分钟提交。4%入场成本硬止损保留；峰值和启动标志记入策略账本，可恢复。
3. 商品达到10交易日等价分钟持有期，在14:50后检查到期：仍为合格领先者且价格不低于前一完成日MA20时才继续持有，否则退出。最迟20日等价分钟到期退出。沿用原交易分钟年龄算法，不是自然日。

主策略仍要求中证500>=MA60、广度>=40%、广度5日改善至少5个百分点及原选股过滤；回归ETF的4%止损、2%止盈、MA5恢复净收益条件和5日到期全部保留。未采用资金预留、商品失去领先就立即退出等失败原型机制。

## 检查与实盘命令

在项目根目录、外部Python 3.10+环境执行：

```powershell
# 仅检查导入、账号与依赖；不连接服务、不启动策略
python run_bigqmt.py --bridge redis --strategy Stragety/RedisQMT/A144/A144_v17_MonthMomentumTrendContinuation.py --check

# 连接查询与生成计划，不下单
python run_bigqmt.py --bridge redis --strategy Stragety/RedisQMT/A144/A144_v17_MonthMomentumTrendContinuation.py --mode check --capital 90000

# 实盘：会提交买卖及撤单；本次研究未运行该命令
python run_bigqmt.py --bridge redis --strategy Stragety/RedisQMT/A144/A144_v17_MonthMomentumTrendContinuation.py --mode live --capital 90000
```

`--bridge pipe` 用于同一台Windows电脑的管道桥接。策略不选择传输。90000仅为示例投入，实盘可用现金不足时会被原有规则阻止。

独立状态默认是 `state/dual_regime_v17.json`，订单标签为`A144V17-…`，券商策略名为`A144V17`；保留v12默认状态与标签兼容。不能使用v12账本启动v17，不能改保存的哈希绕过检查，也不要让两个版本争用同一持仓。研究未迁移、删除或重置任何实盘状态。

## 回测与复现

固定区间2026-08-04—2026-11-03，60交易日，初始9万元空仓。数据为2026-11-04获取并核验的远程Redis QMT原始归档；逐会话240个分钟点、420个ETF会话/100800分钟均已核验。历史成分由官方带日期锚点和调整公告重建。

固定原版分钟撮合模型：前一完成分钟信号、下一分钟限价，前后分钟5%容量，0.1%不利滑点、真实价格区间约束与不利取整；每子单最低费用。挂单当分钟未成交部分视为撤单完成，未模拟排队/网络/撤单失败。保留旧模型是为了同口径比较，不代表真实券商成交。

```powershell
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant baseline
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation --full-market
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant baseline --slippage 0.003
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation --slippage 0.003
python -m Stragety.RedisQMT.A144.research.verify_optimization
```

研究脚本仅读取归档，不加载真实桥接客户端、不发单。快速对照只允许主策略/原入场函数不变且基准主策略候选为空的本窗口；完整流程会逐日重新计算509个代码的日线准备和所有选股信号。不能把这个固定窗口回放器用于其他区间或修改后的股票选股规则。

## 证据边界

这三个月同时用于开发和验收，属于样本内研究；所有七个候选及失败结果均保存，不把筛出的样本内结果称为样本外或普适改进。未验证新策略在上涨市场的股票收益能力，也未独立补齐公司行动公告/分红税。最大回撤按日终权益计算，不能解释为盘中最大回撤；期末未强制平仓。

新版最大日终回撤7.93%，高于原版6.73%，不存在“收益提高且风险不变”的结论。未标记最佳版本，也未自动启用实盘。
