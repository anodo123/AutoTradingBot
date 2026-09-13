"""Shared first-loss targets, derived from existing broker order snapshots."""
import json
import logging
import threading
from decimal import Decimal, InvalidOperation


def key(value):
    return str(Decimal(str(value)).normalize())


def fallback(item):
    value = item.get('first_loss_exit_threshold_points')
    if value in (None, ''):
        return None
    try:
        result = Decimal(str(value))
        initial = Decimal(str(item['exit_trades_threshold_points']))
        if not result.is_finite() or not initial.is_finite() or not 0 < result < initial:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, KeyError):
        raise ValueError('first_loss_exit_threshold_points must be positive and below exit_trades_threshold_points')


class ThresholdGroup:
    def __init__(self, initial, reduced, members):
        self.initial = Decimal(str(initial))
        self.active = self.initial
        self.reduced = reduced
        self.members = members
        self.switched = False
        self.lock = threading.RLock()

    def observe(self, orders):
        """Replay cumulative fills; a loss is eligible only when exposure returns to zero."""
        with self.lock:
            if self.switched or self.reduced is None:
                return
            for exchange, symbol in self.members:
                position = 0
                cash = Decimal(0)
                entry_quantity = 0
                seen = set()
                selected = sorted((o for o in orders if o.get('tradingsymbol') == symbol and
                    o.get('exchange') == exchange), key=lambda o: (str(o.get('order_timestamp', '')), str(o.get('order_id', ''))))
                for order in selected:
                    order_id = order.get('order_id')
                    if order_id in seen:
                        continue
                    seen.add(order_id)
                    quantity = int(order.get('filled_quantity', order.get('quantity', 0) if order.get('status') == 'COMPLETE' else 0))
                    average = Decimal(str(order.get('average_price') or 0))
                    side = order.get('transaction_type')
                    if quantity <= 0 or not average.is_finite() or average <= 0 or side not in ('BUY', 'SELL'):
                        continue
                    sign = 1 if side == 'BUY' else -1
                    # Split an over-closing fill into closure and a new position.
                    while quantity:
                        opening = position == 0 or position * sign > 0
                        part = quantity if opening else min(quantity, abs(position))
                        if opening:
                            entry_quantity += part
                        cash -= sign * part * average
                        position += sign * part
                        quantity -= part
                        if position == 0:
                            if cash < 0:
                                self.active = self.reduced
                                self.switched = True
                                logging.info(json.dumps(dict(event='first_loss_threshold_switch',
                                    symbol=symbol, exchange=exchange, closing_order_id=order_id,
                                    original_threshold=self.initial, active_threshold=self.active,
                                    loss_points=cash / entry_quantity,
                                    reason='Confirmed filled orders fully closed a losing position'), default=str))
                                return
                            cash = Decimal(0)
                            entry_quantity = 0


def build_groups(instruments):
    groups = {}
    for item in instruments:
        group_key = key(item['exit_trades_threshold_points'])
        reduced = fallback(item)
        if group_key not in groups:
            groups[group_key] = ThresholdGroup(group_key, reduced, [])
        group = groups[group_key]
        if group.reduced != reduced:
            raise ValueError('Instruments sharing a profit threshold must use the same first_loss_exit_threshold_points')
        details = item['instrument_details']
        group.members.append((details['exchange'], details['tradingsymbol']))
    return groups
