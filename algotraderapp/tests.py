from django.test import SimpleTestCase
from .thresholds import build_groups, fallback, ThresholdGroup
from .run_script import CandleAggregator
from unittest.mock import Mock, patch


class FirstLossTests(SimpleTestCase):
    def order(self, oid, side, price, qty=10, status='COMPLETE', filled=None):
        return dict(order_id=str(oid), order_timestamp=str(oid), exchange='NFO', tradingsymbol='TEST',
                    transaction_type=side, average_price=price, quantity=qty, status=status,
                    filled_quantity=qty if filled is None else filled)

    def test_loss_changes_target_without_changing_original(self):
        g = ThresholdGroup(15, 5, [('NFO', 'TEST')])
        orders = [self.order(1, 'BUY', 100), self.order(2, 'SELL', 96)]
        g.observe(orders)
        self.assertEqual(g.active, 5)
        self.assertEqual(g.initial, 15)
        g.observe(orders)
        self.assertEqual(g.active, 5)

    def test_partial_and_rejected_exits_do_not_trigger(self):
        g = ThresholdGroup(15, 5, [('NFO', 'TEST')])
        g.observe([self.order(1, 'BUY', 100), self.order(2, 'SELL', 96, status='OPEN', filled=5)])
        self.assertFalse(g.switched)
        g.observe([self.order(1, 'BUY', 100), self.order(2, 'SELL', 96, status='REJECTED', filled=0)])
        self.assertFalse(g.switched)

    def test_partial_closures_use_total_position_profit(self):
        g = ThresholdGroup(15, 5, [('NFO', 'TEST')])
        orders = [self.order(1, 'BUY', 100), self.order(2, 'SELL', 90, qty=5), self.order(3, 'SELL', 112, qty=5)]
        g.observe(orders)
        self.assertFalse(g.switched)
        orders[-1]['average_price'] = 108
        g.observe(orders)
        self.assertTrue(g.switched)

    def test_short_loss_and_disabled_fallback(self):
        orders = [self.order(1, 'SELL', 100), self.order(2, 'BUY', 104)]
        g = ThresholdGroup(15, 6, [('NFO', 'TEST')])
        g.observe(orders)
        self.assertEqual(g.active, 6)
        disabled = ThresholdGroup(15, None, [('NFO', 'TEST')])
        disabled.observe(orders)
        self.assertEqual(disabled.active, 15)

    def test_group_membership_and_config_validation(self):
        def item(token, target, reduced):
            return dict(instrument_token=token, exit_trades_threshold_points=target,
                        first_loss_exit_threshold_points=reduced,
                        instrument_details=dict(exchange='NFO', tradingsymbol=token))
        groups = build_groups([item('A', '15', '5'), item('B', '15.0', '5'), item('C', '5', '')])
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups['15'].members), 2)
        with self.assertRaises(ValueError):
            build_groups([item('A', '15', '5'), item('B', '15', '6')])
        for value in ['0', '-1', '15', 'NaN', 'abc']:
            with self.assertRaises(ValueError):
                fallback(item('A', '15', value))

    def test_daily_pnl_reuses_snapshot_and_compares_reduced_target(self):
        a = CandleAggregator.__new__(CandleAggregator)
        a.threshold_group = ThresholdGroup(15, 5, [('NFO', 'TEST')])
        a.instrument_details_dict = {'15': [{'tradingsymbol': 'TEST'}]}
        a.calculate_total_profit_loss_per_share = Mock(return_value=5)
        a.write_profit_loss_to_json = Mock()
        a.fetch_profit_loss_from_json_dict = Mock(return_value=5)
        a.should_close_trade = Mock()
        kite = Mock()
        kite.orders.return_value = [self.order(1, 'BUY', 100), self.order(2, 'SELL', 96)]
        a.fetch_and_calculate_daily_profit_loss(kite, 109, 1, 'TEST', 'NFO', 15, {}, 10, 0)
        kite.orders.assert_called_once_with()
        self.assertEqual(a.active_exit_threshold(15), 5)
        a.should_close_trade.assert_called_once()

    def test_add_rejects_invalid_fallback_before_database(self):
        from django.test import Client
        with patch('algotraderapp.views.MongoClient') as mongo:
            response = Client().post('/algotraderapp/tradinginstruments/addtradinginstruments',
                dict(lot_size='10', instrument_token='123', exit_trades_threshold_points='15',
                     first_loss_exit_threshold_points='20'))
            self.assertEqual(response.status_code, 400)
            mongo.assert_not_called()
