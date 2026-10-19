# -*- coding: utf-8 -*-
"""Read-only portfolio diagnostics. Never creates intents or mutates the ledger."""
import math
import unicodedata


def pad(value, width):
    text = str(value)
    used = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)
    return text + ' ' * max(0, width - used)


def symbol(market, code):
    return f'{code:9} {pad(market.name(code), 16)}'


def number(value, spec='.2f'):
    return format(value, spec) if math.isfinite(float(value)) else 'N/A'


def daily_report(market, state, broker_cash, actual, log):
    policy = market.policy
    latest = market.primary.loc[market.primary.date.eq(market.asof) & market.primary.member].copy()
    breadth = float(latest.breadth.iloc[0])
    history = market.primary.groupby('date').breadth.first().sort_index().dropna()
    change = float((history - history.shift(policy.PRIMARY_BREADTH_LOOKBACK)).round(12).iloc[-1])
    benchmark = market.daily['000905.SH'].close
    equity = state['cash'] + sum(position['quantity'] * float(market.daily[code].close.iloc[-1])
                                for code, position in state['positions'].items())
    log(f'[Market] signal_date={market.asof} index_close={benchmark.iloc[-1]:.2f}'
        f' index_MA60={benchmark.tail(60).mean():.2f} breadth={breadth:.1%}'
        f' breadth_min={policy.BREADTH_MIN:.1%} breadth_change={change:+.1%}'
        f' primary_enabled={not market.weak} secondary_enabled={market.weak}')
    log(f'[Account] dedicated_initial={state["initial_cash"]:.2f} strategy_cash={state["cash"]:.2f}'
        f' previous_close_equity={equity:.2f}'
        f' broker_available={broker_cash:.2f} owned={len(state["positions"])}'
        f' pending={sum(not order["terminal"] for order in state["orders"].values())}')
    trend = (latest.adj_close.gt(latest.ma50) & latest.ma20.gt(latest.ma50)
             & latest.ma50.gt(latest.ma50_lag5) & latest.mom60.gt(0) & latest.mom60_rank.ge(.7))
    leader = latest.mom60_rank.ge(policy.PRIMARY_LEADER_RANK) & latest.adj_close.gt(latest.ma20)
    filters = [
        ('trend', trend), ('leader', leader), ('index_MA60', latest.market_strict),
        ('breadth', latest.breadth.ge(policy.BREADTH_MIN)), ('history_130', latest.age.ge(130)),
        ('turnover20', latest.amount20.ge(policy.MIN_AVERAGE_TURNOVER)),
        ('ATR_range', latest.atr_pct.between(.01, policy.PRIMARY_ENTRY_ATR_MAX)),
        ('daily_return', latest.ret.lt(.095)), ('efficiency', latest.efficiency.ge(policy.PRIMARY_MIN_EFFICIENCY)),
        ('breadth_confirmation', latest.breadth.notna() & (change >= policy.PRIMARY_BREADTH_MIN_CHANGE)),
    ]
    remaining = latest.member.copy()
    for label, mask in filters:
        remaining &= mask
        log(f'[PrimaryScreen] {label:21} passed={int(mask.sum()):3} remaining={int(remaining.sum()):3}')
    preliminary = latest.loc[trend & leader].sort_values(['score', 'code'], ascending=[False, True])
    log(f'[PrimaryPreMarket] trend_and_leader={len(preliminary)}; subsequent filters may reject these stocks')
    for row in preliminary.itertuples():
        log(f'[PrimaryPreMarket] {symbol(market, row.code)} | Close={row.close:8.2f}'
            f' | AdjReturn={row.ret:+7.2%} | MomentumRank={row.mom60_rank:6.1%} | Score={row.score:6.3f}'
            f' | AvgTurnover20={row.amount20 / 1e8:7.2f} x100M CNY | ATR={row.atr_pct:6.2%}')
    log(f'[PrimaryCandidate] final_selected={len(market.candidates)}; daily qualification only, not an order')
    for row in market.candidates.itertuples():
        cap = min(equity * row.weight, row.amount20 * .001, max(0., min(state['cash'], broker_cash)))
        log(f'[PrimaryCandidate] {symbol(market, row.code)} | Close={row.close:8.2f}'
            f' | Score={row.score:6.3f} | TargetWeight={row.weight:6.1%}'
            f' | PriorCloseBudgetRef<={cap:.2f} | next=minute_checks_reservations_and_shared_cash_allocation')
    log('[SecondaryScreen] all seven ETFs; eligible/leader describe daily conditions only')
    leaders = sum(bool(values['leader']) for values in market.secondary.values())
    log(f'[DailyPlan] primary_candidates={len(market.candidates)} secondary_daily_leaders={leaders}'
        ' | BUY requires current minute checks, available cash and ownership checks; no order is implied')
    for code in policy.CODES:
        signal = market.secondary[code]
        frame = market.daily[code]
        close = float(frame.close.iloc[-1])
        turnover = float(frame.amount.tail(20).mean())
        if signal['is_reversion']:
            average = float(frame.close.tail(policy.REVERSION_MA_DAYS).mean())
            failed = []
            if close <= average:
                failed.append('close<=long_MA')
            if not math.isfinite(signal['score']) or signal['score'] >= policy.REVERSION_RSI_MAX:
                failed.append('RSI_not_oversold')
            values = f'MA{policy.REVERSION_MA_DAYS}={average:8.3f} RSI2={number(signal["score"]):>6}'
            trigger = f'completed_close>={signal["prior_close"]:.3f}'
            group = 'reversion'
        else:
            failed = []
            if close <= signal['ma20']:
                failed.append('close<=MA20')
            if not math.isfinite(signal['score']) or signal['score'] <= 0:
                failed.append('momentum60<=0')
            values = f'MA20={signal["ma20"]:8.3f} Momentum60={number(signal["score"], "+.2%"):>8}'
            trigger = f'completed_close>={signal["ma20"]:.3f}_and_session_midpoint;prior_index_down={signal["index_down"]}'
            group = 'commodity'
        if turnover < policy.MIN_AVERAGE_TURNOVER:
            failed.append('turnover20_too_low')
        status = ','.join(failed) if failed else 'daily_pass'
        log(f'[SecondaryScreen] {symbol(market, code)} | Group={group:9} | Close={close:8.3f}'
            f' | {values} | AvgTurnover20={turnover / 1e8:7.2f} x100M CNY'
            f' | Eligible={signal["good"]} Leader={signal["leader"]} | {status}')
        if signal['leader']:
            log(f'[EntryPlan] {symbol(market, code)} | sleeve=secondary macro_enabled={market.weak}'
                f' | signal_window={policy.SECONDARY_ENTRY_START}-{policy.SECONDARY_ENTRY_END}'
                f' | trigger={trigger} | quantity=await_completed_minute_volume_and_cash')
    if not market.candidates.empty:
        log('[EntryPlan] primary signal_window=093100-093500; next-minute limit order; budgets compete for shared cash')
    for code, position in sorted(state['positions'].items()):
        log(f'[Holding] {symbol(market, code)} | sleeve={position["sleeve"]} qty={position["quantity"]}'
            f' sellable={actual.get(code, {}).get("sellable", 0)}'
            f' | AvgEntry={position["buy_notional"] / position["bought"]:.3f}'
            f' | EntryDate={position["entry_date"]} ExitLatched={position["exit_reason"] or "none"}')
        if position['sleeve'] == 'primary':
            reason = position['exit_reason']
            rows = market.primary.loc[market.primary.code.eq(code)]
            pending = rows.loc[rows.date.ge(position['entry_date']) & rows.date.gt(position.get('last_exit_date', ''))]
            for _, row in pending.iterrows():
                if reason:
                    break
                reason = policy.primary_exit(dict(position), row, rows)
            log(f'[ExitPlan] {symbol(market, code)} | sleeve=primary daily_exit={reason or "none"}'
                ' | execute_only_if_sellable_and_minute_checks_pass; not_submitted_yet')
        else:
            average = position['buy_notional'] / position['bought']
            profit = policy.REVERSION_PROFIT if code in policy.REVERSION_CODES else policy.COMMODITY_PROFIT
            log(f'[ExitPlan] {symbol(market, code)} | sleeve=secondary'
                f' StopReference={average * (1 - policy.SECONDARY_STOP):.3f}'
                f' ProfitReference={average * (1 + profit):.3f}'
                f' ExitLatched={position["exit_reason"] or "none"}'
                ' | await_completed_minute; not_a_resting_broker_stop')
    if not state['positions']:
        log('[ExitPlan] strategy_owned_positions=0; external holdings are not managed')


