# v52_nomom 策略介绍与回测报告

日期：2026-09-14 | 标的：长飞光纤(601869.SH) | 回测周期：2026-04-21 至 2026-09-10（99个交易日）

---

## 1. 策略概述

v52_nomom 是 v52 DirectionalOvernight 策略的**无动量(MOM-disabled)**版本。核心改动仅一行：

```python
MOM_ENABLED = False  # ← MOM屏蔽回测: 禁用动量触发，仅保留REV-T
```

### 策略架构

```
┌─────────────────────────────────────────────────────────────┐
│                    v52 DirectionalOvernight                 │
├─────────────────────────────────────────────────────────────┤
│  1. 方向准入 (DirectionalAdmission)                         │
│     - DIRECTIONAL_THRESHOLD = 0.20                         │
│     - 仅限候选 0/0.20/0.40 三档                            │
│     - 控制正T/反T开仓资格                                   │
├─────────────────────────────────────────────────────────────┤
│  2. 序列化周期 (Serialized Cycle)                           │
│     - MAX_OPEN_CYCLES = 2 (主策略+MOM合计)                  │
│     - CYCLE_EXPOSURE_FRACTION = 0.50                       │
│     - 双向不抵消，绝对值合计                                 │
├─────────────────────────────────────────────────────────────┤
│  3. 核心交易逻辑 (REV-T Only)                               │
│     - 主反T: 冲高回落 → 卖出 → 买回                        │
│     - 主正T: 探底回升 → 买入 → 卖出                        │
│     - 阶梯加仓: ±1.5%追加                                  │
├─────────────────────────────────────────────────────────────┤
│  4. MOM 动量触发 ❌ (本版本已禁用)                          │
│     - 原设计: 2分钟价格窗口 + ATR自适应阈值                  │
│     - MOM_EMERGENCY_BUYBACK_ENABLED = False                │
│     - MOM_REV_PRIORITY_ENABLED = True                      │
├─────────────────────────────────────────────────────────────┤
│  5. 辅助机制 (保留)                                         │
│     - Quantile Trend Regime 信号计算                        │
│     - Intraday Strength 强度指标                            │
│     - Rebound Reference 反弹确认                           │
│     - ATR Reentry 自适应重入                               │
│     - MA5/MA20 均线位置监控                                │
│     - Runtime Watchdog 运行时看门狗                         │
└─────────────────────────────────────────────────────────────┘
```

### 关键参数

| 参数 | 值 | 说明 |
|---|---|---|
| DIRECTIONAL_THRESHOLD | 0.20 | 候选准入阈值 |
| MAX_OPEN_CYCLES | 2 | 最大同时开仓周期 |
| CYCLE_EXPOSURE_FRACTION | 0.50 | 仓位暴露比例上限 |
| QUANTILE_UNITS_SCALE | 0.60 | 首轮信号缩放系数 |
| REENTRY_UP_UNITS_SCALE | 0.80 | 重入上行系数缩放 |
| STRENGTH_STRONG | 0.8 | 强度阈值 |
| LADDER_UP_STEP_PCT | 1.5% | 阶梯加仓步长 |
| T_TARGET_VALUE | 35,000元 | 单笔目标金额 |

---

## 2. 回测结果

### 2.1 核心指标

| 指标 | v52_nomom | v52 原版 | 变化 |
|---|---:|---:|---:|
| **账户净收益** | **+13,805元** | -2,920元 | ✅ +16,725元 |
| **相对持有增量** | **+2,117元** | -14,608元 | ✅ +16,725元 |
| **最大回撤** | 21.05% | 21.97% | ✅ -0.93pct |
| **完成周期** | 36 | 63 | -27 |
| **完成交易笔数** | 73 | 127 | -54 |
| **费用** | 1,322元 | 2,319元 | -997元 |
| **盈亏比(Profit Factor)** | 9.57 | 0.81 | ✅ +8.76 |

> **关键发现**: v52_nomom 是本次三版本消融测试中**唯一跑赢持有(Buy & Hold)的版本**。

### 2.2 方向分解

| 方向 | 周期数 | 已完成毛收益 | 毛胜率 |
|---|---:|---:|---:|
| 主反T (SHORT) | 30 | +15,031元 | **93.3%** |
| 主正T (LONG) | 6 | -204元 | 50.0% |
| **合计** | **36** | **+14,827元** | **89.4%** |

> 主反T贡献了绝大部分收益，正T几乎持平。

### 2.3 恒等式分解

```
已完成毛收益:  +14,827元
未完成腿估值:  -11,388元 (100股在321.86元卖出后未买回)
费用:          -1,322元
─────────────────────────
相对持有净增量: +2,117元
```

**未完成风险**: 期末仍有100股反T未平仓（卖出价321.86元，期末价435.74元），相对持有少赚11,388元。这是未实现的踏空损失，不是已发生亏损。

### 2.4 阶段表现

| 阶段 | 净增量 | 占比 |
|---|---:|---:|
| 前69日 (04-21 ~ 07-30) | +12,402元 | — |
| 后30日 (07-31 ~ 09-10) | -10,284元 | 前期优势大部分被侵蚀 |

> 后期回撤主要来自牛市行情中反T熔断频繁激活。

### 2.5 费用敏感性

