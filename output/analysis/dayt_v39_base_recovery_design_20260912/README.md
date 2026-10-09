# v39_nomom 底仓恢复买入机制设计

## 结论

为 v39 增加独立的“目标底仓恢复”通道，建议新版本命名为
`DayTradeing_v40_nomom_BaseRecovery.py`。恢复通道复用主策略正T的“下探触发—记录低点—反弹确认”买入机制，而不是开盘立即买入。它不以随后卖回获利为目的；成交股票直接补入底仓。

目标是修复每日重启场景中的致命路径：前一日反T卖出后未买回，次日新策略把0股当成新底仓，从此失去交易能力并永久空仓。

## 设计边界

- 初始目标底仓固定为200股，参数名 `TARGET_BASE_SHARES = 200`。
- 账户现金和持仓跨日连续，策略进程可以每日重新创建。
- 恢复买入优先级高于反T、正T和所有新开腿。
- 仅补足目标底仓，不允许借此把持仓增加到200股以上。
- 新增机制应放入完整的新策略文件，不能在 v39 原文件上增加新机制，也不能导入同级旧策略。
- 第一版保持 `MOM_ENABLED = False`，避免同时改变多个变量。

## 状态机

```text
启动/每日初始化
      |
      v
账户持仓 >= 200股 ------> NORMAL（运行原v39逻辑）
      |
      | 持仓缺口为整手且现金足够
      v
RECOVERY_PENDING
      |
      | 当前价 <= 主策略动态正T买入触发价
      v
RECOVERY_DIPPING（持续更新最低价）
      |
      | 从最低价反弹达到 BOUNCE_PCT
      v
RECOVERY_BUYING --未成交/部分成交--> RECOVERY_DIPPING
      |
      | 实际持仓达到200股
      v
RECOVERY_DONE ----------> 当日禁止新开反T腿，次日进入NORMAL
```

恢复完成当日不立刻卖出，是为了避免刚补回的股票受A股 T+1约束而不可卖，同时也避免同一价格波动被“补仓”和“反T开腿”重复消费。

## 核心规则

### 1. 每次启动按真实账户对账

```python
actual_shares = max(0, int(position.m_nVolume))
deficit = max(0, TARGET_BASE_SHARES - actual_shares)
recovery_shares = deficit // TRADE_LOT_SIZE * TRADE_LOT_SIZE
```

不能使用 v39 的 `base_shares` 判断缺口，因为这个值会在新实例初始化时被当前持仓覆盖，正是原问题的来源。

### 2. 恢复通道独占交易权

只要 `recovery_shares > 0`：

- `do_short = False`；
- `do_long = False`；
- MOM保持关闭；
- 不执行 `_handle_idle()` 中的普通反T/正T开腿；
- 只允许底仓恢复订单和相应的撤单、重试、成交确认。

这样可防止“账户缺200股，同时普通正T又买100股”的双重记账。

### 3. 买入数量由持仓和现金双重限制

```python
cash_lots = int(available_cash / (limit_price * TRADE_LOT_SIZE * 1.01))
needed_lots = recovery_shares // TRADE_LOT_SIZE
buy_lots = min(cash_lots, needed_lots)
buy_shares = buy_lots * TRADE_LOT_SIZE
```

其中1%是价格与费用缓冲。若不足一手，策略保持锁定并明确记录 `RECOVERY_BLOCKED_CASH`，不得假装恢复成功。

### 4. 使用主策略正T信号决定买入时机

恢复通道使用 v39 已有的正T买入触发价：

```python
buy_trigger_floor = round(open_price * (1.0 - cfg.BUY_TRIGGER_PCT), 2)
buy_trigger_trail = round(current_price * (1.0 - cfg.BUY_TRIGGER_TRAIL), 2)
buy_trigger = max(buy_trigger_floor, buy_trigger_trail)
```

具体时机如下：

1. 当价格首次小于或等于 `buy_trigger`，进入 `RECOVERY_DIPPING`，但不马上下单。
2. 继续记录 `recovery_dip_price`；只要价格创新低，就更新最低价。
3. 当价格从最低点反弹达到 `cfg.BOUNCE_PCT`，才确认买入。
4. 确认后与原版正T一致，使用 `COMPETE` 对手价方式提交买单。

这与主策略 `_handle_bt_dipping()` 的买入时机一致。不同之处仅在仓位语义和容量计算：