def minute_report(market, journal, actual, signal, bars, intents, log):
    state = journal.state
    policy = market.policy
    window = policy.SECONDARY_ENTRY_START <= signal[8:] <= policy.SECONDARY_ENTRY_END
    log(f'[TradePlan] signal={signal} orders={len(intents)}'
        f' pending={len(journal.active())} secondary_entry_window={window}')
    if window and market.weak:
        for code in policy.CODES:
            values = market.secondary[code]
            if not values['leader']:
                continue
            frame = bars[code]
            row = frame.iloc[-1]
            midpoint = (frame.high.max() + frame.low.min()) / 2
            passed = policy.secondary_entry(values, row, midpoint, signal[8:], True)
            log(f'[EntryCheck] {symbol(market, code)} | Close={row["close"]:.3f}'
                f' Midpoint={midpoint:.3f} VolumeLots={row["volume"]:.0f}'
                f' | price_trigger={passed} owned={code in state["positions"]}'
                f' broker_holding={code in actual} pending={bool(journal.active(code))}')
    for code, parent in sorted(state['parents'].items()):
        if parent['signal'] == signal:
            log(f'[ParentPlan] {symbol(market, code)} | BUY sleeve={parent["sleeve"]}'
                f' budget={parent["budget"]:.2f} remaining={parent["budget"] - parent["spent"]:.2f}'
                ' | lifetime=5_trading_minutes; not_a_fill')
    for code, position in sorted(state['positions'].items()):
        if position['exit_reason']:
            log(f'[ExitPlan] {symbol(market, code)} | SELL reason={position["exit_reason"]}'
                f' qty_remaining={position["quantity"]} broker_sellable={actual.get(code, {}).get("sellable", 0)}'
                f' pending={bool(journal.active(code))}')
    for intent in intents:
        log(f'[OrderPlan] {intent["side"]:4} {symbol(market, intent["code"])}'
            f' | sleeve={intent["sleeve"]} qty={intent["quantity"]} limit={intent["limit"]:.3f}'
            f' limit_notional={intent["quantity"] * intent["limit"]:.2f}'
            f' | key={intent["key"]} | not_submitted_yet')
    if not intents:
        log('[TradePlan] no_executable_order; review daily qualification, time window, holdings, pending orders, volume and available cash')


def execution_report(market, state, previous, log):
    fills_before, trades_before, statuses = previous
    for fill in state['fills'][fills_before:]:
        label = 'Fill' if fill['quantity'] > 0 else 'FillAdjustment'
        price = fill['notional'] / fill['quantity'] if fill['quantity'] else float('nan')
        log(f'[{label}] {fill["side"]:4} {symbol(market, fill["code"])}'
            f' | qty_delta={fill["quantity"]} price={number(price, ".3f")}'
            f' notional_delta={fill["notional"]:.2f} estimated_fee_delta={fill["estimated_fee"]:.2f}'
            f' | key={fill["key"]}')
    for key, order in state['orders'].items():
        if statuses.get(key) != (order['status'], order['terminal']):
            log(f'[OrderStatus] {symbol(market, order["code"])} | key={key} status={order["status"]}'
                f' terminal={order["terminal"]} cumulative_filled={order["applied_quantity"]}/{order["quantity"]}')
    for trade in state['trades'][trades_before:]:
        log(f'[PositionClosed] {symbol(market, trade["code"])} | sleeve={trade["sleeve"]}'
            f' pnl_after_estimated_fees={trade["pnl"]:+.2f}; reconcile_with_broker_statement')
