import datetime as dt
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, Client
from .supertrend import Supertrend, validate_config
from .run_script import CandleAggregator, WebSocketHandler, IST


def config(**extra):
    return validate_config(dict(instrument_token='123', lot_size=10, timeframe=2,
        exit_trades_threshold_points=15, trade_side='BOTH',
        instrument_details=dict(exchange='NFO', tradingsymbol='TEST'), **extra))


def order(oid, side, price, qty=10, status='COMPLETE', filled=None):
    return dict(order_id=str(oid), exchange='NFO', tradingsymbol='TEST',
        product='MIS', transaction_type=side, average_price=price, quantity=qty,
        filled_quantity=qty if filled is None else filled, status=status)


class SupertrendTests(SimpleTestCase):
    def test_defaults_and_validation(self):
        c = config()
        self.assertEqual((c['supertrend_length'], c['supertrend_factor']), (7, 2))
        for field, values in [('supertrend_length', [0, 2.5, 'nan']),
                              ('supertrend_factor', [0, -1, 'inf']),
                              ('timeframe', [0, 1.5]), ('trade_side', ['BAD']),
                              ('exit_trades_threshold_points', [0, 'nan'])]:
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                    validate_config({**c, field: value})

    def test_wilder_atr_bands_and_flips(self):
        s = Supertrend(2, 1)
        self.assertIsNone(s.update(dict(high=11, low=9, close=10)))
        self.assertEqual(s.update(dict(high=12, low=10, close=11)), {'colour': 'RED', 'value': 13})
        # TR=4, ATR=(2+4)/2=3. Prior upper band stays at 13.
        self.assertEqual(s.update(dict(high=15, low=13, close=14)), {'colour': 'GREEN', 'value': 11})
        self.assertEqual(s.atr, 3)
        self.assertEqual(s.update(dict(high=10, low=8, close=9)), {'colour': 'RED', 'value': 13.5})

    def test_candle_boundary_uses_only_previous_ticks(self):
        with tempfile.TemporaryDirectory() as folder:
            a = CandleAggregator(123, 'TEST', 2, supertrend_length=1)
            a.file_path = Path(folder) / 'candles.json'
            def tick(minute, price):
                return a.process_tick(dict(current_datetime=dt.datetime(2026, 11, 17, 9, minute, tzinfo=IST), last_price=price))
            self.assertIsNone(tick(15, 100))
            self.assertIsNone(a.signal)
            tick(16, 101)
            closed = tick(17, 120)
            self.assertEqual((closed['high'], closed['close']), (101, 101))
            self.assertEqual(a.current_candle['open'], 120)
            self.assertEqual(a.signal['colour'], 'RED')
            tick(17, 121)
            self.assertEqual(len(a.candles), 1)

    def test_history_seeds_first_bar_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            now = dt.datetime(2026, 11, 17, 9, 15, tzinfo=IST)
            kite = Mock()
            kite.historical_data.return_value = [dict(date=now-dt.timedelta(days=1)+dt.timedelta(minutes=i),
                open=100, high=101, low=99, close=100, volume=1) for i in range(16)]
            a = CandleAggregator(123, 'TEST', 2)
            a.file_path = Path(folder) / 'candles.json'
            a.bootstrap(kite, now)
            self.assertIsNotNone(a.indicator.atr)
            self.assertIsNone(a.signal)  # No historical orders.
            a.process_tick(dict(current_datetime=now, last_price=100))
            a.process_tick(dict(current_datetime=now+dt.timedelta(minutes=2), last_price=100))
            self.assertEqual(a.signal['colour'], 'RED')
            b = CandleAggregator(123, 'TEST', 2)
            b.file_path = a.file_path
            kite.historical_data.return_value = []
            b.bootstrap(kite, now+dt.timedelta(minutes=3))
            self.assertEqual(b.indicator.atr, a.indicator.atr)
            self.assertEqual(len(b.candles), len(a.candles))


