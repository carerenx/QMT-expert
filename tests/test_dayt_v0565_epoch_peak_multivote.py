import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pandas as pd

from output.analysis.compare_v51_v39_minute import load_strategy


s = load_strategy('v0565')


def prices(values, width=1.0):
    highs = [value + width / 2.0 for value in values]
    lows = [value - width / 2.0 for value in values]
    return highs, lows


class EpochPeakMultiVoteTests(unittest.TestCase):
    def crash_then_confirmed_recovery(self):
        closes = [100.0 + i for i in range(40)]
        closes += [130.0]
        closes += [129.0] * 20
        closes += [135.0, 136.0]
        highs, lows = prices(closes)
        return closes, highs, lows

    def test_vote_count_has_three_independent_named_votes(self):
        votes, count = s.count_reentry_votes(
            close=105.0, ma20=103.0, previous_ma20=104.0,
            breakout_level=102.0)

        self.assertEqual(votes, {
            'above_ma20': True,
            'ma20_rising': False,
            'short_breakout': True,
        })
        self.assertEqual(count, 2)

    def test_reentry_requires_two_consecutive_quorum_days(self):
        closes, highs, lows = self.crash_then_confirmed_recovery()

        one_day = s.replay_epoch_peak_risk(
            closes[:-1], highs[:-1], lows[:-1])
        two_days = s.replay_epoch_peak_risk(closes, highs, lows)

        self.assertFalse(one_day['risk_on'])
        self.assertEqual(one_day['entry_confirmation_streak'], 1)
        self.assertTrue(two_days['risk_on'])
        self.assertTrue(two_days['entry_signal'])
        self.assertEqual(two_days['entry_confirmation_streak'], 2)
        self.assertEqual(two_days['history'][-1]['transition'], 'REENTRY')

    def test_reentry_resets_position_peak_but_not_market_peak(self):
        closes, highs, lows = self.crash_then_confirmed_recovery()

        result = s.replay_epoch_peak_risk(closes, highs, lows)

        self.assertEqual(result['position_peak'], 136.0)
        self.assertEqual(result['market_peak'], 139.0)
        self.assertLess(result['position_peak'], result['market_peak'])

    def test_old_market_peak_no_longer_drives_active_stop(self):
        closes, highs, lows = self.crash_then_confirmed_recovery()
        closes += [134.0]
        high, low = prices([134.0])
        highs += high
        lows += low

        result = s.replay_epoch_peak_risk(closes, highs, lows)

        self.assertTrue(result['risk_on'])
        self.assertEqual(result['market_peak'], 139.0)
        self.assertEqual(result['position_peak'], 136.0)
        self.assertAlmostEqual(result['stop'], 133.0)

    def test_invalid_daily_series_is_rejected(self):
        self.assertIsNone(s.replay_epoch_peak_risk([100.0], [101.0], [99.0]))
        closes = [100.0] * 40
        highs, lows = prices(closes)
        lows[-1] = float('nan')
        self.assertIsNone(s.replay_epoch_peak_risk(closes, highs, lows))


