import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime as _ExecutionDateTime
from pathlib import Path
import hashlib
import uuid
from .thresholds import build_groups, key as threshold_key
import threading
import json
import logging
from django.http import JsonResponse
from kiteconnect import KiteConnect, KiteTicker
import time
import datetime
from .product_setting import REDIS_HOST, REDIS_PORT, REDIS_DB
import redis
import math
import asyncio
import sys
from zoneinfo import ZoneInfo
from collections import defaultdict
import json
import math
import logging
from logging.handlers import RotatingFileHandler
# Initialize Redis client using Django settings
# redis_client = redis.StrictRedis(
#     host=REDIS_HOST,
#     port=REDIS_PORT,
#     db=REDIS_DB
# )

logging.basicConfig(level=logging.DEBUG)


TERMINAL = {'COMPLETE', 'CANCELLED', 'REJECTED'}


@dataclass
class Position:
    quantity: int = 0
    average: float = 0.0
    realized_cash: float = 0.0
    completed_points: float = 0.0
    cycle_quantity: int = 0
    cycle_cash: float = 0.0

    def fill(self, signed_quantity, price):
        remaining = abs(signed_quantity)
        sign = 1 if signed_quantity > 0 else -1
        while remaining:
            if self.quantity == 0 or self.quantity * sign > 0:
                amount = abs(self.quantity)
                self.average = (amount * self.average + remaining * price) / (amount + remaining)
                self.cycle_quantity += remaining
                self.quantity += sign * remaining
                remaining = 0
            else:
                part = min(remaining, abs(self.quantity))
                cash = part * (price - self.average) * (1 if self.quantity > 0 else -1)
                self.realized_cash += cash
                self.cycle_cash += cash
                self.quantity += sign * part
                remaining -= part
                if self.quantity == 0:
                    self.completed_points += self.cycle_cash / self.cycle_quantity
                    self.cycle_cash = 0.0
                    self.cycle_quantity = 0
                    self.average = 0.0

    def unrealized_points(self, price):
        if not self.quantity:
            return 0.0
        return (price - self.average) * (1 if self.quantity > 0 else -1)

    def daily_points(self, price):
        if not self.cycle_quantity:
            return self.completed_points
        # Partial closures contribute by their fraction of the position, not once per order.
        return self.completed_points + (self.cycle_cash +
            self.unrealized_points(price) * abs(self.quantity)) / self.cycle_quantity


def replay(orders):
    positions = {}
    for order in sorted(orders, key=lambda o: (str(o.get('order_timestamp', '')), str(o['order_id']))):
        quantity = int(order.get('filled_quantity', order.get('quantity', 0)
                       if order.get('status') == 'COMPLETE' else 0) or 0)
        price = float(order.get('average_price') or 0)
        if quantity <= 0 or not math.isfinite(price) or price <= 0:
            continue
        if order.get('transaction_type') not in ('BUY', 'SELL'):
            continue
        key = (order.get('exchange'), order.get('tradingsymbol'))
        position = positions.setdefault(key, Position())
        position.fill(quantity if order['transaction_type'] == 'BUY' else -quantity, price)
    return positions


