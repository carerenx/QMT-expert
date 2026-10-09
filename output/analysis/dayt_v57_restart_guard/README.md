# v57 RestartGuard：每日重启与盘中中断保护策略

## 交付

独立策略：`Stragety/MiniQMT_Stragety/DayT/DayT_v57_nomom_RestartGuard.py`。

沿用v56交易规则的独立源码副本，不导入同级策略。新恢复逻辑位于公共模块 `core/restart_guard.py`。旧v56、v40及其检查点不修改。注册名称为`v57_nomom`。

此版本是**安全暂停型研究策略**，不是已验收实盘版，也不是断线后自动补记未知成交的完整恢复系统。

## 新机制

- 使用独立`v57_<账户>.json`检查点，保存策略标识，不自动导入v49/v56状态。
- 保存资金、总持仓、可卖股份、订单和成交身份快照；未知或冲突成交身份停止。
- 重启连续读取两次券商快照。查询失败、两次结果不一致、记录不完整均停止。
- 同日恢复要求快照一致；跨日允许可卖股份增加和日订单/成交列表清空，不允许总持仓或现金变化、不认识的新成交。
- 在途意图、结果不明、缺少检查点、旧版本文件或未来日期均停止，不自动重发委托。
- 未通过准入不可提交交易意图；意图先强制保存，保存失败保留内存意图并向调用方抛错。
- 全部runner重建成功后才一次性启用，不启用半恢复的组合。
- 继承周期成交编号去重；真实检查点往返测试确认未平反T腿和周期数量未丢失。

Python设计技能用于分离恢复判定与交易逻辑，测试技能用于失败分支与检查点往返测试。QMT接口沿用已存在的公共连接基础设施，没有添加臆造的券商函数。

## 使用与限制

研究观察入口（会读取账户和行情，不发送真实委托）：

```powershell
python Stragety/MiniQMT_Stragety/DayT/DayT_v57_nomom_RestartGuard.py --mode signal
```

本次没有运行上述观察入口，避免擅自启动长期账户监控。`--mode live`明确抛错，未解除实盘限制。

signal模式不加载或写入交易检查点。因此不能用信号观察模式冒充完整重启恢复测试。首次交易基线登记/人工对账工具尚未实现，缺失检查点会停止；不要手工复制旧检查点或删除文件绕过保护。

断线期间发生真实成交、手工交易、资金划转或分红入账时，本版本会要求人工对账，而不是猜测其归属并自动继续。两次快照相同不保证券商查询具备原子性；连接重建、成交补记、故障注入长时间运行、进程互斥、稳定订单客户端标识等仍需进一步验收。

## 本轮验证

| 项目 | 结果 |
|---|---|
| v57恢复判定与集成测试 | 15项通过（含多场景子测试） |
| 公共撮合：下一事件、费用、现金、T+1等 | 4项通过 |
| 检查点保存重试与失败保留旧文件 | 3项通过 |
| 冻结DayT黄金回放 | 396次全部通过，成交和损益精确一致 |
| v57严格连续账户回测 | 未执行到收益阶段：冻结依赖哈希校验失败 |

严格回测命令：

```powershell
python backtest/dayt_strict.py --version v57_nomom --output analysis/dayt_v57_restart_guard/strict_continuous.json
```

错误为：`frozen dependency differs: Stragety/MiniQMT_Stragety/DayT/DayTradeing_v39_stragety_miniqmt.py`。

独立黄金回放读取冻结档案，而严格入口还校验工作区依赖，因此两者结果可以不同。没有更改旧v39、黄金哈希或关闭停止保护；不发布v57收益比较。解除此阻碍需在独立匹配环境中验收，不能覆盖用户旧策略。

复现测试：

```powershell
python -m unittest discover -s tests -p test_dayt_v57_restart_guard.py -v
python -m unittest discover -s tests -p test_dayt_exchange.py -v
python -m unittest discover -s tests -p test_dayt_checkpoint_retry.py -v
python backtest/dayt_benchmark.py replay
```

## 判定

新策略代码和安全暂停行为已实现并测试；不能宣称收益最优、严格连续验收通过或可无人值守实盘。下一阶段应完善明确的首次登记/对账流程及故障恢复模拟，再评估恢复能力，不应先解除live保护。
