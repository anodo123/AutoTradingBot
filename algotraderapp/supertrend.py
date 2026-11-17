"""Closed-bar Supertrend using Wilder ATR and TradingView band rules."""
import math


def validate_config(item):
    result = dict(item)
    for field, default in [('supertrend_length', 7), ('timeframe', 15), ('lot_size', 1)]:
        value = float(item.get(field, default))
        if not math.isfinite(value) or not value.is_integer() or value <= 0:
            raise ValueError(f'{field} must be a positive integer')
        result[field] = int(value)
    for field, default in [('supertrend_factor', 2), ('exit_trades_threshold_points', None)]:
        value = float(item.get(field, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{field} must be positive and finite')
        result[field] = value
    result['trade_side'] = str(item.get('trade_side', 'BOTH')).upper()
    if result['trade_side'] not in ('BUY', 'SELL', 'BOTH'):
        raise ValueError('trade_side must be BUY, SELL or BOTH')
    return result


class Supertrend:
    def __init__(self, length=7, factor=2):
        self.length, self.factor = length, factor
        self.previous_close = None
        self.atr = None
        self.seed = []
        self.upper = self.lower = None
        self.colour = None

    def update(self, candle):
        high, low, close = (float(candle[k]) for k in ('high', 'low', 'close'))
        tr = high - low if self.previous_close is None else max(
            high - low, abs(high - self.previous_close), abs(low - self.previous_close))
        if self.atr is None:
            self.seed.append(tr)
            if len(self.seed) < self.length:
                self.previous_close = close
                return None
            self.atr = sum(self.seed) / self.length
        else:
            self.atr = (self.atr * (self.length - 1) + tr) / self.length
        upper = (high + low) / 2 + self.factor * self.atr
        lower = (high + low) / 2 - self.factor * self.atr
        if self.upper is None:
            self.colour = 'RED'
        else:
            upper = upper if upper < self.upper or self.previous_close > self.upper else self.upper
            lower = lower if lower > self.lower or self.previous_close < self.lower else self.lower
            if self.colour == 'RED':
                self.colour = 'GREEN' if close > upper else 'RED'
            else:
                self.colour = 'RED' if close < lower else 'GREEN'
        self.upper, self.lower, self.previous_close = upper, lower, close
        return {'colour': self.colour, 'value': lower if self.colour == 'GREEN' else upper}
