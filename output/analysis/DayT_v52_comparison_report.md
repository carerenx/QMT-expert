# v52 固定比较与连续账户研究报告

## 结论

**没有选出上线候选，v52只交付研究/信号观察版，实盘入口关闭。**

固定比较改善不代表连续账户收益改善。默认组合在5bp研究费率下的连续相对持有净增量为 **-14,608.00元**，最大回撤 **21.97%**。
v39/v51在本严格路径中跨日停止/无效，不能作为完整99天正常运行的合格基线。测试通过不是净收益验收。

## 1. 冻结的每日独立比较

99天，每天200股+10万元，盘中分钟收盘撮合，费用未扣入；黄金原源码/公共依赖在zip中隔离复现。完整396例黄金复现已通过。

| 策略 | 滑点/元 | 相对持有毛增量/元 | 成交次数 |
| --- | --- | --- | --- |
| v39 | 0.0 | -11,531.00 | 392 |
| v39 | 0.01 | -11,836.00 | 392 |
| v51 | 0.0 | -14,042.00 | 366 |
| v51 | 0.01 | -15,449.00 | 366 |
| v52 | 0.0 | -1,044.00 | 265 |
| v52 | 0.01 | -1,088.00 | 265 |

每天重置会消除未回补周期对后续交易的影响，并重新提供现金、可卖底仓；此模型仅用于延续旧版本比较，不能当成可连续实现的收益。

## 2. 严格连续账户，主假设5bp

| 策略 | 运行状态 | 账户净变动/元 | 相对持有净增量/元 | 连续最大回撤 | 完成周期 | 未平绝对数量峰值/股 |
| --- | --- | --- | --- | --- | --- | --- |
| v39 | 停止/无效 | 2,132.22 | -9,555.78 | 2.82% | 2 | 200 |
| v51 | 停止/无效 | 2,952.81 | -8,735.19 | 2.82% | 2 | 200 |
| v52 | 完成回放 | -2,920.00 | -14,608.00 | 21.97% | 63 | 100 |

旧版停止后不再运行策略，剩余持仓仍估值至期末；停止结果不是正常运行的收益排名，其较小回撤也不是胜出的证据。

停止原因：

- v39：INVALID: legacy daily reset would discard open execution legs at 20260423
- v51：STOPPED: original portfolio rollover: STATE-BLOCKED: 601869.SH record-date=20260422 expected=20260423; overnight/uninitialized T legs require reconciliation

适配器包含盘前维护事件，只提供昨日已知数据；v51执行原组合调度器换日检查。委托最早在下一个OPEN事件成交，等待期间只推进市场与成交事件，不向策略提前返回未来开盘价。

## 3. 前69天有限消融

| 实验 | 状态 | 相对持有净增量/元 | 最大回撤 | 完整周期 | 未平数量峰值/股 |
| --- | --- | --- | --- | --- | --- |
| combined_0 | 完成 | 131.03 | 21.97% | 50 | 100 |
| combined_0.2 | 完成 | 131.03 | 21.97% | 50 | 100 |
| combined_0.4 | 完成 | 92.51 | 21.99% | 50 | 100 |
| direction_0 | 停止/无效 | 11,518.92 | 17.41% | 1 | 100 |
| direction_0.2 | 停止/无效 | 11,518.92 | 17.41% | 1 | 100 |
| direction_0.4 | 停止/无效 | 11,518.92 | 17.41% | 1 | 100 |
| long_disabled | 完成 | 1,193.29 | 21.87% | 50 | 100 |
| overnight_only | 完成 | -1,241.75 | 22.44% | 63 | 100 |
| v39 | 停止/无效 | 25,060.22 | 2.82% | 2 | 200 |
| v51 | 停止/无效 | 25,880.81 | 2.82% | 2 | 200 |

`direction_*`只加方向准入，`overnight_only`只允许隔夜，`combined_*`组合两者，`long_disabled`真实重跑关闭正向后的资金路径；不是从旧表减掉亏损方向。

筛选记录：`selected=None`。原因：No eligible candidate; invalid baseline or risk constraints failed。
默认组合实际配置：`{'DIRECTIONAL_THRESHOLD': 0.2, 'DIRECTIONAL_ENABLED': True, 'OVERNIGHT_ENABLED': True, 'LONG_RESEARCH_DISABLED': False}`。
所有阈值只使用0、0.20、0.40；没有扩大搜索。由于连续基线无效，不发布“最优上线策略”。

