---
name: signal-validation
description: Validate A-stock sell/top-detection signals against historical data. Run when asked to validate trading signals, backtest sell rules, check momentum indicators, evaluate escape-top signals, or verify technical indicators for any A-stock. Covers 18 signals: 5 original rules + 13 momentum (RSI/MACD/KDJ/Bollinger/ROC).
---

# A股卖出/逃顶信号 历史回测验证

对任意A股验证18个技术信号的有效性——从腾讯财经拉取前复权日K线，计算MA/MACD/RSI/KDJ/Bollinger/ROC/ATR全部指标，检测信号触发，分析各持有期的前向收益，输出结构化Markdown报告。

## 验证的信号体系

| 类别 | 信号数 | 包含 |
|------|--------|------|
| 原始5规则 | 5 | 缩量涨停、涨停后放量滞涨、偏离MA10>15%、天量长上影、10日涨超30% |
| RSI系列 | 3 | 超买>80、极端超买>85、顶背离 |
| MACD系列 | 3 | 死叉、零轴上死叉、顶背离 |
| KDJ系列 | 2 | 超买死叉、极端超买死叉 |
| 布林带系列 | 2 | 突破上轨、偏离中轨>25% |
| ROC动量系列 | 2 | 正动量减速、动量转负 |
| 组合信号 | 1 | 高RSI+放量长上影 |

## 运行

```bash
cd <项目根目录>    # d:\02Project\QMT-expert

# 默认300天回看，输出到output/
python .Codex/skills/signal-validation/driver.py 601869

# 自定义回看天数
python .Codex/skills/signal-validation/driver.py 601869 500

# 自定义输出目录
python .Codex/skills/signal-validation/driver.py 600519 300 output
```

报告输出到 `output/<代码>_signal_validation_report.md`。

## 报告内容

生成的Markdown报告包含7个部分:
1. **验证概述** — 全部信号一览表(类别/次数/各期胜率/评级)
2. **原始5规则回顾** — 每规则的各持有期收益统计
3. **动量信号详细分析** — 按RSI/MACD/KDJ/Boll/ROC分组
4. **高频信号触发明细** — 触发>3次的信号逐日明细
5. **动量信号有效性排名** — 逃顶评价(优秀/有效/弱效/无效)
6. **信号共振分析** — 多信号同日触发 + 原规则动量叠加
7. **结论与建议** — 信号保留/改造/废弃建议 + 实操策略表

## 胜率定义

**胜率 = 信号出现后N日股价下跌的比例。** 对卖出/逃顶信号，胜率越高越好。

| 胜率区间 | 评级 |
|---------|------|
| >= 70% | 强有效 |
| 60-70% | 有效 |
| 50-60% | 弱有效 |
| < 50% | 无效/反向 |

## 注意事项

- 数据源: 腾讯财经前复权日线(HTTP，不封IP)
- 未计入交易成本(佣金/滑点/印花税)
- **市场环境是第一因素**——牛市卖出信号系统性失效是预期内的
- 建议分行情阶段(上涨/震荡/下跌)分别回测

## 依赖

```bash
pip install requests
```

(Python标准库即可运行，requests仅用于可选扩展)
