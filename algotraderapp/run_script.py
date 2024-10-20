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
    def __init__(self, instrument_token, interval_minutes=15, file_path='15_minute_candles.json'):
        self.instrument_token = instrument_token  # Add the instrument token
        self.interval_minutes = interval_minutes
        self.current_candle = None
        self.candles = []
        self.file_path = file_path
        
        #attributes for order management
        self.current_stop_loss = None
        self.current_order_type = None
        self.order_active = False  # Track if an order is active
        
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
                # Update the current order type and stop loss
                self.current_order_type = 'Buy'
                self.current_stop_loss = stop_loss

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
                # Update the current order type and stop loss
                self.current_order_type = 'Sell'
                self.current_stop_loss = stop_loss

            return order_id

        except Exception as e:
            logging.error(f"Error placing order: {str(e)}")
            return None
        
    def handle_reverse_order(self, instrument_token, strategy_response, lot_size, percentage):
        """
        Handles reverse order logic when stop-loss is hit.
        """
        # Check if stop-loss is hit
        stop_loss_price = strategy_response['stop_loss']
        
        # Get the latest tick data to compare the stop-loss price
        current_price = self.current_candle['close']
        
        if (strategy_response['order_type'] == 'Buy' and current_price <= stop_loss_price) or \
        (strategy_response['order_type'] == 'Sell' and current_price >= stop_loss_price):
            
            # Stop-loss hit, place reverse order
            reverse_order_type = 'Sell' if strategy_response['order_type'] == 'Buy' else 'Buy'
            
            # Place the reverse order at the stop-loss price
            reverse_order_id = self.place_order(
                self.kite,
                instrument_token,
                reverse_order_type,
                lot_size,
                stop_loss_price  # Using stop-loss price as the price for the reverse order
            )
            
            if reverse_order_id:
                logging.info(f"Reverse order placed: {reverse_order_id} for {reverse_order_type} {instrument_token}")
                
                # Calculate new stop-loss for the reverse order
                new_stop_loss = self.calculate_stop_loss(reverse_order_type, percentage)
                
                # Update the current stop loss in the object for the new reverse order
                self.current_stop_loss = new_stop_loss  # Update the stored stop loss
                
                # Update the trailing stop-loss for this reverse order
                self.update_trailing_stop_loss(self.kite, percentage)
                logging.info(f"New trailing stop loss for {reverse_order_type} order of {instrument_token} set to {new_stop_loss}")




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

                # Update the current stop loss for the instrument
                self.current_stop_loss = new_stop_loss  # Store the new stop loss in the object

            elif order_type == "SELL":
                new_stop_loss = math.ceil(x_value_higher + (percentage / 100 * x_value_higher))
                # Kite API does not allow direct modification of the order object; you'll need to update the order via the API
                kite.modify_order(
                    order_id=order['order_id'],  # Ensure to use the correct order ID
                    trigger_price=new_stop_loss
                )
                logging.info(f"Updated trailing stop loss for Sell order of {instrument_token} to {new_stop_loss}")

                # Update the current stop loss for the instrument
                self.current_stop_loss = new_stop_loss  # Store the new stop loss in the object


# WebSocket Handler Class
class WebSocketHandler:
    def __init__(self, kite, instruments=[]):
        self.kite = kite
        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        
        # Store instrument details
        self.instruments = instruments
        self.instrument_tokens = [x['instrument_token'] for x in instruments]
        
        # Create a CandleAggregator instance for each instrument
        self.candle_aggregators = {x['instrument_token']: CandleAggregator() for x in instruments}

        # Define on_ticks method
        self.kite_ticker.on_ticks = self.on_ticks
        self.kite_ticker.on_connect = self.on_connect
        self.kite_ticker.on_close = self.on_close

    def on_connect(self, ws, response):
        logging.info("WebSocket connected. Subscribing to instruments.")
        self.kite_ticker.subscribe(self.instrument_tokens)

    def on_close(self, ws, code, reason):
        logging.info("WebSocket closed.")

    def on_ticks(self, ws, ticks):
        # Process each tick and store candles
        for tick in ticks:
            instrument_token = tick['instrument_token']

            # Get instrument-specific data
            instrument_data = next((x for x in self.instruments if x['instrument_token'] == instrument_token), None)
            if instrument_data is None:
                logging.error(f"Instrument data not found for token: {instrument_token}")
                continue

            lot_size = int(instrument_data['lot_size'])
            percentage = float(instrument_data['trade_calculation_percentage'])

            # Process the tick using the respective CandleAggregator for the instrument
            candle_aggregator = self.candle_aggregators[instrument_token]
            candle_aggregator.process_tick(tick)

            # Update trailing stop loss based on the latest tick
            new_stop_loss = candle_aggregator.update_trailing_stop_loss(self.kite, percentage)

            # Check if the current price hits the stored stop loss
            current_price = candle_aggregator.current_candle['close']
            if (candle_aggregator.order_active and
                    ((candle_aggregator.order_type == 'Buy' and current_price <= candle_aggregator.current_stop_loss) or
                    (candle_aggregator.order_type == 'Sell' and current_price >= candle_aggregator.current_stop_loss))):
                
                # Stop-loss hit, handle reverse order
                logging.info(f"Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss}")
                candle_aggregator.handle_reverse_order(instrument_token, {'order_type': candle_aggregator.order_type, 'stop_loss': candle_aggregator.current_stop_loss}, lot_size, percentage)

                # Keep the current stop loss and order type to use in reverse order handling
                # Optionally, deactivate the order if needed for logic clarity
                candle_aggregator.order_active = False  # Mark order as inactive to prevent new orders until a fresh signal

            # Check strategy based on the candle data and the specific percentage
            strategy_response = candle_aggregator.check_strategy(instrument_token, percentage)
            if strategy_response and not candle_aggregator.order_active:
                # Use the instrument's lot_size for the order quantity
                order_id = candle_aggregator.place_order(
                    self.kite,
                    instrument_token,
                    strategy_response['order_type'],
                    lot_size,  # Quantity based on the lot size
                    strategy_response['stop_loss']
                )
                if order_id:
                    logging.info(f"Order placed: {order_id} for {strategy_response['order_type']} {instrument_token}")

                    # Mark the order as active and store the current stop loss and order type
                    candle_aggregator.order_active = True
                    candle_aggregator.current_stop_loss = strategy_response['stop_loss']
                    candle_aggregator.order_type = strategy_response['order_type']

                    # Update trailing stop loss immediately after placing the order
                    candle_aggregator.update_trailing_stop_loss(self.kite, percentage)



    def run_websocket(self):
        """ Start the WebSocket and listen for ticks. """
        while True:
            try:
                self.kite_ticker.connect(threaded=True)
                break
            except Exception as e:
                logging.error(f"Error connecting WebSocket: {e}")
                time.sleep(5)  # Retry connection every 5 seconds if it fails

