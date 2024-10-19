import os
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

# Initialize Redis client using Django settings
redis_client = redis.StrictRedis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB
)

logging.basicConfig(level=logging.DEBUG)

# Candle Aggregator Class
class CandleAggregator:
    def __init__(self, interval_minutes=15, file_path='15_minute_candles.json'):
        self.interval_minutes = interval_minutes
        self.current_candle = None
        self.candles = []
        self.file_path = file_path

        # Load previous candles from the file, if available
        if os.path.exists(file_path):
            with open(file_path, 'r') as file:
                try:
                    self.candles = json.load(file)
                except json.JSONDecodeError:
                    self.candles = []
        else:
            self.candles = []

    def save_candles(self):
        """ Save candles to the JSON file. """
        with open(self.file_path, 'w') as file:
            json.dump(self.candles, file, indent=4)

    def process_tick(self, tick):
        """ Process a new tick and update the candle data. """
        last_price = tick['last_price']
        tick_time = datetime.datetime.strptime(tick['current_datetime'], '%Y-%m-%d %H:%M:%S')

        # Round the time to the nearest candle start time
        candle_start_time = tick_time.replace(minute=(tick_time.minute // self.interval_minutes) * self.interval_minutes, second=0, microsecond=0)

        if self.current_candle is None or candle_start_time != self.current_candle['start_time']:
            # If this is a new candle, store the previous one and start a new one
            if self.current_candle is not None:
                self.candles.append(self.current_candle)
                self.save_candles()  # Save candles after every 15-minute interval

            # Start a new candle
            self.current_candle = {
                'start_time': candle_start_time.strftime('%Y-%m-%d %H:%M:%S'),
                'open': last_price,
                'high': last_price,
                'low': last_price,
                'close': last_price,
                'volume': tick['last_traded_quantity']
            }
        else:
            # Update the current candle's OHLC values and volume
            self.current_candle['high'] = max(self.current_candle['high'], last_price)
            self.current_candle['low'] = min(self.current_candle['low'], last_price)
            self.current_candle['close'] = last_price
            self.current_candle['volume'] += tick['last_traded_quantity']

    def check_strategy(self, instrument_token, percentage):
        """ Check the strategy based on the previous two candles and the percentage for buy/sell signals. """
        if len(self.candles) < 2:
            return None  # Not enough candles to make a decision

        prev_candle_1 = self.candles[-1]  # Most recent completed candle
        prev_candle_2 = self.candles[-2]  # The candle before the most recent one

        # Calculate the high and low for the strategy
        max_high = max(prev_candle_1['high'], prev_candle_2['high'])
        min_low = min(prev_candle_1['low'], prev_candle_2['low'])

        # Calculate x_value_higher and x_value_lower using the user-defined percentage
        self.x_value_higher = math.ceil(max_high + (percentage / 100 * max_high))
        self.x_value_lower = math.floor(min_low - (percentage / 100 * min_low))

        # Get the current candle's high and low values
        current_high = self.current_candle['high']
        current_low = self.current_candle['low']

        # Initialize response data
        response = {}

        # Check for Buy or Sell signals and calculate stop loss
        if current_high > self.x_value_higher:
            stop_loss = self.calculate_stop_loss("Buy", percentage)
            response = {
                "instrument_token": instrument_token,
                "order_type": "Buy",
                "stop_loss": stop_loss
            }
        elif current_low < self.x_value_lower:
            stop_loss = self.calculate_stop_loss("Sell", percentage)
            response = {
                "instrument_token": instrument_token,
                "order_type": "Sell",
                "stop_loss": stop_loss
            }

        return response

    def calculate_stop_loss(self, order_type, percentage):
        """ Calculate the stop loss for the current order based on previous candles. """
        prev_candle_1 = self.candles[-1]
        prev_candle_2 = self.candles[-2]

        # Determine floor and ceiling values from previous candles
        floor_value = min(prev_candle_1['low'], prev_candle_2['low'])  # Minimum low for Buy
        ceil_value = max(prev_candle_1['high'], prev_candle_2['high'])  # Maximum high for Sell

        # Calculate stop loss based on the order type
        if order_type == "Buy":
            stop_loss = math.floor(floor_value - (percentage / 100 * floor_value))
        elif order_type == "Sell":
            stop_loss = math.ceil(ceil_value + (percentage / 100 * ceil_value))
        else:
            stop_loss = None

        return stop_loss

    def place_order(self, kite, instrument_token, order_type, quantity, stop_loss, price=None):
        try:
            # Check for existing orders
            existing_orders = kite.orders()
            for order in existing_orders:
                if order['tradingsymbol'] == instrument_token and order['status'] in ['OPEN', 'REOPEN']:
                    logging.info(f"An order already exists for {instrument_token}. Not placing a new order.")
                    return  # Exit if an order is already placed

            # If no existing order, proceed to place a new one
            if order_type == "Buy":
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=instrument_token,
                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    order_type=kite.ORDER_TYPE_MARKET,  # or LIMIT if you specify a price
                    product=kite.PRODUCT_MIS,  # or CNC for delivery
                    trigger_price=stop_loss  # This is the stop-loss for Buy
                )
            elif order_type == "Sell":
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=instrument_token,
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=quantity,
                    order_type=kite.ORDER_TYPE_MARKET,  # or LIMIT if you specify a price
                    product=kite.PRODUCT_MIS,
                    trigger_price=stop_loss  # This is the stop-loss for Sell
                )
            return order_id
        except Exception as e:
            logging.error(f"Error placing order: {str(e)}")
            return None

    def update_trailing_stop_loss(self, kite, percentage):
        """ Update trailing stop loss for open orders based on the latest candle values. """
        if len(self.candles) < 2:
            return  # Not enough candles to calculate trailing stop loss

        prev_candle_1 = self.candles[-1]
        prev_candle_2 = self.candles[-2]

        x_value_higher = max(prev_candle_1['high'], prev_candle_2['high'])
        x_value_lower = min(prev_candle_1['low'], prev_candle_2['low'])

        # Fetch open orders from Kite
        open_orders = kite.orders()

        for order in open_orders:
            if order['status'] != 'OPEN':
                continue  # Skip closed orders

            instrument_token = order['tradingsymbol']
            order_type = order['transaction_type']  # Ensure you get the right order type

            # Calculate new trailing stop loss
            if order_type == "BUY":
                new_stop_loss = math.floor(x_value_lower - (percentage / 100 * x_value_lower))
                # Kite API does not allow direct modification of the order object; you'll need to update the order via the API
                kite.modify_order(
                    order_id=order['order_id'],  # Ensure to use the correct order ID
                    trigger_price=new_stop_loss
                )
                logging.info(f"Updated trailing stop loss for Buy order of {instrument_token} to {new_stop_loss}")

            elif order_type == "SELL":
                new_stop_loss = math.ceil(x_value_higher + (percentage / 100 * x_value_higher))
                # Kite API does not allow direct modification of the order object; you'll need to update the order via the API
                kite.modify_order(
                    order_id=order['order_id'],  # Ensure to use the correct order ID
                    trigger_price=new_stop_loss
                )
                logging.info(f"Updated trailing stop loss for Sell order of {instrument_token} to {new_stop_loss}")