| 费率 | 相对持有净增量 | 是否仍跑赢持有 |
|---|---:|:---:|
| 3bp | +2,646元 | ✅ 是 |
| 5bp (默认) | +2,117元 | ✅ 是 |
| 10bp | +796元 | ⚠️ 仅微弱领先 |

### 2.6 风险指标

| 指标 | v52_nomom |
|---|---:|
| 期末持股 | 100股 |
| 未完成绝对数量峰值 | 100股 |
| 占用峰值(元) | 59,985元 |
| 最长周期(交易日) | 29天 |

---

## 3. 为什么关闭MOM反而更好？

### MOM的负面影响

1. **资金占用**: MOM触发的交易与主策略争抢资金和可卖底仓
2. **过度交易**: MOM产生大量短线交易（v52原版127笔 vs nomom 73笔）
3. **负期望**: MOM交易整体亏损（v52原版MOM部分为负贡献）
4. **滑点放大**: 更多交易意味着更多滑点成本

### REV-T的独立优势

- **趋势信号**: Quantile Trend Regime 有效识别冲高回落时机
- **强度过滤**: IntradayStrength 避免弱势时开仓
- **反弹确认**: Rebound Reference 确保回调充分后再买回
- **阶梯加仓**: 1.5%步长追加，不追高杀跌

---

## 4. 如何运行

### 4.1 前置条件

```bash
# 确保已安装依赖
cd d:\02Project\QMT-expert
pip install pandas numpy
```

### 4.2 运行命令

**标准回测 (5bp费率)**:
```bash
python backtest/dayt_strict.py --version v52_nomom --output analysis/nomom_20260911/v52_nomom.json
```

**自定义费率回测**:
```bash
# 3bp费率
python backtest/dayt_strict.py --version v52_nomom --rate 0.0003 --output analysis/nomom_20260911/v52_nomom_3bp.json

# 10bp费率
python backtest/dayt_strict.py --version v52_nomom --rate 0.001 --output analysis/nomom_20260911/v52_nomom_10bp.json
```

**与其他版本对比**:
```bash
# v51_nomom 需要 --legacy-carry 参数
python backtest/dayt_strict.py --version v51_nomom --legacy-carry --output analysis/nomom_20260911/v51_nomom.json

# v39_nomom 也需要 --legacy-carry
python backtest/dayt_strict.py --version v39_nomom --legacy-carry --output analysis/nomom_20260911/v39_nomom.json
```

### 4.3 输出文件说明

运行后生成JSON文件，包含：
- `summary`: 净收益、最大回撤、周期统计
- `cycles`: 每个周期的详细交易记录
- `equity_curve`: 每日权益曲线
- `orders`: 所有委托订单明细
- `unfilled`: 未完成腿详情

### 4.4 查看结果

```bash
# 查看JSON摘要
python -c "import json; d=json.load(open('analysis/nomom_20260911/v52_nomom.json')); print(json.dumps(d['summary'], indent=2, ensure_ascii=False))"
```

---

## 5. 实盘注意事项

⚠️ **v52_nomom 仍处于研究阶段，未启用实盘**

1. **单股票验证**: 99天回测不足以证明未来收益
2. **未完成腿风险**: 期末100股未平仓，实际可能面临更大回撤
3. **后期衰减**: 后30日净增量-10,284元，前期优势被侵蚀
4. **市场环境**: 当前强牛市对反T策略不利

### 建议下一步

1. **延长回测**: 覆盖更多市场周期（熊市/震荡市）
2. **多股票验证**: 在其他高波动标的上测试
3. **影子观察**: 在实盘中用影子模式跟踪信号
4. **参数优化**: 针对不同市场环境调整DIRECTIONAL_THRESHOLD

---

## 6. 文件位置

| 文件 | 路径 |
|---|---|
| 策略源码 | `Stragety/MiniQMT_Stragety/DayT/DayT_v52_nomom.py` |
| 回测引擎 | `backtest/dayt_strict.py` |
| 策略注册 | `backtest/dayt_registry.py` |
| 回测结果 | `analysis/nomom_20260911/v52_nomom.json` |
| 对比报告 | `analysis/DayT_no_MOM_1min_comparison.md` |

---

## 7. 附录：完整回测命令参考

```bash
# === v52 系列 ===
python backtest/dayt_strict.py --version v52 --output analysis/v52_5bp.json
python backtest/dayt_strict.py --version v52_nomom --output analysis/v52_nomom_5bp.json

# === v51 系列 ===
python backtest/dayt_strict.py --version v51 --legacy-carry --output analysis/v51_5bp.json
python backtest/dayt_strict.py --version v51_nomom --legacy-carry --output analysis/v51_nomom_5bp.json

# === v39 系列 ===
python backtest/dayt_strict.py --version v39 --legacy-carry --output analysis/v39_5bp.json
python backtest/dayt_strict.py --version v39_nomom --legacy-carry --output analysis/v39_nomom_5bp.json

# === 费用敏感性 ===
python backtest/dayt_strict.py --version v52_nomom --rate 0.0003 --output analysis/v52_nomom_3bp.json
python backtest/dayt_strict.py --version v52_nomom --rate 0.001 --output analysis/v52_nomom_10bp.json
```
