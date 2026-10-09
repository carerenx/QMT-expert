# 601869 v42 自适应线性反T机制说明

生成日期：2026-09-07  
适用策略：`Stragety/MiniQMT_Stragety/DayT/DayT_v42_AdaptiveMarketStrength.py`  
核心模型：`Stragety/MiniQMT_Stragety/core/adaptive_linear_regime.py`

## 1. 它解决什么问题

旧反T阈值采用固定的 ATR 乘数和缩放系数，例如：

```text
sell = open × (1 + ATR% × mult × scale)
```

问题是固定 `mult` 和 `scale` 无法随近期走势变化：在连续下跌、但均线仍偏多的阶段，容易把卖出启动价设得过远；反之，强趋势上涨时又可能过早卖出。

v42 将固定的 `mult × scale` 替换为 `linear_units`：

```text
sell = open × (1 + ATR% × linear_units)
```

`linear_units` 由最近完成日线自动学习，而不是手工固定为某个数值。它代表“按当前市场特征估计，当日从开盘价向上可较容易触达的幅度，折算为 ATR 的倍数”。

## 2. 数据边界与前瞻性

模型只使用**已完成日线**。当日开盘前的输入不包含当日最高、最低、收盘和成交量。

训练触发价模型时采用如下映射：

```text
第 i 日收盘后的特征  →  第 i+1 日的 (最高价 / 开盘价 - 1)
```

因此第 i+1 日的阈值可以在开盘时得到，避免了用“同一根日K的最高价或收盘价”预测其自身触发价的标签泄漏。

当前 `ADAPTIVE_HISTORY_DAYS = 80`；2026-09-07 日志显示有 79 根可用完成日线，形成 69 个有效训练样本。

## 3. 五个输入特征

每个特征都按 ATR 归一化，避免 ¥100 和 ¥400 的价格量级改变模型含义。设当前特征日为 `i`，特征窗口为 `span`：

| 特征 | 计算 | 含义 |
|---|---|---|
| trend | `(close[i] - close[i-span]) / ATR[i]` | 多日趋势强度 |
| momentum | `(close[i] - close[i-1]) / ATR[i]` | 单日动量 |
| drawdown | `(close[i] - max(close[i-span:i])) / ATR[i]` | 距近期高点的回撤 |
| volatility | `ATR[i] / mean(ATR窗口) - 1` | 波动相对近期均值的变化 |
| volume | `volume[i] / mean(volume窗口) - 1` | 成交量相对近期均值的变化 |

默认 `LOOKBACK_EXPONENT = 0.5`，窗口为 `int(历史样本数 ** 0.5)`。在 79 根日线下，实际窗口约为 8 日。

## 4. 线性模型如何拟合

模型先对每项特征标准化：

```text
z_j = (x_j - 历史均值_j) / 历史标准差_j
```

然后用带数据自适应 Ridge 正则的线性回归拟合：

```text
预测值 = 截距 + β_trend×z_trend + β_momentum×z_momentum
                 + β_drawdown×z_drawdown + β_volatility×z_volatility
                 + β_volume×z_volume
```

Ridge 强度从训练样本的矩阵规模计算，用于避免 5 个特征在样本有限、彼此相关时出现极端系数。它不是手工市场阈值。

## 5. 市场风格：bull / sideways / bear

风格模型预测约 `span / STYLE_HORIZON_DIVISOR` 日后的 ATR 标准化收益。默认窗口约 8 日、`STYLE_HORIZON_DIVISOR = 3`，因此预测期约为 2 日。

风格分数记为 `style_score`。其牛熊边界来自历史预测残差的标准误：

```text
neutral_band = std(历史风格残差) / sqrt(样本数) × STYLE_CONFIDENCE_MULTIPLIER

score < -neutral_band  → bear
-neutral_band ≤ score ≤ neutral_band → sideways
score > neutral_band   → bull
```

默认 `STYLE_CONFIDENCE_MULTIPLIER = 1.0`。调小会更容易被判为 bull 或 bear，调大则会更多落入 sideways。

策略层的处理规则：

- `bull`：阻断新的 REV-T 反T开腿。
- `sideways` / `bear`：可解除旧 MA5/MA20 强牛造成的阻断，但成交量、RSI、持仓可卖数量等安全条件仍保留。
- 风格只决定是否允许新开反T腿；已经卖出的腿仍按既有买回、止损和强制平仓逻辑处理。

## 6. `linear_units` 如何计算

### 6.1 均值预测

触发价模型先得到线性预测均值：

```text
mean_units = intercept + Σ(β_j × z_j)
```

它预测下一交易日“开盘到最高价”的 ATR 倍数均值。

### 6.2 低分位可达值

仅用均值会导致阈值偏远，因此模型引入历史残差的低分位：

```text
tail_rank = 1 / max(2, int(样本数 ** TRIGGER_TAIL_EXPONENT))
lower_residual = Quantile(历史触发模型残差, tail_rank)

attainable_units = mean_units
                  + TRIGGER_LOWER_BOUND_STRENGTH × lower_residual
```

默认参数为：

```python
TRIGGER_LOWER_BOUND_STRENGTH = 1.0
TRIGGER_TAIL_EXPONENT = 0.5
```