- 恢复买入不调用 `_paired_long_capacity()`，因为0股底仓时该函数会被“没有可卖底仓”条件阻止；
- 恢复数量由“200股目标缺口”和可用现金决定；
- 恢复成交直接增加 `base_shares`，不写入 `long_legs`，也不设置正T卖回目标。

### 5. 成交确认和重试

- 每次下单后必须用成交或持仓变化确认实际买入量。
- 部分成交只扣减实际成交数量，剩余缺口继续处于 `RECOVERY_DIPPING`。
- 超时不能清空恢复状态；价格再次满足反弹确认时，允许重新读取报价后重试。
- 单日最多3次恢复委托，防止异常行情或连接问题产生订单风暴。

QMT的指定股数交易接口为
`order_shares(stockcode, shares[, style, price], ContextInfo[, accId])`；正数股数表示买入，`COMPETE`表示对手价。[Python API, p.101]

### 6. 恢复来源保护

仅依赖“低于200股就自动买入”会把用户手工减仓误判为策略遗留。因此实盘版必须增加以下二选一保护：

1. **推荐：持久化恢复凭据。** 反T卖出实际成交后，原子保存证券、成交股数、成交价、日期和策略订单标识；买回后按实际成交量核销。每日重启只恢复有凭据的缺口。
2. **研究模式：固定目标底仓。** 明确配置 `ALLOW_TARGET_BASE_RECONCILIATION = True`，允许策略无条件恢复至200股。该模式适用于当前回测，但不应默认用于实盘。

如果无法读取或校验恢复凭据，实盘应停止新开腿并报警，不应依据持仓差额盲目买入。

## 建议参数

| 参数 | 初始值 | 含义 |
|---|---:|---|
| `TARGET_BASE_SHARES` | 200 | 目标底仓 |
| `BASE_RECOVERY_ENABLED` | `True` | 启用恢复通道 |
| `ALLOW_TARGET_BASE_RECONCILIATION` | 回测`True`、实盘`False` | 是否允许无凭据补足目标底仓 |
| `RECOVERY_MAX_ORDERS_PER_DAY` | 3 | 单日恢复订单上限 |
| `RECOVERY_FILL_TIMEOUT_SEC` | 8 | 单次成交确认超时 |
| `RECOVERY_CASH_BUFFER_PCT` | 1% | 现金容量缓冲 |
| `RECOVERY_COOLDOWN_SEC` | 15 | 两次重试间隔 |
| `NO_NEW_LEG_AFTER_RECOVERY` | `True` | 恢复完成当日不再开腿 |

## 复用正T信号，但不能直接运行普通正T交易

v39的普通正T是一笔需要随后卖回的盈利交易，而且其容量还受“可卖底仓”约束。账户已经是0股时，`calculate_execution_capacity()` 会同时得到0个可卖手数，普通正T因此仍被阻止。即使取消这个约束，正T也会把补底仓误认为待卖出的临时多头腿，无法解决底仓语义问题。

因此新机制只复用普通正T的触发价、探底和反弹确认，不复用其容量约束与卖回状态。底仓恢复必须是独立订单类型，并且成交后计入底仓，而不是写入 `long_legs`。

## 回测验收

新策略发布比较前应满足仓库固定约定：

1. 运行 `python backtest/dayt_benchmark.py replay`，确认冻结的 v39/v51 黄金结果没有变化。
2. 将 v40 通过 `backtest/dayt_registry.py` 注册为新策略，不覆盖旧版本。
3. 用100,000元现金和200股底仓，从2026-08-01至数据末日执行“每日重启策略、连续账户”回测。
4. 8月6日若仍卖出200股未买回，8月7日必须进入 `RECOVERY_PENDING`；只有价格满足主策略正T的下探与反弹确认后才能买入。
5. 任意时点持仓不得超过200股；恢复期间不得出现新的反T卖出或普通正T买入。
6. 部分成交、零成交、现金不足、报价缺失、保存失败和连接中断均要单独测试。
7. 期末必须同时报告现金、持仓市值、总资产、费用、最大回撤和相对持有超额净收益，不能只比较现金。

## 预期影响

该机制让 v39 在卖空底仓后的每个交易日继续寻找主策略正T买点，不会因为每日重启而永久关闭买入能力。它不会保证次日一定买回：如果价格始终没有满足正T的下探与反弹确认，策略仍会继续持有现金。这是严格遵守主策略正T买入机制的直接结果。

它大概率会改变本次上涨区间的最终资产，但具体收益必须由新增版本的严格回测给出，不能直接把 v53 的结果当作 v40 的结果。

历史回测和研究费用模型不代表实盘收益验证；该设计不授权启动实盘。