class TradingTests(SimpleTestCase):
    def setUp(self):
        self.h = WebSocketHandler.__new__(WebSocketHandler)
        self.h.kite = Mock()
        self.h.kite.place_order.return_value = 'new'
        self.h.instruments = [config()]
        self.h.candle_aggregators = {'123': CandleAggregator(123, 'TEST', 2)}
        self.h.candle_aggregators['123'].signal = {'colour': 'RED'}
        self.h.pending, self.h.blocked, self.h.halted = {}, set(), set()
        self.h.expected_positions = {}
        self.h.prices = {'123': 100}
        self.h._save_state = Mock()
        now = dt.datetime(2026, 11, 17, 10, tzinfo=IST)
        clock = patch('algotraderapp.run_script.dt.datetime')
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = now

    def position(self, quantity):
        return [dict(exchange='NFO', tradingsymbol='TEST', product='MIS', quantity=quantity)]

    def test_first_red_enters_sell_and_duplicate_is_blocked(self):
        self.h.reconcile([], [])
        self.assertEqual(self.h.kite.place_order.call_args.kwargs['transaction_type'], 'SELL')
        self.h.reconcile([], [])
        self.h.kite.place_order.assert_called_once()

    def test_first_green_enters_buy(self):
        self.h.candle_aggregators['123'].signal = {'colour': 'GREEN'}
        self.h.reconcile([], [])
        self.assertEqual(self.h.kite.place_order.call_args.kwargs['transaction_type'], 'BUY')

    def test_reversal_waits_for_full_exit_then_enters(self):
        self.h.reconcile([], self.position(15))
        self.assertEqual(self.h.kite.place_order.call_args.kwargs['quantity'], 15)
        self.h.reconcile([order('new', 'SELL', 100, 15, 'OPEN', 5)], self.position(10))
        self.h.kite.place_order.assert_called_once()
        self.h.reconcile([order('new', 'SELL', 100, 15)], self.position(0))
        self.assertEqual(self.h.kite.place_order.call_count, 2)
        self.assertEqual(self.h.kite.place_order.call_args.kwargs['quantity'], 10)

    def test_one_side_only_exits_on_opposite_colour(self):
        for mode, colour, quantity in [('BUY', 'RED', 10), ('SELL', 'GREEN', -10)]:
            self.h.pending.clear()
            self.h.instruments[0]['trade_side'] = mode
            self.h.candle_aggregators['123'].signal = {'colour': colour}
            self.h.kite.reset_mock()
            self.h.reconcile([], self.position(quantity))
            self.h.kite.place_order.assert_called_once()
            self.h.reconcile([order('new', 'SELL' if quantity > 0 else 'BUY', 100)], self.position(0))
            self.h.kite.place_order.assert_called_once()

    def test_same_colour_holds_position(self):
        self.h.reconcile([], self.position(-10))
        self.h.kite.place_order.assert_not_called()

    def test_no_signal_no_order(self):
        self.h.candle_aggregators['123'].signal = None
        self.h.reconcile([], [])
        self.h.kite.place_order.assert_not_called()

    def test_rejected_exit_does_not_reverse_or_retry(self):
        self.h.pending['123'] = 'exit'
        self.h.reconcile([order('exit', 'SELL', 0, status='REJECTED', filled=0)], self.position(10))
        self.h.kite.place_order.assert_not_called()
        self.assertIn('123', self.h.blocked)

    def test_timeout_does_not_repeat_order(self):
        self.h.kite.place_order.side_effect = TimeoutError()
        with self.assertLogs(level='ERROR'):
            self.h.reconcile([], [])
        self.h.reconcile([], [])
        self.h.kite.place_order.assert_called_once()
        self.assertIn('123', self.h.blocked)

    def test_complete_exit_with_stale_position_does_not_duplicate(self):
        self.h.reconcile([], self.position(10))
        self.h.reconcile([order('new', 'SELL', 100)], self.position(10))
        self.h.kite.place_order.assert_called_once()
        self.assertIn('123', self.h.pending)

    def test_group_target_combines_members_and_latches_all(self):
        second = {**config(), 'instrument_token': '456',
                  'instrument_details': dict(exchange='NFO', tradingsymbol='SECOND')}
        self.h.instruments.append(second)
        self.h.prices.update({'123': 108, '456': 107})
        orders = [order(1, 'BUY', 100), {**order(2, 'BUY', 100), 'tradingsymbol': 'SECOND'}]
        self.h.check_targets(orders)
        self.assertEqual(self.h.halted, {'15.0'})
        self.h.prices.update({'123': 100, '456': 100})
        self.h.check_targets(orders)
        self.assertEqual(self.h.halted, {'15.0'})

    def test_state_persists_pending_and_reached_target(self):
        from .run_script import atomic_json
        import json
        with tempfile.TemporaryDirectory() as folder:
            self.h.state_path = Path(folder) / 'state.json'
            self.h.day = '2026-11-17'
            self.h.halted.add('15.0')
            self.h.pending['123'] = 'pending'
            self.h.expected_positions['123'] = 0
            WebSocketHandler._save_state(self.h)
            saved = json.loads(self.h.state_path.read_text())
            self.assertEqual(saved['pending'], {'123': 'pending'})
            self.assertEqual(saved['halted'], ['15.0'])
            self.assertEqual(saved['expected_positions'], {'123': 0})

    def test_single_threshold_keeps_realized_losses(self):
        self.h.check_targets([order(1, 'BUY', 100), order(2, 'SELL', 96)])
        self.assertFalse(self.h.halted)
        self.h.prices['123'] = 119
        orders = [order(1, 'BUY', 100), order(2, 'SELL', 96), order(3, 'BUY', 100)]
        self.h.check_targets(orders)
        self.assertIn('15.0', self.h.halted)
        self.h.reconcile(orders, self.position(10))
        self.assertEqual(self.h.kite.place_order.call_args.kwargs['transaction_type'], 'SELL')
        self.h.reconcile(orders+[order('new', 'SELL', 119)], self.position(0))
        self.h.kite.place_order.assert_called_once()


class ConfigApiTests(SimpleTestCase):
    def test_add_defaults_without_old_fields(self):
        with patch('algotraderapp.views.MongoClient') as mongo, patch('algotraderapp.views.kite') as kite:
            collection = mongo.return_value.__getitem__.return_value.__getitem__.return_value
            collection.find_one.return_value = None
            kite.instruments.return_value = [dict(instrument_token=123, expiry='', exchange='NFO', tradingsymbol='TEST')]
            response = Client().post('/algotraderapp/tradinginstruments/addtradinginstruments',
                dict(lot_size=10, instrument_token=123, timeframe=2, exit_trades_threshold_points=15))
            self.assertEqual(response.status_code, 200)
            data = collection.insert_one.call_args.args[0]
            self.assertEqual((data['supertrend_length'], data['supertrend_factor']), (7, 2))
            self.assertNotIn('first_loss_exit_threshold_points', data)
            self.assertNotIn('per_trade_exit_trades_threshold_points', data)

    def test_invalid_config_rejected_before_database(self):
        with patch('algotraderapp.views.MongoClient') as mongo:
            response = Client().post('/algotraderapp/tradinginstruments/addtradinginstruments',
                dict(lot_size=10, instrument_token=123, timeframe=2,
                     exit_trades_threshold_points=15, supertrend_length=0))
            self.assertEqual(response.status_code, 400)
            mongo.assert_not_called()
