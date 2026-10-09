"""Generate the 600584 v40/v53/v54 trade-level Markdown audit."""
from collections import deque
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'dayt_600584_v53_v54_v40_20260913'


def money(value):
    return '{:+,.2f}'.format(value)


def load(name):
    return json.loads((OUT / name).read_text(encoding='utf-8'))


def strict_rows(data):
    sells = deque()
    rows = []
    cycle_number = 0
    for trade in data['trades']:
        shares = abs(trade['shares'])
        if trade['shares'] < 0:
            sells.append(dict(trade=trade, remaining=shares, fee=trade['fee']))
            continue
        remaining = shares
        while remaining and sells:
            opened = sells[0]
            quantity = min(remaining, opened['remaining'])
            open_trade = opened['trade']
            open_fee = opened['fee'] * quantity / abs(open_trade['shares'])
            close_fee = trade['fee'] * quantity / shares
            gross = (open_trade['price'] - trade['price']) * quantity
            net = gross - open_fee - close_fee
            cycle_number += 1
            rows.append((cycle_number, open_trade['time'], trade['time'],
                         trade['label'], quantity, open_trade['price'],
                         trade['price'], gross, open_fee + close_fee, net))
            opened['remaining'] -= quantity
            remaining -= quantity
            if not opened['remaining']:
                sells.popleft()
    return rows


def strict_section(title, data):
    rows = strict_rows(data)
    lines = [
        '## {}'.format(title), '',
        '| # | 卖出时间 | 买回时间 | 平仓原因 | 股数 | 卖价 | 买回价 | 毛收益 | 费用 | 净收益 |',
        '|---:|---|---|---|---:|---:|---:|---:|---:|---:|',
    ]
    for row in rows:
        number, opened, closed, reason, quantity, sell_price, buy_price, gross, fee, net = row
        lines.append('| {} | {} | {} | {} | {} | {:.2f} | {:.2f} | {} | {:.2f} | {} |'.format(
            number, opened, closed, reason, quantity, sell_price, buy_price,
            money(gross), fee, money(net)))
    for leg in data['unclosed']:
        lines.extend([
            '',
            '未闭合腿：{} 卖出 {} 股，卖价 {:.2f}，截至 {:.2f} 的浮动毛收益 {}；尚未发生买回费用。'.format(
                leg['opened'], leg['quantity'], leg['price'], leg['mark'],
                money(leg['unrealized'])),
        ])
    gross_total = sum(row[7] for row in rows)
    fee_total = sum(row[8] for row in rows)
    net_total = sum(row[9] for row in rows)
    wins = sum(row[9] > 0 for row in rows)
    losses = sum(row[9] < 0 for row in rows)
    lines.extend([
        '',
        '- 策略完整周期：{} 个；逐腿配对明细：{} 段，净盈利/净亏损 {} / {}；毛收益 {}，配对费用 {:.2f}，配对净收益 {}。'.format(
            data['completed_cycles'], len(rows), wins, losses, money(gross_total), fee_total,
            money(net_total)),
        '- 最终现金 {:.2f}，最终持仓 {} 股，账户收益 {}，相对持有基准超额净收益 {}。'.format(
            data['final_cash'], data['final_position'], money(data['account_net']),
            money(data['excess_net'])),
    ])
    return lines


def v40_rows(data):
    rows = []
    cycle_number = 0
    target_position = data['initial_position']
    for day in data['days']:
        short_lots = deque()
        long_lots = deque()
        prior_position = day['start_position']
        for trade in day['trades']:
            realized = None
            classification = '开仓'
            quantity = abs(trade['shares'])
            if trade['label'] == 'REV-T sell':
                short_lots.append(dict(price=trade['price'], quantity=quantity, fee=trade['fee']))
            elif 'buyback' in trade['label']:
                gross = 0.0
                fee = trade['fee']
                remaining = quantity
                while remaining and short_lots:
                    lot = short_lots[0]
                    matched = min(remaining, lot['quantity'])
                    gross += (lot['price'] - trade['price']) * matched
                    fee += lot['fee'] * matched / lot['quantity']
                    lot['quantity'] -= matched
                    remaining -= matched
                    if not lot['quantity']:
                        short_lots.popleft()
                realized = gross - fee
                classification = '反T平仓'
                cycle_number += 1
            elif trade['label'] == 'FWD-T buy':
                recovery = min(quantity, max(0, target_position - prior_position))
                trading = quantity - recovery
                if trading:
                    long_lots.append(dict(price=trade['price'], quantity=trading,
                                          fee=trade['fee'] * trading / quantity))
                classification = '恢复底仓' if recovery == quantity else '正T开仓'
                if recovery and trading:
                    classification = '恢复底仓+正T开仓'
            elif trade['label'] in ('FWD-T sell', 'FWD-T force sell'):
                gross = 0.0
                fee = trade['fee']
                remaining = quantity
                while remaining and long_lots:
                    lot = long_lots[0]
                    matched = min(remaining, lot['quantity'])
                    gross += (trade['price'] - lot['price']) * matched
                    fee += lot['fee'] * matched / lot['quantity']
                    lot['quantity'] -= matched
                    remaining -= matched
                    if not lot['quantity']:
                        long_lots.popleft()
                realized = gross - fee
                classification = '正T平仓' if not remaining else '正T平仓/库存卖出'
                cycle_number += 1
            rows.append(dict(date=day['date'], trade=trade, kind=classification,
                             realized=realized, cycle=cycle_number if realized is not None else ''))
            prior_position = trade['position']
    return rows