## 4. 后30天诊断及费用敏感性

仅对预先指定默认组合0.20做完整连续路径诊断，不用后30天调整参数。后30天继承前69天的真实现金、持仓和未平腿，不在边界重新开户。

- 后30天相对持有净增量：-14,739.03元。
- 后30天完成周期：13。样本不足：不足30个完整周期。
- 99天此前已被观察，本次后30天不是严格样本外；真正新增样本数仍为0。

| 假设单边费率/bp | 总费用/元 | v52相对持有净增量/元 | 最大回撤 |
| --- | --- | --- | --- |
| 3 | 1,391.40 | -13,680.40 | 21.77% |
| 5 | 2,319.00 | -14,608.00 | 21.97% |
| 10 | 4,638.01 | -16,927.00 | 22.49% |

费用逐笔扣现金，最低5元按委托累计；以上是研究假设，不能冒充账户真实佣金和税费。

## 5. 未完成风险及执行明细

| 方向 | 开仓记录时间 | 成交均价 | 剩余数量 | 期末估值价 | 未实现毛损益/元 |
| --- | --- | --- | --- | --- | --- |
| SHORT | 2026-08-25T09:59:00 | 365.84 | 100 | 435.74 | -6,990.00 |

最长周期持有：23个样本交易日。资金占用峰值（多头开仓成本+空头按成交价/现价较大者计价，不含额外ATR预留）：59,985.00元。
每个反向周期在策略中另有ATR及费用预留；其ATR、费用、剩余数量与归属在JSON的`owned_cycle_records`中。期末未完成周期没有从收益表删除。

未成交原因事件计数（同一委托可多次出现，不是独立拒单数量）：`{'VOLUME_CAP': 50, 'LIMIT_NOT_MET': 37, 'NON_TRADABLE': 12}`。

逐笔成交、费用、连续权益、未平仓、委托及周期已导出到`analysis/v52_research/release/exports`；原始JSON保留全部配置、日志尾部与哈希。

## 6. 验证范围与上线边界

- 当前单元/回归测试186项通过，覆盖原有成交、日志、新鲜度、保存重试/恢复，以及新增事件撮合、双周期额度、阶梯归属、退出优先、资金预留、分钟失效、已确认部分成交恢复、换日和实盘入口拒绝。
- 默认signal；live入口直接拒绝。本次未启动实盘，也未修改账户检查点。
- 旧检查点仅允许账户核对一致的平坦账本只读导入；旧未平腿缺少唯一订单归属时拒绝。未决/不确定订单保留文件并要求核对，不宣称已经验证全自动崩溃中委托恢复。
- 已知暂停交易状态暂停该股，依据本地QMT文档证券状态表[Python API, p.171]；未知状态不能据此判定交易正常。报价时间过期仍仅告警，不新增以报价年龄为依据的停单条件。
- 分钟模拟不能保证排队成交、识别所有板块的涨跌停制度、还原盘中停复牌及全部公司行动。1%容量以已实现分钟量为撮合假设，不是实时队列证据。分钟采样下的tick聚合观察存在保守确认延迟。
- 本样本底仓200股，50%绝对敞口上限只有100股，因此历史中不能同时容纳两手未平T；双周期互不抵消主要由更大底仓的合成测试覆盖，不能宣称历史已验证“双周期提高收益”。
- 尚无5日影子观察、20个新增交易日/30个新增完整周期，也未完成独立双轨代码审查。暂不具备上线验收条件。

## 7. 复现与哈希

固定流程见`docs/DayT_fixed_benchmark.md`。本报告仅使用`analysis/v52_research/release`，早期草稿和适配器修正前结果不参与结论；没有覆盖黄金数据或旧报告。