`lower_residual` 通常为负，因此 `attainable_units` 通常低于 `mean_units`，目的在于把“平均可达高点”下调为“较容易可达的启动价”。

### 6.3 历史可达区间夹取

为了不让低分位预测低到不合理，最终结果必须落在历史下一日开盘至最高价幅度的经验区间内：

```text
lower_bound = Quantile(历史下一日上冲幅度, tail_rank)
upper_bound = Quantile(历史下一日上冲幅度, 1 - tail_rank)

linear_units = clip(attainable_units, lower_bound, upper_bound)
```

最后计算价格：

```text
sell_trigger = open × (1 + ATR% × linear_units)
```

## 7. 2026-09-07 13:43 实例

日志：

```text
mean 0.392 | attainable 0.048 | trigger ATR-units 0.125
units=clip(mean 0.392 (...) + lower-residual 1.00×-0.344,
           [0.125, 1.032]) = 0.125
```

逐步解释：

1. 五个当日特征输入线性模型，预测均值是 `0.392 ATR`。
2. 历史低分位残差为 `-0.344 ATR`，所以低分位可达值为 `0.392 - 0.344 = 0.048 ATR`。
3. 历史经验下界是 `0.125 ATR`，不能低于它，因此最终 `linear_units = 0.125`。
4. 以开盘价 ¥390.00、ATR 7.52% 代入：

```text
390.00 × (1 + 7.52% × 0.125) = ¥393.52
```

当前日志价格 ¥392.62，距离启动价只约 0.23%。这已经是低阈值状态，而不是阈值过高。

但该日风格分数为 `+0.2309`，高于 bull 边界 `+0.1191`，所以日志显示：

```text
[REV-T] BLOCKED adaptive linear bull style
```

这说明当日没有反T，不是因为 ¥393.52 难以触达，而是 bull 风格的安全规则阻断了开腿。

## 8. 从启动价到完整反T

达到 `sell_trigger` 不会立即卖出，而是进入冲高跟踪状态：

```text
IDLE → SPIKING（达到启动价，跟踪峰值）
     → SOLD（发生回撤确认后卖出）
     → DIPPING（达到计划买回区，等待反弹确认）
     → DONE（买回完成）
```

计划买回价仍由既有逻辑决定：

```text
buyback = sell_price × (1 - ATR% × BUYBACK_TRIGGER_MULT)
```

当前 `BUYBACK_TRIGGER_MULT = 0.15`，因此示例中：

```text
393.52 × (1 - 7.52% × 0.15) = ¥389.08
```

已经按要求移除“持仓成本 + 税费保护线”。因此阈值不再被成本价抬高；同时也意味着阈值本身不能保证整轮反T收益，仍需依赖回撤、成交和买回执行。

## 9. 参数含义与当前建议

| 参数 | 当前值 | 当前判断 |
|---|---:|---|
| `ADAPTIVE_HISTORY_DAYS` | 80 | 合理；历史扫描中比 40 日更稳定。 |
| `LOOKBACK_EXPONENT` | 0.5 | 合理；当前约为 8 日特征窗口。 |
| `STYLE_HORIZON_DIVISOR` | 3 | 合理；对应约 2 日的短周期风格预测。 |
| `STYLE_CONFIDENCE_MULTIPLIER` | 1.0 | 稳健；不建议仅为了允许交易而调高。 |
| `TRIGGER_LOWER_BOUND_STRENGTH` | 1.0 | 合理；降低会抬高启动价，降低触发率。 |
| `TRIGGER_TAIL_EXPONENT` | 0.5 | 可测试 0.60；日线代理回测优于默认，但样本偏小。 |

参数扫描详情见 [v42 参数回测报告](601869_v42_adaptive_parameter_backtest.md)。其中 `TRIGGER_TAIL_EXPONENT = 0.60` 的日线回补代理累计净收益估算为 6.04%，默认值为 4.33%；但这不是实盘收益承诺。

## 10. 日志阅读指南

| 日志字段 | 含义 |
|---|---|
| `score` | 市场风格线性预测分数 |
| `bear<=` / `bull>=` | 数据驱动的风格边界 |
| `samples` | 有效训练样本数 |
| `span` | 特征和 ATR 的实际日线窗口 |
| `horizon` | 风格预测期 |
| `mean` | 触发价线性预测均值单位 |
| `attainable` | 加上低分位残差后的可达单位 |
| `trigger ATR-units` | 夹取历史上下界后的最终 `linear_units` |
| `tail` | 实际使用的经验分位位置 |
| `REV-T BLOCKED` | 阈值计算成功，但新反T腿不满足风险或执行条件 |

## 11. 限制与正确使用方式

1. 日线只能验证“是否触及启动价”，不能验证盘中“先触达卖出、后回撤至买回价”的顺序。
2. 日线回测中的回补收益是代理指标，未包含滑点、最低佣金、限价未成交、隔夜风险和强制平仓。
3. 当前 69 个有效训练样本足够运行模型，但不足以把单次参数扫描结果视为稳定统计规律。
4. `bull` 硬阻断会显著影响真实交易频率；阈值变低不等于实际反T次数变多。
5. 若要从“日线代理有效”升级为“可实盘验证”，下一步应使用 1 分钟或 tick 数据按时间顺序模拟状态机和成交。