class ExecutionEngine:
    """One engine per account/session. REST reconciliation runs on a background worker."""
    def __init__(self, kite, instruments, groups, state_path='trading_execution_state.json',
                 clock=time.monotonic, day=None):
        self.kite = kite
        self.instruments = instruments
        self.groups = groups
        self.clock = clock
        self.day = day or (lambda: _ExecutionDateTime.now(ZoneInfo('Asia/Kolkata')).date().isoformat())
        self.session_day = self.day()
        self.owner = hashlib.sha256((str(kite.api_key) + ':' + str(kite.access_token)).encode()).hexdigest()
        self.path = Path(state_path) if state_path else None
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.orders = {}
        self.versions = {}
        self.version = 0
        self.positions = {}
        self.prices = {}
        self.contexts = {}
        self.closing_groups = set()
        self.ready = False
        self.connected = True
        self.next_poll = 0.0
        self.failures = 0
        self.last_success = None
        self.submission_times = deque()
        self.stop_event = threading.Event()
        self.worker = None
        self.persistence_error = False
        self._load()

    @staticmethod
    def identity(item):
        d = item['instrument_details']
        return d['exchange'], d['tradingsymbol']

    def context(self, item):
        token = str(item['instrument_token'])
        return self.contexts.setdefault(token, {})

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text())
            if saved['day'] == self.session_day and saved['owner'] == self.owner:
                self.contexts = saved['contexts']
                for ctx in self.contexts.values():
                    ctx['retry_at'] = 0.0  # Monotonic deadlines do not survive process restarts.
                    ctx['cancel_sent'] = False
                self.closing_groups = set(saved.get('closing_groups', []))
        except Exception:
            self.persistence_error = True
            logging.exception('Cannot recover execution checkpoint; trading blocked')

    def save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(dict(day=self.session_day, owner=self.owner,
                contexts=self.contexts, closing_groups=list(self.closing_groups)), default=str))
            os.replace(tmp, self.path)
        except Exception:
            self.persistence_error = True
            logging.exception('Execution checkpoint failed; further submissions blocked')

    def _merge(self, update):
        if not update.get('order_id'):
            return False
        oid = str(update['order_id'])
        old = self.orders.get(oid, {})
        # A late OPEN snapshot must not roll a confirmed fill/terminal status backwards.
        old_fill = int(old.get('filled_quantity', 0) or 0)
        new_fill = int(update.get('filled_quantity', 0) or 0)
        if new_fill < old_fill or (old.get('status') in TERMINAL and
                update.get('status') not in TERMINAL):
            return False
        if old.get('exchange_update_timestamp') and update.get('exchange_update_timestamp'):
            if str(update['exchange_update_timestamp']) < str(old['exchange_update_timestamp']):
                return False
        merged = {**old, **update, 'order_id': oid}
        if merged == old:
            return False
        if self.ready and (new_fill > old_fill or (not old and merged.get('status') not in TERMINAL)):
            for item in self.instruments:
                if self.identity(item) != (merged.get('exchange'), merged.get('tradingsymbol')):
                    continue
                ctx = self.context(item)
                owned = oid in (str(ctx.get('entry_id')), str(ctx.get('pending_id')))
                owned = owned or (ctx.get('pending_tag') and merged.get('tag') == ctx['pending_tag'])
                if not owned:
                    ctx['blocked'] = 'External fill detected; ledger updated, automatic trading blocked'
                    self.save()
        self.orders[oid] = merged
        self.version += 1
        self.versions[oid] = self.version
        return True

    def _rebuild(self):
        self.positions = replay(self.orders.values())
        snapshot = list(self.orders.values())
        for group in self.groups.values():
            group.observe(snapshot)
        # Non-MIS exposure cannot safely be closed by this MIS-only trading bot.
        for item in self.instruments:
            key = self.identity(item)
            if any((o.get('exchange'), o.get('tradingsymbol')) == key and
                   o.get('product', 'MIS') != 'MIS' and int(o.get('filled_quantity', 0) or 0)
                   for o in snapshot):
                self.context(item)['blocked'] = 'Configured symbol has non-MIS fills; reconcile manually'

    def on_order_update(self, ws, data):
        with self.lock:
            if self._merge(data):
                self._rebuild()

    def reconcile(self):
        """At most one account-wide history read/second; back off failures without sleeping ticks."""
        if not self.poll_lock.acquire(False):
            return False
        try:
            with self.lock:
                now = self.clock()
                if now < self.next_poll or self.day() != self.session_day:
                    return False
                self.next_poll = now + 1.0
                version = self.version
                bootstrap = not self.ready
            try:
                snapshot = self.kite.orders()
                broker_positions = self.kite.positions()['net'] if bootstrap else None
            except Exception:
                with self.lock:
                    self.failures += 1
                    self.next_poll = self.clock() + min(60.0, 2.0 ** min(self.failures, 6))
                logging.exception('Broker reconciliation failed; cached fills retained, retry delayed')
                return False
            with self.lock:
                changed = False
                for order in snapshot:
                    if self.versions.get(str(order.get('order_id')), 0) <= version:
                        changed = self._merge(order) or changed
                if changed or bootstrap:
                    self._rebuild()
                if bootstrap:
                    for item in self.instruments:
                        key = self.identity(item)
                        ctx = self.context(item)
                        position = self.positions.get(key, Position())
                        actual = sum(int(p['quantity']) for p in broker_positions
                            if (p.get('exchange'), p.get('tradingsymbol')) == key)
                        if actual != position.quantity:
                            ctx['blocked'] = 'Broker position differs from daily fills (carry-in/manual exposure)'
                        active_orders = [o for o in self.orders.values() if
                            (o.get('exchange'), o.get('tradingsymbol')) == key and
                            o.get('status') not in TERMINAL]
                        if position.quantity and not ctx.get('entry_id') and not ctx.get('pending_tag'):
                            ctx['blocked'] = 'Open position has no saved entry/stop/target context'
                        if any(not ((ctx.get('pending_id') and str(o['order_id']) == str(ctx['pending_id'])) or
                                    (ctx.get('pending_tag') and o.get('tag') == ctx['pending_tag']))
                               for o in active_orders):
                            ctx['blocked'] = 'Untracked pending broker order'
                    self.ready = True
                    self.save()
                self.failures = 0
                self.last_success = self.clock()
                self.next_poll = self.clock() + 1.0
                return True
        finally:
            self.poll_lock.release()

    def start(self):
        if self.worker:
            return
        def work():
            while not self.stop_event.is_set():
                self.reconcile()
                self.stop_event.wait(0.25)
        self.worker = threading.Thread(target=work, name='broker-reconciliation', daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()

    def mark(self, item, price):
        if not math.isfinite(price) or price <= 0:
            return
        with self.lock:
            self.prices[self.identity(item)] = price

    def group_points(self, item):
        group = self.groups[str_key(item['exit_trades_threshold_points'])]
        total = 0.0
        for key in set(group.members):
            position = self.positions.get(key, Position())
            if position.quantity and key not in self.prices:
                return None
            total += position.daily_points(self.prices.get(key, 0.0))
        return total

    def _take_submission_slot(self):
        now = self.clock()
        while self.submission_times and now - self.submission_times[0] >= 1.0:
            self.submission_times.popleft()
        if len(self.submission_times) >= 5:
            return False
        self.submission_times.append(now)
        return True

    def _cancel_entry(self, ctx):
        if not ctx.get('pending_id'):
            return
        if ctx.get('cancel_sent') and (self.clock() < ctx.get('cancel_after', 0) or
                self.last_success is None or self.last_success <= ctx.get('cancel_at', 0)):
            return
        current = self.orders.get(str(ctx['pending_id']), {})
        if current.get('status') in TERMINAL or current.get('status') == 'CANCEL PENDING':
            return
        if not self._take_submission_slot():
            return
        ctx['cancel_sent'] = True
        ctx['cancel_at'] = self.clock()
        ctx['cancel_after'] = self.clock() + 2.0
        self.save()
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=ctx['pending_id'])
        except Exception:
            logging.exception('Entry remainder cancellation uncertain; awaiting broker status')

    def _submit(self, item, ctx, side, quantity, kind):
        if self.persistence_error or ctx.get('pending_kind') or self.clock() < ctx.get('retry_at', 0):
            return None
        if not self._take_submission_slot():
            return None
        tag = 'at' + uuid.uuid4().hex[:18]
        ctx.update(pending_kind=kind, pending_tag=tag, pending_id=None, cancel_sent=False)
        self.save()  # Durable intent exists before the network call.
        if self.persistence_error:
            return None
        try:
            oid = self.kite.place_order(variety=self.kite.VARIETY_REGULAR,
                exchange=item['instrument_details']['exchange'],
                tradingsymbol=item['instrument_details']['tradingsymbol'],
                transaction_type=side.upper(), quantity=int(quantity),
                order_type=self.kite.ORDER_TYPE_MARKET, product=self.kite.PRODUCT_MIS,
                market_protection=10, tag=tag)
        except Exception as error:
            if getattr(error, 'code', None) in (400, 403, 429):
                # An explicit rejection differs from a lost/ambiguous submission response.
                ctx.pop('pending_kind', None)
                ctx.pop('pending_tag', None)
                ctx.pop('pending_id', None)
                ctx['retry_at'] = self.clock() + 2.0
                if error.code != 429:
                    ctx['blocked'] = 'Broker rejected submission: ' + str(error)
                self.save()
                logging.error('Submission rejected code=%s symbol=%s', error.code,
                    item['instrument_details']['tradingsymbol'])
                return None
            # A timeout can occur after acceptance. Reconcile by tag; never blindly resubmit.
            ctx['uncertain'] = True
            self.save()
            logging.exception('Submission outcome uncertain tag=%s; waiting for reconciliation', tag)
            return None
        ctx['pending_id'] = str(oid)
        ctx['uncertain'] = False
        if kind == 'entry':
            ctx['entry_id'] = str(oid)
        self.save()
        logging.info('order_submitted symbol=%s id=%s side=%s quantity=%s kind=%s',
            item['instrument_details']['tradingsymbol'], oid, side, quantity, kind)
        return oid

    def _pending(self, ctx):
        oid = ctx.get('pending_id')
        if not oid and ctx.get('pending_tag'):
            matches = [o for o in self.orders.values() if o.get('tag') == ctx['pending_tag']]
            if len(matches) == 1:
                oid = str(matches[0]['order_id'])
                ctx['pending_id'] = oid
                ctx['uncertain'] = False
                if ctx['pending_kind'] == 'entry':
                    ctx['entry_id'] = oid
                self.save()
        return self.orders.get(str(oid)) if oid else None

    def sync(self, a, item):
        ctx = self.context(item)
        pos = self.positions.get(self.identity(item), Position())
        a.current_order_type = ('Buy' if pos.quantity > 0 else 'Sell') if pos.quantity else None
        a.order_active = bool(pos.quantity or ctx.get('pending_kind'))
        a.order_id = ctx.get('entry_id')
        a.current_stop_loss = ctx.get('stop')
        a.per_trade_candle_based_profit = ctx.get('range', a.per_trade_candle_based_profit)
        a.close_trade_for_the_day = bool(ctx.get('closed'))
        return ctx, pos

    def process(self, a, item, price):
        """Runs on ticks; reads local fills only. Only actual submissions/cancels use REST."""
        with self.lock:
            if self.stop_event.is_set() or not self.ready or self.day() != self.session_day:
                return
            ctx, pos = self.sync(a, item)
            if ctx.get('blocked') or self.persistence_error:
                return
            group_key = str_key(item['exit_trades_threshold_points'])
            points = self.group_points(item)
            a.profit_threshold_points = points if points is not None else 0.0
            if points is not None and points >= float(self.groups[group_key].active):
                if group_key not in self.closing_groups:
                    self.closing_groups.add(group_key)
                    self.save()
            closing = group_key in self.closing_groups
            if ctx.get('closed') and not pos.quantity and not ctx.get('pending_kind'):
                return
            pending = self._pending(ctx) if ctx.get('pending_kind') else None
            # Unknown/manual exposure is reflected in group P/L but must not spawn duplicate entries.
            if pos.quantity and not ctx.get('entry_id'):
                ctx['blocked'] = 'Unmanaged exposure requires entry/stop/target recovery'
                self.save()
                return
            if pending and pending.get('status') in TERMINAL:
                kind = ctx.pop('pending_kind')
                ctx.pop('pending_id', None)
                ctx.pop('pending_tag', None)
                ctx.pop('uncertain', None)
                ctx['retry_at'] = self.clock() + (2.0 if pending['status'] != 'COMPLETE' else 0.0)
                if kind == 'entry' and not pos.quantity:
                    ctx.pop('entry_id', None)
                if kind == 'exit' and not pos.quantity:
                    reason = ctx.pop('exit_reason', None)
                    if closing or reason == 'day':
                        ctx['closed'] = True
                    elif reason == 'reverse' and a.trade_side == 'BOTH':
                        ctx['reverse_ready'] = True
                    elif reason == 'profit':
                        ctx['wait_candle'] = ctx.get('exit_candle')
                    ctx.pop('entry_id', None)
                self.save()
                self.sync(a, item)
                pending = None
                if kind == 'exit' and not pos.quantity and not ctx.get('reverse_ready'):
                    return
            if pos.quantity:
                if ctx.get('stop') is None or not ctx.get('range'):
                    ctx['blocked'] = 'Open position lacks a valid stop/target'
                    self.save()
                    return
                a.update_trailing_stop_loss(self.kite, float(item['trade_calculation_percentage']), a.tradingsymbol)
                if ctx['stop'] != a.current_stop_loss:
                    ctx['stop'] = a.current_stop_loss
                    self.save()
                profit = pos.unrealized_points(price)
                target = float(item['per_trade_exit_trades_threshold_points']) * ctx['range']
                sl_hit = price <= ctx['stop'] if pos.quantity > 0 else price >= ctx['stop']
                reason = 'day' if closing else ('profit' if profit >= target else ('reverse' if sl_hit else None))
                if reason and not ctx.get('exit_reason'):
                    ctx['exit_reason'] = reason
                    ctx['exit_candle'] = dict(a.candles[-2]) if len(a.candles) >= 2 else None
                    if reason == 'reverse' and len(a.candles) >= 2:
                        candle = a.candles[-2]
                        ctx['reverse_range'] = candle['high'] - candle['low']
                        ctx['reverse_side'] = 'Sell' if pos.quantity > 0 else 'Buy'
                    self.save()
                if ctx.get('pending_kind') == 'entry' and ctx.get('exit_reason'):
                    if pending:
                        self._cancel_entry(ctx)
                    return
                if ctx.get('pending_kind'):
                    return
                if ctx.get('exit_reason'):
                    self._submit(item, ctx, 'Sell' if pos.quantity > 0 else 'Buy', abs(pos.quantity), 'exit')
                return
            if ctx.get('pending_kind'):
                if closing and ctx['pending_kind'] == 'entry' and pending:
                    self._cancel_entry(ctx)
                return
            if closing:
                ctx['closed'] = True
                ctx.pop('reverse_ready', None)
                self.save()
                return
            if ctx.get('entry_id'):
                # A manual closure must clear active state and suppress an unintended reversal.
                ctx.pop('entry_id', None)
                ctx.pop('exit_reason', None)
                ctx.pop('reverse_ready', None)
                ctx['wait_candle'] = dict(a.candles[-2]) if len(a.candles) >= 2 else None
                self.save()
                return
            if not self.connected or self.last_success is None or self.clock() - self.last_success > 10:
                return  # New exposure needs a recent successful reconciliation.
            if self.clock() < ctx.get('retry_at', 0):
                return
            if ctx.get('reverse_ready'):
                side = ctx['reverse_side']
                candle_range = ctx['reverse_range']
                ctx.pop('reverse_ready')
            else:
                if ctx.get('wait_candle'):
                    if len(a.candles) < 2 or a.candles[-2] == ctx['wait_candle']:
                        return
                    ctx.pop('wait_candle', None)
                signal = a.check_strategy(int(item['instrument_token']), float(item['trade_calculation_percentage']))
                if not signal:
                    return
                side = signal['order_type']
                ctx['stop'] = signal['stop_loss']
                candle_range = a.per_trade_candle_based_profit
            if not math.isfinite(candle_range) or candle_range <= 0:
                logging.error('Invalid entry candle range for %s', a.tradingsymbol)
                return
            ctx['range'] = candle_range
            self._submit(item, ctx, side, int(item['lot_size']), 'entry')
            self.sync(a, item)


