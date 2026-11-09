# -*- coding: utf-8 -*-
"""Run: python -m Stragety.RedisQMT.A144.research.verify_optimization.

Independently reconstruct archived fills, fees, positions and each daily equity;
require original baseline parity, full daily signal parity and +10 percentage
points before writing acceptance.json. No broker imports or live state writes.
"""
import ast
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/a144_v13_optimization_20261104'
CACHE = ROOT / 'output/analysis/a144_v12_recent3m_20261104'
VARIANTS = ['baseline', 'reserve20', 'trailing_profit', 'continuation', 'rotation',
            'rotation_continuation', 'month_momentum', 'month_continuation']


def verify_ledger(folder):
    summary = json.loads((folder / 'summary.json').read_text())
    assert summary['completed'] and summary['sessions'] == 60
    assert summary['minute_sessions_validated'] == 420
    curve = pd.read_csv(folder / 'equity.csv', dtype={'date': str})
    fills = pd.read_csv(folder / 'fills.csv', dtype={'date': str, 'signal': str, 'execution_stamp': str})
    state = json.loads((folder / 'state.json').read_text())
    cash = 90000.
    owned = {}
    fees = 0.
    assert (fills.signal < fills.execution_stamp).all()
    for row in curve.itertuples():
        for fill in fills.loc[fills.date.eq(row.date)].itertuples():
            # All actual trades are ETFs. Recompute fees without the engine's fee function.
            assert fill.sleeve == 'secondary'
            charge = max(5., fill.filled*fill.price*.0001)
            assert abs(charge-fill.fee) < 1e-7
            fees += charge
            if fill.side == 'BUY':
                cash -= fill.filled*fill.price+charge
                owned[fill.code] = owned.get(fill.code, 0)+fill.filled
            else:
                cash += fill.filled*fill.price-charge
                owned[fill.code] -= fill.filled
            assert owned[fill.code] >= 0 and cash >= -1e-6
        market_value = 0.
        for code, quantity in owned.items():
            if quantity:
                frame = pd.read_pickle(CACHE / 'minutes' / (code+'_'+row.date+'.pkl'))
                assert str(frame.iloc[-1].stime) == row.date+'150000'
                market_value += quantity*float(frame.iloc[-1].close)
        assert abs(cash-row.cash) < 1e-6
        assert abs(market_value-row.market_value) < 1e-6
        assert abs(cash+market_value-row.equity) < 1e-6
    assert abs(fees-summary['estimated_fees']) < 1e-6
    assert abs(cash-state['cash']) < 1e-6
    assert {code: quantity for code, quantity in owned.items() if quantity} == {
        code: position['quantity'] for code, position in state['positions'].items()}
    values = pd.Series([90000.]+curve.equity.tolist())
    assert abs(float((values/values.cummax()-1).min())-summary['max_drawdown']) < 1e-10
    assert abs(curve.iloc[-1].equity-summary['final_equity']) < 1e-6
    return summary