def v40_section(data):
    rows = v40_rows(data)
    lines = [
        '## v40_nomom_BaseRecovery（每日重启、账户连续）', '',
        '| # | 时间 | 标签 | 归类 | 股数 | 成交价 | 费用 | 成交后现金 | 成交后持仓 | 本次闭合净收益 |',
        '|---:|---|---|---|---:|---:|---:|---:|---:|---:|',
    ]
    for number, row in enumerate(rows, 1):
        trade = row['trade']
        realized = '—' if row['realized'] is None else money(row['realized'])
        lines.append('| {} | {} | {} | {} | {:+d} | {:.2f} | {:.2f} | {:.2f} | {} | {} |'.format(
            number, trade['time'], trade['label'], row['kind'], trade['shares'],
            trade['price'], trade['fee'], trade['cash'], trade['position'], realized))
    discarded = []
    for day in data['days']:
        state = day['discarded_strategy_state']
        if state['short'] or state['long']:
            discarded.append('{}：short={}，long={}，状态={}'.format(
                day['date'], len(state['short']), len(state['long']), state['state']))
    lines.extend([
        '',
        '- 成交 {} 笔，费用 {:.2f}；最终现金 {:.2f}，最终持仓 {} 股，最终资产 {:.2f}。'.format(
            len(rows), data['fees'], data['final_cash'], data['final_position'],
            data['final_equity']),
        '- 账户收益 {}，相对持有基准超额净收益 {}。'.format(
            money(data['account_net']), money(data['excess_net'])),
        '- 每日重启丢弃未闭合策略账本的交易日共 {} 天：{}。'.format(
            len(discarded), '；'.join(discarded)),
    ])
    return lines


