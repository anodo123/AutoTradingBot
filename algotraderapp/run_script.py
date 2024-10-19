import os
import threading
import json
import logging
from django.http import JsonResponse
from kiteconnect import KiteConnect, KiteTicker
import time
import datetime
from .product_setting import REDIS_HOST,REDIS_PORT,REDIS_DB
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
    def check_strategy(self,instrument_token, percentage):
        """ Check the strategy based on the previous two candles and the percentage for buy/sell signals. """
        prev_candle_1 = self.candles[-1]  # Most recent completed candle
        prev_candle_2 = self.candles[-2]  # The candle before the most recent one
        
        # Calculate the high and low for the strategy
        max_high = max(prev_candle_1['high'], prev_candle_2['high'])
        min_low = min(prev_candle_1['low'], prev_candle_2['low'])
        
        # Calculate x_value_higher and x_value_lower using the user-defined percentage
        self.x_value_higher = math.ceil(max_high + (percentage//100 * max_high))
        self.x_value_lower = math.floor(min_low - (percentage//100 * min_low))
        
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
        # Retrieve the most recent two candles
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
    
    def place_order(self, kite, instrument_token, order_type, quantity, stop_loss):
        """ Place an order using Kite Connect API. """
        try:
            order_params = {
                "exchange": "NSE",  # or "BSE" based on your needs
                "tradingsymbol": instrument_token,
                "transaction_type": order_type,
                "quantity": quantity,
                "order_type": "MARKET",  # You can choose "LIMIT" if needed
                "product": "MIS",  # For intraday trading
                "stop_loss": stop_loss  # Pass the exact stop loss value
            }

            # Place the order
            order_id = kite.place_order(**order_params)
            logging.info(f"Order placed successfully: {order_id}")
            return order_id
        except Exception as e:
            logging.error(f"Error placing order: {str(e)}")
            return None


    def save_candles(self):
        """ Placeholder for saving candles to a database or file. """
        pass
# WebSocket Runner with Candle Aggregation
def run_websocket(kite, access_token, tokens=[]):
    kws = KiteTicker(os.getenv("api_key"), access_token)
    tokens = [110667015]  # Your token list
    percentage = .04
    # Instantiate CandleAggregator globally
    global candle_aggregator
    candle_aggregator = CandleAggregator(interval_minutes=15, file_path="15_minute_candles.json")

    def on_tick(ws, ticks):
        # Add current timestamp to the tick data
        ticks[0]['current_datetime'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Store tick data in Redis based on instrument token
        instrument_token = ticks[0]['instrument_token']
        redis_key = f"tick:{instrument_token}"
        print("ticks",ticks)
        # Store the tick data in Redis
        redis_client.lpush(redis_key, json.dumps(ticks[0]))
        # Process the tick data for candlestick generation
        candle_aggregator.process_tick(ticks[0])
        signal = candle_aggregator.check_strategy(instrument_token, percentage)
        # Print ticks (for debugging purposes)
        # print(ticks)
    
    

    def on_connect(ws, response):
        logging.info("Successfully connected to WebSocket")
        for token in tokens:
            logging.info("Subscribing to: {}".format(token))
            kws.subscribe([token])
            kws.set_mode(kws.MODE_QUOTE, [token])

    def on_close(ws, code, reason):
        logging.info("WebSocket connection closed",reason)

    def on_error(ws, code, reason):
        logging.error("Connection error: {code} - {reason}".format(code=code, reason=reason))

    kws.on_ticks = on_tick
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error

    kws.connect(threaded=True)

    while True:
        if kws.is_connected():
            logging.info("WebSocket is connected and running.")
        else:
            logging.info("WebSocket is not connected, attempting to reconnect...")
        time.sleep(5)

