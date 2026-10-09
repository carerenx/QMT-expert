"""Report the fixed three-strategy continuous replay; never rerun or tune strategies."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'analysis/dayt_threeway_20260911'
DEST = ROOT / 'analysis/DayT_v39_v51_v52_continuous_comparison.md'
VERSIONS = ('v39', 'v51', 'v52')
NAMES = {'SHORT': '主反T', 'LONG': '主正T', 'MOM SHORT': 'MOM反T', 'MOM LONG': 'MOM正T'}


def group(label):
    short = label == 'REV-T sell' or 'buyback' in label or label == 'MOM short'
    return ('MOM ' if label.startswith('MOM') else '') + ('SHORT' if short else 'LONG')


def money(value):
    return f'{value:,.2f}'


def main():
    runs = {(v, bp): json.loads((DATA / (f'{v}_5bp_verified.json' if bp == 5 else f'{v}_{bp}bp.json')).read_text(encoding='utf-8'))
            for v in VERSIONS for bp in (3, 5, 10)}
    for (v, bp), r in runs.items():
        assert r['failure'] is None and r['days'] == 99, (v, bp, r['failure'])
        assert len({e['time'][:10] for e in r['equity']}) == 99
        assert abs(100000 - sum(t['shares'] * t['price'] + t['fee'] for t in r['trades']) - r['final_cash']) < 1e-6
        assert 200 + sum(t['shares'] for t in r['trades']) == r['final_position']
        orders = {o['order_id']: o for o in r['orders']}
        assert all(t['event'] > orders[t['order']]['submitted_event'] for t in r['trades'])
        for o in orders.values():
            assert abs(o['fee'] - (max(5, o['traded_value'] * r['rate']) if o['traded_volume'] else 0)) < 1e-6
        gross = sum(c['gross'] for c in r['cycles'])
        unrealized = sum(c['unrealized'] for c in r['unclosed'])
        assert abs(gross + unrealized - r['fees'] - r['excess_net']) < 1e-6
        for name, digest in r['hashes'].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name

    lines = ['# v39、v51、v52：99天一分钟连续回测对比', '',
             '报告日期：2026-09-14。原始回放从2026-09-11启动；暂停期间入口更新，主结果已用当前入口重跑为5bp_verified文件，与3/10bp使用相同代码。无verified后缀的5bp文件是旧入口结果，不用于本报告。数据不包含9月11日之后的新行情。', '',
             '## 1. 先说结论', '',
             '**本次样本中，v51跨日适配版最终收益最好，v52其次，v39跨日适配版最后；但三者都跑输了原样持有200股。v52降低了交易敞口，并未证明提高收益。**', '',
             '“账户赚钱”包含底仓随股价上涨的收益；“做T增量”才回答折腾买卖比不动多赚了多少。以下费用是研究假设，不是真实券商收费验收。', '',
             '## 2. 回测口径与可比性', '',
             '- 标的仅长飞光纤601869.SH；2026-04-21至2026-09-10，共99个完整交易日，不能推广为账户所有股票的结论。',
             '- 初始10万元现金＋200股底仓；初始价格377.30元，初始权益175,460元。期末435.74元，原样持有期末权益187,148元，盈利11,688元（6.66%）。',
             '- 连续保留现金、持仓、T+1可卖数量和未完成腿，不每日重置；日线特征排除当日未完成数据。',
             '- 完整分钟收盘形成信号，最早下一个开盘事件撮合，不使用分钟最高最低价推断先后；固定价须满足限价，单分钟成交不超过成交股数1%并按手取整，可能部分成交。',
             '- 非交易时段、零成交及模型识别的单边封板采用保守不成交。并非逐笔订单簿仿真，实际排队、冲击及滑点仍可能更差；严格模型未另叠加固定一分钱滑点。',
             '- 主费用单边5bp（0.05%），每笔委托最低5元，部分成交累计计算且实际扣现金；另真实重跑3bp与10bp。该统一费用未声称精确复刻佣金、印花税、过户费。',
             '- v39、v51明确使用离线跨日适配：旧腿未平先管理退出，禁止新增主策略/MOM/阶梯，全部平掉再初始化当日信号；未成交不丢腿，v39部分成交取消余单。原策略源码未改，实盘跨日保护未关闭。',
             '- v52用当前默认参数：方向阈值0.20、方向准入与隔夜开启，首轮缩放0.60、后续0.80、盘中强度shadow。未调参。',
             '- 这不是单一参数的因果实验：旧版跨日适配和v52原生隔夜管理不同。v52在200股底仓下总敞口上限100股，两个100股周期无法同时开启，不能据此验证多周期收益。',
             '- 三版均完整跑完99天，failure为空；不是此前中途停止后只把剩余持仓估值到期末的结果。', '',
             '## 3. 收益与风险总表（5bp）', '',
             '| 指标 | v39跨日适配 | v51跨日适配 | v52 |', '|---|---:|---:|---:|']
    base = [runs[v, 5] for v in VERSIONS]
    rows = [('账户净收益（元）', 'account_net', money), ('相对持有净增量（元）', 'excess_net', money),
            ('账户收益率', 'account_net', lambda x: f'{x / 175460:.2%}'),
            ('连续最大回撤', 'maximum_drawdown', lambda x: f'{x:.2%}'),
            ('完成周期数', 'completed_cycles', str), ('交易费用（元）', 'fees', money),
            ('未完成绝对数量峰值（股）', 'unfinished_abs_peak', str),
            ('做T资金占用峰值（元）', 'capital_occupation_peak', money),
            ('最长周期持续交易日', 'longest_holding_sessions', str), ('期末持股', 'final_position', str)]
    for label, key, fmt in rows:
        lines.append('| ' + label + ' | ' + ' | '.join(fmt(r[key]) for r in base) + ' |')
    hold = [175460] + [100000 + 200 * e['price'] for e in base[0]['equity']]
    peak = hold[0]
    hold_dd = 0
    for value in hold:
        peak = max(peak, value)
        hold_dd = max(hold_dd, 1 - value / peak)
    lines += ['', f'原样持有的同频最大回撤为{hold_dd:.2%}。最大回撤就是账户从某个历史高点到后续低点最多缩水多少，不是期末盈亏。', '',
              '资金占用按正向开仓金额、反向开仓价与当前价较大者估值统计，不等于券商冻结资金，也未包含v52预留算法额外的ATR和费用缓冲。数量峰值是各方向未完成股数绝对值相加，不相互抵消。', '',
              '## 4. 钱到底亏在哪里', '',
              '相对持有净增量＝已完成周期毛收益＋未完成腿按期末价估值的增量－全部费用。卖出未买回的负数是相对持有的踏空损失，不是保证发生的现金回购亏损。', '',
              '| 版本 | 已完成毛收益 | 未完成腿估值影响 | 费用 | 最终做T净增量 |', '|---|---:|---:|---:|---:|']
    for v, r in zip(VERSIONS, base):
        lines.append(f"| {v} | {money(sum(c['gross'] for c in r['cycles']))} | {money(sum(c['unrealized'] for c in r['unclosed']))} | {money(r['fees'])} | {money(r['excess_net'])} |")
    lines += ['', '### 各方向拆解', '',
              '主反T＝先卖底仓再买回；主正T＝先买再卖；MOM是独立动量交易腿。胜率只统计已完成周期的毛收益大于零，未扣费，也不包含未平仓周期，不能据此判断是否赚钱。', '',
              '| 版本 | 方向 | 完成数 | 毛胜率 | 已完成毛收益 | 未完成估值 | 归属费用 | 净增量 |', '|---|---|---:|---:|---:|---:|---:|---:|']
    for v, r in zip(VERSIONS, base):
        total = 0
        for g, name in NAMES.items():
            cycles = [c['gross'] for c in r['cycles'] if c['group'] == g]
            unreal = sum(c['unrealized'] for c in r['unclosed'] if c['group'] == g)
            fee = sum(t['fee'] for t in r['trades'] if group(t['label']) == g)
            net = sum(cycles) + unreal - fee
            total += net
            win = f'{sum(c > 0 for c in cycles) / len(cycles):.1%}' if cycles else '—'
            lines.append(f'| {v} | {name} | {len(cycles)} | {win} | {money(sum(cycles))} | {money(unreal)} | {money(fee)} | {money(net)} |')
        assert abs(total - r['excess_net']) < 1e-6
    lines += ['', '### 通俗解读', '',
              '1. **v39：赚了小差价，留下了大的踏空。** 已完成周期毛赚1,906元，但100股在276.18元卖出后，期末涨到435.74元仍没买回，少参与的上涨价值15,956元；再扣费用，总体落后持有16,787.78元。只看已完成交易会把核心风险隐藏。',
              '2. **v51：最终最少落后，但交易多、回撤最大。** 主反T已完成毛赚9,720元，主正T毛亏5,079元、MOM反T毛亏8,961元，把收益抵消。期末已无未平腿，仍落后持有8,460元，所以不能把问题全归结为期末踏空。',
              '3. **v52：限制了规模，却没管住反向大亏。** 主反T完成20轮，19轮毛盈利，但未买回的100股仍造成6,990元相对损失。MOM反T完成41轮，毛亏13,110元，是最大的已实现拖累。正向交易只有2轮MOM、主正T为0，不能认为正向过滤已经得到充分验证；这是过滤、仓位上限、资金和信号共同作用的结果。',
              '4. **高胜率不是高收益。** 少量大亏和未平仓风险可以吃掉多轮小赚。v52减少手续费与敞口，并没有自动变成最赚钱的版本。', '',
              '### 每版最差的三个已完成周期（毛收益）', '',
              '| 版本 | 方向 | 开始 | 结束 | 毛收益（元） |', '|---|---|---|---|---:|']
    for v, r in zip(VERSIONS, base):
        for c in sorted(r['cycles'], key=lambda c: c['gross'])[:3]:
            lines.append(f"| {v} | {NAMES[c['group']]} | {c['opened']} | {c['closed']} | {money(c['gross'])} |")
    lines += ['', '这些是实际模拟成交差价的结果；仅凭周期汇总不能认定具体是强制平仓、目标调整或某个信号造成，进一步改策略前应逐单复核。', '',
              '## 5. 分阶段表现：同一账户连续运行，不在分界点重置', '',
              '| 阶段 | v39做T净增量变化 | v51做T净增量变化 | v52做T净增量变化 |', '|---|---:|---:|---:|']
    for first in (True, False):
        values = []
        for r in base:
            boundary = [e for e in r['equity'] if e['time'][:10] <= '2026-07-30'][-1]
            early = boundary['equity'] - (100000 + 200 * boundary['price'])
            values.append(money(early if first else r['excess_net'] - early))
        lines.append('| ' + ('前69日（至7月30日）' if first else '后30日（7月31日起）') + ' | ' + ' | '.join(values) + ' |')
    lines += ['', '阶段结果包括当时未完成腿的价格变化，因此不等于“该阶段平仓交易收益”。99天已经被观察过，后30天只是诊断分段，不是真正未见过的样本外；本次未利用它调参。', '',
              '## 6. 费用敏感性：不是简单从原表减手续费', '',
              '| 单边费率假设 | v39净增量 | v51净增量 | v52净增量 | 完成数（v39/v51/v52） |', '|---|---:|---:|---:|---|']
    for bp in (3, 5, 10):
        lines.append(f'| {bp}bp | ' + ' | '.join(money(runs[v, bp]['excess_net']) for v in VERSIONS) + ' | ' + '/'.join(str(runs[v, bp]['completed_cycles']) for v in VERSIONS) + ' |')
    lines += ['', '各费率均独立重跑现金与成交路径，最低5元仍保留。即使只看5bp结果扣费前的“已完成＋未完成”合计，三版也均为负，不能靠降低佣金解决全部问题。', '',
              '## 7. 未完成风险与成交限制', '',
              '| 版本 | 未完成方向 | 起始成交时间 | 数量 | 开仓均价 | 期末价 | 相对估值影响 |', '|---|---|---|---:|---:|---:|---:|']
    for v, r in zip(VERSIONS, base):
        for c in r['unclosed']:
            lines.append(f"| {v} | {NAMES[c['group']]} | {c['opened']} | {c['quantity']} | {c['price']:.2f} | {c['mark']:.2f} | {money(c['unrealized'])} |")
        if not r['unclosed']:
            lines.append(f'| {v} | 无 | — | 0 | — | — | 0 |')
    lines += ['', '期末未人为强制平仓，故没有给未完成腿增加一笔假想回购手续费；实际以后买回还会发生费用及价格风险。', '',
              '| 版本 | 成交量容量不足事件 | 限价不满足事件 | 非可交易事件 |', '|---|---:|---:|---:|']
    for v, r in zip(VERSIONS, base):
        lines.append(f'| {v} | ' + ' | '.join(str(r['rejection_counts'].get(k, 0)) for k in ('VOLUME_CAP', 'LIMIT_NOT_MET', 'NON_TRADABLE')) + ' |')
    lines += ['', '这里是撮合受阻事件数，不是独立拒单数；同一委托可多次遇到限制。未无条件假设所有信号都成交。', '',
              '## 8. 与旧固定结果的关系', '',
              '旧固定比较按每天200股＋10万元独立重置、同分钟成交、未扣费用；零滑点冻结结果v39为−11,531元、v51为−14,042元。本次已重跑黄金验证：396次独立日运行的逐笔交易与盈亏精确一致。', '',
              '旧日重置口径曾显示v52更好，不代表连续实盘更好。跨日累计的现金、旧腿、限价成交、费用会改变之后每一步可交易机会。本报告以连续账户结果为主，旧表永久保留，不能把两张表混为一谈。', '',
              '## 9. 结论与下一步研究优先级', '',
              '- 优先排查MOM反T的少数大亏周期，再评估主反T卖出后持续上涨而无法买回的风险；这两类问题比单纯降低卖出阈值更直接。',
              '- 关闭MOM反向可作为下一轮有限消融，但必须重跑共享资金路径，不能从本表直接扣掉亏损方向就宣称改进成功。',
              '- 目前只能说v51在这段样本最终收益较好，不能据此推荐直接实盘；它的回撤更高。v52是控制敞口有所体现、收益尚未达标的研究版。',
              '- 继续保留费用、未完成腿和相对持有评价，新增未见数据后再验证；不以单一股票99天推断长期或多股有效性。', '',
              '本次未修改三个策略或账户检查点，也未启动实盘。使用signal-validation的成本、市场阶段与胜率口径核查原则，但未运行无关的18个日线卖出信号，也未下载替换固定分钟数据。', '',
              '## 10. 复现与审计', '',
              '- 190项单元测试通过；黄金回放通过。全部9次连续回放检查99天覆盖、现金与股数守恒、下一事件成交、最低费用累计及收益归因恒等式。',
              '- 每个JSON包含逐笔成交trades、订单orders、连续权益equity、周期cycles、未完成腿unclosed、适配记录carry_events以及代码/数据SHA-256。报告生成前再次验证各输入哈希与当前文件一致。',
              '- [完整结果目录](dayt_threeway_20260911/)；[固定基准说明](../docs/DayT_fixed_benchmark.md)；[报告生成脚本](report_dayt_threeway.py)。', '',
              '```powershell',
              'python backtest/dayt_strict.py --version v39 --legacy-carry --output <新目录>/v39_5bp.json',
              'python backtest/dayt_strict.py --version v51 --legacy-carry --output <新目录>/v51_5bp.json',
              'python backtest/dayt_strict.py --version v52 --output <新目录>/v52_5bp.json',
              '# 费用敏感性追加 --rate 0.0003 或 --rate 0.001；已存在结果拒绝覆盖。',
              'python analysis/report_dayt_threeway.py', '```', '',
              '### 本次结果文件SHA-256', '', '| 文件 | SHA-256 |', '|---|---|']
    for path in sorted(DATA.glob('*.json')):
        if path.name.endswith('_5bp.json'):
            continue
        lines.append(f'| [{path.name}](dayt_threeway_20260911/{path.name}) | {hashlib.sha256(path.read_bytes()).hexdigest()} |')
    DEST.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('AUDIT PASS: 9 complete runs; report:', DEST)


if __name__ == '__main__':
    main()
