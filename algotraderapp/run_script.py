"""Live price-action brick generation with no trading or order logic."""

import logging
import time
from datetime import datetime, time as clock_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteTicker

from .price_action import PriceActionBrickGenerator


MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
COLLECTION_START = clock_time(9, 15)
COLLECTION_END = clock_time(15, 15)


def is_collection_time(current_datetime=None):
    """Return true only from 09:15:00 (inclusive) to 15:15:00 (exclusive) IST."""
    current_datetime = current_datetime or datetime.now(MARKET_TIMEZONE)
    if current_datetime.tzinfo is None:
        current_datetime = current_datetime.replace(tzinfo=MARKET_TIMEZONE)
    local_time = current_datetime.astimezone(MARKET_TIMEZONE).time().replace(tzinfo=None)
    return COLLECTION_START <= local_time < COLLECTION_END


class WebSocketHandler:
    def __init__(self, kite, instruments=None):
        self.websocket_running = True
        self.kite = kite
        self.instruments = instruments or []
        self.instrument_tokens = [int(item["instrument_token"]) for item in self.instruments]
        self.generators = {}

        for item in self.instruments:
            token = str(item["instrument_token"])
            details = item.get("instrument_details", {})
            brick_size = item.get("brick_size", 5)
            try:
                brick_size = Decimal(str(brick_size))
            except (InvalidOperation, TypeError):
                raise ValueError(f"Invalid brick_size for instrument {token}: {brick_size}")

            self.generators[token] = PriceActionBrickGenerator(
                instrument_token=token,
                brick_size=brick_size,
                trading_symbol=details.get("tradingsymbol", token),
                output_directory=Path.cwd(),
            )

        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        self.kite_ticker.on_ticks = self.on_ticks
        self.kite_ticker.on_connect = self.on_connect
        self.kite_ticker.on_close = self.on_close
        self.kite_ticker.on_error = self.on_error
        self.kite_ticker.on_noreconnect = self.on_noreconnect
        self.kite_ticker.on_reconnect = self.on_reconnect

    def on_connect(self, ws, response):
        logging.info("WebSocket connected; subscribing to %s", self.instrument_tokens)
        if self.instrument_tokens:
            ws.subscribe(self.instrument_tokens)
            ws.set_mode(ws.MODE_LTP, self.instrument_tokens)

    def on_ticks(self, ws, ticks):
        for tick in ticks:
            if not is_collection_time():
                continue
            token = str(tick.get("instrument_token", ""))
            generator = self.generators.get(token)
            if generator is None or "last_price" not in tick:
                continue
            try:
                generator.process_price(tick["last_price"])
            except (InvalidOperation, TypeError, ValueError) as error:
                logging.error("Invalid tick for instrument %s: %s", token, error)
            except OSError:
                logging.exception("Could not save bricks for instrument %s", token)

    def on_close(self, ws, code, reason):
        self.websocket_running = False
        logging.info("WebSocket closed (%s): %s", code, reason)

    def on_error(self, ws, code, reason):
        logging.error("WebSocket error (%s): %s", code, reason)
        # Handle the error and attempt to reconnect if necessary
        self.reconnect_websocket()

    def on_noreconnect(self, ws):
        self.websocket_running = False
        logging.error("WebSocket reconnection stopped")

    def on_reconnect(self, ws, attempt_count):
        logging.info("WebSocket reconnect attempt %s", attempt_count)

    def reconnect_websocket(self):
        self.kite_ticker.close()
        time.sleep(2)
        self.kite_ticker.connect(threaded=True)

    def stop_websocket(self):
        if not self.websocket_running:
            return
        if self.instrument_tokens:
            self.kite_ticker.unsubscribe(self.instrument_tokens)
        self.kite_ticker.close(1000, "Price action collection stopped")
        self.websocket_running = False

    def is_running(self):
        return self.websocket_running

    def run_websocket(self):
        self.kite_ticker.connect(threaded=True)
