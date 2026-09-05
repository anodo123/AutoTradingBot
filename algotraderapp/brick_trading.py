"""Completed-brick execution with confirmed fills and a JSON audit trail."""
import json
import logging
import threading
import time
from decimal import Decimal
from pathlib import Path


def validate_instrument(item):
    quantity = Decimal(str(item.get('lot_size', '')))
    size = Decimal(str(item.get('brick_size', 5)))
    if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError('lot_size must be a positive integer quantity')
    if not size.is_finite() or size <= 0:
        raise ValueError('brick_size must be finite and greater than zero')
    if item.get('trade_side', 'BOTH') not in ('BUY', 'SELL', 'BOTH'):
        raise ValueError('trade_side must be BUY, SELL or BOTH')
    if not str(item.get('instrument_token', '')).isdigit():
        raise ValueError('instrument_token must be numeric')
    details = item.get('instrument_details', {})
    if not details.get('tradingsymbol') or not details.get('exchange'):
        raise ValueError('Instrument symbol and exchange are required')


def setup_run_logging(directory):
    """Reset only bot-owned logs, once per explicitly started run."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, 'brick_bot_handler', False):
            root.removeHandler(handler)
            handler.close()
    directory = Path(directory)
    logs = directory / 'bot_logs'
    logs.mkdir(exist_ok=True)
    for path in logs.glob('*.log*'):
        if path.is_file():
            path.unlink()
    # Legacy bot outputs; never delete arbitrary user .txt/.log files.
    for pattern in ('ticks.txt', 'last_price_log.txt', 'strategy_*_log.txt',
                    'calculate_stop_loss_func.txt', 'order_placement.log',
                    'reverse_order.log', 'reverse_logic entered.log',
                    'calculate_total_profit_loss_per_share.log*', 'close_trade_logger.log',
                    're_entry.log*', 'stop_web_socket.log', 'stop_websocket_run_script_.log'):
        for path in directory.glob(pattern):
            if path.is_file():
                path.unlink()
    handler = logging.FileHandler(logs / 'session.log', encoding='utf-8')
    handler.brick_bot_handler = True
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class BrickTrader:
    def __init__(self, kite, instrument):
        validate_instrument(instrument)
        self.kite = kite
        self.token = str(instrument['instrument_token'])
        self.details = instrument['instrument_details']
        self.quantity = int(Decimal(str(instrument['lot_size'])))
        self.side = instrument.get('trade_side', 'BOTH')
        self.position = 0
        self.target = 0
        self.pending = None
        self.halted = False
        self.sequence = 0
        self.brick = None
        self.last_poll = 0
        self.lock = threading.RLock()

    def audit(self, event, **data):
        logging.info(json.dumps(dict(event=event, instrument_token=self.token,
            trade_side=self.side, position=self.position, target=self.target,
            brick=self.brick, **data), default=str))

    def on_brick(self, brick):
        with self.lock:
            if brick['sequence'] <= self.sequence:
                return
            self.sequence = brick['sequence']
            self.brick = brick
            direction = 'BUY' if brick['direction'] == 'green' else 'SELL'
            self.target = (self.quantity if direction == 'BUY' else -self.quantity) if self.side in (direction, 'BOTH') else 0
            self.audit('decision', reason='Completed brick determines permitted position', direction=direction)
            self._drive()

    def _drive(self):
        if self.halted or self.pending:
            self.audit('hold', reason='Execution halted or order awaiting confirmation')
            return
        if self.position == self.target or (self.position and self.target and self.position * self.target > 0):
            self.audit('hold', reason='Same direction; no additional entry')
            return
        # Always flatten first. Opposite entry is sent only after confirmed exit.
        delta = -self.position if self.position else self.target
        payload = dict(variety='regular', exchange=self.details['exchange'],
            tradingsymbol=self.details['tradingsymbol'], transaction_type='BUY' if delta > 0 else 'SELL',
            quantity=abs(delta), order_type='MARKET', product='MIS', validity='DAY', market_protection=10)
        self.pending = dict(order_id=None, sign=1 if delta > 0 else -1, filled=0, quantity=abs(delta))
        self.audit('order_request', action='EXIT' if self.position else 'ENTRY', payload=payload)
        try:
            order_id = self.kite.place_order(**payload)
            if not order_id:
                raise ValueError('Missing order ID')
            self.pending['order_id'] = str(order_id)
            self.audit('order_response', order_id=order_id)
        except Exception as error:
            # A timeout may have reached the broker: never blindly submit twice.
            self.halted = True
            self.audit('execution_halted', reason='Placement outcome uncertain; reconcile with broker', error=str(error))

    def on_order_update(self, update):
        with self.lock:
            if not self.pending or str(update.get('order_id')) != self.pending['order_id']:
                return
            self.audit('order_update', response=update)
            filled = int(update.get('filled_quantity', 0))
            if filled < self.pending['filled']:
                return
            self.position += self.pending['sign'] * (filled - self.pending['filled'])
            self.pending['filled'] = filled
            status = update.get('status')
            if status in ('COMPLETE', 'REJECTED', 'CANCELLED'):
                complete = status == 'COMPLETE' and filled == self.pending['quantity']
                self.pending = None
                self.audit('position_update', status=status)
                if not complete:
                    self.halted = True
                    self.audit('execution_halted', reason='Rejected, cancelled or incomplete order; manual reconciliation required')
                else:
                    self._drive()

    def poll(self):
        with self.lock:
            if not self.pending or not self.pending['order_id'] or time.monotonic() - self.last_poll < 1:
                return
            self.last_poll = time.monotonic()
            order_id = self.pending['order_id']
            try:
                self.audit('order_history_request', order_id=order_id)
                history = self.kite.order_history(order_id)
                self.audit('order_history_response', response=history)
                if history:
                    self.on_order_update(history[-1])
            except Exception as error:
                self.audit('order_history_error', error=str(error))