def main():
    v53 = load('v53_strict_5m.json')
    v54 = load('v54_strict_5m.json')
    v40 = load('v40_daily_restart_5m.json')
    initial_equity = v40['initial_equity']
    hold_final = v40['hold_final']
    lines = [
        '# 长电科技 600584：v53、v54、v40 逐笔回测审计', '',
        '## 结论', '',
        '三个策略均未跑赢“10万元现金 + 200股一直持有”。v54 的相对亏损最小；v40 每日重启后持仓漂移至 600 股，是本次最重要的风险问题。', '',
        '| 策略 | 运行模型 | 完整T | 成交 | 费用 | 最终现金 | 最终持仓 | 最终资产 | 账户收益 | 超额净收益 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|',
        '| v53 | 严格连续 | 17 | 35 | 175.00 | 105,754.00 | 100 | 112,528.00 | -292.00 | -1,020.00 |',
        '| v54 | 严格连续 | 8 | 17 | 85.00 | 106,133.00 | 100 | 112,907.00 | +87.00 | -641.00 |',
        '| v40 BaseRecovery | 每日重启/账户连续 | — | 63 | 329.56 | 69,169.44 | 600 | 109,813.44 | -3,006.56 | -3,734.56 |', '',
        '持有基准：初始资产 {:.2f}（首根开盘价 64.10），期末资产 {:.2f}（末日收盘价 67.74），收益 +728.00。'.format(
            initial_equity, hold_final), '',
        '## 回测口径与限制', '',
        '- 标的：长电科技 600584.SH；请求区间 2026-08-01 至 2026-09-13，实际交易数据为 2026-08-03 至 2026-09-11，共 30 个交易日。',
        '- 初始资金：现金 100,000 元、底仓 200 股；研究费率 0.05%，每笔最低 5 元；未加入额外滑点。',
        '- v53/v54 使用连续策略状态和连续账户；v40 每日开盘前重建策略实例，但现金和持仓连续继承。',
        '- 数据源只能稳定取得该区间 5 分钟线，因此这是 **5分钟研究回测**。策略原设计以 1 分钟节奏运行，成交次数、触发时刻和收益不可直接当作 1 分钟或实盘结论。',
        '- 委托在信号产生后的下一可成交 5 分钟 K 线开盘价成交；成交量上限、A股整手、T+1和费用由严格撮合器约束。QMT 实盘订单接口及价格类型仍应以官方 `order_shares` 说明为准。[Python API, p.101]', '',
    ]
    lines.extend(strict_section('v53_nomom_CycleRiskExit（严格连续）', v53))
    lines.append('')
    lines.extend(strict_section('v54_nomom_TrendGuard（严格连续）', v54))
    lines.append('')
    lines.extend(v40_section(v40))
    lines.extend([
        '', '## 原因归因', '',
        '1. **v53 的跨日逆T止损吞掉小额盈利。** 17 个完整周期中，多数小赚，但 8月4日→5日、8月6日→7日、8月17日→18日、8月25日→27日四次逆向买回毛亏损合计 -2,043 元，超过其余盈利周期贡献。',
        '2. **v54 的趋势保护减少了低质量逆T。** 它把完整周期从 17 降至 8，避开 v53 的 8月6日、8月17日两次大亏，也同时过滤若干小盈利；相对 v53 改善 379 元，但仍受 8月4日和8月25日两次跨日亏损拖累。',
        '3. **v40 的每日状态重置与连续账户不相容。** 当日未闭合的正T/反T腿在策略内存中被丢弃，次日恢复机制只把低于200股视为缺口，却不把高于200股视为风险库存，导致持仓逐步堆到 600 股。股价由阶段高位回落时，额外库存放大亏损。',
        '4. **参数来自长飞光纤，未做跨标的归一化。** 长电科技的价格、ATR、日内结构和趋势阶段不同；直接换代码只能验证可运行性，不能证明参数适配。',
        '5. **日线信号验证支持“只在极端过热时减仓”。** 500日样本中，RSI>85 的5日下跌胜率100%（4次，样本很小）、偏离布林中轨>25%的5日下跌胜率78.6%（14次）、10日涨超30%的5日下跌胜率75%（20次）；普通ROC减速、RSI顶背离等信号较弱，不宜作为单独反T开仓条件。', '',
        '## 可优化点（按优先级）', '',
        '1. **先修 v40 库存上限。** 每日初始化时从真实持仓重建“超额库存”，正T开仓仅允许 `当前持仓 < 目标底仓 + 当日正T上限`；收盘前必须把超额库存降回200股，未完成则次日继续以恢复状态管理，不能清空账本。',
        '2. **v53/v54 加跨日亏损预算。** 反T卖出后若未在当日买回，次日采用基于卖价与ATR的分级回补，并限制最多持有1个完整交易日；对本样本的四个主要亏损周期做逐个回放验证，避免一个止损吞掉多笔小赚。',
        '3. **保留 v54 的趋势门控，但把二元禁入改为仓位缩放。** 强趋势只做0或100股，极端过热信号共振时才允许反T；这样有机会保留被过滤的小盈利，同时控制上涨趋势中的逆向风险。',
        '4. **参数按波动率归一化。** 用长电科技自身20日ATR%、开盘缺口和前30分钟实现波动率设定触发/回补阈值，并做滚动训练-验证，禁止用本区间最优参数直接回填。',
        '5. **补齐1分钟复核。** 取得完整1分钟数据后，用完全相同账户条件重跑；只有方向、主要亏损周期和超额收益结论稳定，才进入模拟盘。', '',
        '## 验收判断', '',
        '- 三个回测均无运行异常；v53/v54 无跨日丢弃策略腿，但期末各有一条 100 股未买回的逆T腿，因此只能标为研究结果。',
        '- v40 虽可运行，但每日重启造成账本丢弃与持仓漂移，**不通过连续账户风险验收**。',
        '- 本报告不构成实盘授权；5分钟数据结果不能替代完整1分钟严格回测。', '',
        '## 产物', '',
        '- `v53_strict_5m.json`、`v54_strict_5m.json`：严格连续逐笔原始结果。',
        '- `v40_daily_restart_5m.json`：每日重启、连续账户逐笔原始结果。',
        '- `../dayt_600584_signal_validation_20260913/600584_signal_validation_report.md`：600584日线卖出信号验证。',
    ])
    target = OUT / 'README.md'
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(target)


if __name__ == '__main__':
    main()
