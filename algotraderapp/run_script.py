"""Closed-candle Supertrend trading; broker fills are the source of truth."""
import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from kiteconnect import KiteTicker
from .supertrend import Supertrend, validate_config

IST = ZoneInfo('Asia/Kolkata')
TERMINAL = {'COMPLETE', 'CANCELLED', 'REJECTED'}


def local_time(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def atomic_json(path, value):
    temp = Path(str(path) + '.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temp, path)


class CandleAggregator:
    def __init__(self, instrument_token, tradingsymbol, interval_minutes=15,
                 exchange='NFO', supertrend_length=7, supertrend_factor=2):
        self.instrument_token = int(instrument_token)
        self.tradingsymbol, self.exchange = tradingsymbol, exchange
        self.interval_minutes = int(interval_minutes)
        self.file_path = Path(f'{instrument_token}_{interval_minutes}_minute_candles.json')
        self.indicator = Supertrend(supertrend_length, supertrend_factor)
        self.current_candle = None
        self.candles = []
        self.signal = None

    def session_start(self, stamp):
        hour, minute = (9, 15) if self.exchange in ('NSE', 'NFO', 'BSE', 'BFO') else (9, 0)
        return stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def bucket(self, stamp):
        start = self.session_start(stamp)
        minutes = int((stamp - start).total_seconds() // 60)
        return start + dt.timedelta(minutes=(minutes // self.interval_minutes) * self.interval_minutes)

    def end(self, candle):
        start = local_time(candle['start_time'])
        end = start + dt.timedelta(minutes=self.interval_minutes)
        if self.exchange in ('NSE', 'NFO', 'BSE', 'BFO'):
            end = min(end, start.replace(hour=15, minute=30))
        return end

    def bootstrap(self, kite, now):
        existing = json.loads(self.file_path.read_text(encoding='utf-8')) if self.file_path.exists() else []
        # Legacy candles included boundary ticks in the wrong bar.
        existing = [c for c in existing if c.get('source') == 'supertrend']
        since = now - dt.timedelta(days=30)
        if existing:
            since = max(since, local_time(existing[-1]['start_time']) - dt.timedelta(days=1))
        since = self.session_start(since)
        rows = kite.historical_data(self.instrument_token, since, now, 'minute')
        buckets = {}
        for row in rows:
            stamp = local_time(row['date'])
            if stamp > now or stamp < self.session_start(stamp):
                continue
            start = self.bucket(stamp).isoformat()
            if start not in buckets:
                buckets[start] = dict(start_time=start, open=row['open'], high=row['high'],
                    low=row['low'], close=row['close'], volume=row.get('volume', 0), source='supertrend')
            else:
                c = buckets[start]
                c['high'], c['low'], c['close'] = max(c['high'], row['high']), min(c['low'], row['low']), row['close']
                c['volume'] += row.get('volume', 0)
        merged = {c['start_time']: c for c in existing}
        merged.update(buckets)
        for c in sorted(merged.values(), key=lambda c: c['start_time']):
            if self.end(c) <= now:
                c['final_save'] = True
                c['supertrend'] = self.indicator.update(c)
                self.candles.append(c)
            elif local_time(c['start_time']).date() == now.date():
                self.current_candle = c
        if self.indicator.atr is None:
            raise ValueError(f'Not enough historical candles for {self.tradingsymbol}')
        atomic_json(self.file_path, self.candles)

    def process_tick(self, tick):
        stamp = local_time(tick['current_datetime'])
        start, price = self.bucket(stamp), float(tick['last_price'])
        closed = None
        if self.current_candle and stamp >= self.end(self.current_candle):
            closed = self.current_candle
            closed['final_save'] = True
            closed['supertrend'] = self.indicator.update(closed)
            self.candles.append(closed)
            atomic_json(self.file_path, self.candles)
            self.current_candle = None
            if local_time(closed['start_time']).date() == stamp.date():
                self.signal = closed['supertrend']
        if self.current_candle is None:
            self.current_candle = dict(start_time=start.isoformat(), open=price,
                high=price, low=price, close=price, volume=0, source='supertrend')
        elif start < local_time(self.current_candle['start_time']):
            return None
        c = self.current_candle
        c['high'], c['low'], c['close'] = max(c['high'], price), min(c['low'], price), price
        c['volume'] += tick.get('last_traded_quantity', 0)
        return closed


class WebSocketHandler:
    def __init__(self, kite, instruments=None):
        self.kite = kite
        self.instruments = [validate_config(i) for i in (instruments or [])]
        if not self.instruments:
            raise ValueError('Configure at least one instrument before starting')
        self.instrument_tokens = [int(i['instrument_token']) for i in self.instruments]
        if len(set(self.instrument_tokens)) != len(self.instrument_tokens):
            raise ValueError('Duplicate instrument tokens')
        self.candle_aggregators, self.prices = {}, {}
        self.halted, self.pending, self.blocked = set(), {}, set()
        self.expected_positions = {}
        self.last_poll = 0
        self.lock = threading.RLock()
        self.websocket_running = False
        self.connected_before = False
        self.state_path = Path('supertrend_trading_state.json')
        self.day = dt.datetime.now(IST).date().isoformat()
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if state.get('day') == self.day:
                self.halted = set(state.get('halted', []))
                self.pending = state.get('pending', {})
                self.blocked = set(state.get('blocked', []))
                self.expected_positions = state.get('expected_positions', {})
        now = dt.datetime.now(IST)
        for item in self.instruments:
            token = str(item['instrument_token'])
            exchange, symbol = self.identity(item)
            agg = CandleAggregator(token, symbol, item['timeframe'], exchange,
                item['supertrend_length'], item['supertrend_factor'])
            agg.bootstrap(kite, now)
            self.candle_aggregators[token] = agg
        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        self.kite_ticker.on_connect, self.kite_ticker.on_ticks = self.on_connect, self.on_ticks
        self.kite_ticker.on_close, self.kite_ticker.on_error = self.on_close, self.on_error
        self._save_state()

    def _save_state(self):
        atomic_json(self.state_path, dict(day=self.day, halted=sorted(self.halted),
            pending=self.pending, blocked=sorted(self.blocked), expected_positions=self.expected_positions))

    @staticmethod
    def identity(item):
        d = item['instrument_details']
        return d['exchange'], d['tradingsymbol']

    @staticmethod
    def group(item):
        return str(float(item['exit_trades_threshold_points']))

    def _submit(self, item, side, quantity, expected_position):
        token = str(item['instrument_token'])
        # Persist intent first. An ambiguous timeout must not duplicate an order.
        self.blocked.add(token)
        self._save_state()
        try:
            exchange, symbol = self.identity(item)
            oid = self.kite.place_order(variety='regular', exchange=exchange,
                tradingsymbol=symbol, transaction_type=side, quantity=quantity,
                order_type='MARKET', product='MIS', market_protection=10, tag='supertrend')
            self.pending[token] = str(oid)
            self.expected_positions[token] = expected_position
            self.blocked.discard(token)
            self._save_state()
            logging.info('Supertrend order %s: %s %s %s', oid, symbol, side, quantity)
        except Exception:
            logging.exception('Order outcome unknown; instrument %s blocked pending broker review', token)

    def reconcile(self, orders, positions):
        by_id = {str(o['order_id']): o for o in orders}
        for token, oid in list(self.pending.items()):
            order = by_id.get(oid)
            if order and order['status'] in TERMINAL:
                if order['status'] == 'COMPLETE' and token in self.expected_positions:
                    actual = sum(int(p['quantity']) for p in positions
                        if (p['exchange'], p['tradingsymbol']) == (order['exchange'], order['tradingsymbol'])
                        and p['product'] == 'MIS')
                    if actual != self.expected_positions[token]:
                        continue  # Positions snapshot can lag the order update.
                del self.pending[token]
                self.expected_positions.pop(token, None)
                if order['status'] != 'COMPLETE':
                    self.blocked.add(token)
                self._save_state()
        live = {(o['exchange'], o['tradingsymbol']) for o in orders
                if o.get('product') == 'MIS' and o['status'] not in TERMINAL}
        for item in self.instruments:
            token = str(item['instrument_token'])
            if token in self.pending or token in self.blocked or self.identity(item) in live:
                continue
            quantity = sum(int(p['quantity']) for p in positions
                if (p['exchange'], p['tradingsymbol']) == self.identity(item) and p['product'] == 'MIS')
            if self.group(item) in self.halted:
                desired = 0
            else:
                agg = self.candle_aggregators[token]
                now = dt.datetime.now(IST)
                close_hour, close_minute = (15, 30) if agg.exchange in ('NSE', 'NFO', 'BSE', 'BFO') else (23, 0)
                if now < agg.session_start(now) or now >= now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0):
                    continue
                signal = self.candle_aggregators[token].signal
                if signal is None:
                    continue
                side = 'BUY' if signal['colour'] == 'GREEN' else 'SELL'
                desired = (item['lot_size'] if side == 'BUY' else -item['lot_size']) if item['trade_side'] in ('BOTH', side) else 0
            if quantity and (desired == 0 or (quantity > 0) != (desired > 0)):
                self._submit(item, 'SELL' if quantity > 0 else 'BUY', abs(quantity), 0)
            elif not quantity and desired:
                self._submit(item, 'BUY' if desired > 0 else 'SELL', abs(desired), desired)
            # Reverse entry waits for exit COMPLETE and a flat broker position.

    def check_targets(self, orders):
        totals = {}
        for item in self.instruments:
            exchange, symbol = self.identity(item)
            cash, exposure = 0.0, 0
            for o in orders:
                if (o.get('exchange'), o.get('tradingsymbol')) != (exchange, symbol) or o.get('product') != 'MIS':
                    continue
                filled = int(o.get('filled_quantity', 0))
                if not filled:
                    continue
                sign = 1 if o['transaction_type'] == 'BUY' else -1
                exposure += sign * filled
                cash -= sign * filled * float(o['average_price'])
            token, group = str(item['instrument_token']), self.group(item)
            if exposure and token not in self.prices:
                totals[group] = None
                continue
            points = (cash + exposure * self.prices.get(token, 0)) / item['lot_size']
            if totals.get(group, 0) is not None:
                totals[group] = totals.get(group, 0) + points
        for group, points in totals.items():
            if points is not None and points >= float(group) and group not in self.halted:
                self.halted.add(group)
                self._save_state()
                logging.info('Daily group target reached: target=%s points=%s', group, points)

    def on_ticks(self, ws, ticks):
        with self.lock:
            if not self.websocket_running:
                return
            try:
                now = dt.datetime.now(IST)
                if now.date().isoformat() != self.day:
                    # Rebuild before processing a new session, preserving ATR history.
                    refreshed = {}
                    for token, agg in self.candle_aggregators.items():
                        fresh = CandleAggregator(agg.instrument_token, agg.tradingsymbol,
                            agg.interval_minutes, agg.exchange, agg.indicator.length, agg.indicator.factor)
                        fresh.bootstrap(self.kite, now)
                        refreshed[token] = fresh
                    self.candle_aggregators = refreshed
                    self.day = now.date().isoformat()
                    self.halted.clear()
                    self.prices.clear()
                    self._save_state()
                for tick in ticks:
                    token = str(tick['instrument_token'])
                    agg = self.candle_aggregators.get(token)
                    if agg is None:
                        continue
                    self.prices[token] = float(tick['last_price'])
                    if now < agg.session_start(now):
                        continue
                    end_hour, end_minute = (15, 30) if agg.exchange in ('NSE', 'NFO', 'BSE', 'BFO') else (23, 0)
                    if now >= now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0):
                        agg.signal = None
                        continue
                    agg.process_tick(dict(tick, current_datetime=now))
                if time.monotonic() - self.last_poll < 1:
                    return
                self.last_poll = time.monotonic()
                orders = self.kite.orders()
                positions = self.kite.positions()['net']
                self.check_targets(orders)
                self.reconcile(orders, positions)
            except Exception:
                logging.exception('Supertrend tick/reconciliation failed')

    def on_connect(self, ws, response):
        with self.lock:
            if self.connected_before:
                for token, agg in list(self.candle_aggregators.items()):
                    fresh = CandleAggregator(agg.instrument_token, agg.tradingsymbol,
                        agg.interval_minutes, agg.exchange, agg.indicator.length, agg.indicator.factor)
                    fresh.bootstrap(self.kite, dt.datetime.now(IST))
                    self.candle_aggregators[token] = fresh
            self.connected_before = True
        self.kite_ticker.subscribe(self.instrument_tokens)
        self.kite_ticker.set_mode(self.kite_ticker.MODE_FULL, self.instrument_tokens)

    def on_close(self, ws, code, reason):
        logging.info('Ticker closed: %s %s', code, reason)

    def on_error(self, ws, code, reason):
        logging.error('Ticker error: %s %s', code, reason)

    def run_websocket(self):
        self.websocket_running = True
        try:
            self.kite_ticker.connect(threaded=True)
        except Exception:
            self.websocket_running = False
            raise

    def stop_websocket(self):
        with self.lock:
            self.websocket_running = False
            self.kite_ticker.close(1000, 'Stopped by user')

    def is_running(self):
        return self.websocket_running