def str_key(value):
    from .thresholds import key
    return key(value)


class CandleAggregator:
    def __init__(self, instrument_token,tradingsymbol ,interval_minutes=15 ,file_path='minute_candles.json',trade_side="BOTH",instrument_details_dict = []):
        self.file_path = str(instrument_token)+'_'+str(interval_minutes) + '_' + file_path
        self.instrument_token = instrument_token  # Add the instrument token
        self.tradingsymbol = tradingsymbol  # Add the instrument token
        self.interval_minutes = interval_minutes
        self.current_candle = None
        self.candles = []  # This can remain as a list if needed elsewhere
        self.threshold_group = None
        self.trade_side = trade_side
        # Attributes for order management
        self.current_stop_loss = None
        self.current_order_type = None
        self.order_active = False  # Track if an order is active
        self.profit_threshold_points = 0  # To track total profit or loss
        self.open_price = None
        self.close_price = None
        self.close_trade_for_the_day = False
        self.previous_trailing_candle = None
        self.open_positions = False
        self.instrument_details_dict = instrument_details_dict
        self.per_trade_candle_based_profit = 1
        self.order_id = None
        self.just_closed_trade = False
        self.just_closed_trade_last_candle = None
        # Load previous candles from the file, if available
        if os.path.exists(self.file_path):
            with open(self.file_path, 'r') as file:
                try:
                    self.candles = json.load(file)
                except json.JSONDecodeError:
                    self.candles = []
        else:
            self.candles = []

    def active_exit_threshold(self, original):
        return float(self.threshold_group.active) if self.threshold_group else original

    def _reset_position(self):
        """Reset the open position attributes."""
        self.open_position = False
        self.open_price = None
        self.open_quantity = 0
        self.current_order_type = None

    def save_candles(self, new_candle):
        try:
            # Load existing candles from the file
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as file:
                    try:
                        previous_candles = json.load(file)
                        # Convert the list to a dictionary for easier updates
                        candle_dict = {candle['start_time']: candle for candle in previous_candles}
                    except json.JSONDecodeError:
                        candle_dict = {}
            else:
                candle_dict = {}

            # Update or add the new candle
            candle_dict[new_candle['start_time']] = new_candle
            
            # Save the updated candles to the JSON file
            with open(self.file_path, 'w') as file:
                json.dump(list(candle_dict.values()), file, indent=4)
            
            logging.info(f"Candle with start_time {new_candle['start_time']} updated or added successfully.")
            # Return all candles in the format: a list of dictionaries
            return list(candle_dict.values())
        except Exception as error:
            logging.error(f"error {error}")
            return []

    def process_tick(self, tick):
        """ Process a new tick and update the candle data. """
        try:
            # Ensure required fields exist in the tick data
            if 'last_price' not in tick or 'last_traded_quantity' not in tick or 'current_datetime' not in tick:
                logging.error(f"Missing required fields in tick: {tick}")
                return  # Skip processing this tick if essential fields are missing

            last_price = tick['last_price']
            with open('last_price_log.txt', 'a') as log_file: log_file.write(f"last_price: {tick['last_price']},{tick['current_datetime']}\n")

            # Parse tick time
            tick_time = datetime.datetime.strptime(str(tick['current_datetime']), '%Y-%m-%d %H:%M:%S.%f%z')

            # Make tick_time timezone-naive by stripping the tzinfo
            tick_time = tick_time.replace(tzinfo=None)

            # Align the tick time to the nearest second and reset microseconds
            candle_start_time = tick_time.replace(second=0, microsecond=0)

            # If no candle exists, create the first candle at the tick's time
            if self.current_candle is None:
                self.current_candle = {
                    'start_time': candle_start_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'open': last_price,
                    'high': last_price,
                    'low': last_price,
                    'close': last_price,
                    'volume': tick['last_traded_quantity'],
                    'ohlc_high':tick['ohlc']['high'],
                    'ohlc_low':tick['ohlc']['low'],
                    'final_save': False
                }
            else:
                # Get the start time of the current candle and make it timezone-naive
                last_candle_start_time = datetime.datetime.strptime(self.current_candle['start_time'], '%Y-%m-%d %H:%M:%S')
                last_candle_start_time = last_candle_start_time.replace(second=0, microsecond=0, tzinfo=None)

                # Calculate the next candle's start time
                next_candle_start_time = last_candle_start_time + datetime.timedelta(minutes=self.interval_minutes)

                # Check if the tick_time indicates the need for a new candle
                if tick_time >= next_candle_start_time:
                    # Update the current candle's OHLC values before closing
                    if not self.current_candle['final_save']:
                        if self.current_candle['ohlc_high']!= tick['ohlc']['high']:
                            self.current_candle['high'] = max(self.current_candle['high'], last_price, tick['ohlc']['high'])
                        else:
                            self.current_candle['high'] = max(self.current_candle['high'], last_price)
                        if self.current_candle['ohlc_low']!= tick['ohlc']['low']:
                            self.current_candle['low'] = min(self.current_candle['low'], last_price, tick['ohlc']['low'])
                        else:
                            self.current_candle['low'] = min(self.current_candle['low'], last_price)
                        self.current_candle['close'] = last_price
                        self.current_candle['volume'] += tick['last_traded_quantity']
                        self.current_candle['ohlc_high'] = tick['ohlc']['high']
                        self.current_candle['ohlc_low'] = tick['ohlc']['low']
                        self.current_candle['final_save'] = True
                        # Save the closed candle
                        self.candles = self.save_candles(self.current_candle)
                        logging.info(f"Candle closed and saved: {self.current_candle}")
                        return 

                    # Start a new candle at the next interval
                    self.current_candle = {
                        'start_time': next_candle_start_time.strftime('%Y-%m-%d %H:%M:%S'),
                        'open': last_price,
                        'high': last_price,
                        'low': last_price,
                        'close': last_price,
                        'volume': tick['last_traded_quantity'],
                        'ohlc_high':tick['ohlc']['high'],
                        'ohlc_low':tick['ohlc']['low'],
                        'final_save': False
                    }
                else:
                    # Update the current candle's OHLC values and volume
                    if self.current_candle['ohlc_high']!= tick['ohlc']['high']:
                        self.current_candle['high'] = max(self.current_candle['high'], last_price, tick['ohlc']['high'])
                    else:
                        self.current_candle['high'] = max(self.current_candle['high'], last_price)
                    if self.current_candle['ohlc_low']!= tick['ohlc']['low']:
                        self.current_candle['low'] = min(self.current_candle['low'], last_price, tick['ohlc']['low'])
                    else:
                        self.current_candle['low'] = min(self.current_candle['low'], last_price)
                    self.current_candle['close'] = last_price
                    self.current_candle['volume'] += tick['last_traded_quantity']
                    self.current_candle['ohlc_high'] = tick['ohlc']['high']
                    self.current_candle['ohlc_low'] = tick['ohlc']['low']

                    # Save the updated candle
                    self.candles = self.save_candles(self.current_candle)
                    logging.debug(f"Candle updated and saved: {self.current_candle}")

        except KeyError as e:
            logging.error(f"KeyError: Missing expected key {e} in tick: {tick}")
        except ValueError as e:
            logging.error(f"ValueError: Invalid value in tick data: {tick}, Error: {e}")
        except Exception as e:
            logging.error(f"Unexpected error while processing tick: {tick}, Error: {e}")


    def check_strategy(self, instrument_token, percentage):
        """ Check the strategy based on the previous two candles and the percentage for buy/sell signals. """
        
        # Open the log file in append mode
        with open(f'strategy_{instrument_token}_log.txt', 'a') as log_file:
            
            # Log initial info
            print(f"Checking strategy for instrument_token: {instrument_token}, percentage: {percentage}", file=log_file)

            # Check if there are enough candles
            if len(self.candles) < 2:
                print(f"Not enough candles. Candles count: {len(self.candles)}", file=log_file)
                return None  # Not enough candles to make a decision

            # Get previous two candles
            prev_candle_1 = self.candles[-2]  # Most recent completed candle
            prev_candle_2 = self.candles[-2]  # The candle before the most recent one

            print(f"Previous Candle 1: {prev_candle_1}, Previous Candle 2: {prev_candle_2}", file=log_file)

            # Calculate the high and low for the strategy
            max_high = max(prev_candle_1['high'], prev_candle_2['high'])
            min_low = min(prev_candle_1['low'], prev_candle_2['low'])

            print(f"max_high: {max_high}, min_low: {min_low}", file=log_file)

            # Calculate x_value_higher and x_value_lower using the user-defined percentage
            self.x_value_higher = math.ceil(max_high + ((percentage / 100) * max_high))
            self.x_value_lower = math.floor(min_low - ((percentage / 100) * min_low))

            print(f"x_value_higher: {self.x_value_higher}, x_value_lower: {self.x_value_lower}", file=log_file)

            # Get the current candle's high and low values
            current_high = self.current_candle['close']
            current_low = self.current_candle['close']
            
            self.per_trade_candle_based_profit = prev_candle_1['high'] - prev_candle_1['low']

            print(f"Current Candle High: {current_high}, Current Candle Low: {current_low}, per_trade_candle_based_profit:{self.per_trade_candle_based_profit}", file=log_file)

            # Initialize response data
            response = {}

            # Check for Buy or Sell signals and calculate stop loss
            if current_high > self.x_value_higher:
                stop_loss = self.calculate_stop_loss_func("Buy", percentage)
                response = {
                    "instrument_token": instrument_token,
                    "order_type": "Buy",
                    "stop_loss": stop_loss
                }
                print(f"Buy signal generated. Stop Loss: {stop_loss}", file=log_file)
            elif current_low < self.x_value_lower:
                stop_loss = self.calculate_stop_loss_func("Sell", percentage)
                response = {
                    "instrument_token": instrument_token,
                    "order_type": "Sell",
                    "stop_loss": stop_loss
                }
                print(f"Sell signal generated. Stop Loss: {stop_loss}", file=log_file)
            else:
                print(f"No signals generated. Conditions not met.", file=log_file)

            #Adjusting Strategy based on user defined order sides
            if response and "order_type" in response:
                if (self.trade_side == "BUY" and response["order_type"].lower() == "sell") or \
                (self.trade_side == "SELL" and response["order_type"].lower() == "buy"):response = {}

            print(f"Response: {response}", file=log_file)

            return response

    def calculate_stop_loss_func(self, order_type, percentage):
        """ Calculate the stop loss for the current order based on previous candles. """

        # Open the log file in append mode
        with open('calculate_stop_loss_func.txt', 'a') as log_file:
            
            # Print that stop loss calculation has started
            print(f"Calculating stop loss for order_type: {order_type} with percentage: {percentage}", file=log_file)

            # Get the previous two candles
            prev_candle_1 = self.candles[-2]
            prev_candle_2 = self.candles[-2]

            # Print previous candles information
            print(f"Previous Candle 1: {prev_candle_1}, Previous Candle 2: {prev_candle_2}", file=log_file)

            # Determine floor and ceiling values from previous candles
            floor_value = min(prev_candle_1['low'], prev_candle_2['low'])  # Minimum low for Buy
            ceil_value = max(prev_candle_1['high'], prev_candle_2['high'])  # Maximum high for Sell

            # Print floor and ceiling values
            print(f"Floor Value: {floor_value}, Ceiling Value: {ceil_value}", file=log_file)

            # Calculate stop loss based on the order type
            if order_type == "Buy":
                stop_loss = math.floor(floor_value - (percentage / 100 * floor_value))
                print(f"Calculated Buy Stop Loss: {stop_loss}", file=log_file)
            elif order_type == "Sell":
                stop_loss = math.ceil(ceil_value + (percentage / 100 * ceil_value))
                print(f"Calculated Sell Stop Loss: {stop_loss}", file=log_file)
            else:
                stop_loss = None
                print(f"Unknown order type: {order_type}. Stop loss set to None.", file=log_file)

            # Return the calculated stop loss
            return stop_loss


    
        
        

    
            
        


    def update_trailing_stop_loss(self, kite, percentage,tradingsymbol):
        """ Update trailing stop loss for open orders based on the latest candle values. """
        try:
            # Set up logging with a FileHandler
            logger = logging.getLogger("trailing_stop_loss")
            logger.setLevel(logging.INFO)

            # Avoid duplicate handlers
            if not logger.handlers:
                file_handler = logging.FileHandler("trailing_stop_loss_updates.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

            # Check for minimum candles
            if len(self.candles) < 2:
                #logger.info("Insufficient candle data (less than 3 candles). Exiting function.")
                return

            prev_candle_1 = self.candles[-2]
            prev_candle_2 = self.candles[-2]

            # Check if previous trailing candle is the same
            # if self.previous_trailing_candle == prev_candle_1:
            #     logger.info("Previous trailing candle is the same as the last candle. Exiting function.")
            #     return
            # else:
            #     self.previous_trailing_candle = prev_candle_1

            # Calculate high and low values
            x_value_higher = max(prev_candle_1['high'], prev_candle_2['high'])
            x_value_lower = min(prev_candle_1['low'], prev_candle_2['low'])


            order_type = self.current_order_type

            # Calculate new trailing stop loss
            if order_type == "Buy":
                new_stop_loss = math.floor(x_value_lower - (percentage / 100 * x_value_lower))
                if self.current_stop_loss == new_stop_loss:
                    #logger.info("Trailing stop loss for BUY order is unchanged. Exiting function.")
                    return
                logger.info(f"Updated trailing stop loss for BUY order of {tradingsymbol} to {new_stop_loss}")
                self.current_stop_loss = new_stop_loss

            elif order_type == "Sell":
                new_stop_loss = math.ceil(x_value_higher + (percentage / 100 * x_value_higher))
                if self.current_stop_loss == new_stop_loss:
                    #logger.info("Trailing stop loss for SELL order is unchanged. Exiting function.")
                    return
                logger.info(f"Updated trailing stop loss for SELL order of {tradingsymbol} to {new_stop_loss}")
                self.current_stop_loss = new_stop_loss

        except Exception as error:
            logger.exception(f"Error in update_trailing_stop_loss for {tradingsymbol}: {error}")


    def write_profit_loss_to_json(self,profit_loss_data, filename="current_profit_loss.json"):
        """
        Appends profit or loss data to a JSON file in the format
        :param profit_loss_data: Dictionary containing stock symbols and their profit/loss values
        :param filename: The name of the JSON file to write to (default: profit_loss.json)
        """
        try:
            # Set up logging with a FileHandler
            logger = logging.getLogger("trailing_stop_loss")
            logger.setLevel(logging.INFO)

            # Avoid duplicate handlers
            if not logger.handlers:
                file_handler = logging.FileHandler("trailing_stop_loss_updates.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

            try:
                # Read existing data from the file if it exists
                try:
                    with open(filename, 'r') as file:
                        existing_data = json.load(file)
                except FileNotFoundError:
                    # Create the file with an empty dictionary if it doesn't exist
                    with open(filename, 'w') as file:
                        json.dump({}, file)
                    existing_data = {}

                # Update the existing data with the new profit/loss data
                existing_data.update(profit_loss_data)

                # Write the updated data back to the file
                with open(filename, 'w') as file:
                    json.dump(existing_data, file, indent=4)

                #logger.info(f"Profit/loss data successfully updated in {filename}.")
                print(f"Profit/loss data successfully updated in {filename}.")
                return True
            except Exception as e:
                logger.error(f"An error occurred while updating the file: {e}")
                print(f"An error occurred while updating the file: {e}")
                return False
        except Exception as e:
            logger.error(f"An error occurred while updating the file: {e}")
            print(f"An error occurred while updating the file: {e}")
            return False
    def fetch_profit_loss_from_json_dict(self,keys, filename="current_profit_loss.json"):
        """
        Fetches profit or loss values for one or more keys from a JSON file.

        :param keys: List of keys to fetch values for.
        :param filename: The name of the JSON file to read from (default: profit_loss.json)
        :return: Dictionary containing the requested keys and their profit/loss values.
        """
        try:
            # Read data from the file
            with open(filename, 'r') as file:
                data = json.load(file)

            # Extract values for the requested keys
            result = [data.get(key, 0) for key in keys]
            return sum(result)
        except FileNotFoundError:
            print(f"The file {filename} does not exist.")
            return 0
        except Exception as e:
            print(f"An error occurred while reading the file: {e}")
            return 0

# WebSocket Handler Class
class WebSocketHandler:
    def __init__(self, kite, instruments=[]):
        self.threshold_groups = build_groups(instruments)
        self.websocket_running = True
        self.kite = kite
        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        
        # Store instrument details
        self.instruments = instruments
        self.instrument_tokens = [int(x['instrument_token']) for x in instruments]
        # Create a CandleAggregator instance for each instrument, passing the instrument_token
        self.candle_aggregators = {
            x['instrument_token']: CandleAggregator(instrument_token=int(x['instrument_token']),
                                                    tradingsymbol=x['instrument_details']['tradingsymbol'],
                                                    interval_minutes=int(x['timeframe']),trade_side=x['trade_side'],
                                                    instrument_details_dict = self.restructure_for_combined_threshold(instruments)) for x in instruments
        }

        for item in instruments:
            self.candle_aggregators[item['instrument_token']].threshold_group = self.threshold_groups[threshold_key(item['exit_trades_threshold_points'])]

        self.execution = ExecutionEngine(kite, instruments, self.threshold_groups,
            state_path=os.getenv('TRADING_STATE_PATH', 'trading_execution_state.json'))
        self.kite_ticker.on_order_update = self.execution.on_order_update

        # Define on_ticks method
        self.kite_ticker.on_ticks = self.on_ticks
        self.kite_ticker.on_connect = self.on_connect
        self.kite_ticker.on_close = self.on_close
        self.kite_ticker.on_error = self.on_error
        self.kite_ticker.on_noreconnect = self.on_noreconnect
        self.kite_ticker.on_reconnect = self.on_reconnect

    def on_connect(self, ws, response):
        self.execution.connected = True
        logging.info("WebSocket connected. Subscribing to instruments.")
        self.kite_ticker.subscribe(self.instrument_tokens)

    def on_close(self, ws, code, reason):
        self.execution.connected = False
        logging.info(f"WebSocket closed. {code} with reason {reason}")
    def on_error(self, ws, code, reason):
        logging.error(f"WebSocket encountered an error: Code {code}, Reason: {reason}.")
        # Handle the error and attempt to reconnect if necessary
        self.reconnect_websocket()

    def on_noreconnect(self, ws):
        logging.error("WebSocket reconnection failed permanently.")
        # You can implement notification or escalation here if needed

    def on_reconnect(self, ws, attempt_count):
        logging.info(f"WebSocket is attempting to reconnect. Attempt {attempt_count}.")

    def reconnect_websocket(self):
        """ Close existing connection and attempt reconnection. """
        try:
            logging.info("Attempting to reconnect WebSocket...")
            self.kite_ticker.close()  # Close the existing connection
            time.sleep(2)
            self.kite_ticker.connect()  # Reconnect
        except Exception as e:
            logging.error(f"Error while reconnecting WebSocket: {e}")

    def on_ticks(self, ws, ticks):
        """Mark positions with live prices, then evaluate the existing candle strategy."""
        now = datetime.datetime.now(ZoneInfo('Asia/Kolkata'))
        eligible = []
        for tick in ticks:
            item = next((i for i in self.instruments
                         if int(i['instrument_token']) == tick.get('instrument_token')), None)
            if item is None or now.hour < 9 or now.hour >= 23:
                continue
            exchange = item['instrument_details']['exchange']
            if exchange in ('NFO', 'NSE', 'BSE') and (now.hour >= 16 or
                    (now.hour == 9 and now.minute < 15)):
                continue
            try:
                price = float(tick['last_price'])
                if not math.isfinite(price) or price <= 0:
                    continue
                self.execution.mark(item, price)
                eligible.append((item, tick, price))
            except (TypeError, ValueError, KeyError):
                logging.exception('Invalid tick')
        for item, tick, price in eligible:
            a = self.candle_aggregators[str(item['instrument_token'])]
            try:
                tick['current_datetime'] = now
                a.process_tick(tick)
                self.execution.process(a, item, price)
            except Exception:
                logging.exception('Tick processing failed for %s', a.tradingsymbol)

    def stop_websocket(self):
        """Stop the WebSocket and handle cleanup, with logging."""
        self.execution.stop()
        try:
            # Set up logging with a FileHandler
            logger = logging.getLogger("stop_websocket")
            logger.setLevel(logging.INFO)

            # Avoid duplicate handlers
            if not logger.handlers:
                file_handler = logging.FileHandler("stop_websocket_run_script_.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

            logger.info("Attempting to stop the WebSocket.")

            # Check if the WebSocket is already stopped
            if not self.websocket_running:
                logger.warning("WebSocket stop called, but it was not running.")
                return

            # Perform unsubscription
            self.kite_ticker.unsubscribe(self.instrument_tokens)
            logger.info(f"Unsubscribed from tokens: {self.instrument_tokens}")
            # Close the WebSocket connection
            self.kite_ticker.close(1000, "No More Trade Required")
            logger.info("WebSocket closed with code 1000 and reason 'No More Trade Required'.")

            # Update the running state
            self.websocket_running = False
            logger.info("WebSocket stopped successfully.")
        except Exception as error:
            # Log any exception that occurs during the stop process
            logger.error(f"Failed to stop WebSocket: {error}")

        #self.kite_ticker.
    def is_running(self):
        """ Start the WebSocket and listen for ticks, with connection checks and retries. """
        # Connect to the WebSocket initially
        return self.websocket_running

    def run_websocket(self):
        """ Start the WebSocket and listen for ticks, with connection checks and retries. """
        # Connect to the WebSocket initially
        self.execution.start()
        self.kite_ticker.connect(threaded=True)

        # Backoff parameters
        backoff_time = 5  # Initial backoff time in seconds
        max_backoff_time = 60  # Maximum backoff time in seconds
        is_reconnecting = False


    def restructure_for_combined_threshold(self, instruments_data=[]):
        """
        Groups instruments by their 'exit_trades_threshold_points' values.

        Parameters:
        - instruments_data (list): A list of dictionaries containing instrument details.

        Returns:
        - dict: A dictionary grouping instruments by 'exit_trades_threshold_points'.
        """
        try:
            grouped_data = defaultdict(list)
            
            for instrument in instruments_data:
                exit_threshold = instrument.get('exit_trades_threshold_points')
                if exit_threshold is None:
                    continue  # Skip invalid entries with missing 'exit_trades_threshold_points'
                
                grouped_data[threshold_key(exit_threshold)].append({
                    "instrument_token": instrument.get('instrument_token'),
                    "tradingsymbol": instrument.get('instrument_details', {}).get('tradingsymbol'),
                    "exit_trades_threshold_points": exit_threshold,
                    "lot_size":instrument.get('lot_size')
                })
            
            return dict(grouped_data)  # Convert defaultdict to regular dict for output consistency
        
        except (KeyError, TypeError) as error:
            # Log the error for better debugging
            print(f"Error restructuring data: {error}")
            return {}