class RuntimeSafetyTests(unittest.TestCase):
    @staticmethod
    def _daily_frame(dates):
        closes = [100.0 + index for index in range(len(dates))]
        return pd.DataFrame({
            'open': closes,
            'high': [value + 1.0 for value in closes],
            'low': [value - 1.0 for value in closes],
            'close': closes,
            'volume': [1000.0] * len(dates),
            'amount': [100000.0] * len(dates),
        }, index=dates)

    def test_v0565_aligns_harmless_oldest_date_window_difference(self):
        front = self._daily_frame(['20260921', '20260922', '20260923'])
        raw = self._daily_frame(['20260919', '20260921', '20260922'])

        def get_local_data(**kwargs):
            frame = front if kwargs['dividend_type'] == 'front' else raw
            return {'601869.SH': frame.copy()}

        shared = SimpleNamespace(xtdata=SimpleNamespace(
            download_history_data=lambda *args, **kwargs: None,
            get_local_data=get_local_data,
            get_trading_dates=lambda *args, **kwargs: [
                '20260919', '20260921', '20260922', '20260923'],
        ))
        connector = s.SymbolConnector(shared, '601869.SH')

        snapshot = connector.load_daily_snapshot(
            3, today='20260923', now_hms='09:00:00', retries=1)

        self.assertIsNotNone(snapshot)
        self.assertEqual(list(snapshot['adjusted'].index),
                         ['20260921', '20260922'])
        self.assertEqual(list(snapshot['raw'].index),
                         ['20260921', '20260922'])

        plain_connector = s.MiniQMTConnector()
        plain_connector.xtdata = shared.xtdata
        self.assertIsNone(plain_connector.load_daily_snapshot(
            3, today='20260923', now_hms='09:00:00', retries=1,
            stock_code='601869.SH'))

    def test_v0565_rejects_recent_date_mismatch(self):
        front = self._daily_frame(['20260921', '20260922', '20260923'])
        raw = self._daily_frame(['20260920', '20260921', '20260924'])

        def get_local_data(**kwargs):
            frame = front if kwargs['dividend_type'] == 'front' else raw
            return {'601869.SH': frame.copy()}

        shared = SimpleNamespace(xtdata=SimpleNamespace(
            download_history_data=lambda *args, **kwargs: None,
            get_local_data=get_local_data,
            get_trading_dates=lambda *args, **kwargs: [
                '20260920', '20260921', '20260922', '20260923'],
        ))
        connector = s.SymbolConnector(shared, '601869.SH')

        snapshot = connector.load_daily_snapshot(
            3, today='20260923', now_hms='09:00:00', retries=1)

        self.assertIsNone(snapshot)

    def test_ex_dividend_reference_close_is_accepted_via_adjusted_series(self):
        dates = ['20260922', '20260923', '20260924']
        front = self._daily_frame(dates)
        raw = self._daily_frame(dates)
        front.loc['20260923', ['open', 'high', 'low', 'close']] = [
            71.77, 72.00, 71.00, 71.77]
        raw.loc['20260923', ['open', 'high', 'low', 'close']] = [
            72.40, 72.52, 70.98, 71.82]

        def get_local_data(**kwargs):
            frame = front if kwargs['dividend_type'] == 'front' else raw
            return {'600584.SH': frame.copy()}

        shared = SimpleNamespace(xtdata=SimpleNamespace(
            download_history_data=lambda *args, **kwargs: None,
            get_local_data=get_local_data,
            get_trading_dates=lambda *args, **kwargs: dates,
        ))
        connector = s.SymbolConnector(shared, '600584.SH')

        snapshot = connector.load_daily_snapshot(
            3, today='20260924', tick_last_close=71.77,
            tick_time='20260924093000', now_hms='09:30:00', retries=1)

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot['tick_last_close_adjusted_match'])
        self.assertEqual(snapshot['verified_last_close'], 71.82)

    def test_close_mismatch_is_rejected_when_neither_price_series_matches(self):
        dates = ['20260922', '20260923', '20260924']
        front = self._daily_frame(dates)
        raw = self._daily_frame(dates)
        front.loc['20260923', 'close'] = 71.70
        raw.loc['20260923', 'close'] = 71.82

        def get_local_data(**kwargs):
            frame = front if kwargs['dividend_type'] == 'front' else raw
            return {'600584.SH': frame.copy()}

        shared = SimpleNamespace(xtdata=SimpleNamespace(
            download_history_data=lambda *args, **kwargs: None,
            get_local_data=get_local_data,
            get_trading_dates=lambda *args, **kwargs: dates,
        ))
        connector = s.SymbolConnector(shared, '600584.SH')

        snapshot = connector.load_daily_snapshot(
            3, today='20260924', tick_last_close=71.77,
            tick_time='20260924093000', now_hms='09:30:00', retries=1)

        self.assertIsNone(snapshot)

    def test_daily_init_fetches_one_snapshot_without_inline_retry_sleep(self):
        dates = pd.bdate_range('2025-08-27', periods=280).strftime('%Y%m%d')
        frame = self._daily_frame(list(dates))
        snapshot = {
            'adjusted': frame,
            'raw': frame,
            'last_complete_date': dates[-1],
            'verified_last_close': float(frame.iloc[-1]['close']),
        }
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        runner._init_state()
        runner._log = Mock()
        runner.conn = Mock()
        runner.conn.load_daily_snapshot.return_value = snapshot
        runner.ctx = Mock()
        runner.ctx.get_full_tick.return_value = {
            '600584.SH': {
                'open': 100.0,
                'lastPrice': 100.0,
                'lastClose': float(frame.iloc[-1]['close']),
                'timetag': '20260924090000',
            },
        }

        def refresh_position():
            runner.st['base_shares'] = 1000
            runner.st['base_can_use'] = 1000
            runner.st['base_cost'] = 90.0

        risk_result = {
            'risk_on': True,
            'reason': 'RISK-ON',
            'atr': 1.0,
            'position_peak': 100.0,
            'market_peak': 100.0,
            'ma20': 99.0,
            'previous_ma20': 98.0,
            'breakout_level': 99.0,
            'daily_close': 100.0,
            'exit_signal': False,
            'entry_signal': False,
            'entry_votes': {},
            'entry_vote_count': 0,
            'entry_confirmation_streak': 0,
            'flip_count': 0,
        }
        signal = {
            'do_short': True,
            'blocked_reason': '',
            'open_price': 100.0,
            'volume_current': 1000.0,
            'atr_pct': 0.02,
            'sell_mult': 0.5,
            'sell_trigger': 101.0,
        }
        runner._refresh_position = Mock(side_effect=refresh_position)
        account = SimpleNamespace(m_dAvailable=100000.0, m_dBalance=200000.0)

        with patch.object(s, 'replay_epoch_peak_risk', return_value=risk_result), \
                patch.object(s, 'compute_signal', return_value=signal), \
                patch.object(s, 'get_trade_detail_data', return_value=[account]):
            runner._daily_init()

        self.assertTrue(runner.st['initialized'])
        runner.conn.load_daily_snapshot.assert_called_once()
        _, kwargs = runner.conn.load_daily_snapshot.call_args
        self.assertEqual(kwargs['retries'], 1)
        self.assertEqual(kwargs['retry_delay'], 0.0)

    def test_after_hours_signal_anchor_uses_latest_complete_close(self):
        price, source = s.resolve_signal_open('16:00:00', 500.0, 450.0)

        self.assertEqual(price, 450.0)
        self.assertIn('AFTER-HOURS', source)

    def test_auction_period_is_pre_market(self):
        runner = object.__new__(s.StrategyRunner)

        self.assertEqual(runner._detect_period('09:29:59'),
                         runner._PERIOD_PRE_MARKET)
        self.assertEqual(runner._detect_period('09:30:00'),
                         runner._PERIOD_MORNING)

    def test_market_closed_day_never_initializes_or_advances_state_machine(self):
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
            is_trading_day=Mock(return_value=False),
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        runner._try_daily_init = Mock(return_value=True)
        runner._get_tick_price = Mock(return_value=(
            {'timetag': '20260926135823'}, 68.78))
        runner._handle_afternoon = Mock(return_value=iter([0.5]))
        fake_datetime = Mock(wraps=s.datetime)
        fake_datetime.now.return_value = s.datetime(2026, 9, 26, 13, 58, 23)

        with patch.object(s, 'datetime', fake_datetime), \
                patch.object(s.cfg, 'now_hms', return_value='13:58:23'):
            worker = runner.run()
            delay = next(worker)

        self.assertEqual(delay, 30)
        runner._try_daily_init.assert_not_called()
        runner._get_tick_price.assert_not_called()
        runner._handle_afternoon.assert_not_called()
        self.assertEqual(runner.st['fstate'], s.STATE_IDLE)
        self.assertEqual(runner.st['trade_count_long'], 0)
        worker.close()

    def test_stale_tick_date_blocks_intraday_handler(self):
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
            is_trading_day=Mock(return_value=True),
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        runner._try_daily_init = Mock(return_value=True)
        runner._get_tick_price = Mock(return_value=(
            {'timetag': '20260924150000'}, 68.78))
        runner._handle_afternoon = Mock(return_value=iter([0.5]))
        fake_datetime = Mock(wraps=s.datetime)
        fake_datetime.now.return_value = s.datetime(2026, 9, 28, 13, 58, 23)

        with patch.object(s, 'datetime', fake_datetime), \
                patch.object(s.cfg, 'now_hms', return_value='13:58:23'):
            worker = runner.run()
            delay = next(worker)

        self.assertEqual(delay, 1)
        runner._handle_afternoon.assert_not_called()
        worker.close()

    def test_post_market_summary_waits_for_verified_same_day_daily_close(self):
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        runner._init_state()
        runner._print_post_market = Mock()
        runner._verified_close = Mock(return_value=(68.78, 'RAW-DAILY'))
        tick = {
            'lastPrice': 69.03,
            'lastClose': 71.77,
            'timetag': '20260924162353',
        }

        first = runner._handle_post_market(
            tick, 1000.0, '20260924', '16:23:53')
        second = runner._handle_post_market(
            tick, 1030.0, '20260924', '16:24:23')
        self.assertEqual(next(first), 30)
        self.assertEqual(next(second), 30)

        runner._verified_close.assert_called_once_with(
            tick, 1000.0, '20260924', '16:23:53')
        runner._print_post_market.assert_called_once_with(
            68.78, '20260924', 'RAW-DAILY')

    def test_verified_close_uses_finalized_raw_daily_bar_not_tick_price(self):
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        raw = self._daily_frame(['20260923', '20260924'])
        raw.loc['20260924', 'close'] = 68.78
        runner.conn = Mock()
        runner.conn.load_daily_snapshot.return_value = {
            'raw': raw,
            'adjusted': raw.copy(),
            'last_complete_date': '20260924',
        }
        tick = {
            'lastPrice': 69.03,
            'lastClose': 71.77,
            'timetag': '20260924162353',
        }

        verified_close = runner._verified_close(
            tick, 1000.0, '20260924', '16:23:53')
        close_price = verified_close[0]
        source = verified_close[1]

        self.assertEqual(close_price, 68.78)
        self.assertEqual(source, 'RAW-DAILY')
        runner.conn.refresh_daily_cache.assert_called_once_with()
        kwargs = runner.conn.load_daily_snapshot.call_args.kwargs
        self.assertEqual(kwargs['today'], '20260924')
        self.assertEqual(kwargs['now_hms'], '16:23:53')

    def test_failed_daily_init_never_enters_morning_handler(self):
        portfolio = SimpleNamespace(
            conn=SimpleNamespace(),
            dry_run=True,
            order_uncertain=False,
            is_trading_day=Mock(return_value=True),
        )
        runner = s.StrategyRunner(portfolio, '600584.SH', '长电科技')
        runner._log = Mock()

        def fail_daily_init():
            runner.st['trade_date'] = '20260923'
            runner._lock_all_trading('daily data unavailable or stale')

        runner._daily_init = Mock(side_effect=fail_daily_init)
        runner._get_tick_price = Mock(return_value=({}, 100.0))
        runner._handle_morning = Mock(return_value=iter([0.5]))
        fake_datetime = Mock(wraps=s.datetime)
        fake_datetime.now.return_value = s.datetime(2026, 9, 23, 10, 9, 12)

        with patch.object(s.cfg, 'now_hms', return_value='10:09:12'), \
                patch.object(s._time, 'time', return_value=100.0), \
                patch.object(s, 'datetime', fake_datetime):
            worker = runner.run()
            delay = next(worker)
            worker.close()

        self.assertFalse(runner.st['initialized'])
        self.assertEqual(delay, 1)
        runner._handle_morning.assert_not_called()

    def test_recalc_open_trigger_ignores_incomplete_daily_signal(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {'daily_signal': {}}
        runner._log = Mock()
        runner._update_fwd_buy_trigger = Mock()

        result = runner._recalc_open_trigger({'open': 100.0}, 100.0)

        self.assertFalse(result)
        runner._update_fwd_buy_trigger.assert_not_called()

    def test_sell_trigger_hit_logs_once_per_rev_t_cycle(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {
            'daily_signal': {'sell_trigger': 461.07},
            'do_short': True,
            'base_can_use': 800,
            'trade_count_short': 0,
            '_sell_trigger_hit_key': '',
        }
        runner._log = Mock()

        self.assertFalse(runner._log_sell_trigger_hit(461.06))
        self.assertTrue(runner._log_sell_trigger_hit(461.07))
        self.assertFalse(runner._log_sell_trigger_hit(462.00))
        self.assertEqual(runner._log.call_count, 1)
        message = runner._log.call_args.args[0]
        self.assertIn('[SELL-TRIG HIT]', message)
        self.assertIn('planned=400 sh', message)

        runner.st['trade_count_short'] = 1
        self.assertTrue(runner._log_sell_trigger_hit(462.00))
        self.assertEqual(runner._log.call_count, 2)

    def test_sell_trigger_hit_logs_block_reason_when_rev_t_disabled(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {
            'daily_signal': {'sell_trigger': 461.07},
            'do_short': False,
            'short_reason': 'sellable shares insufficient',
            'base_can_use': 0,
            'trade_count_short': 0,
            '_sell_trigger_hit_key': '',
        }
        runner._log = Mock()

        self.assertTrue(runner._log_sell_trigger_hit(462.00))

        message = runner._log.call_args.args[0]
        self.assertIn('BLOCKED sellable shares insufficient', message)

    def test_sell_trigger_hit_falls_back_to_signal_block_reason(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {
            'daily_signal': {
                'sell_trigger': 461.07,
                'short_reason': 'sellable 0 sh < 100 sh',
            },
            'do_short': False,
            'short_reason': '',
            'base_can_use': 0,
            'trade_count_short': 0,
            '_sell_trigger_hit_key': '',
        }
        runner._log = Mock()

        self.assertTrue(runner._log_sell_trigger_hit(462.00))

        message = runner._log.call_args.args[0]
        self.assertIn('BLOCKED sellable 0 sh < 100 sh', message)

    def test_rev_t_sell_fill_immediately_logs_next_buyback_plan(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {
            'daily_signal': {'atr_pct': 0.10},
            'short_arm_bars': 2,
            'peak_price': 462.00,
            'short_arm_trigger': 461.07,
            'trade_count_short': 1,
            'short_legs': [],
        }
        runner._execution_price = 461.07
        runner._log = Mock()
        runner._new_leg_block_reason = Mock(return_value='')
        runner._submit_order = Mock(return_value=('FILLED', -100))

        with patch.object(s, 'confirmed_short_reversal', return_value=True):
            runner._handle_spiking(461.14)

        messages = [call.args[0] for call in runner._log.call_args_list]
        plans = [message for message in messages if '[NEXT-PLAN]' in message]
        self.assertEqual(len(plans), 2)
        self.assertIn('buyback 100 sh', plans[0])
        self.assertIn('sold Y461.07', plans[0])
        self.assertIn('rebound >= 0.10%', plans[0])
        self.assertNotIn('force buyback', plans[1])
        self.assertIn('no end-of-day forced close', plans[1])
        self.assertEqual(runner.st['fstate'], s.STATE_SOLD)

    def test_late_short_leg_is_not_forced_closed(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {
            'fstate': s.STATE_DIPPING,
            'dip_price': 135.0,
            'short_legs': [(140.0, 200)],
        }
        runner.execution_book = Mock()
        runner.execution_book.legs = {'SHORT': []}
        runner._last_heartbeat = 0.0
        runner._log = Mock()
        runner._file_log = Mock()
        runner._submit_buyback_order = Mock(return_value=('FILLED', 200))

        list(runner._handle_late({}, 135.0, 1.0))

        runner._submit_buyback_order.assert_not_called()
        self.assertEqual(runner.st['fstate'], s.STATE_DIPPING)
        self.assertEqual(runner.st['short_legs'], [(140.0, 200)])

    def test_late_forward_leg_is_not_forced_closed(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {
            'fstate': s.STATE_BT_SPIKING,
            'long_legs': [(130.0, 100)],
        }
        runner.execution_book = Mock()
        runner.execution_book.legs = {'LONG': []}
        runner._last_heartbeat = 0.0
        runner._log = Mock()
        runner._file_log = Mock()
        runner._submit_order = Mock(return_value=('FILLED', -100))

        list(runner._handle_late({}, 135.0, 1.0))

        runner._submit_order.assert_not_called()
        self.assertEqual(runner.st['fstate'], s.STATE_BT_SPIKING)
        self.assertEqual(runner.st['long_legs'], [(130.0, 100)])

    def test_outer_risk_signal_is_observation_only_and_never_orders(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {
            'risk_ready': True,
            'risk_on': False,
            'risk_reason': 'EXIT observed',
            'base_shares': 800,
            'base_can_use': 800,
        }
        runner._log = Mock()
        runner._submit_order = Mock()

        settled = runner._apply_risk_switch(135.0)

        self.assertTrue(settled)
        runner._submit_order.assert_not_called()
        self.assertEqual(runner.st['base_shares'], 800)
        self.assertEqual(runner.st['risk_transition_status'], 'OBSERVE-ONLY')

    def test_risk_off_does_not_block_new_inner_leg(self):
        runner = object.__new__(s.StrategyRunner)
        runner.portfolio = Mock(order_uncertain=False)
        runner.st = {
            'locked': False,
            'risk_ready': False,
            'risk_on': False,
            'reentry_pending': None,
        }

        reason = runner._new_leg_block_reason()

        self.assertEqual(reason, '')

    def test_observation_is_marked_done_without_risk_order(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {
            'risk_switch_done': '',
            'trade_date': '20260923',
            'risk_on': False,
        }
        runner._last_heartbeat = 0.0
        runner._file_log = Mock()
        runner._apply_risk_switch = Mock(return_value=True)
        runner._refresh_capacity = Mock()

        self.assertTrue(runner._ensure_risk_transition(135.0, 1.0))
        self.assertEqual(runner.st['risk_switch_done'], '20260923')
        runner._refresh_capacity.assert_called_once_with()


class RevTSizingTests(unittest.TestCase):
    def test_daily_brief_prints_trigger_formulas_even_when_fwd_t_is_blocked(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner._log = Mock()
        runner.st = {
            'risk_on': True,
            'risk_reason': 'observation only',
            'risk_entry_votes': {},
            'do_short': True,
            'do_long': False,
            'base_shares': 1000,
            'base_can_use': 1000,
            'avail_cash': 5000,
            'long_reason': 'insufficient cash for 1 lot',
        }
        signal = {
            'open_price': 100.0,
            'atr_pct': 0.10,
            'sell_mult': 0.50,
            'sell_trigger_raw': 103.40,
            'daily_range_ma10': 0.08,
            'range_capped': False,
            'sell_trigger': 103.40,
            'buy_trigger_floor': 97.00,
            'buy_trigger_trail': 98.00,
            'buy_trigger_max_trail': 98.00,
            'buy_trigger': 98.00,
            'sellback_target_hint': 99.18,
        }

        runner._print_daily_brief(signal)

        logs = '\n'.join(call.args[0] for call in runner._log.call_args_list)
        self.assertIn(
            '[REV-T FORMULA] raw Y103.40 = open Y100.00 × '
            '(1 + ATR 10.00% × mult 0.500 × scale 0.68)', logs)
        self.assertIn(
            'cap Y106.40 = open Y100.00 × '
            '(1 + rangeMA10 8.00% × cap-mult 0.80)', logs)
        self.assertIn(
            'final Y103.40 = min(raw Y103.40, cap Y106.40) [RAW]', logs)
        self.assertIn(
            '[REV-T SIZE] min(1000, max(100, floor('
            '1000 × 60% / 100) × 100)) = 600 sh', logs)
        self.assertIn(
            '[REV-T FORMULA] buyback Y101.85 = sell Y103.40 × '
            '(1 - ATR 10.00% × 0.15)', logs)
        self.assertIn(
            '[FWD-T FORMULA] buy Y98.00 = max('
            'floor Y97.00 = open Y100.00 × (1 - 3.00%), '
            'trail-max Y98.00 = max-observed(price × (1 - 2.00%)))', logs)
        self.assertIn(
            '[FWD-T FORMULA] sellback Y99.18 = buy Y98.00 × '
            '(1 + 1.20%)', logs)
        self.assertIn('[FWD-T] BLOCKED: insufficient cash for 1 lot', logs)

    def test_sell_size_is_sixty_percent_rounded_down_to_whole_lots(self):
        self.assertEqual(s.calculate_rev_t_sell_shares(800, 100), 400)
        self.assertEqual(s.calculate_rev_t_sell_shares(1000, 100), 600)

    def test_sell_size_has_one_lot_minimum_when_one_lot_is_sellable(self):
        self.assertEqual(s.calculate_rev_t_sell_shares(100, 100), 100)
        self.assertEqual(s.calculate_rev_t_sell_shares(150, 100), 100)
        self.assertEqual(s.calculate_rev_t_sell_shares(200, 100), 100)

    def test_sell_size_is_zero_below_one_sellable_lot(self):
        self.assertEqual(s.calculate_rev_t_sell_shares(99, 100), 0)

    def test_sell_size_rejects_invalid_fraction(self):
        with self.assertRaises(ValueError):
            s.calculate_rev_t_sell_shares(800, 100, 0)

    def test_live_rev_t_sizing_entry_uses_sixty_percent_rule(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {}
        runner._paired_long_capacity = Mock(return_value={
            'pairing_shares': 800,
        })

        shares = runner._new_t_shares(500.0, 'SELL')

        self.assertEqual(shares, 400)

    def test_capacity_keeps_rev_t_enabled_for_one_sellable_lot(self):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {
            'daily_signal': {
                'do_short': True,
                'short_signal_allowed': True,
                'short_reason': '',
            },
            'base_can_use': 100,
            'long_legs': [],
            'trade_count_short': 0,
            'trade_count_long': 0,
            'do_short': True,
            'do_long': False,
        }
        runner._refresh_position = Mock()
        runner._cur_price = Mock(return_value=100.0)
        runner._available_cash = Mock(return_value=0.0)
        runner._log = Mock()

        runner._refresh_capacity()

        self.assertTrue(runner.st['do_short'])
        self.assertEqual(runner.st['short_reason'], '')


class MultiSymbolPortfolioTests(unittest.TestCase):
    @staticmethod
    def _worker(events, code):
        while True:
            events.append(code)
            yield 0.5

    def test_discovers_all_account_holdings_with_independent_runners(self):
        positions = [
            SimpleNamespace(
                stock_code='601869.SH', stock_name='长飞光纤', volume=800),
            SimpleNamespace(
                stock_code='000001.SZ', stock_name='平安银行', volume=1200),
            SimpleNamespace(
                stock_code='600000.SH', stock_name='零仓位', volume=0),
        ]
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio.conn.trader = SimpleNamespace(
            query_stock_positions=lambda account: positions)
        portfolio.conn._account_obj = object()

        portfolio.refresh_holdings(0.0)

        self.assertEqual(set(portfolio.runners), {
            '601869.SH', '000001.SZ',
        })
        self.assertEqual(set(portfolio.tasks), set(portfolio.runners))
        first = portfolio.runners['601869.SH']
        second = portfolio.runners['000001.SZ']
        first._init_state()
        second._init_state()
        first.st['short_legs'].append((461.07, 100))
        self.assertEqual(second.st['short_legs'], [])

        portfolio.refresh_holdings(61.0)
        self.assertEqual(len(portfolio.runners), 2)

    def test_scheduler_advances_each_due_symbol_worker(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        events = []
        first = self._worker(events, '601869.SH')
        second = self._worker(events, '000001.SZ')
        portfolio.tasks = {
            '601869.SH': (first, 0.0),
            '000001.SZ': (second, 0.0),
        }

        portfolio._run_due_tasks(1.0)

        self.assertEqual(events, ['601869.SH', '000001.SZ'])
        self.assertEqual(set(portfolio.tasks), {
            '601869.SH', '000001.SZ',
        })
        first.close()
        second.close()

    def test_slow_holdings_query_is_identified(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio.conn.trader = SimpleNamespace(
            query_stock_positions=Mock(return_value=[]))
        portfolio.conn._account_obj = object()

        with patch.object(s._time, 'monotonic', side_effect=[100.0, 106.0]), \
                patch.object(s, '_log') as log:
            portfolio.refresh_holdings(100.0)

        self.assertIn('[HOLDINGS-LAG] elapsed=6.0s', log.call_args.args[0])

    def test_slow_symbol_worker_is_identified(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        task = self._worker([], '600584.SH')
        portfolio.tasks = {'600584.SH': (task, 0.0)}

        with patch.object(s._time, 'monotonic', side_effect=[100.0, 106.5]), \
                patch.object(s, '_log') as log:
            portfolio._run_due_tasks(100.0)

        self.assertIn('[WORKER-LAG] code=600584.SH elapsed=6.5s',
                      log.call_args.args[0])
        task.close()

    def test_new_holding_is_added_during_runtime_and_zero_runner_is_retained(self):
        positions = [SimpleNamespace(
            stock_code='601869.SH', stock_name='长飞光纤', volume=800)]
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio.conn.trader = SimpleNamespace(
            query_stock_positions=lambda account: positions)
        portfolio.conn._account_obj = object()

        portfolio.refresh_holdings(0.0)
        positions.append(SimpleNamespace(
            stock_code='000001.SZ', stock_name='平安银行', volume=1000))
        portfolio.refresh_holdings(61.0)
        positions[0].volume = 0
        portfolio.refresh_holdings(122.0)

        self.assertEqual(set(portfolio.runners), {
            '601869.SH', '000001.SZ',
        })

    def test_trading_calendar_fails_closed_on_holiday(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio.conn.xtdata = SimpleNamespace(
            get_trading_dates=Mock(return_value=['20260924']))

        self.assertFalse(portfolio.is_trading_day('20260926', '600584.SH'))
        portfolio.conn.xtdata.get_trading_dates.assert_called_once_with(
            'SH', start_time='', end_time='20260926', count=1)

    def test_negative_calendar_result_retries_and_recovers(self):
        for initial in ([], ['20261008'], RuntimeError('offline')):
            with self.subTest(initial=initial):
                portfolio = s.PortfolioRunner(dry_run=True)
                query = Mock(side_effect=[initial, ['20261009']])
                portfolio.conn.xtdata = SimpleNamespace(get_trading_dates=query)
                with patch.object(s._time, 'monotonic', return_value=100.0) as clock:
                    self.assertFalse(portfolio.is_trading_day('20261009', '600584.SH'))
                    clock.return_value = 159.0
                    self.assertFalse(portfolio.is_trading_day('20261009', '600584.SH'))
                    self.assertEqual(query.call_count, 1)
                    clock.return_value = 160.0
                    self.assertTrue(portfolio.is_trading_day('20261009', '600584.SH'))
                    clock.return_value = 1000.0
                    self.assertTrue(portfolio.is_trading_day('20261009', '600584.SH'))
                    self.assertEqual(query.call_count, 2)

    def test_calendar_holiday_stays_closed_after_retry(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        query = Mock(return_value=['20261009'])
        portfolio.conn.xtdata = SimpleNamespace(get_trading_dates=query)
        with patch.object(s._time, 'monotonic', return_value=100.0) as clock:
            self.assertFalse(portfolio.is_trading_day('20261010', '600584.SH'))
            clock.return_value = 160.0
            self.assertFalse(portfolio.is_trading_day('20261010', '600584.SH'))
        self.assertEqual(query.call_count, 2)

    def test_date_rollover_logs_new_day_trading_status(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio._calendar_date = '20260924'
        portfolio.is_trading_day = Mock(return_value=False)

        with patch.object(s, '_log') as log:
            portfolio._observe_date_rollover('20260926')

        log.assert_called_once_with(
            '[DATE-ROLLOVER] old=20260924 new=20260926 trading_day=False')

    def test_scheduler_lag_is_reported_after_worker_or_host_stall(self):
        portfolio = s.PortfolioRunner(dry_run=True)
        portfolio._last_scheduler_tick = 100.0
        portfolio._last_scheduler_wall = '2026-09-24 14:26:38'

        with patch.object(s, '_log') as log:
            portfolio._observe_scheduler(
                7115.0, '2026-09-24 16:23:53')

        self.assertIn('[SCHEDULER-LAG]', log.call_args.args[0])
        self.assertIn('elapsed=7015.0s', log.call_args.args[0])
        self.assertIn('previous=2026-09-24 14:26:38', log.call_args.args[0])

    def test_portfolio_liveness_heartbeat_is_half_hourly(self):
        portfolio = s.PortfolioRunner(dry_run=True)

        with patch.object(s, '_log') as log:
            portfolio._emit_liveness_heartbeat(
                1000.0, '20260924', '16:00:00', True)
            portfolio._emit_liveness_heartbeat(
                2799.0, '20260924', '16:29:59', True)
            portfolio._emit_liveness_heartbeat(
                2800.0, '20260924', '16:30:00', True)

        self.assertEqual(log.call_count, 2)
        self.assertIn('state=POST-MARKET', log.call_args_list[0].args[0])
        self.assertIn('interval=1800s', log.call_args_list[1].args[0])



class ScheduledDailyPlanTests(unittest.TestCase):
    def make_runner(self):
        portfolio = SimpleNamespace(conn=SimpleNamespace(), dry_run=True,
                                    order_uncertain=False,
                                    is_trading_day=Mock(return_value=True))
        runner = s.StrategyRunner(portfolio, '600584.SH', 'test')
        runner._init_state()
        runner._log = Mock()
        runner._print_daily_brief = Mock()
        runner._get_tick_price = Mock(return_value=(
            {'timetag': '20261009092600'}, 101.0))

        def initialize():
            today = s.datetime.now().strftime('%Y%m%d')
            runner.st.update(initialized=True, trade_date=today,
                             daily_signal={'sell_trigger': 102.0,
                                           'buy_trigger': 99.0,
                                           'plan_price': 101.0,
                                           'open_price': 100.5,
                                           'open_price_source': 'AUCTION_OPEN'})
        runner._daily_init = Mock(side_effect=initialize)
        return runner

    def test_pre_market_worker_prepares_at_0926_once(self):
        runner = self.make_runner()
        wall = Mock(wraps=s.datetime)
        wall.now.return_value = s.datetime(2026, 10, 9, 9, 25, 59)
        with patch.object(s, 'datetime', wall), \
                patch.object(s.cfg, 'now_hms', return_value='09:25:59') as clock:
            worker = runner.run()
            self.assertEqual(next(worker), 1)
            runner._daily_init.assert_not_called()
            wall.now.return_value = s.datetime(2026, 10, 9, 9, 26)
            clock.return_value = '09:26:00'
            self.assertEqual(next(worker), 1)
            runner._daily_init.assert_called_once()
            runner._print_daily_brief.assert_called_once()
            runner.st['trade_count_short'] = 1
            runner.st['short_legs'] = [(102.0, 100)]
            self.assertEqual(next(worker), 1)
            self.assertEqual(runner.st['trade_count_short'], 1)
            self.assertEqual(runner.st['short_legs'], [(102.0, 100)])
            runner._daily_init.assert_called_once()
            worker.close()
        self.assertTrue(any('[DAILY-PLAN-READY]' in call.args[0]
                            for call in runner._log.call_args_list))

    def test_late_start_and_next_day_prepare(self):
        runner = self.make_runner()
        wall = Mock(wraps=s.datetime)
        wall.now.return_value = s.datetime(2026, 10, 9, 10, 0)
        with patch.object(s, 'datetime', wall), \
                patch.object(s.cfg, 'now_hms', return_value='10:00:00') as clock:
            self.assertTrue(runner._try_daily_init(1000.0, '20261009'))
            runner._daily_init.assert_called_once()
            wall.now.return_value = s.datetime(2026, 10, 12, 9, 26)
            clock.return_value = '09:26:00'
            runner._get_tick_price.return_value = (
                {'timetag': '20261012092600'}, 103.0)
            self.assertTrue(runner._try_daily_init(2000.0, '20261012'))
        self.assertEqual(runner._daily_init.call_count, 2)
        self.assertEqual(runner._print_daily_brief.call_count, 2)

    def test_stale_price_retries_after_60_seconds(self):
        runner = self.make_runner()
        runner._get_tick_price.return_value = (
            {'timetag': '20261008150000'}, 101.0)
        wall = Mock(wraps=s.datetime)
        wall.now.return_value = s.datetime(2026, 10, 9, 9, 26)
        with patch.object(s, 'datetime', wall), \
                patch.object(s.cfg, 'now_hms', return_value='09:26:00'):
            self.assertFalse(runner._try_daily_init(1000.0, '20261009'))
            runner._daily_init.assert_not_called()
            runner._get_tick_price.return_value = (
                {'timetag': '20261009092700'}, 101.0)
            self.assertFalse(runner._try_daily_init(1059.0, '20261009'))
            self.assertTrue(runner._try_daily_init(1060.0, '20261009'))
        runner._daily_init.assert_called_once()

    def test_auction_anchor_and_missing_open_fallback(self):
        self.assertEqual(s.resolve_signal_open('09:26:00', 101.0, 99.0),
                         (101.0, 'AUCTION_OPEN'))
        self.assertEqual(s.resolve_signal_open('09:25:59', 101.0, 99.0),
                         (99.0, 'AFTER-HOURS latest_complete_close'))
        self.assertEqual(s.resolve_signal_open('09:26:00', 0.0, 99.0),
                         (99.0, 'LATEST_COMPLETE_CLOSE fallback'))
        self.assertEqual(s.resolve_signal_open('09:30:00', 102.0, 99.0),
                         (102.0, 'TICK_OPEN'))


class ForwardFillNextPlanTests(unittest.TestCase):
    def make_runner(self, filled_shares=100, existing_legs=None):
        runner = object.__new__(s.StrategyRunner)
        runner.trade_lot = 100
        runner.st = {'bt_dip_price': 60.15, 'long_legs': list(existing_legs or []),
                     'trade_count_long': 1}
        runner._log = Mock()
        runner._new_leg_block_reason = Mock(return_value='')
        runner._paired_long_capacity = Mock(return_value={'can_long': True})
        runner._execution_price = 60.25
        runner._submit_order = Mock(return_value=(
            'FILLED' if filled_shares == 100 else 'PARTIAL', filled_shares))
        return runner

    def test_buy_fill_immediately_logs_actual_sellback_plan(self):
        runner = self.make_runner()
        runner._handle_bt_dipping(60.30)
        plans = [call.args[0] for call in runner._log.call_args_list
                 if '[NEXT-PLAN]' in call.args[0]]
        self.assertEqual(len(plans), 2)
        self.assertIn('sellback 100 sh', plans[0])
        self.assertIn('avg buy Y60.2500', plans[0])
        self.assertIn('watch >= Y60.97 (+1.20%)', plans[0])
        self.assertIn('pullback >= 0.10% from peak', plans[0])
        self.assertNotIn('force sellback', plans[1])
        self.assertIn('no end-of-day forced close', plans[1])
        self.assertEqual(runner.st['fstate'], s.STATE_BT_BOUGHT)
        self.assertEqual(runner.st['bt_sellback_target'], 60.97)
        self.assertEqual(runner.st['long_legs'], [(60.25, 100)])

    def test_partial_fill_plan_uses_combined_quantity_and_weighted_price(self):
        runner = self.make_runner(50, [(60.0, 100)])
        runner._handle_bt_dipping(60.30)
        plans = [call.args[0] for call in runner._log.call_args_list
                 if '[NEXT-PLAN]' in call.args[0]]
        self.assertIn('sellback 150 sh', plans[0])
        self.assertIn('avg buy Y60.0833', plans[0])
        self.assertIn('watch >= Y60.80', plans[0])

    def test_unfilled_buy_does_not_print_sellback_plan(self):
        runner = self.make_runner()
        runner._submit_order.return_value = ('TIMEOUT', 0)
        runner._handle_bt_dipping(60.30)
        self.assertFalse(any('[NEXT-PLAN]' in call.args[0]
                             for call in runner._log.call_args_list))


class LateNormalCloseTests(unittest.TestCase):
    def make_runner(self):
        runner = object.__new__(s.StrategyRunner)
        runner.st = {}
        runner.trade_lot = 100
        runner._last_heartbeat = 0.0
        runner._log = Mock()
        runner._file_log = Mock()
        runner._execution_price = 100.2
        runner._recalculate_next_t_triggers = Mock()
        runner._try_resume = Mock()
        return runner

    def test_late_buyback_still_requires_normal_rebound(self):
        runner = self.make_runner()
        runner.st = {'fstate': s.STATE_DIPPING, 'dip_price': 100.0,
                     'short_legs': [(102.0, 100)], 'sell_fill_price': 102.0}
        runner._submit_buyback_order = Mock(return_value=('FILLED', 100))
        list(runner._handle_late({}, 100.2, 1.0))
        runner._submit_buyback_order.assert_called_once_with(
            100, 100.2, 'REV-T buyback(NORMAL)')
        self.assertEqual(runner.st['fstate'], s.STATE_DONE)

    def test_late_sellback_still_requires_normal_pullback(self):
        runner = self.make_runner()
        runner.st = {'fstate': s.STATE_BT_SPIKING, 'bt_sell_peak_price': 102.0,
                     'long_legs': [(100.0, 100)]}
        runner._submit_order = Mock(return_value=('FILLED', -100))
        list(runner._handle_late({}, 101.8, 1.0))
        runner._submit_order.assert_called_once_with(-100, 101.8, 'FWD-T sell')
        self.assertEqual(runner.st['fstate'], s.STATE_DONE)

    def test_late_idle_and_armed_entries_never_submit_new_orders(self):
        for state in (s.STATE_IDLE, s.STATE_SPIKING, s.STATE_BT_DIPPING):
            with self.subTest(state=state):
                runner = self.make_runner()
                runner.st = {'fstate': state}
                runner._submit_order = Mock()
                runner._submit_buyback_order = Mock()
                list(runner._handle_late({}, 100.2, 1.0))
                runner._submit_order.assert_not_called()
                runner._submit_buyback_order.assert_not_called()
                self.assertEqual(runner.st['fstate'], state)


class EveryExecutionPlanTests(unittest.TestCase):
    def make_runner(self):
        portfolio = SimpleNamespace(conn=SimpleNamespace(), dry_run=True,
                                    order_uncertain=False)
        runner = s.StrategyRunner(portfolio, '600584.SH', 'test')
        runner._init_state()
        runner._log = Mock()
        runner._execution_price = 61.55
        runner._last_executed_order = {'order_id': 635188817, 'shares': 500}
        runner._refresh_position = Mock()
        runner._cur_price = Mock(return_value=61.53)
        runner._available_cash = Mock(return_value=7422.0)
        runner._retry_atr_reentry = Mock(return_value=False)
        runner.st.update(base_can_use=400, do_short=True, do_long=True,
                         rev_t_sell_fills=1,
                         daily_signal={'sell_trigger': 61.97, 'buy_trigger': 60.9,
                                       'atr_pct': 0.0467}, next_t_cycle=2)
        def recalculate(completed_by):
            runner.st['daily_signal'].update(sell_trigger=62.25, buy_trigger=60.85)
        runner._recalculate_next_t_triggers = Mock(side_effect=recalculate)
        return runner

    def plans(self, runner):
        return [call.args[0] for call in runner._log.call_args_list
                if '[NEXT-PLAN]' in call.args[0]]

    def test_complete_rev_buyback_logs_new_triggers_after_recalculation(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_DIPPING, short_legs=[(61.96, 500)],
                         sell_fill_price=61.96)
        runner._submit_buyback_order = Mock(return_value=('FILLED', 500))
        with patch.object(s.cfg, 'now_hms', return_value='13:48:03'):
            self.assertEqual(runner._do_buyback(61.53, 'NORMAL'), 500)
            runner._try_resume()
        plans = self.plans(runner)
        self.assertEqual(len(plans), 3)
        self.assertIn('sellable=400 cash=Y7422.00', plans[0])
        self.assertIn('sell watch >= Y62.25, planned=400 sh', plans[1])
        self.assertIn('buy touch <= Y60.85, planned=100 sh', plans[2])
        self.assertEqual(runner.st['fstate'], s.STATE_IDLE)
        runner._recalculate_next_t_triggers.assert_called_once_with('REV-T')

    def test_complete_forward_sell_logs_new_entry_plan(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_BT_SPIKING, long_legs=[(60.25, 100)],
                         bt_sell_peak_price=62.0)
        runner._submit_order = Mock(return_value=('FILLED', -100))
        with patch.object(s.cfg, 'now_hms', return_value='13:48:03'):
            runner._handle_bt_spiking(61.55)
        self.assertEqual(len(self.plans(runner)), 3)
        runner._recalculate_next_t_triggers.assert_called_once_with('FWD-T')

    def test_partial_buyback_logs_remaining_300_shares(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_DIPPING, short_legs=[(61.96, 500)],
                         sell_fill_price=61.96)
        runner.execution_book = SimpleNamespace(legs={'SHORT': [(61.96, 300)]})
        runner._submit_buyback_order = Mock(return_value=('PARTIAL', 200))
        runner._do_buyback(61.53, 'NORMAL')
        self.assertIn('buyback 300 sh', self.plans(runner)[0])
        runner._recalculate_next_t_triggers.assert_not_called()

    def test_partial_forward_sell_logs_remaining_300_shares(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_BT_SPIKING, long_legs=[(60.25, 500)],
                         bt_sell_peak_price=62.0, bt_sellback_target=60.97)
        runner.execution_book = SimpleNamespace(legs={'LONG': [(60.25, 300)]})
        runner._submit_order = Mock(return_value=('PARTIAL', -200))
        runner._handle_bt_spiking(61.55)
        self.assertIn('sellback 300 sh', self.plans(runner)[0])
        runner._recalculate_next_t_triggers.assert_not_called()

    def test_reentry_pending_prints_wait_without_old_thresholds(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_DONE,
                         reentry_pending={'order_id': 635188817})
        with patch.object(s.cfg, 'now_hms', return_value='13:48:03'):
            runner._try_resume()
        plans=self.plans(runner)
        self.assertEqual(len(plans), 1)
        self.assertIn('next-T WAIT', plans[0])
        self.assertNotIn('61.97', plans[0])
        self.assertEqual(runner.st['fstate'], s.STATE_DONE)

    def test_blocked_and_subsequent_execution_plans_are_logged_once(self):
        runner = self.make_runner()
        runner.st.update(fstate=s.STATE_DONE)
        runner._available_cash.return_value=0.0
        with patch.object(s.cfg, 'now_hms', return_value='13:48:03'):
            runner._try_resume()
            runner._try_resume()
            plans=self.plans(runner)
            self.assertEqual(len(plans),3)
            self.assertIn('BLOCKED: cash insufficient',plans[2])
            runner._last_executed_order={'order_id':635188818,'shares':100}
            runner._try_resume()
        self.assertEqual(len(self.plans(runner)),6)


class FirstThenRemainingRevSizingTests(unittest.TestCase):
    def make_runner(self):
        portfolio=SimpleNamespace(conn=SimpleNamespace(), dry_run=True,
                                  order_uncertain=False)
        runner=s.StrategyRunner(portfolio,'600584.SH','test')
        runner._init_state()
        runner._log=Mock()
        runner._new_leg_block_reason=Mock(return_value='')
        runner._execution_price=61.96
        runner.st.update(daily_signal={'atr_pct':0.0467, 'sell_trigger':61.97},
                         peak_price=62.2, short_arm_trigger=61.97,
                         short_arm_bars=2, trade_count_short=1)
        runner._paired_long_capacity=Mock(return_value={'pairing_shares':900})
        return runner

    def test_first_confirmed_sale_500_then_remaining_400(self):
        runner=self.make_runner()
        self.assertEqual(runner._new_t_shares(61.97,'SELL'),500)
        runner._submit_order=Mock(return_value=('FILLED',-500))
        with patch.object(s,'confirmed_short_reversal',return_value=True):
            runner._handle_spiking(61.97)
        self.assertEqual(runner.st['rev_t_sell_fills'],1)
        runner._paired_long_capacity.return_value={'pairing_shares':400}
        self.assertEqual(runner._new_t_shares(62.96,'SELL'),400)
        self.assertIn('buyback 500 sh',runner._log.call_args_list[-2].args[0])

    def test_unfilled_arm_does_not_consume_first_sale_rule(self):
        for status in ('SKIP','TIMEOUT'):
            with self.subTest(status=status):
                runner=self.make_runner()
                runner._submit_order=Mock(return_value=(status,0))
                with patch.object(s,'confirmed_short_reversal',return_value=True):
                    runner._handle_spiking(61.97)
                self.assertEqual(runner.st['rev_t_sell_fills'],0)
                self.assertEqual(runner._new_t_shares(61.97,'SELL'),500)

    def test_partial_first_sale_counts_as_first_actual_sale(self):
        runner=self.make_runner()
        runner._submit_order=Mock(return_value=('PARTIAL',-200))
        with patch.object(s,'confirmed_short_reversal',return_value=True):
            runner._handle_spiking(61.97)
        runner._paired_long_capacity.return_value={'pairing_shares':700}
        self.assertEqual(runner.st['rev_t_sell_fills'],1)
        self.assertEqual(runner._new_t_shares(62.96,'SELL'),700)

    def test_next_plan_and_threshold_log_show_all_remaining(self):
        runner=self.make_runner()
        runner.st.update(rev_t_sell_fills=1, base_can_use=400,
                         trade_count_short=1, do_short=True, do_long=False,
                         long_reason='cash insufficient')
        runner._execution_price=61.55
        with patch.object(s.cfg,'now_hms',return_value='13:48:03'):
            runner._log_next_t_entry_plan(61.55,7422.0,400)
            runner._log_sell_trigger_hit(61.97)
        messages=[call.args[0] for call in runner._log.call_args_list]
        self.assertTrue(any('planned=400 sh' in text for text in messages))
        self.assertIn('planned=400 sh',messages[-1])

    def test_daily_reset_and_forward_trades_do_not_use_up_first_rev_sale(self):
        runner=self.make_runner()
        runner.st['trade_count_long']=2
        self.assertEqual(runner._rev_t_sell_fraction(),0.6)
        runner.st['rev_t_sell_fills']=2
        self.assertEqual(runner._rev_t_sell_fraction(),1.0)
        runner._init_state()
        self.assertEqual(runner._rev_t_sell_fraction(),0.6)

if __name__ == '__main__':
    unittest.main()
