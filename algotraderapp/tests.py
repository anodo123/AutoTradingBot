import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .price_action import PriceActionBrickGenerator, cleanup_price_action_files
from .run_script import is_collection_time


class CollectionWindowTests(SimpleTestCase):
    def ist_datetime(self, hour, minute, second=0):
        return datetime(2026, 8, 30, hour, minute, second, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_ticks_are_blocked_before_091500(self):
        self.assertFalse(is_collection_time(self.ist_datetime(9, 14, 59)))

    def test_ticks_start_at_091500(self):
        self.assertTrue(is_collection_time(self.ist_datetime(9, 15, 0)))

    def test_ticks_are_blocked_from_151500(self):
        self.assertTrue(is_collection_time(self.ist_datetime(15, 14, 59)))
        self.assertFalse(is_collection_time(self.ist_datetime(15, 15, 0)))


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

    def test_exact_up_and_down_thresholds_create_bricks(self):
        generator = self.generator()
        generator.process_price(218.5)
        self.assertEqual(generator.process_price(223.5)[0]["direction"], "green")
        red = generator.process_price(218.5)[0]
        self.assertEqual(red["direction"], "red")
        self.assertEqual((red["open"], red["close"]), (223.5, 218.5))

    def test_movement_smaller_than_brick_size_creates_nothing(self):
        generator = self.generator()
        generator.process_price(100)
        self.assertEqual(generator.process_price(104.99), [])

    def test_large_jump_creates_every_crossed_level(self):
        generator = self.generator()
        generator.process_price(100)
        created = generator.process_price(116)
        self.assertEqual([item["close"] for item in created], [105, 110, 115])

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
