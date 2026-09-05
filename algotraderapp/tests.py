import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .price_action import PriceActionBrickGenerator, cleanup_price_action_files
from .run_script import WebSocketHandler, is_collection_time
from .raw_ticks import RawTickLogger, cleanup_raw_tick_files


class CollectionWindowTests(SimpleTestCase):
    def ist_datetime(self, hour, minute, second=0):
        return datetime(2026, 8, 30, hour, minute, second, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_ticks_are_blocked_before_091510(self):
        self.assertFalse(is_collection_time(self.ist_datetime(9, 15, 9)))

    def test_ticks_start_at_091510(self):
        self.assertTrue(is_collection_time(self.ist_datetime(9, 15, 10)))

    def test_ticks_are_blocked_from_151500(self):
        self.assertTrue(is_collection_time(self.ist_datetime(15, 14, 59)))
        self.assertFalse(is_collection_time(self.ist_datetime(15, 15, 0)))


class RawTickLoggerTests(SimpleTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_directory.name)
        self.instruments = [{
            "instrument_token": "123",
            "instrument_details": {"tradingsymbol": "NIFTY 50"},
        }]

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_stores_complete_tick_with_timestamp_in_instrument_file(self):
        logger = RawTickLogger(self.instruments, self.directory)
        tick = {
            "instrument_token": 123,
            "last_price": 24366.5,
            "depth": {"buy": [{"price": 24366.0, "quantity": 10}]},
        }
        received_at = datetime(2026, 9, 1, 9, 15, 9, 123456, tzinfo=ZoneInfo("Asia/Kolkata"))
        path = logger.log_tick(tick, received_at)

        self.assertEqual(path.name, "NIFTY_50_123_09_01_part001.jsonl")
        record = json.loads(path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["received_at"], received_at.isoformat())
        self.assertEqual(record["trading_symbol"], "NIFTY 50")
        self.assertEqual(record["tick"], tick)

    def test_rotates_and_deletes_oldest_parts_to_stay_under_limit(self):
        logger = RawTickLogger(
            self.instruments,
            self.directory,
            max_total_bytes=900,
            max_part_bytes=300,
        )
        received_at = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        for index in range(12):
            logger.log_tick({"instrument_token": 123, "last_price": index}, received_at)

        files = list((self.directory / "raw_ticks").glob("*.jsonl"))
        self.assertLessEqual(sum(path.stat().st_size for path in files), 900)
        all_lines = [line for path in files for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(json.loads(line)["tick"]["last_price"] == 11 for line in all_lines))

    def test_cleanup_removes_raw_logs_but_not_unrelated_files(self):
        logger = RawTickLogger(self.instruments, self.directory)
        logger.log_tick({"instrument_token": 123, "last_price": 100})
        unrelated = self.directory / "keep.json"
        unrelated.write_text("{}", encoding="utf-8")
        cleanup_raw_tick_files(self.directory)
        self.assertFalse((self.directory / "raw_ticks").exists())
        self.assertTrue(unrelated.exists())

    @patch("algotraderapp.run_script.is_collection_time", return_value=False)
    def test_tick_is_logged_before_price_action_time_filter(self, collection_time):
        handler = WebSocketHandler.__new__(WebSocketHandler)
        handler.raw_tick_logger = Mock()
        handler.generators = {"123": Mock()}
        tick = {"instrument_token": 123, "last_price": 100}

        handler.on_ticks(None, [tick])

        handler.raw_tick_logger.log_tick.assert_called_once_with(tick)
        handler.generators["123"].process_price.assert_not_called()


class PriceActionBrickGeneratorTests(SimpleTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_directory.name)

    def tearDown(self):
        self.temp_directory.cleanup()

    def generator(self, brick_size=5):
        return PriceActionBrickGenerator("123", brick_size, "TEST", self.directory)

    def test_first_price_sets_base_without_creating_a_brick(self):
        generator = self.generator()
        self.assertEqual(generator.process_price(218.5), [])
        payload = json.loads(generator.file_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["base_price"], 218.5)
        self.assertEqual(payload["bricks"], [])

    def test_reversal_requires_two_brick_sizes(self):
        generator = self.generator()
        generator.process_price(218.5)
        self.assertEqual(generator.process_price(223.5)[0]["direction"], "green")
        self.assertEqual(generator.process_price(218.5), [])
        red = generator.process_price(213.5)[0]
        self.assertEqual(red["direction"], "red")
        self.assertEqual(red["type"], "reversal")
        self.assertEqual((red["open"], red["close"]), (218.5, 213.5))

    def test_movement_smaller_than_brick_size_creates_nothing(self):
        generator = self.generator()
        generator.process_price(100)
        self.assertEqual(generator.process_price(104.99), [])

    def test_large_jump_creates_every_crossed_level(self):
        generator = self.generator()
        generator.process_price(100)
        created = generator.process_price(116)
        self.assertEqual([item["close"] for item in created], [105, 110, 115])

    def test_large_reversal_creates_reversal_then_continuation_bricks(self):
        generator = self.generator()
        generator.process_price(100)
        generator.process_price(105)
        created = generator.process_price(85)
        self.assertEqual(
            [(item["open"], item["close"], item["type"]) for item in created],
            [(100, 95, "reversal"), (95, 90, "continuation"), (90, 85, "continuation")],
        )

    def test_up_reversal_after_red_brick_also_requires_two_sizes(self):
        generator = self.generator()
        generator.process_price(100)
        generator.process_price(95)
        self.assertEqual(generator.process_price(100), [])
        green = generator.process_price(105)[0]
        self.assertEqual((green["open"], green["close"], green["type"]), (100, 105, "reversal"))

    def test_decimal_brick_size_has_stable_values(self):
        generator = self.generator("0.1")
        generator.process_price("1.1")
        generator.process_price("1.3")
        payload = json.loads(generator.file_path.read_text(encoding="utf-8"))
        self.assertEqual([item["close"] for item in payload["bricks"]], [1.2, 1.3])

    def test_cleanup_removes_only_price_action_json(self):
        generated = self.directory / "123_price_action_bricks.json"
        unrelated = self.directory / "keep.json"
        generated.write_text("{}", encoding="utf-8")
        unrelated.write_text("{}", encoding="utf-8")
        cleanup_price_action_files(self.directory)
        self.assertFalse(generated.exists())
        self.assertTrue(unrelated.exists())


class PriceActionApiTests(SimpleTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_directory.name)

    def tearDown(self):
        self.temp_directory.cleanup()

    @patch("algotraderapp.views.Path.cwd")
    def test_fetch_returns_generated_bricks(self, cwd):
        cwd.return_value = self.directory
        generator = PriceActionBrickGenerator("123", 5, output_directory=self.directory)
        generator.process_price(100)
        generator.process_price(105)
        response = self.client.get(
            "/algotraderapp/api/price-action/bricks/", {"instrument_token": "123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bricks"][0]["direction"], "green")

    def test_fetch_rejects_invalid_instrument_token(self):
        response = self.client.get(
            "/algotraderapp/api/price-action/bricks/", {"instrument_token": "../secret"}
        )
        self.assertEqual(response.status_code, 400)

    @patch("algotraderapp.views.view_all_added_trading_instrument")
    def test_instrument_api_exposes_brick_size(self, instruments):
        instruments.return_value = [{
            "instrument_token": "123",
            "brick_size": "2.5",
            "instrument_details": {"tradingsymbol": "TEST"},
        }]
        response = self.client.get("/algotraderapp/api/price-action/instruments/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["instruments"][0]["brick_size"], "2.5")


class BrickTradingTests(SimpleTestCase):
    def trader(self, side='BOTH'):
        from .brick_trading import BrickTrader
        kite = Mock()
        kite.place_order.side_effect = [str(i) for i in range(1, 20)]
        return BrickTrader(kite, dict(instrument_token='123', lot_size='10', brick_size=5,
            trade_side=side, instrument_details=dict(tradingsymbol='TEST', exchange='NSE')))

    def brick(self, trader, color):
        trader.on_brick(dict(sequence=trader.sequence + 1, direction=color, close=105))

    def fill(self, trader, status='COMPLETE', quantity=10):
        trader.on_order_update(dict(order_id=trader.pending['order_id'], status=status, filled_quantity=quantity))

    def test_both_exits_before_opposite_entry(self):
        t = self.trader()
        self.brick(t, 'green')
        self.fill(t)
        self.brick(t, 'green')
        self.assertEqual(t.kite.place_order.call_count, 1)
        self.brick(t, 'red')
        self.assertEqual(t.kite.place_order.call_count, 2)
        self.assertEqual(t.position, 10)
        self.fill(t)
        self.assertEqual(t.kite.place_order.call_count, 3)
        self.fill(t)
        self.assertEqual(t.position, -10)

    def test_single_sides_exit_and_wait(self):
        for side, color, opposite in [('BUY', 'green', 'red'), ('SELL', 'red', 'green')]:
            t = self.trader(side)
            self.brick(t, opposite)
            t.kite.place_order.assert_not_called()
            self.brick(t, color)
            self.fill(t)
            self.brick(t, opposite)
            self.fill(t)
            self.assertEqual(t.position, 0)
            self.assertEqual(t.kite.place_order.call_count, 2)
            self.brick(t, color)
            self.assertEqual(t.kite.place_order.call_count, 3)

    def test_pending_and_duplicate_bricks_do_not_duplicate_orders(self):
        t = self.trader()
        self.brick(t, 'green')
        t.on_brick(t.brick)
        self.brick(t, 'green')
        self.assertEqual(t.kite.place_order.call_count, 1)

    def test_rejected_exit_never_opens_opposite(self):
        t = self.trader()
        self.brick(t, 'green')
        self.fill(t)
        self.brick(t, 'red')
        self.fill(t, 'REJECTED', 0)
        self.assertTrue(t.halted)
        self.assertEqual(t.position, 10)
        self.brick(t, 'red')
        self.assertEqual(t.kite.place_order.call_count, 2)

    def test_partial_fill_and_cancellation_preserve_actual_quantity(self):
        t = self.trader()
        self.brick(t, 'green')
        self.fill(t, 'OPEN', 4)
        self.fill(t, 'CANCELLED', 4)
        self.assertEqual(t.position, 4)
        self.assertTrue(t.halted)

    def test_timeout_halts_without_retry(self):
        t = self.trader()
        t.kite.place_order.side_effect = TimeoutError('timeout')
        self.brick(t, 'green')
        self.brick(t, 'green')
        self.assertTrue(t.halted)
        self.assertEqual(t.kite.place_order.call_count, 1)

    def test_payload_uses_configured_quantity(self):
        t = self.trader()
        self.brick(t, 'red')
        payload = t.kite.place_order.call_args.kwargs
        self.assertEqual(payload['quantity'], 10)
        self.assertEqual(payload['transaction_type'], 'SELL')
        self.assertEqual(payload['product'], 'MIS')
        self.assertEqual(payload['order_type'], 'MARKET')

    def test_no_order_until_first_completed_brick(self):
        t = self.trader()
        with tempfile.TemporaryDirectory() as directory:
            g = PriceActionBrickGenerator('123', 5, output_directory=directory)
            for price in [100, 104]:
                for brick in g.process_price(price):
                    t.on_brick(brick)
            t.kite.place_order.assert_not_called()
            for brick in g.process_price(105):
                t.on_brick(brick)
            self.assertEqual(t.kite.place_order.call_count, 1)

    def test_fresh_run_resets_logs_and_records_payload(self):
        import logging
        from .brick_trading import setup_run_logging
        root = logging.getLogger()
        original_level = root.level
        try:
            with tempfile.TemporaryDirectory() as directory:
                setup_run_logging(directory)
                t = self.trader()
                self.brick(t, 'green')
                path = Path(directory) / 'bot_logs' / 'session.log'
                self.assertIn('order_request', path.read_text())
                self.assertIn('quantity', path.read_text())
                setup_run_logging(directory)
                self.assertEqual(path.read_text(), '')
                for handler in list(root.handlers):
                    if getattr(handler, 'brick_bot_handler', False):
                        root.removeHandler(handler)
                        handler.close()
        finally:
            root.setLevel(original_level)


class ConfigurableStartTests(SimpleTestCase):
    def test_second_precision_boundaries(self):
        from .run_script import parse_start_time
        start = parse_start_time('09:20:30')
        for h, m, s, expected in [(9, 20, 29, False), (9, 20, 30, True), (9, 25, 0, True), (15, 15, 0, False)]:
            now = datetime(2026, 9, 1, h, m, s, tzinfo=ZoneInfo('Asia/Kolkata'))
            self.assertEqual(is_collection_time(now, start), expected)
        utc = datetime(2026, 9, 1, 3, 50, 30, tzinfo=ZoneInfo('UTC'))
        self.assertTrue(is_collection_time(utc, start))

    def test_invalid_api_times_have_no_startup_side_effects(self):
        with patch('algotraderapp.views.run_script.WebSocketHandler') as handler, patch('algotraderapp.views.setup_run_logging') as logs:
            for value in ['09:20', '9:20:30', '24:00:00', '09:60:00', '09:20:60', '15:15:00', '16:00:00', '', '123']:
                response = self.client.post('/algotraderapp/access_web_socket', {'start_time': value})
                self.assertEqual(response.status_code, 400, value)
            handler.assert_not_called()
            logs.assert_not_called()

    def test_form_and_default_reach_handler(self):
        from contextlib import ExitStack
        item = dict(instrument_token='123', lot_size='10', brick_size=5, trade_side='BUY',
                    instrument_details=dict(exchange='NSE', tradingsymbol='TEST'))
        for data, content_type, expected in [('start_time=09%3A25%3A45', 'application/x-www-form-urlencoded', '09:25:45'),
                                            ({'start_time': '09:15:00'}, None, '09:15:00'), ({}, None, '09:15:10')]:
            with ExitStack() as stack:
                stack.enter_context(patch('algotraderapp.views.ws_handler', None))
                stack.enter_context(patch.dict('os.environ', {'access_token': 'test'}))
                kite = stack.enter_context(patch('algotraderapp.views.kite'))
                kite.positions.return_value = {'net': []}
                kite.orders.return_value = []
                stack.enter_context(patch('algotraderapp.views.view_all_added_trading_instrument', return_value=[item]))
                for name in ['setup_run_logging', 'cleanup_price_action_files', 'cleanup_raw_tick_files', 'threading.Thread']:
                    stack.enter_context(patch('algotraderapp.views.' + name))
                handler = stack.enter_context(patch('algotraderapp.views.run_script.WebSocketHandler'))
                kwargs = {'content_type': content_type} if content_type else {}
                response = self.client.post('/algotraderapp/access_web_socket', data, **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['start_time'], expected)
                handler.assert_called_once_with(kite, [item], start_time=expected)

    def test_ticks_before_custom_start_only_log_raw_data(self):
        from .run_script import parse_start_time
        handler = WebSocketHandler.__new__(WebSocketHandler)
        handler.collection_start = parse_start_time('09:25:45')
        handler.raw_tick_logger = Mock()
        handler.generators = {'123': Mock()}
        handler.generators['123'].process_price.return_value = []
        handler.traders = {'123': Mock()}
        with patch('algotraderapp.run_script.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 1, 9, 25, 44, tzinfo=ZoneInfo('Asia/Kolkata'))
            handler.on_ticks(None, [{'instrument_token': 123, 'last_price': 100}])
            handler.generators['123'].process_price.assert_not_called()
            handler.raw_tick_logger.log_tick.assert_called_once()
            clock.now.return_value = datetime(2026, 9, 1, 9, 25, 45, tzinfo=ZoneInfo('Asia/Kolkata'))
            handler.on_ticks(None, [{'instrument_token': 123, 'last_price': 101}])
            handler.generators['123'].process_price.assert_called_once_with(101)

    def test_raw_json_is_rejected_without_starting(self):
        with patch('algotraderapp.views.run_script.WebSocketHandler') as handler:
            response = self.client.post('/algotraderapp/access_web_socket',
                                        {'start_time': '09:20:30'}, content_type='application/json')
            self.assertEqual(response.status_code, 415)
            handler.assert_not_called()