def main():
    summaries = {variant: verify_ledger(OUT / variant) for variant in VARIANTS}
    baseline = summaries['baseline']
    chosen = summaries['month_continuation']
    full = verify_ledger(OUT / 'month_continuation_full')
    baseline_stress = verify_ledger(OUT / 'baseline_stress')
    chosen_stress = verify_ledger(OUT / 'month_continuation_stress')
    assert baseline['baseline_equity_and_all_fills_equal']
    assert full['full_daily_selection_and_all_fills_equal']
    for filename in ['equity.csv', 'fills.csv', 'trades.csv']:
        pd.testing.assert_frame_equal(pd.read_csv(OUT / 'month_continuation' / filename),
                                      pd.read_csv(OUT / 'month_continuation_full' / filename))
    target = baseline['total_return']+.10
    assert chosen['total_return'] >= target
    assert chosen_stress['total_return'] >= baseline_stress['total_return']+.10
    source_files = [ROOT / 'Stragety/RedisQMT/A144/A144_v17_MonthMomentumTrendContinuation.py',
        ROOT / 'Stragety/RedisQMT/A144/core/dual_regime_runtime.py',
        ROOT / 'Stragety/RedisQMT/A144/core/dual_regime_execution.py', Path(__file__),
        Path(__file__).with_name('replay_frozen_window.py'), OUT / 'experiment_plan.json']
    baseline_policy = ROOT / 'Stragety/RedisQMT/A144/A144_v12_DualRegimePortfolio.py'
    assert baseline_policy.read_bytes() == (OUT / 'frozen_v12/A144_v12_DualRegimePortfolio.py').read_bytes()
    tree = ast.parse(source_files[0].read_text(encoding='utf-8'))
    assert all('A144_v' not in (node.module or '') for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    (OUT / 'source_hashes_final.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    data_paths = list((CACHE / 'daily').glob('*.pkl')) + list((CACHE / 'minutes').glob('*.pkl'))
    data_paths += [CACHE / 'membership.json', OUT / 'ipo_details.json']
    data_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in data_paths}
    (OUT / 'data_hashes_final.json').write_text(json.dumps(data_hashes, indent=2), encoding='utf-8')
    acceptance = dict(completed=True, initial_cash=90000, start='20260804', end='20261103',
        target='at least +10 percentage points against v12 on the identical verified window',
        baseline_return=baseline['total_return'], selected_return=chosen['total_return'],
        improvement_percentage_points=(chosen['total_return']-baseline['total_return'])*100,
        required_return=target, selected_final_equity=chosen['final_equity'],
        stress_baseline_return=baseline_stress['total_return'], stress_selected_return=chosen_stress['total_return'],
        baseline_all_fills_equal=True, full_daily_signals_all_fills_equal=True,
        independent_daily_ledger_verified=True, primary_trade_count=0,
        strategy='Stragety/RedisQMT/A144/A144_v17_MonthMomentumTrendContinuation.py',
        out_of_sample=False, live_orders=0,
        limitation='In-sample development, simulated fills, daily-close drawdown; no independent corporate-action audit')
    (OUT / 'acceptance.json').write_text(json.dumps(acceptance, indent=2), encoding='utf-8')
    table = []
    for name, result in summaries.items():
        table.append(f'| {name} | {result["total_return"]:.2%} | '
                     f'{(result["total_return"]-baseline["total_return"])*100:+.2f} | '
                     f'{abs(result["max_drawdown"]):.2%} | {result["closed_trades"]} |')
    figure, axis = plt.subplots(figsize=(10, 4.5))
    for name, label in [('baseline', 'v12 baseline'), ('month_momentum', '20-session momentum only'),
                        ('month_continuation', 'v17 selected')]:
        curve = pd.read_csv(OUT / name / 'equity.csv', dtype={'date': str})
        axis.plot(pd.to_datetime(curve.date), curve.equity/90000., label=label)
    axis.set_title('A144 matched 1-minute replay | CNY 90,000 | In-sample development')
    axis.set_ylabel('Net asset value / initial cash')
    axis.grid(alpha=.2)
    axis.legend()
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(OUT / 'comparison.png', dpi=160)
    plt.close(figure)
    report = f'''# A144 v17 优化验收报告

数据取得及核验日期：2026-11-04。固定2026-08-04—11-03、60交易日、9万元空仓、原费用与分钟撮合。
用户确认目标是增加10个百分点；同一窗口开发与验收，属于样本内结果。

| 指标 | 原v12 | 新v17 |
| --- | ---: | ---: |
| 累计收益率 | {baseline['total_return']:.2%} | {chosen['total_return']:.2%} |
| 期末资产（元，含未平仓持仓） | {baseline['final_equity']:,.2f} | {chosen['final_equity']:,.2f} |
| 净盈利（元） | {baseline['profit']:,.2f} | {chosen['profit']:,.2f} |
| 最大日终回撤 | {abs(baseline['max_drawdown']):.2%} | {abs(chosen['max_drawdown']):.2%} |
| 已平仓交易 / 盈利交易 | {baseline['closed_trades']} / {baseline['winning_trades']} | {chosen['closed_trades']} / {chosen['winning_trades']} |
| 成交子单数 | {baseline['fill_count']} | {chosen['fill_count']} |
| 估计手续费（元） | {baseline['estimated_fees']:.2f} | {chosen['estimated_fees']:.2f} |
| 0.3%滑点压力收益 | {baseline_stress['total_return']:.2%} | {chosen_stress['total_return']:.2%} |

正常假设收益增加 **{acceptance['improvement_percentage_points']:.6f}个百分点**；目标至少{target:.8%}，实际{chosen['total_return']:.8%}。
压力情景相比同压力原版增加{(chosen_stress['total_return']-baseline_stress['total_return'])*100:.6f}个百分点。
回撤上升{(abs(chosen['max_drawdown'])-abs(baseline['max_drawdown']))*100:.2f}个百分点，并非风险不变。

![同口径资金曲线](comparison.png)

## 新机制与归因

- 商品资格与排名用20交易日动量代替60日，仍需MA20之上及原成交额门槛。7只ETF池及主策略全部不变，不按事后赢家删减股票池。
- 商品浮盈达到6%启动完成分钟收盘峰值跟踪，峰值回撤3%退出，保留4%硬止损。
- 商品10日等价分钟到期后，仅仍为合格领先者且价格不低于MA20时继续持有，最多20日。
- 月度动量单独贡献的收益差为{(summaries['month_momentum']['total_return']-baseline['total_return'])*100:+.2f}个百分点；再加入退出组合增加{(chosen['total_return']-summaries['month_momentum']['total_return'])*100:+.2f}个百分点。退出组合单独使用曾失败，不能把交互效果说成各机制普遍有效。
- 新版全部实际成交来自化工ETF159981.SZ，主策略仍无成交，未证明股票主策略或资金切换问题已优化。

## 所有尝试与失败结果

| 变体 | 区间收益 | 相对原版百分点差 | 最大日终回撤 | 已平仓笔数 |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(table)}

reserve20为预留20%资金；trailing_profit仅跟踪止盈；continuation再加到期延期；rotation失去领先即退出；rotation_continuation为前两者组合；month_momentum仅改20日动量；month_continuation为最终v17。
前五项未改善原版，源文件完整保留在`candidates/`，不留在策略目录冒充达标版本。实验假设及用户目标澄清见`experiment_plan.json`。

## 验证证据

1. 原版每日权益及全部25笔成交逐字段复现一致，容差1e-7。
2. 新版完整逐日计算509个代码的日线准备及全部选股流程；与快速对照的每日权益和全部20笔成交逐字段一致。
3. 420个代码交易日、100800根真实1分钟K线，每会话240个精确时点、哈希及价量核验。官方历史成分按生效日使用，IPO零占位仅按只读RPC核实的上市日裁剪。
4. 验收脚本独立重算每笔ETF费用、现金、持仓数量以及每日15:00市值；正常、压力、所有失败变体和完整流程均通过。
5. 新v17源码独立，不导入旧策略；状态默认`state/dual_regime_v17.json`，订单和券商策略标识`A144V17`；未迁移或修改实盘账本，未启动交易。

## 复现

```powershell
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant baseline
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation --full-market
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant baseline --slippage 0.003
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation --slippage 0.003
python -m Stragety.RedisQMT.A144.research.verify_optimization
```

失败变体使用相应`--variant`重跑；不需要真实QMT连接，不能把这个固定窗口脚本用于改过股票选股的策略或其他区间。

## 边界

保留原撮合：上一完成分钟信号，下一分钟限价成交；前后分钟5%容量，开盘叠加不利滑点并受真实分钟高低价及限价约束、不利取整；每子单最低费用，未成交部分视为当分钟结束撤单。压力改变此模型的滑点参数，并不能替代盘口排队或延迟压力。未模拟网络、撤单失败、分红税和独立公司行动公告；未强制平仓末仓，回撤仅日终。
同一三个月使用了7项候选研究，存在选择偏差；无独立样本外收益证明。不标记最佳或普适有效，不将23.63%视作未来收益承诺。

[验收JSON](acceptance.json)、[最终源码哈希](source_hashes_final.json)、[原版冻结资料](baseline_source_hashes.json)、[新版正常明细](month_continuation/fills.csv)、[完整流程明细](month_continuation_full/fills.csv)、[新版压力明细](month_continuation_stress/fills.csv)。
'''
    (OUT / 'report.md').write_text(report, encoding='utf-8')
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
