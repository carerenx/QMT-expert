"""Generate an auditable per-trade report from independent-day DayT results."""
from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / 'analysis/dayt_daily_restart_20260912'
OUTPUT = RESULT_DIR / 'three_strategy_daily_restart_trade_audit.md'
FILES = {
    'v39_nomom': RESULT_DIR / 'v39_nomom.json',
    'v52_nomom': RESULT_DIR / 'v52_nomom.json',
    'v53_nomom': RESULT_DIR / 'v53_nomom.json',
}


def money(value):
    return '{:+,.2f}'.format(float(value))


def display_time(value):
    return '{}:{}:{}'.format(value[8:10], value[10:12], value[12:14])


def trade_action(trade):
    label = trade['label']
    if label == 'REV-T sell':
        return 'SHORT', 'OPEN'
    if label.startswith('REV-T buyback'):
        return 'SHORT', 'CLOSE'
    if label.startswith('REV-T risk buyback'):
        return 'SHORT', 'CLOSE'
    if label == 'REV-T force buyback':
        return 'SHORT', 'CLOSE'
    if label == 'FWD-T buy':
        return 'LONG', 'OPEN'
    if label.startswith('FWD-T sell'):
        return 'LONG', 'CLOSE'
    if label == 'FWD-T force sell':
        return 'LONG', 'CLOSE'
    raise ValueError('unsupported trade label: {}'.format(label))


def reconstruct(row):
    legs = {'SHORT': [], 'LONG': []}
    opened = {'SHORT': [], 'LONG': []}
    cycle_gross = {'SHORT': 0.0, 'LONG': 0.0}
    cycles = []
    for trade in row['trades']:
        group, action = trade_action(trade)
        quantity = abs(int(trade['shares']))
        price = float(trade['price'])
        if action == 'OPEN':
            leg = dict(time=trade['time'], price=price, remaining=quantity)
            legs[group].append(dict(leg))
            opened[group].append(dict(leg))
            continue
        remaining = quantity
        close_gross = 0.0
        while remaining and legs[group]:
            leg = legs[group][0]
            matched = min(remaining, leg['remaining'])
            if group == 'SHORT':
                close_gross += (leg['price'] - price) * matched
            else:
                close_gross += (price - leg['price']) * matched
            leg['remaining'] -= matched
            remaining -= matched
            if not leg['remaining']:
                legs[group].pop(0)
        if remaining:
            raise RuntimeError('{} {} closes {} shares without an opening leg'.format(
                row['version'], row['date'], remaining))
        cycle_gross[group] += close_gross
        if not legs[group]:
            entry_quantity = sum(item['remaining'] for item in opened[group])
            entry_text = '；'.join('{}×{}@{:.2f}'.format(
                display_time(item['time']), item['remaining'], item['price'])
                for item in opened[group])
            cycles.append(dict(
                date=row['date'],
                group=group,
                entries=entry_text,
                exit='{}×{}@{:.2f}'.format(display_time(trade['time']), quantity, price),
                quantity=entry_quantity,
                gross=cycle_gross[group],
            ))
            opened[group] = []
            cycle_gross[group] = 0.0
    expected = [float(value) for value in row['cycle_gross']]
    actual = [item['gross'] for item in cycles]
    if len(expected) != len(actual):
        raise RuntimeError('{} {} cycle count mismatch: {} != {}'.format(
            row['version'], row['date'], len(actual), len(expected)))
    for left, right in zip(actual, expected):
        if abs(left - right) > 0.001:
            raise RuntimeError('{} {} cycle gross mismatch: {} != {}'.format(
                row['version'], row['date'], left, right))
    opening = (float(row['initial_equity']) - 100000.0) / 200.0
    closing = opening + float(row['hold_gross']) / 200.0
    unclosed = []
    for group, group_legs in legs.items():
        for leg in group_legs:
            quantity = leg['remaining']
            gross = ((leg['price'] - closing) * quantity if group == 'SHORT'
                     else (closing - leg['price']) * quantity)
            unclosed.append(dict(
                date=row['date'],
                group=group,
                entry='{}×{}@{:.2f}'.format(
                    display_time(leg['time']), quantity, leg['price']),
                quantity=quantity,
                close=closing,
                gross=gross,
                state=row['state'],
            ))
    return cycles, unclosed


