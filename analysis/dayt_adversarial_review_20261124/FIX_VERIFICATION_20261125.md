# v0628执行修复验证（2026-11-25）

修改对象：`Stragety/MiniQMT_Stragety/DayT/DayT_v0628_NetCostRiskBudgetDayT.py`。修复原有执行机制，不新建策略版本。所有故障均在离线替代行情/券商回包中验证；未连接或重启实盘、未下真实订单。

| 审查项 | 修复后的行为与证据 |
| --- | --- |
| F1 旧/未来行情 | 同日旧11秒/4小时、未来3秒/30分钟、缺失/损坏/冲突时间戳均拒绝；新鲜时间戳可用。重复/倒退时间戳不累计确认。提交前再次校验，覆盖计算期间报价过期。原对手价路径使用新鲜盘口/最新价的FIX限价，按拟报价做数量和准入检查。 |
| F2 成交后异常 | 真实持仓适配收到`available='malformed'`时仍返回已知FILLED，账本只记录一次；同一订单重入不重复扣费。双账本副本校验失败时两者都不发布并锁定账户。日志故障不改变确认结果。 |
| F3 矛盾订单 | 状态56但数量0/不足、超量、废单有成交、终态成交价格0及身份错配均锁定并抛出异常，不回退TIMEOUT。合法零成交撤单及部分撤单有正向控制。 |
| F4 零股残余 | 股票/ETF不足一手报`CLOSE-RESIDUAL-MANUAL`，账户阻止新增T/建仓；150股可闭合100并保留真实50股。不会超买或虚记完成；不足一手自动闭合不在本次解决范围，需人工核对处理。 |
| F5 最低费用 | 现金175.20元、价格1.75不再允许100份；现金181元允许。预算包含模型最低佣金及0.1%缓冲，仍须券商实际费用对账。 |

## 验证

`tests/test_dayt_net_risk.py`从30项增加到55项；与其余相关测试合计**151 passed**。

```powershell
python -m pytest tests/test_dayt_net_risk.py tests/test_dayt_etf_adaptation.py tests/test_dayt_sell_confirmation_line.py tests/test_dayt_buyback_tightening.py tests/test_dayt_connector_callback.py tests/test_dayt_seed_position.py tests/test_ma5_ma20_seed_selection.py tests/test_dayt_regime_minute_lab.py tests/test_dayt_cost_space_lab.py tests/test_dayt_empirical_edge_lab.py -q
python run_bigqmt.py --bridge pipe --strategy Stragety/MiniQMT_Stragety/DayT/DayT_v0628_NetCostRiskBudgetDayT.py --check
git diff --check
```

本地检查返回`local setup OK; server connectivity has NOT been checked`；不能据此声称实盘连接或收益已验证。新增反例先在原源码上运行并观察失败，原等待重试路径会拖延测试，随后中断该轮；完成修复后上述测试完整通过。

跨日初始化、前日重置、checkpoint及原平腿状态方法保留AST一致性检查；不新增止损或超时退出。现金/费用及风险预算仍为估计，不保证实际最大亏损。FIX可能降低成交率；10秒/2秒时效阈值可能拒绝延迟行情。人工残余和持仓对账锁不自动解除，不应以随意重启绕过。进程内幂等缓存不提供重启恢复保证。

## 原始证据

原故障源码保存为`source_before_fix.py`，SHA256：`e59e89a085047780531075831a65c2c0366c405cd9f82642649253f509666d0b`，与原`evidence.json`一致。历史`probe.py`指向该快照，保留原始失败证据；修复验证使用上面的pytest命令。

修复后策略SHA256：`d69fa7070911cc2992d71379574e07397eed8f6ee5338fd23cad89f7383a32ab`。