# WebSocket Handler Class
class WebSocketHandler:
    def __init__(self, kite, instruments):
        self.kite = kite
        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        self.instruments = instruments
        self.candle_aggregator = CandleAggregator()
        self.quantity = 1  # Define your quantity for orders

        # Define on_ticks method
        self.kite_ticker.on_ticks = self.on_ticks
        self.kite_ticker.on_connect = self.on_connect
        self.kite_ticker.on_close = self.on_close

    def on_connect(self, ws, response):
        logging.info("WebSocket connected. Subscribing to instruments.")
        self.kite_ticker.subscribe(self.instruments)

    def on_close(self, ws, code, reason):
        logging.info("WebSocket closed.")

    def on_ticks(self, ws, ticks):
        # Process each tick and store candles
        for tick in ticks:
            self.candle_aggregator.process_tick(tick)

            # Fetch percentage from instrument details
            instrument_token = tick['tradingsymbol']
            percentage = self.instruments[instrument_token]['trade_calculation_percentage']

            # Check strategy for each instrument
            strategy_response = self.candle_aggregator.check_strategy(instrument_token, percentage)
            if strategy_response:
                order_id = self.candle_aggregator.place_order(
                    self.kite,
                    instrument_token,
                    strategy_response['order_type'],
                    self.quantity,
                    strategy_response['stop_loss']
                )
                if order_id:
                    logging.info(f"Order placed: {order_id} for {strategy_response['order_type']} {instrument_token}")

    def run_websocket(self):
        """ Start the WebSocket and listen for ticks. """
        while True:
            try:
                self.kite_ticker.connect(threaded=True)
                break
            except Exception as e:
                logging.error(f"Error connecting WebSocket: {e}")
                time.sleep(5)  # Wait before retrying


