"""Audit and explain the one-minute, continuous-account MOM ablation."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'analysis/nomom_20260914/verified'


def money(value):
    return f'{value:,.2f}'


def main():
    runs = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in DATA.glob('*.json')}
    expected = [f'{v}_5bp' for v in ('v39', 'v51', 'v52')]
    expected += [f'{v}_nomom_{bp}bp' for v in ('v39', 'v51', 'v52') for bp in (3, 5, 10)]
    for name in expected:
        r = runs[name]
        assert r['failure'] is None and r['days'] == 99, (name, r['failure'])
        assert len({e['time'][:10] for e in r['equity']}) == 99
        assert abs(100000 - sum(t['shares'] * t['price'] + t['fee'] for t in r['trades']) - r['final_cash']) < 1e-6
        assert 200 + sum(t['shares'] for t in r['trades']) == r['final_position']
        orders = {o['order_id']: o for o in r['orders']}
        assert all(t['event'] > orders[t['order']]['submitted_event'] for t in r['trades'])
        for o in orders.values():
            assert abs(o['fee'] - (max(5, o['traded_value'] * r['rate']) if o['traded_volume'] else 0)) < 1e-6
        assert abs(sum(c['gross'] for c in r['cycles']) + sum(c['unrealized'] for c in r['unclosed']) - r['fees'] - r['excess_net']) < 1e-6
        if 'nomom' in name:
            assert not any('MOM' in o['label'] for o in r['orders'])
            assert not any('MOM' in c['group'] for c in r['cycles'] + r['unclosed'])
        for path, digest in r['hashes'].items():
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path

    lines = ['# v39、v51、v52关闭MOM：1分钟K线重回测', '',
             '日期：2026-09-14。只关闭MOM正反向交易，保留主正T和主反T；重新运行共享现金、持仓与跨日路径，不从旧收益表直接减去MOM亏损。', '',
             '## 1. 数据与规则', '',
             '- 固定长飞光纤601869.SH，2026-04-21至2026-09-10，99个完整交易日。使用1分钟K线，未下载或替换黄金数据；不包含9月11日及之后新行情。',
             '- 初始200股＋10万元现金，起始价377.30元，初始权益175,460元；原样持有至435.74元盈利11,688元。',
             '- 分钟收盘形成决策，最早下一分钟开盘事件成交。日线只提供当时已完成的ATR等特征，不用于推测分钟内成交顺序。',
             '- 连续账户不每日重置，执行T+1、限价、部分成交、单分钟成交量1%上限及交易单位约束；零量、非交易时段、模型识别的单边封板保守不成交。不模拟逐笔排队，未额外叠加固定一分钱滑点。',
             '- 主费用单边5bp（0.05%），每委托最低5元，部分成交累计扣费；关闭MOM另外重跑3bp和10bp。统一费用是研究假设，非实际券商完整费率。',
             '- v39/v51继续采用离线跨日适配：隔夜腿未平时只管理退出，不新增交易；全部平仓后恢复当日初始化。v52保留原生方向准入和隔夜限制。',
             '- 关闭前后使用同一版引擎重跑。无MOM副本语法树与原版仅MOM_ENABLED由True改为False，其他参数、主策略规则不变；保留MOM代码及恢复字段，避免无关重构。',
             '- 修复回测适配器及加载器对v39_nomom的版本识别，保证沿用v39的逐分钟推进、跨日退出及部分成交处理；原版逻辑不变。卡住的试跑已停止，仅verified目录为本次正式结果。三个无MOM副本已有于工作区，本次复用并纠正“仅保留REV-T”的误导注释。', '',
             '## 2. 关闭前后主结果（5bp）', '',
             '账户收益＝底仓涨跌＋做T影响；相对持有增量才是做T比不动多赚或少赚的钱。', '',
             '| 版本 | MOM | 账户收益 | 相对持有增量 | 最大回撤 | 完成周期 | 费用 |', '|---|---|---:|---:|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        for suffix, label in (('', '开启'), ('_nomom', '关闭')):
            r = runs[f'{v}{suffix}_5bp']
            lines.append(f"| {v} | {label} | {money(r['account_net'])} | {money(r['excess_net'])} | {r['maximum_drawdown']:.2%} | {r['completed_cycles']} | {money(r['fees'])} |")
    lines += ['', '### 关闭MOM带来的变化', '', '| 版本 | 最终收益改善（元） | 最大回撤变化（百分点） | 完成周期变化 |', '|---|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        old, new = runs[f'{v}_5bp'], runs[f'{v}_nomom_5bp']
        lines.append(f"| {v} | {money(new['excess_net'] - old['excess_net'])} | {(new['maximum_drawdown'] - old['maximum_drawdown']) * 100:+.2f} | {new['completed_cycles'] - old['completed_cycles']:+d} |")
    lines += ['', '正的收益改善表示比自己的原版更好，不一定已经跑赢持有；回撤变化为负才表示回撤降低。', '',
              '### 通俗结论', '']
    for v in ('v39', 'v51', 'v52'):
        old, new = runs[f'{v}_5bp'], runs[f'{v}_nomom_5bp']
        delta = new['excess_net'] - old['excess_net']
        lines.append(f"- **{v}**：关闭后比自己的原版{'多赚' if delta >= 0 else '少赚'}{money(abs(delta))}元；相比一直持有{'多赚' if new['excess_net'] >= 0 else '少赚'}{money(abs(new['excess_net']))}元。")
    lines += ['',
              'v39的已完成毛收益改善，但最后留下两笔合计200股的未回补反T，踏空估值23,833元，抵消了收益；v51已完成毛收益为正，期末100股未回补的9,824元相对损失仍较大。两者都说明“只看平仓记录”会漏掉风险。', '',
              '**v52无MOM是本次唯一跑赢持有的版本，但并非风险已经解决。** 主反T已完成毛赚15,031元；期末仍有100股在321.86元卖出后未买回，相对期末价格少赚11,388元。后30日做T净增量回落10,284.26元，前期积累的大部分优势被侵蚀。10bp费用下仅领先持有795.64元，未模拟的冲击、排队和额外成本也可能影响这一小幅优势，不能把本次结果当作实盘收益保证。', '',
              '## 3. 无MOM后，剩下的收益从哪里来', '',
              '| 版本 | 完成周期毛收益 | 未完成腿期末估值 | 费用 | 相对持有净增量 |', '|---|---:|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        r = runs[f'{v}_nomom_5bp']
        lines.append(f"| {v} | {money(sum(c['gross'] for c in r['cycles']))} | {money(sum(c['unrealized'] for c in r['unclosed']))} | {money(r['fees'])} | {money(r['excess_net'])} |")
    lines += ['', '恒等式：已完成毛收益＋未完成估值−费用＝相对持有净增量。未买回反T的负估值代表踏空，相对持有少赚，不是已经发生同额现金亏损。', '',
              '| 版本 | 方向 | 完成周期 | 已完成毛收益 | 毛胜率 |', '|---|---|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        for g, label in (('SHORT', '主反T'), ('LONG', '主正T')):
            c = [c['gross'] for c in runs[f'{v}_nomom_5bp']['cycles'] if c['group'] == g]
            win = f'{sum(x > 0 for x in c) / len(c):.1%}' if c else '—'
            lines.append(f'| {v} | {label} | {len(c)} | {money(sum(c))} | {win} |')
    lines += ['', '毛胜率未扣费用且排除未完成腿，不等于账户赚钱概率。', '',
              '## 4. 未平仓与资金风险', '',
              '| 版本 | 期末持股 | 未完成绝对数量峰值 | 占用峰值（元） | 最长周期（交易日） |', '|---|---:|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        r = runs[f'{v}_nomom_5bp']
        lines.append(f"| {v} | {r['final_position']} | {r['unfinished_abs_peak']} | {money(r['capital_occupation_peak'])} | {r['longest_holding_sessions']} |")
    lines += ['', '占用峰值按正向成本及反向开仓价/现价较大者计算，不等于冻结资金，不含额外ATR/费用缓冲；不同方向股数不抵消。', '',
              '| 版本 | 未完成方向 | 开始时间 | 数量 | 开仓均价 | 期末价格 | 估值影响 |', '|---|---|---|---:|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        rows = runs[f'{v}_nomom_5bp']['unclosed']
        for c in rows:
            lines.append(f"| {v} | {c['group']} | {c['opened']} | {c['quantity']} | {c['price']:.2f} | {c['mark']:.2f} | {money(c['unrealized'])} |")
        if not rows:
            lines.append(f'| {v} | 无 | — | 0 | — | — | 0 |')
    lines += ['', '不在期末虚构强制平仓；后续真实回补仍有费用和价格风险。200股底仓下v52的100股额度不能同时容纳两个100股周期，不能据此证明双周期有效。', '',
              '## 5. 阶段与费用敏感性', '',
              '| 无MOM版本 | 前69日净增量 | 后30日净增量变化 |', '|---|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        r = runs[f'{v}_nomom_5bp']
        e = [e for e in r['equity'] if e['time'][:10] <= '2026-07-30'][-1]
        early = e['equity'] - 100000 - 200 * e['price']
        lines.append(f"| {v} | {money(early)} | {money(r['excess_net'] - early)} |")
    lines += ['', '同一账户分段，不在7月30日重置，包含未完成腿估值。99天已看过，不是全新样本外。', '',
              '| 无MOM版本 | 3bp净增量 | 5bp净增量 | 10bp净增量 |', '|---|---:|---:|---:|']
    for v in ('v39', 'v51', 'v52'):
        lines.append(f'| {v} | ' + ' | '.join(money(runs[f'{v}_nomom_{bp}bp']['excess_net']) for bp in (3, 5, 10)) + ' |')
    lines += ['', '每档费用独立重跑真实模拟现金路径，不做简单算术扣除。', '',
              '## 6. 如何理解、如何使用', '',
              '关闭MOM既移除了它自己的盈亏，也释放了资金、可卖底仓及交易时间，因此主策略之后的交易会变化；不能把旧MOM总亏损直接当成收益改善。', '',
              '本次仅是历史消融验证，不修改原版的MOM开关、不启用实盘、不导入或修改账户检查点。无MOM副本仍保留原版本的运行入口，尤其v39/v51副本不应未经审核直接用于已有MOM腿的实盘账户。', '',
              '即使历史改善，也需新增数据和影子观察确认；单股票99天不足以保证未来收益。按signal-validation技能关注成本、阶段差异及胜率局限，但不运行无关日线18信号、不替换固定数据。', '',
              '## 7. 验证与复现', '',
              '- 193项测试通过，包括无MOM副本仅开关不同、v39无MOM分钟循环与跨日退出、既有执行安全测试。',
              '- 黄金回放396个独立日用例逐笔及盈亏精确复现；本报告连续回测与黄金每日重置口径分开，不覆盖黄金。',
              '- 12次连续回放全部覆盖99天且无停止；资金/持股守恒、下一事件成交、最低费用累计、收益分解、代码/数据哈希核验通过。',
              '- 无MOM回放所有委托、完成周期和未完成腿均无MOM归属。',
              '- [结果目录](nomom_20260914/verified/)中的JSON保存逐笔成交、连续权益、订单、未完成明细及SHA-256；[报告生成脚本](report_dayt_nomom.py)。', '',
              '```powershell',
              'python backtest/dayt_strict.py --version v39_nomom --legacy-carry --output <新目录>/v39_nomom_5bp.json',
              'python backtest/dayt_strict.py --version v51_nomom --legacy-carry --output <新目录>/v51_nomom_5bp.json',
              'python backtest/dayt_strict.py --version v52_nomom --output <新目录>/v52_nomom_5bp.json',
              '# 费用敏感性另加 --rate 0.0003 或 --rate 0.001。现有结果禁止覆盖。', '```', '']
    dest = ROOT / 'analysis/DayT_no_MOM_1min_comparison.md'
    dest.write_text('\n'.join(lines), encoding='utf-8')
    print('AUDIT PASS:', dest)


if __name__ == '__main__':
    main()