- `Stragety/MiniQMT_Stragety/DayT/DayTradeing_v39_stragety_miniqmt.py`：`6bcfe76949fb7b10ce0945a19fcb7a5bfc1e0fe5be02d94d760b40ee64f42d70`
- `Stragety/MiniQMT_Stragety/DayT/DayT_v51_IntradayStrength.py`：`f4658f89d8199fe5231d4e322d61c25dc0592ee8c211858216c09780c4b1fa95`
- `Stragety/MiniQMT_Stragety/core/adaptive_linear_regime.py`：`de3b1dc11aa3b847f49556b22e83c17fa2002db9f6b5b8ba9b5a41faff51006d`
- `Stragety/MiniQMT_Stragety/core/atr_reentry.py`：`8c5a0f9e78882a40287f68b2ab79ed15cf6855a50a28e53ab71a0a5bca486ba9`
- `Stragety/MiniQMT_Stragety/core/config.py`：`c2d2d25cd3b1a07dc34b97405e7e4d93cf490ddf9fd63c5e9184d71851c4144e`
- `Stragety/MiniQMT_Stragety/core/connection_monitor.py`：`1c778307d2430f84b086c0e8096e18e956f49e1d87ff6da610d52e6139b5a533`
- `Stragety/MiniQMT_Stragety/core/dayt_checkpoint.py`：`164502807575b80a4e881070dddcfe365d7a0ea144a445c60214749f3e7b4f42`
- `Stragety/MiniQMT_Stragety/core/execution_book.py`：`0401cb3a1ecebf0575439073490350b24a3e78fec0f23bbdea03b929799e136d`
- `Stragety/MiniQMT_Stragety/core/indicators.py`：`d5af9dd936cf50ae2515e28cf8f7204311271097d48e6ebdcb7f1119d4898932`
- `Stragety/MiniQMT_Stragety/core/indicators_np.py`：`59d96dac40e22579266b5880a385b7ab1a245e933d06d65be6aa5ea33f3f8303`
- `Stragety/MiniQMT_Stragety/core/intraday_rebound.py`：`17e55408249c043a57c101b1613c82cb48f13cd2cddba9d7ce790266419e44f8`
- `Stragety/MiniQMT_Stragety/core/intraday_strength.py`：`7fe48953ea18a88f099fbdf81eecab349098bea4c52da7dc46c7e3d784577a7d`
- `Stragety/MiniQMT_Stragety/core/quantile_trend_regime.py`：`691223c9e49f22bfc9a8eda24002cfd77d4d668fb44d02a7a1b689ff8c6ab58c`
- `Stragety/MiniQMT_Stragety/core/runtime_watchdog.py`：`f4e04789e6cd9b8596d334e17e49dc3eec24eef1daeb119b178dc7d783b05929`
- `Stragety/MiniQMT_Stragety/core/signals.py`：`91f70f97cb58abb7096ee033e95f14e277c0b2df5192149175eefbe25d086ee8`
- `Stragety/MiniQMT_Stragety/core/t_position_size.py`：`0de0f6f20e2f08301d6c61779dda08b42d981211e8004d178cfb64568b6134dc`
- `Stragety/MiniQMT_Stragety/core/__init__.py`：`e821d39c8e5bd4d46d444f3a654240af697a13b593036c38105f16e793a19d84`
- `Stragety/MiniQMT_Stragety/DayT/infra/connector.py`：`95c06f6e5f66b72f6c8275c1f4dcf481f6c865c6bcdbdc6e109a626e8f169498`
- `Stragety/MiniQMT_Stragety/DayT/infra/logger.py`：`7dabe1a20b8f050004cace8289459ae9d551264ca8854592ff086b3cc6d92b3f`
- `Stragety/MiniQMT_Stragety/DayT/infra/__init__.py`：`13e876959c6a452ef17d777668eaf12b1f30288b7eeb529e36ac13afa117d171`
- `analysis/v51_v39_minute/1m.csv`：`e08d169a0d9c3a5cb585375db2d223a974eaf088afe18184b8b408977cc9c11c`
- `analysis/v51_v39_minute/1d.csv`：`c1074bc308adee34d1b7eb04196e4bded40e7e5a7cc8a7ed62390a363a8f2faa`
- `backtest/dayt_strict.py`：`3c943019bde0818f981f38aac1298d0711f5cdbfd2dbb8ae4c6e95cf098bcca6`
- `backtest/dayt_exchange.py`：`fda9f4eee5bd41d9ab73de60d1ebffebe2b178b3ad88bdb1e3a3274d76d4bef8`
- `analysis/compare_v51_v39_minute.py`：`6a0fbc0e633441adaef8a48edb6fdd4d474961e3a6e847a50d8d70122e3066b9`
- `Stragety/MiniQMT_Stragety/core/directional_overnight.py`：`6f71a4cdd5c571ccf88b573ddba8c980248df7a68fa54b5a43d359fde5b3dbf6`
- `Stragety/MiniQMT_Stragety/DayT/DayT_v52_DirectionalOvernight.py`：`df3a1d7fcdff57f06068677872b44f4eee200428a8b0bb2ddaf36c3e2c12ebd4`
