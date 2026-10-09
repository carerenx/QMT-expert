"""Read-only strategy attribution; write research artifacts, never orders."""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent/'profit_priority_cross_stock_20'


def main():
    summary = json.loads((ROOT/'comparison_summary.json').read_text(encoding='utf8'))
    details = []
    allrows = []
    for stock, item in summary.items():
        if 'rows' not in item:
            continue
        pair = {key:json.loads((ROOT/stock/'diagnostic'/(key+'.json')).read_text(encoding='utf8'))
                for key in ('v2_control','profit_core')}
        base = pair['v2_control']
        candidate = pair['profit_core']
        initial = base['initial']
        delta = candidate['terminal_scenarios']['0.8']-base['terminal_scenarios']['0.8']
        fee_delta = candidate['fees']-base['fees']
        # Daily equity increments difference, no reinvested dividends; sums to raw final gap.
        curves = pd.DataFrame({k:dict(v['eod']) for k,v in pair.items()})
        daily = curves.diff()
        daily.iloc[0] = curves.iloc[0]-initial
        contribution = daily.profit_core-daily.v2_control
        assert abs(contribution.sum()-(candidate['final']-base['final'])) < .001
        weakness = [p for p in base['plans'] if 'CONFIRMED_WEAKNESS_CAP_60' in p['reason_codes']]
        ddplans = [p for p in base['plans'] if any(r.startswith('DRAWDOWN_') for r in p['reason_codes'])]
        rec = dict(stock=stock,name=item['name'],delta=delta,delta_rate=delta/initial,
                   fee_delta=fee_delta,weight_delta=candidate['average_weight']-base['average_weight'],
                   base_fills=len(base['fills']),candidate_fills=len(candidate['fills']),
                   weakness_days=len(weakness),dd_days=len(ddplans),
                   worst_days=contribution.nsmallest(3).to_dict(),best_days=contribution.nlargest(3).to_dict())
        allrows.append(rec)
        details += [f"### {stock} {item['name']}", '',
                    f"候选相对v2：{delta:+.2f}元（{delta/initial:+.2%}）。平均仓位变化{rec['weight_delta']:+.2%}；成交回报数{len(base['fills'])}→{len(candidate['fills'])}；费用变化{fee_delta:+.2f}元。", '',
                    f"v2决策记录中弱势限仓出现{len(weakness)}日，回撤控制出现{len(ddplans)}日。两类可以同日出现，记录出现不代表一定成交。", '',
                    '| 日期 | 当日候选-v2账户增量 |', '|---|---:|']
        for date,value in contribution.nsmallest(3).items():
            details.append(f'| {date} | {value:+.2f}元 |')
        details += ['', '这些是两账户当日净值变动之差，包含隔夜持仓影响，不是当日成交已实现利润。', '',
                    '| 日期 | v2成交 | 候选成交 |', '|---|---|---|']
        worstdate = contribution.idxmin()
        dates = list(curves.index)
        pos = dates.index(worstdate)
        # Last five preceding sessions through worst date show positioning context.
        for date in dates[max(0,pos-5):pos+1]:
            texts = []
            for row in (base,candidate):
                fills = [f for f in row['fills'] if f['time'][:8]==date]
                texts.append('；'.join(f"{f['shares']:+d}股@{f['price']:.2f} [{f['reason']}]" for f in fills) or '无成交')
            details.append(f'| {date} | {texts[0]} | {texts[1]} |')
        details.append('')
    lines=['# 20股收益差异归因与优化研究', '',
           '研究对象是现有v2与profit_core，不修改参数、不重选股票。本报告沿用80%分红期末补计的诊断口径；并非真实税费、分红再投资或独立样本外验证。', '',
           '## 核心发现', '',
           '**存在优化空间，但证据不支持直接继续加仓或增加做T频率。首先需要修正实验设计：候选实际同时改动了三个机制，而不只是两个。**', '',
           '1. 普通多头目标仓位85%提高至100%；强多头在v2里本来就是100%，并非所有多头都提高。',
           '2. 删除账户回撤15%/20%/25%的仓位控制。',
           '3. 还删除了v2的“多头但收盘低于MA60且20日方向效率为负→仓位不高于60%”规则。该差异见核心代码的CONFIRMED_WEAKNESS_CAP_60；此前将差异仅解释为提高仓位和取消回撤控制是不完整的。', '',
           '这意味着当前回测不能单独回答“提高普通多头仓位是否有效”，也不能单独证明“回撤控制一定更好”。必须做单机制消融。', '',
           '## 逐股总体归因', '',
           '| 股票 | 净收益差 | 收益率差 | 平均仓位差 | 费用差 | v2弱势限仓日 | v2回撤控制日 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for r in sorted(allrows,key=lambda r:r['delta_rate']):
        lines.append(f"| {r['name']} | {r['delta']:+.2f} | {r['delta_rate']:+.2%} | {r['weight_delta']:+.2%} | {r['fee_delta']:+.2f} | {r['weakness_days']} | {r['dd_days']} |")
    lines += ['', '费用差正值表示候选更贵。仓位差与控制日数是描述性关联，不是机制因果证明。成交回报数包含部分成交，不能称为完整交易次数。', '',
              '## 优化建议与可证伪检验', '',
              '| 优先级 | 候选方向 | 原理与检验 |', '|---|---|---|',
              '| P0 | 三因素消融 | 以v2为基线，分别只改普通多头仓位、只去账户回撤规则、只去MA60弱势限仓，再做组合；三因素全组合8组统一成本，不按单股选优。 |',
              '| P1 | 保留弱势限仓，条件性100% | 在候选中恢复MA60弱势规则；先判断损失是否收窄，再研究多头强度，不同时增加新指标。 |',
              '| P1 | 风险减仓与加仓采用不同冷却 | 当前目标60%的弱势减仓不被订单函数视为风险减仓，仍受5日冷却；而目标≤45%才豁免。检验弱势减仓豁免是否改善，不能预设有利。 |',
              '| P2 | 调整目标跟踪死区 | 目前仓位偏差不足10个百分点不交易，单次最多25%，成交后冷却5日。目标100%并不意味着实际100%；先记录未跟踪原因，再比较5%/10%/15%死区与交易成本。 |',
              '| P2 | 趋势恢复后再加仓 | 研究弱势解除后分段恢复，避免趋势尚未修复便因宽泛多头标签加仓；需要完整消融，不能用事后反弹日期选择条件。 |',
              '| 暂缓 | 独立日内T与复杂波段 | 当前跨股弱点首先是方向与风险暴露；多一个交易通道不自动创造超额，而且会引入更多费用与卖飞风险。 |', '',
              '## 实验与评价规则', '',
              '- 原20股现在已经被研究使用，后续结果都应标为样本内优化。保留冻结版本，并预留未触碰的更早完整区间或未来模拟盘。',
              '- 各股同时报告收益、相对v2/持有超额、回撤、换手、费用、实际仓位以及最差股；用等权指标补充金额，不让高价股决定结论。',
              '- 先统一真实公司行动、起初持仓税务假设和基准成交规则，再做成本压力。未解决这些问题前，不宣称实盘有效。',
              '- 一次只添加一个机制；收益改善必须在多数股票或明确事前分类中复现，而不是事后把失败股排除。', '',
              '## 逐股关键日期与成交背景', '']
    lines += details
    (ROOT/'OPTIMIZATION_RESEARCH.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    (ROOT/'attribution.json').write_text(json.dumps(allrows,ensure_ascii=False,indent=2),encoding='utf8')
    print('stocks',len(allrows),'fee_delta_total',sum(r['fee_delta'] for r in allrows))


if __name__=='__main__':
    main()
