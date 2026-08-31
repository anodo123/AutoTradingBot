"""Time-independent fixed-size price brick generation and JSON persistence."""

import json
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


FILE_SUFFIX = "_price_action_bricks.json"


def _number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def brick_file_path(instrument_token, directory=None):
    directory = Path(directory or Path.cwd())
    return directory / f"{instrument_token}{FILE_SUFFIX}"


def cleanup_price_action_files(directory=None):
    """Delete only generated brick files so each WebSocket run starts fresh."""
    directory = Path(directory or Path.cwd())
    for path in directory.glob(f"*{FILE_SUFFIX}"):
        if path.is_file():
            path.unlink()


class PriceActionBrickGenerator:
    def __init__(self, instrument_token, brick_size, trading_symbol=None, output_directory=None):
        try:
            self.brick_size = Decimal(str(brick_size))
        except (InvalidOperation, TypeError) as error:
            raise ValueError("brick_size must be a number") from error
        if self.brick_size <= 0:
            raise ValueError("brick_size must be greater than zero")

        self.instrument_token = str(instrument_token)
        self.trading_symbol = trading_symbol or self.instrument_token
        self.file_path = brick_file_path(self.instrument_token, output_directory)
        self.base_price = None
        self.last_price = None
        self.last_direction = None
        self.bricks = []
        self._lock = threading.Lock()

    def process_price(self, price):
        current_price = Decimal(str(price))
        with self._lock:
            self.last_price = current_price
            if self.base_price is None:
                self.base_price = current_price
                self._save()
                return []

            new_bricks = []
            if self.last_direction is None:
                if current_price >= self.base_price + self.brick_size:
                    while current_price >= self.base_price + self.brick_size:
                        brick_type = "initial" if self.last_direction is None else "continuation"
                        new_bricks.append(self._append_brick(
                            "green", self.base_price, self.base_price + self.brick_size, brick_type
                        ))
                elif current_price <= self.base_price - self.brick_size:
                    while current_price <= self.base_price - self.brick_size:
                        brick_type = "initial" if self.last_direction is None else "continuation"
                        new_bricks.append(self._append_brick(
                            "red", self.base_price, self.base_price - self.brick_size, brick_type
                        ))
            elif self.last_direction == "green":
                while current_price >= self.base_price + self.brick_size:
                    new_bricks.append(self._append_brick(
                        "green", self.base_price, self.base_price + self.brick_size, "continuation"
                    ))
                if current_price <= self.base_price - (self.brick_size * 2):
                    new_bricks.append(self._append_brick(
                        "red",
                        self.base_price - self.brick_size,
                        self.base_price - (self.brick_size * 2),
                        "reversal",
                    ))
                    while current_price <= self.base_price - self.brick_size:
                        new_bricks.append(self._append_brick(
                            "red", self.base_price, self.base_price - self.brick_size, "continuation"
                        ))
            else:
                while current_price <= self.base_price - self.brick_size:
                    new_bricks.append(self._append_brick(
                        "red", self.base_price, self.base_price - self.brick_size, "continuation"
                    ))
                if current_price >= self.base_price + (self.brick_size * 2):
                    new_bricks.append(self._append_brick(
                        "green",
                        self.base_price + self.brick_size,
                        self.base_price + (self.brick_size * 2),
                        "reversal",
                    ))
                    while current_price >= self.base_price + self.brick_size:
                        new_bricks.append(self._append_brick(
                            "green", self.base_price, self.base_price + self.brick_size, "continuation"
                        ))

            if new_bricks:
                self._save()
            return new_bricks

    def _append_brick(self, direction, open_price, close_price, brick_type):
        brick = {
            "sequence": len(self.bricks) + 1,
            "open": _number(open_price),
            "close": _number(close_price),
            "high": _number(max(open_price, close_price)),
            "low": _number(min(open_price, close_price)),
            "direction": direction,
            "type": brick_type,
            "brick_size": _number(self.brick_size),
            "created_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        }
        self.bricks.append(brick)
        self.base_price = close_price
        self.last_direction = direction
        return brick

    def _save(self):
        payload = {
            "instrument_token": self.instrument_token,
            "trading_symbol": self.trading_symbol,
            "brick_size": _number(self.brick_size),
            "base_price": _number(self.base_price),
            "last_price": _number(self.last_price),
            "last_direction": self.last_direction,
            "bricks": self.bricks,
        }
        temporary_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary_path.replace(self.file_path)