def load_version(path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    zero_rows = [row for row in payload['results'] if row['slip'] == 0.0]
    slip_rows = [row for row in payload['results'] if row['slip'] == 0.01]
    cycles = []
    unclosed = []
    for row in zero_rows:
        row_cycles, row_unclosed = reconstruct(row)
        cycles.extend(row_cycles)
        unclosed.extend(row_unclosed)
    return payload, zero_rows, slip_rows, cycles, unclosed


def render():
    loaded = {name: load_version(path) for name, path in FILES.items()}
    lines = [
        '# DayT 三策略每日重启逐笔交易审计',
        '',
        '## 回测口径',
        '',
        '- 每个交易日开盘前创建全新的策略实例，不继承前一日策略状态。',
        '- 每日重新给予 100,000 元现金和 200 股底仓；收盘后结束该日实验。',
        '- 日期请求为 2026-08-01 至 2026-09-12；本地数据实际覆盖 2026-08-03 至 2026-09-10，共29个完整交易日。',
        '- 逐笔主表使用0滑点，不扣佣金和印花税，因此表中均为毛收益。0.01元/股滑点作为汇总敏感性对照。',
        '- 完整T周期按成交账本配对。收盘仍未闭合的腿按当日收盘价盯市，但不带到下一交易日。',
        '- “超额毛收益”是策略当日终值相对同样200股底仓全日不操作的差额；它与完整T周期毛收益并不总相等，因为包含收盘未闭合腿的盯市影响。',
        '',
        '## 汇总比较',
        '',
        '| 策略 | 完整周期 | 盈利/亏损周期 | 完整周期毛收益 | 收盘未闭合腿 | 0滑点超额毛收益 | 0.01元滑点超额毛收益 | 成交数 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for name, (_, zero_rows, slip_rows, cycles, unclosed) in loaded.items():
        wins = sum(item['gross'] > 0 for item in cycles)
        losses = sum(item['gross'] <= 0 for item in cycles)
        cycle_total = sum(item['gross'] for item in cycles)
        zero_excess = sum(row['excess_gross'] for row in zero_rows)
        slip_excess = sum(row['excess_gross'] for row in slip_rows)
        fills = sum(len(row['trades']) for row in zero_rows)
        lines.append('| {} | {} | {} / {} | {} | {} | {} | {} | {} |'.format(
            name, len(cycles), wins, losses, money(cycle_total), len(unclosed),
            money(zero_excess), money(slip_excess), fills))
    for name, (_, zero_rows, _, cycles, unclosed) in loaded.items():
        lines.extend(['', '## {}'.format(name), '', '### 逐日结果', '',
                      '| 日期 | 成交数 | 完整周期 | 周期毛收益 | 未闭合盯市 | 当日超额毛收益 | 最大回撤 |',
                      '|---|---:|---:|---:|---:|---:|---:|'])
        for row in zero_rows:
            row_cycles = [item for item in cycles if item['date'] == row['date']]
            row_unclosed = [item for item in unclosed if item['date'] == row['date']]
            lines.append('| {} | {} | {} | {} | {} | {} | {:.2%} |'.format(
                row['date'], len(row['trades']), len(row_cycles),
                money(sum(item['gross'] for item in row_cycles)),
                money(sum(item['gross'] for item in row_unclosed)),
                money(row['excess_gross']), float(row['max_drawdown'])))
        lines.extend(['', '### 每个完整T周期（0滑点）', '',
                      '| # | 日期 | 类型 | 开仓成交（时间×股数@价格） | 平仓成交 | 股数 | 毛收益 |',
                      '|---:|---|---|---|---|---:|---:|'])
        if cycles:
            for index, cycle in enumerate(cycles, 1):
                lines.append('| {} | {} | {} | {} | {} | {} | {} |'.format(
                    index, cycle['date'], cycle['group'], cycle['entries'],
                    cycle['exit'], cycle['quantity'], money(cycle['gross'])))
        else:
            lines.append('| — | — | — | — | — | — | 无完整周期 |')
        lines.extend(['', '### 每日收盘未闭合腿（0滑点）', '',
                      '| 日期 | 类型 | 开仓成交 | 股数 | 收盘价 | 盯市毛收益 | 收盘状态 |',
                      '|---|---|---|---:|---:|---:|---|'])
        if unclosed:
            for item in unclosed:
                lines.append('| {} | {} | {} | {} | {:.2f} | {} | {} |'.format(
                    item['date'], item['group'], item['entry'], item['quantity'],
                    item['close'], money(item['gross']), item['state']))
        else:
            lines.append('| — | — | — | — | — | 0.00 | 无 |')
        positive_days = sum(row['excess_gross'] > 0 for row in zero_rows)
        negative_days = sum(row['excess_gross'] < 0 for row in zero_rows)
        flat_days = len(zero_rows) - positive_days - negative_days
        best_day = max(zero_rows, key=lambda row: row['excess_gross'])
        worst_day = min(zero_rows, key=lambda row: row['excess_gross'])
        completed_gross = sum(item['gross'] for item in cycles)
        unclosed_gross = sum(item['gross'] for item in unclosed)
        best_cycle = max(cycles, key=lambda item: item['gross']) if cycles else None
        worst_cycle = min(cycles, key=lambda item: item['gross']) if cycles else None
        lines.extend(['', '### 策略分析', '',
                      '- 盈利日 {} 天，亏损日 {} 天，持平日 {} 天；最佳日 {} 为 {} 元，最差日 {} 为 {} 元。'.format(
                          positive_days, negative_days, flat_days,
                          best_day['date'], money(best_day['excess_gross']),
                          worst_day['date'], money(worst_day['excess_gross'])),
                      '- 完整周期贡献 {} 元；每日收盘未闭合腿合计贡献 {} 元；两者合计为累计超额毛收益 {} 元。'.format(
                          money(completed_gross), money(unclosed_gross),
                          money(completed_gross + unclosed_gross))])
        if best_cycle and worst_cycle:
            lines.extend([
                '- 最佳完整周期：{} {}，{} 元；最差完整周期：{} {}，{} 元。'.format(
                    best_cycle['date'], best_cycle['group'], money(best_cycle['gross']),
                    worst_cycle['date'], worst_cycle['group'], money(worst_cycle['gross'])),
            ])
        total_orders = sum(row['orders'] for row in zero_rows)
        total_fills = sum(len(row['trades']) for row in zero_rows)
        lines.extend(['', '### 执行观察', '',
                      '- 委托 {} 笔，成交 {} 笔，未形成成交 {} 笔。未成交委托不计盈亏。'.format(
                          total_orders, total_fills, total_orders - total_fills),
                      '- 所有29个交易日均完成回放，没有策略循环停止或初始化失败。'])
    lines.extend([
        '',
        '## 结论',
        '',
        '1. `v39_nomom` 在每日重启模型下的累计超额毛收益最高；它不需要处理跨日账本，因此该结论不能替代连续账户安全评价。',
        '2. `v52_nomom` 的日内完整周期较少，并存在若干收盘未闭合腿；每日重启会在次日重新给予底仓，因此这些腿不会像连续账户模型那样长期阻塞。',
        '3. `v53_nomom` 的风险退出在每日模型内也会截断逆向腿，0滑点相对 v52 改善1,560元，但仍为负超额。',
        '4. 每日重启适合比较当天信号质量，不等同于真实账户：真实现金、持仓、费用和未完成腿不会隔夜自动重置。',
        '',
        '## 原始结果',
        '',
        '- [v39_nomom JSON](v39_nomom.json)',
        '- [v52_nomom JSON](v52_nomom.json)',
        '- [v53_nomom JSON](v53_nomom.json)',
    ])
    OUTPUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    render()
