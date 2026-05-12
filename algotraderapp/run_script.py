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
import asyncio
import sys
from zoneinfo import ZoneInfo
from collections import defaultdict
import json
import math
import logging
from logging.handlers import RotatingFileHandler
import shutil
# Initialize Redis client using Django settings
# redis_client = redis.StrictRedis(
#     host=REDIS_HOST,
#     port=REDIS_PORT,
#     db=REDIS_DB
# )

logging.basicConfig(level=logging.DEBUG)


class CandleAggregator:
    def __init__(self, instrument_token,tradingsymbol ,interval_minutes=15 ,file_path='minute_candles.json',trade_side="BOTH",instrument_details_dict = []):
        self.file_path = str(instrument_token)+'_'+str(interval_minutes) + '_' + file_path
        self.instrument_token = instrument_token  # Add the instrument token
        self.tradingsymbol = tradingsymbol  # Add the instrument token
        self.interval_minutes = interval_minutes
        self.current_candle = None
        self.candles = []  # This can remain as a list if needed elsewhere
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
        self.alert_candle = None
        self.buy_alert_candle = None
        self.sell_alert_candle = None
        self.previous_order_type = None
        self.last_second_alert_candle = None
        self.per_trade_candle_based_profit = 1
        self.order_id = None
        self.just_closed_trade = False
        self.keep_check_strategy = True
        self.last_used_vwap_candle = None
        self.cached_vwap = 0
        self.last_calculated_vwap = 0
        self.trailing_stop_loss_json_data = None
        self.length_of_candles_at_small_exit = None
        self.per_trade_exit_candle_start_time = None
        # Load previous candles from the file, if available
        if os.path.exists(self.file_path):
            with open(self.file_path, 'r') as file:
                try:
                    self.candles = json.load(file)
                except json.JSONDecodeError:
                    self.candles = []
        else:
            self.candles = []

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
        
    def reset_candles(self):
        try:
            if os.path.exists(self.file_path):
                # Split the filename and extension
                base, ext = os.path.splitext(self.file_path)

                # Construct backup filename
                timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_path = f"{base}_backup_{timestamp}{ext}"

                # Copy the file to the backup location
                shutil.copy2(self.file_path, backup_path)
                logging.info(f"Backup of candle file saved at {backup_path}.")

                # Delete the original file
                os.remove(self.file_path)
                logging.info(f"Candle file at {self.file_path} has been deleted.")
            else:
                logging.warning(f"No candle file found at {self.file_path} to delete.")
        except Exception as error:
            logging.error(f"Error while resetting candles: {error}")

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
                    'vwap_upto_n_minus1': 0,
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
                        self.current_candle['vwap_upto_n_minus1'] = self.get_vwap_upto_n_current_candle(self.candles)
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
                        'vwap_upto_n_minus1': self.get_vwap_upto_n_current_candle(self.candles),
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
                    self.current_candle['vwap_upto_n_minus1'] = self.get_vwap_upto_n_current_candle(self.candles)

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
        try:
            # Open the log file in append mode
            with open(f'strategy_{instrument_token}_log.txt', 'a') as log_file:
                
                # Log initial info
                print(f"Checking strategy for instrument_token: {instrument_token}, percentage: {percentage}", file=log_file)

                # Check if there are enough candles
                if len(self.candles) < 2:
                    print(f"Not enough candles. Candles count: {len(self.candles)}", file=log_file)
                    return None  # Not enough candles to make a decision
                
                if self.trade_side in ["BUY", "BOTH"]:
                    self.check_alert_candle_and_its_validity_for_buy_side()
                if self.trade_side in ["SELL", "BOTH"]:
                    self.check_alert_candle_and_its_validity_for_sell_side()

                # Get previous two candles
                if self.trade_side == 'BOTH' and (not self.buy_alert_candle and not self.sell_alert_candle):
                    print(f"Buy and Sell alert candle are None. Buy Alert Candle: {self.buy_alert_candle}, Sell Alert Candle: {self.sell_alert_candle}", file=log_file)
                    return {}

                elif self.trade_side == 'BUY' and self.buy_alert_candle is None:
                    print(f"Buy alert candle is None. Buy Alert Candle: {self.buy_alert_candle}", file=log_file)
                    return {}
                elif self.trade_side == 'SELL' and self.sell_alert_candle is None:
                    print(f"Sell alert candle is None. Sell Alert Candle: {self.sell_alert_candle}", file=log_file)
                    return {}
                if self.keep_check_strategy is False:
                    return {}
                    
                
                # alert_candle_high = self.alert_candle['high']
                # alert_candle_low  = self.alert_candle['low']
                # # Calculate x_value_higher and x_value_lower using the user-defined percentage
                # self.x_value_higher = math.ceil(alert_candle_high + ((percentage / 100) * alert_candle_high))
                # self.x_value_lower = math.floor(alert_candle_low - ((percentage / 100) * alert_candle_low))

                # print(f"x_value_higher: {self.x_value_higher}, x_value_lower: {self.x_value_lower}", file=log_file)

                # Get the current candle's high and low values
                current_high = self.current_candle['close']
                current_low = self.current_candle['close']

                print(f"Current Candle High: {current_high}, Current Candle Low: {current_low}", file=log_file)

                # Initialize response data
                response = {}

                # Check for Buy or Sell signals and calculate stop loss
                if (
                    self.buy_alert_candle
                    and (self.current_order_type is None or self.current_order_type.lower() == "sell")
                    and current_high > math.ceil(
                        self.buy_alert_candle['high'] + ((percentage / 100) * self.buy_alert_candle['high'])
                    )
                ):
                    self.alert_candle = self.buy_alert_candle
                    #self.per_trade_candle_based_profit = self.alert_candle['high'] - self.alert_candle['low']
                    stop_loss = self.calculate_stop_loss_func("Buy", percentage,self.buy_alert_candle)
                    response = {
                        "instrument_token": instrument_token,
                        "order_type": "Buy",
                        "stop_loss": stop_loss
                    }
                    print(f"Buy signal generated. Stop Loss: {stop_loss}", file=log_file)
                elif (
                    self.sell_alert_candle
                    and (self.current_order_type is None or self.current_order_type.lower() == "buy")
                    and current_low < math.floor(
                        self.sell_alert_candle['low'] - ((percentage / 100) * self.sell_alert_candle['low'])
                    )
                ):
                    self.alert_candle = self.sell_alert_candle
                    #self.per_trade_candle_based_profit = self.alert_candle['high'] - self.alert_candle['low']
                    stop_loss = self.calculate_stop_loss_func("Sell", percentage,self.sell_alert_candle)
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
        except Exception as e:
            print(f"Error in check_strategy: {e}")
            return None

    def calculate_stop_loss_func(self, order_type, percentage,alert_candle):
        """ Calculate the stop loss for the current order based on previous candles. """

        # Open the log file in append mode
        with open('calculate_stop_loss_func.txt', 'a') as log_file:
            
            # Print that stop loss calculation has started
            print(f"Calculating stop loss for order_type: {order_type} with percentage: {percentage}", file=log_file)

                

            # Print previous candles information
            print(f"alert_candle Candle 1: {alert_candle}", file=log_file)

            # Determine floor and ceiling values from previous candles

            # Print floor and ceiling values
            print(f"Floor Value: {alert_candle['low']}, Ceiling Value: {alert_candle['high']}", file=log_file)

            # Calculate stop loss based on the order type
            if order_type == "Buy":
                stop_loss = math.floor(alert_candle['low'] - (percentage / 100 * alert_candle['low']))
                print(f"Calculated Buy Stop Loss: {stop_loss}", file=log_file)
            elif order_type == "Sell":
                stop_loss = math.ceil(alert_candle['high'] + (percentage / 100 * alert_candle['high']))
                print(f"Calculated Sell Stop Loss: {stop_loss}", file=log_file)
            else:
                stop_loss = None
                print(f"Unknown order type: {order_type}. Stop loss set to None.", file=log_file)

            # Return the calculated stop loss
            return stop_loss
    
    def get_logger_of_calculate_total_profit_loss_per_share(self):
        logger = logging.getLogger("calculate_total_profit_loss_per_share")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            # Create rotating file handler
            file_handler = RotatingFileHandler(
                "calculate_total_profit_loss_per_share.log", 
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=3  # Keep 3 old logs (e.g., log, log.1, log.2, log.3)
            )
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger


    def check_alert_candle_and_its_validity_for_buy_side(self):
        try:
            current_vwap = self.get_vwap_upto_n_minus_1_candles(self.candles)
            last_close = self.candles[-2]['close']
            start_time = self.candles[-2]['start_time']

            if self.buy_alert_candle is None and last_close > current_vwap:
                #self.alert_candle = self.candles[-2]
                self.buy_alert_candle = self.candles[-2]
            elif self.buy_alert_candle and last_close < current_vwap:
                self.buy_alert_candle = None
                self.alert_candle = None
                
            

            # Prepare JSON entry
            log_entry = {
                start_time: {
                    "last_close": last_close,
                    "current_vwap": current_vwap,
                    "alert_candle": self.buy_alert_candle
                }
            }

            # Read existing log if exists
            try:
                with open(f"buy_alert_log_{self.tradingsymbol}.json", "r") as f:
                    existing_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                existing_data = {}

            # Update with new entry
            existing_data.update(log_entry)

            # Write back to file
            with open(f"buy_alert_log_{self.tradingsymbol}.json", "w") as f:
                json.dump(existing_data, f, indent=4)

            return True
        except Exception as e:
            print(f"Error in check_alert_candle_and_its_validity_for_buy_side: {e}")
            return False


    def check_alert_candle_and_its_validity_for_sell_side(self):
        try:
            current_vwap = self.get_vwap_upto_n_minus_1_candles(self.candles)
            last_close = self.candles[-2]['close']
            start_time = self.candles[-2]['start_time']

            if self.sell_alert_candle is None and current_vwap > last_close:
                #self.alert_candle = self.candles[-2]
                self.sell_alert_candle = self.candles[-2]
            elif self.sell_alert_candle and current_vwap < last_close:
                self.sell_alert_candle = None
                self.alert_candle = None

            # Prepare JSON entry
            log_entry = {
                start_time: {
                    "last_close": last_close,
                    "current_vwap": current_vwap,
                    "alert_candle": self.sell_alert_candle
                }
            }

            # Read existing log if exists
            try:
                with open(f"sell_alert_log_{self.tradingsymbol}.json", "r") as f:
                    existing_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                existing_data = {}

            # Update with new entry
            existing_data.update(log_entry)

            # Write back to file
            with open(f"sell_alert_log_{self.tradingsymbol}.json", "w") as f:
                json.dump(existing_data, f, indent=4)

            return True
        except Exception as e:
            print(f"Error in check_alert_candle_and_its_validity_for_sell_side: {e}")
            return False

    
    def get_vwap_upto_n_current_candle(self,candles):
        """
        Calculate VWAP up to the given candle index.
        
        candles: List of dicts with keys: 'high', 'low', 'close', 'volume'
        index: Index of the candle up to which VWAP is calculated
        """
        try:
            cumulative_pv = 0
            cumulative_volume = 0
            if self.candles == []:
                return 0
            # Loop in reverse, excluding the most recent (last) candle
            for i in range(len(candles) - 1, -1, -1):  # Exclude last candle
                candle = candles[i]
                typical_price = (candle['high'] + candle['low'] + candle['close']) / 3
                volume = candle['volume']
                cumulative_pv += typical_price * volume
                cumulative_volume += volume

            if cumulative_volume == 0:
                return 0  # Avoid division by zero
            return round(cumulative_pv / cumulative_volume,2)
        except Exception as e:
            print(f"Error in get_vwap_upto_n_current_candle: {e}")
            return 0
    
    
    
    def get_vwap_upto_n_minus_1_candles(self,candles):
        """
        Calculate VWAP up to the given candle index.
        
        candles: List of dicts with keys: 'high', 'low', 'close', 'volume'
        index: Index of the candle up to which VWAP is calculated
        """
        try:
            cumulative_pv = 0
            cumulative_volume = 0
            if self.candles == []:
                return 0
            if len(self.candles)>1 and self.last_used_vwap_candle is not None and self.last_used_vwap_candle == candles[-2]:
                return self.last_calculated_vwap
            if len(self.candles)>1 and self.last_used_vwap_candle is None:
                self.last_used_vwap_candle = candles[-2]
            # Loop in reverse, excluding the most recent (last) candle
            for i in range(len(candles) - 2, -1, -1):  # Exclude last candle
                candle = candles[i]
                typical_price = (candle['high'] + candle['low'] + candle['close']) / 3
                volume = candle['volume']
                cumulative_pv += typical_price * volume
                cumulative_volume += volume

            if cumulative_volume == 0:
                return 0  # Avoid division by zero
            self.last_calculated_vwap = round((cumulative_pv / cumulative_volume),2)
            return self.last_calculated_vwap
        except Exception as e:
            print(f"Error in get_vwap_upto_n_minus_1_candles: {e}")
            return 0

    def place_single_order(self,kite,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, order_type, quantity, stop_loss, price=None,percentage = 0.00,order_mode="Reverse_side"):
        log_file = 'order_placement.log'
        with open(log_file, 'a') as f:  # Open log file in append mode
            try:
                # Check for existing orders
                order_id = None

                f.write(f"----------------------------------------------------------------------------------------------------------------------------\n")
                f.write(f"----------------------------------------------------------------------------------------------------------------------------\n")
                f.write(f"Attempting {order_mode } at {datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} to place order for {trading_symbol} - {order_type} {quantity} stop loss {stop_loss} price {price}.\n")
                if self.close_trade_for_the_day:
                    f.write(f" Trade Closed for Attempted {order_mode} for {trading_symbol}")
                    return 
                # If no existing order, proceed to place a new one
                if order_type == "Buy":
                    order_id = kite.place_order(
                                    variety=kite.VARIETY_REGULAR,  # Set order type to Cover Order
                                    exchange=exchange,
                                    tradingsymbol=trading_symbol,
                                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                                    quantity=quantity,
                                    order_type=kite.ORDER_TYPE_MARKET,  # Use MARKET or LIMIT based on your preference
                                    product=kite.PRODUCT_MIS,  # For intraday trading
                                    market_protection=10,
                                )

                elif order_type == "Sell":
                    order_id = kite.place_order(
                                    variety=kite.VARIETY_REGULAR,  # Set order type to Cover Order
                                    exchange=exchange,
                                    tradingsymbol=trading_symbol,
                                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                                    quantity=quantity,
                                    order_type=kite.ORDER_TYPE_MARKET,  # Use MARKET or LIMIT based on your preference
                                    product=kite.PRODUCT_MIS,  # For intraday trading
                                    market_protection=10,
                                )

                if order_id:
                    all_orders = kite.orders()
                    result = not all_orders or all_orders[-1]['status'] != 'REJECTED'
                    f.write(str(result))
                    if all_orders==[] or (all_orders!=[] and all_orders[-1]['status'] != 'REJECTED'):
                        self.current_order_type = order_type
                        self.current_stop_loss = stop_loss
                        # Update the current stop loss in the object for the new reverse order
                        self.order_active = True
                        if self.alert_candle:
                            self.per_trade_candle_based_profit = self.alert_candle['high'] - self.alert_candle['low']
                        else:
                            self.per_trade_candle_based_profit = 1
                        f.write(f"{order_type} {order_mode} order placed for {trading_symbol}. Order ID: {order_id}, Stop Loss: {self.current_stop_loss}, Quantity: {quantity}, Price: {price} alert_candle_based_profit_points_unmul: {self.per_trade_candle_based_profit}\n")
                        f.write("**********")
                        f.write(f"alert_candle: {self.alert_candle}\n")
                        # Fetch all orders
                    else:
                        self.current_order_type = None
                        self.current_stop_loss = None
                        # Update the current stop loss in the object for the new reverse order
                        self.order_active = False
                        self.per_trade_candle_based_profit = 1
                        f.write(f"{order_type} {order_mode} order NOT placed REJECTED for {trading_symbol}. Order ID: {order_id}, Stop Loss: {self.current_stop_loss}, Quantity: {quantity}, Price: {price} alert_candle_based_profit_points_unmul: {self.per_trade_candle_based_profit}\n")
                        f.write("**********")
                        #sys.exit()
                f.write(f"Order placed successfully for {trading_symbol}. Order ID: {order_id}\n")
                self.order_id = order_id
                return order_id

            except Exception as e:
                f.write(f"Error placing order for {trading_symbol}: {str(e)}\n")
                return None

    def handle_reverse_order(self, kite,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, reverse_strategy_response, lot_size, percentage,mode ="Square OFF"):
        """
        Handles reverse order logic when stop-loss is hit.
        """
        # Set up a dedicated logger for this function
        reverse_order_logger = logging.getLogger("reverse_order_logger")
        reverse_order_logger.setLevel(logging.DEBUG)

        # Create a file handler specific for reverse order handling logs
        file_handler = logging.FileHandler("reverse_order.log")
        file_handler.setLevel(logging.DEBUG)

        # Define a log format and set it for the handler
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)

        # Add the handler to the logger, avoiding duplicate handlers if the function is called multiple times
        if not reverse_order_logger.handlers:
            reverse_order_logger.addHandler(file_handler)

        #reverse_order_logger.info("Executing handle_reverse_order.")
        
        # Check if stop-loss is hit
        stop_loss_price = self.current_stop_loss
        #reverse_order_logger.debug(f"Stop-loss price fetched: {stop_loss_price}")
        
        # Get the latest tick data to compare the stop-loss price
        current_price = self.current_candle['close']
        #reverse_order_logger.debug(f"Current price from candle data: {current_price}")

        # Check stop-loss condition
        if (self.current_order_type == 'Buy' and stop_loss_price and current_price <= stop_loss_price) or \
        (self.current_order_type== 'Sell' and stop_loss_price and current_price >= stop_loss_price) or (self.current_order_type and mode == "Square Off For Reversal Order"):
            
            reverse_order_logger.info(f"Stop-loss hit for {instrument_token} at price: {current_price}")
            print(f"Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {stop_loss_price} inside reverse handling function", file=open("reverse_logic entered.log", "a"))

            # Calculate daily profit or loss before reversing the order
            reverse_order_logger.info("Calculating daily profit or loss.")
            self.fetch_and_calculate_daily_profit_loss(kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, reverse_strategy_response, lot_size, percentage)
            # Stop-loss hit, place reverse order
            reverse_order_type = "Sell" if reverse_strategy_response['order_type'] == "Buy" else "Buy"
            reverse_order_logger.info(f"Reverse order type determined as: {reverse_order_type}")
            
            # Place the reverse order at the stop-loss price for square off
            # Fetch current positions
            for position in kite.positions()['net']:
                if position['tradingsymbol'] ==  trading_symbol and position['quantity']!=0:
                    #squaringoffopenpositions
                    reverse_order_id_sq_off = self.place_single_order(
                                                        kite,
                                                        instrument_token,
                                                        trading_symbol,
                                                        exchange,
                                                        exit_trades_threshold_points,
                                                        loss_trades_threshold_points,
                                                        reverse_order_type,
                                                        lot_size,
                                                        stop_loss_price,
                                                        stop_loss_price,  # Using stop-loss price as the price for the reverse order
                                                        percentage,
                                                        order_mode=mode
                                                    )
            # for position in kite.positions()['net']:
            #     if position['tradingsymbol'] ==  trading_symbol and position['quantity']==0:
            #         #squaredoffsuccessfully
            #         self.order_active = False
            # Place the reverse order at the stop-loss price
            #if self.trade_side == "BOTH":
                #pass
                #reverse order not required for both side trades
                # reverse_order_id = self.place_single_order(
                #                                         kite,
                #                                         instrument_token,
                #                                         trading_symbol,
                #                                         exchange,
                #                                         exit_trades_threshold_points,
                #                                         reverse_order_type,
                #                                         lot_size,
                #                                         stop_loss_price,
                #                                         stop_loss_price,  # Using stop-loss price as the price for the reverse order
                #                                         percentage,
                #                                         order_mode="Reverse Mode"
                #                                     )
                
                # if reverse_order_id:
                #     reverse_order_logger.info(f"Reverse order placed with ID: {reverse_order_id} for {reverse_order_type} on {trading_symbol}")
                # else:
                #     reverse_order_logger.warning(f"Failed to place reverse order for {trading_symbol}.")
            #else:
                #if order is not both side make order inactive
            self.order_active = False
            #self.alert_candle = None
            # self.buy_alert_candle = None
            # self.sell_alert_candle = None
            self.current_order_type = None
        else:
            reverse_order_logger.debug("Stop-loss condition not met. No reverse order placed.")

    
    def fetch_and_calculate_daily_profit_loss(self,kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, strategy_response, lot_size, percentage):
        """
        Fetch orders from Kite API and calculate daily profit or loss, with extensive logging.
        """
        # Create a logger
        # fetch_and_calculate_daily_profit_loss = logging.getLogger("daily_profit_loss_calculation")
        # fetch_and_calculate_daily_profit_loss.setLevel(logging.DEBUG)

        # # Create a file handler
        # file_handler = logging.FileHandler("daily_profit_loss_calculation.log")
        # file_handler.setLevel(logging.DEBUG)

        # # Create a formatter and set it for the file handler
        # formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        # file_handler.setFormatter(formatter)

        # # Add the file handler to the logger
        # fetch_and_calculate_daily_profit_loss.addHandler(file_handler)

        # Log an info message
        #fetch_and_calculate_daily_profit_loss.info("Starting fetch_and_calculate_daily_profit_loss process.")
        
        try:
            # Fetch all orders
            all_orders = kite.orders()
            #fetch_and_calculate_daily_profit_loss.debug(f"Fetched {len(all_orders)} orders from Kite API.")

            # Filter for completed buy/sell orders
            completed_orders = [
                order for order in all_orders if order['status'] == 'COMPLETE' and
                order['transaction_type'] in ['BUY', 'SELL'] and 
                order['tradingsymbol'] == trading_symbol
            ]
            #fetch_and_calculate_daily_profit_loss.debug(f"Filtered completed buy/sell orders. Count: {len(completed_orders)}")

            # Sort orders by timestamp
            sorted_orders = sorted(completed_orders, key=lambda x: x['order_timestamp'])
            #fetch_and_calculate_daily_profit_loss.debug("Sorted orders by timestamp.")

            # Calculate daily profit or loss based on the sorted orders
            daily_profit_loss_per_share = self.calculate_total_profit_loss_per_share(sorted_orders, current_price,trading_symbol)
            #fetch_and_calculate_daily_profit_loss.info(f"Calculated daily profit/loss: {daily_profit_loss_per_share}")
            self.write_profit_loss_to_json({trading_symbol:daily_profit_loss_per_share})


            # combinedthresholdinstrumentdetails = {}
            # for single_dict in self.instrument_details_dict[str(int(exit_trades_threshold_points))]:
            #     combinedthresholdinstrumentdetails[single_dict['tradingsymbol']] = single_dict['lot_size']

            trading_symbols_list = [x['tradingsymbol'] for x in  self.instrument_details_dict[str(int(exit_trades_threshold_points))]]

            # Assign the daily profit/loss to the profit threshold points
            self.profit_threshold_points = self.fetch_profit_loss_from_json_dict(trading_symbols_list)

            #self.profit_threshold_points = 0 #assigned to zero for testing
            #fetch_and_calculate_daily_profit_loss.info(f"Updated profit threshold points for {trading_symbol} and  list {trading_symbols_list}: {self.profit_threshold_points}")
            if self.profit_threshold_points>=exit_trades_threshold_points:
                self.should_close_trade(kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, strategy_response, lot_size, percentage)
            #CLOSE ON THE BASIS OF LOSS THRESHOLD
            if self.profit_threshold_points<=(exit_trades_threshold_points + loss_trades_threshold_points):
                self.should_close_trade(kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, strategy_response, lot_size, percentage)

            # Optional console output
            print(f"Total Profit/Loss for the day: {daily_profit_loss_per_share},loss_trades_threshold_points {exit_trades_threshold_points + loss_trades_threshold_points } ,self.profit_threshold_points:{self.profit_threshold_points},exit_trades_threshold_points:{exit_trades_threshold_points}")

            #fetch_and_calculate_daily_profit_loss.info("Completed fetch_and_calculate_daily_profit_loss process successfully.")
            return daily_profit_loss_per_share
        except Exception as error:
            print("error in fetch_and_calculate_daily_profit_loss",error)
            #fetch_and_calculate_daily_profit_loss.error(f"Error in fetch_and_calculate_daily_profit_loss: {error}", exc_info=True)
            return 0
        
        
    def fetch_and_calculate_per_trade_per_instrument_profit_loss(self,kite,current_price,instrument_token, trading_symbol, exchange, per_instrument_exit_trades_threshold_points, strategy_response, lot_size, percentage,order_id):
        """
        Fetch orders from Kite API and calculate daily profit or loss, with extensive logging.
        """
        
        try:
            # Fetch all orders
            per_trade_profit_loss_per_share = 0
            all_orders = kite.orders()
            #fetch_and_calculate_daily_profit_loss.debug(f"Fetched {len(all_orders)} orders from Kite API.")

            # Filter for completed buy/sell orders
            completed_orders = [
                order for order in all_orders if order['status'] == 'COMPLETE' and
                order['transaction_type'] in ['BUY', 'SELL'] and 
                order['tradingsymbol'] == trading_symbol
            ]

            # Sort orders by timestamp
            sorted_orders = sorted(completed_orders, key=lambda x: x['order_timestamp'])
            #fetch_and_calculate_daily_profit_loss.debug("Sorted orders by timestamp.")
            if self.order_active:
                per_trade_profit_loss_per_share = self.calculate_total_profit_loss_per_instrument_per_order(sorted_orders, current_price,trading_symbol,order_id)
            # Calculate daily profit or loss based on the sorted orders
            
            
            
            threshold = per_instrument_exit_trades_threshold_points * self.per_trade_candle_based_profit

            print(
                f"[PER TRADE PER INSTRUMENT EXIT CHECK] symbol={trading_symbol} | "
                f"profit={per_trade_profit_loss_per_share} | "
                f"threshold={threshold} | "
                f"order_active={self.order_active}"
            )

            if (
                per_trade_profit_loss_per_share is not None
                and per_trade_profit_loss_per_share >= threshold
                and self.order_active
            ):
                print(
                    f"[EXIT TRIGGERED] symbol={trading_symbol} | "
                    f"profit={per_trade_profit_loss_per_share:.2f} >= threshold={threshold:.2f}"
                )

                self.exit_trade_for_the_instrument(
                    kite,
                    current_price,
                    instrument_token,
                    trading_symbol,
                    exchange,
                    per_instrument_exit_trades_threshold_points,
                    strategy_response,
                    lot_size,
                    percentage,
                    per_trade_profit_loss_per_share
                )
            #fetch_and_calculate_daily_profit_loss.info("Completed fetch_and_calculate_daily_profit_loss process successfully.")
            return per_trade_profit_loss_per_share
        except Exception as error:
            print("error in fetch_and_calculate_daily_profit_loss per_instrument_exit_trades_threshold_points",error)
            #fetch_and_calculate_daily_profit_loss.error(f"Error in fetch_and_calculate_daily_profit_loss: {error}", exc_info=True)
            return 0

    
    def calculate_total_profit_loss_per_share(self, sorted_orders, current_price, trading_symbol):
        """
        Calculate total profit or loss per share, including realized and unrealized P/L.
        """
        try:
            logger = self.get_logger_of_calculate_total_profit_loss_per_share()

            realized_profit_loss_per_share = 0
            unrealized_profit_loss_per_share = 0
            self.open_position = False
            self.open_price = None
            self.open_quantity = 0
            self.current_order_type = None

            for order in sorted_orders:
                avg_price = order['average_price']
                quantity = order['quantity']
                transaction_type = order['transaction_type']

                if transaction_type == 'BUY':
                    if not self.open_position:
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Buy"
                        self.open_position = True
                    elif self.current_order_type == "Sell":
                        realized_profit_loss_per_share += (self.open_price - avg_price)
                        if quantity == self.open_quantity:
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            self.open_quantity -= quantity

                elif transaction_type == 'SELL':
                    if not self.open_position:
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Sell"
                        self.open_position = True
                    elif self.current_order_type == "Buy":
                        realized_profit_loss_per_share += (avg_price - self.open_price)
                        if quantity == self.open_quantity:
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            self.open_quantity -= quantity

            if self.open_position:
                if self.current_order_type == "Buy":
                    unrealized_profit_loss_per_share = current_price - self.open_price
                elif self.current_order_type == "Sell":
                    unrealized_profit_loss_per_share = self.open_price - current_price

            total_profit_loss_per_share = realized_profit_loss_per_share + unrealized_profit_loss_per_share

            logger.info(f"Realized P/L per share: {realized_profit_loss_per_share}")
            logger.info(f"Unrealized P/L per share: {unrealized_profit_loss_per_share}")
            logger.info(f"Total P/L per share for {trading_symbol}: {total_profit_loss_per_share}")

            return total_profit_loss_per_share

        except Exception as error:
            logger = self.get_logger_of_calculate_total_profit_loss_per_share()
            logger.error(f"Error in calculate_total_profit_loss_per_share: {error}", exc_info=True)
            return 0

        
    
    def calculate_total_profit_loss_per_instrument_per_order(self, sorted_orders, current_price, trading_symbol, order_id):
        """
        Calculate total profit or loss for a specific order including realized and unrealized P/L.
        """
        try:
            # Set up logging with a FileHandler
            logger = logging.getLogger("calculate_total_profit_loss_per_share")
            logger.setLevel(logging.INFO)

            # Avoid duplicate handlers
            if not logger.handlers:
                file_handler = logging.FileHandler("calculate_total_profit_loss_per_share.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

            realized_profit_loss_per_share = 0  # Realized P/L per share
            unrealized_profit_loss_per_share = 0  # Unrealized P/L per share
            self.open_position = False
            self.open_price = None
            self.open_quantity = 0
            self.current_order_type = None

            for order in sorted_orders:
                avg_price = order['average_price']
                quantity = order['quantity']
                transaction_type = order['transaction_type']
                
                # Process only the specific order
                # if order['order_id'] != order_id:
                #     continue

                if transaction_type == 'BUY':
                    if not self.open_position:
                        # Open a new Buy position
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Buy"
                        self.open_position = True
                    elif self.current_order_type == "Sell":
                        if self.open_quantity == quantity:
                            # Fully close Sell position
                            realized_profit_loss_per_share += (self.open_price - avg_price)
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            # Partially close Sell position
                            realized_profit_loss_per_share += (self.open_price - avg_price)
                            self.open_quantity -= quantity

                elif transaction_type == 'SELL':
                    if not self.open_position:
                        # Open a new Sell position
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Sell"
                        self.open_position = True
                    elif self.current_order_type == "Buy":
                        if self.open_quantity == quantity:
                            # Fully close Buy position
                            realized_profit_loss_per_share += (avg_price - self.open_price)
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            # Partially close Buy position
                            realized_profit_loss_per_share += (avg_price - self.open_price)
                            self.open_quantity -= quantity

            # Calculate unrealized profit or loss per share for open positions
            if self.open_position:
                if self.current_order_type == "Buy":
                    unrealized_profit_loss_per_share = (current_price - self.open_price)
                elif self.current_order_type == "Sell":
                    unrealized_profit_loss_per_share = (self.open_price - current_price)

            # Total profit or loss per share for the specific order
            total_profit_loss_per_share = unrealized_profit_loss_per_share

            logger.info(f"Total P/L for order Per Instrument {order_id}: {total_profit_loss_per_share}")

            return total_profit_loss_per_share

        except Exception as error:
            logger.error(f"Error in calculate_total_profit_loss_per_share: {error}", exc_info=True)
            return 0

    def update_trailing_stop_loss(self, kite, percentage, tradingsymbol):
        """Update trailing stop loss and persist to JSON with timestamp if it changes."""
        try:
            if self.current_stop_loss:
                return self.current_stop_loss
            return 
            # Lazy-load the JSON data into memory if it's None
            if self.trailing_stop_loss_json_data is None:
                file_path = "trailing_stop_loss.json"
                if os.path.exists(file_path):
                    try:
                        with open(file_path, "r") as file:
                            self.trailing_stop_loss_json_data = json.load(file)
                    except json.JSONDecodeError:
                        self.trailing_stop_loss_json_data = {}
                else:
                    self.trailing_stop_loss_json_data = {}

            if len(self.candles) < 2:
                return

            order_type = self.current_order_type
            current_vwap = self.get_vwap_upto_n_minus_1_candles(self.candles)
            updated = False

            if order_type == "Buy" and self.candles[-2]['close'] < current_vwap:
                new_value = self.candles[-2]['low']
                new_stop_loss = math.floor(new_value - (percentage / 100 * new_value))
                if new_stop_loss > self.current_stop_loss:
                    self.current_stop_loss = new_stop_loss
                    updated = True

            elif order_type == "Sell" and self.candles[-2]['close'] > current_vwap:
                new_value = self.candles[-2]['high']
                new_stop_loss = math.ceil(new_value + (percentage / 100 * new_value))
                if new_stop_loss < self.current_stop_loss:
                    self.current_stop_loss = new_stop_loss
                    updated = True

            if updated:
                old_value = self.trailing_stop_loss_json_data.get(tradingsymbol)
                if old_value != self.current_stop_loss:
                    self.trailing_stop_loss_json_data[tradingsymbol] = self.current_stop_loss
                    self.trailing_stop_loss_json_data[tradingsymbol + "_update_time"] = str(datetime.datetime.now(ZoneInfo('Asia/Kolkata')))
                    with open("trailing_stop_loss.json", "w") as file:
                        json.dump(self.trailing_stop_loss_json_data, file, indent=4)
                        
        except Exception as error:
            print(f"Error in update_trailing_stop_loss for {tradingsymbol}: {error}")
            print("*****************************************")
            print("Error in update_trailing_stop_loss function")
            print("*****************************************")
            print(" ERROR in update_trailing_stop_loss function")





    def should_close_trade(self,kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, strategy_response, lot_size, percentage):
        try:
            """
            Determine if the trade should be closed based on the exit trades threshold points.
            
            Args:
                instrument_token (int): The token of the instrument.
                exit_trades_threshold_points (float): The threshold for exiting trades.
            
            Returns:
                bool: True if the trade should be closed, False otherwise.
            """
            # Set up a dedicated logger for this function
            close_order_logger = logging.getLogger("close_trade_logger")
            close_order_logger.setLevel(logging.DEBUG)

            # Avoid duplicate handlers
            if not close_order_logger.handlers:
                file_handler = logging.FileHandler("close_trade_logger.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                close_order_logger.addHandler(file_handler)

            if self.close_trade_for_the_day:
                print("in should_close_trade close_trade already for",trading_symbol)
                return True
            if not self.close_trade_for_the_day and ((self.profit_threshold_points and self.profit_threshold_points>=exit_trades_threshold_points and (self.current_order_type == 'Buy' or self.current_order_type== 'Sell'))
                or (self.profit_threshold_points and  self.profit_threshold_points<=(exit_trades_threshold_points + loss_trades_threshold_points) and (self.current_order_type == 'Buy' or self.current_order_type== 'Sell'))):            
                close_order_logger.info(f"""Threshold hit for PROFIT EXIT{exit_trades_threshold_points} and {self.profit_threshold_points} and 
                                        {self.profit_threshold_points>=exit_trades_threshold_points} {trading_symbol} at price: {current_price}""")
                close_order_logger.info(f"""Threshold hit for LOSS EXIT {exit_trades_threshold_points + loss_trades_threshold_points} and { self.profit_threshold_points} and 
                                        { self.profit_threshold_points<=(exit_trades_threshold_points + loss_trades_threshold_points)} {trading_symbol} at price: {current_price}""")
                # Calculate daily profit or loss before reversing the order
                #self.fetch_and_calculate_daily_profit_loss(kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points, strategy_response, lot_size, percentage)
                # Stop-loss hit, place reverse order
                reverse_order_type = "Sell" if self.current_order_type and self.current_order_type == "Buy" else "Buy"
                close_order_logger.info(f"Reverse order type determined as: {reverse_order_type}")
                
                # Place the reverse order at the stop-loss price for square off
                # Fetch current positions
                for position in kite.positions()['net']:
                    if position['tradingsymbol'] ==  trading_symbol and position['quantity']!=0:
                        #squaringoffopenpositions
                        close_order_logger.info(f"Reverse order placement  {reverse_order_type} for {trading_symbol} with {position['quantity']}")
                        reverse_order_id_sq_off = self.place_single_order(
                                                            kite,
                                                            instrument_token,
                                                            trading_symbol,
                                                            exchange,
                                                            exit_trades_threshold_points,
                                                            loss_trades_threshold_points,
                                                            reverse_order_type,
                                                            lot_size,
                                                            current_price,
                                                            current_price,  # Using stop-loss price as the price for the reverse order
                                                            percentage,
                                                            order_mode="Final Square Off"
                                                        )
                close_order_logger.info(
                                            f"datetime:{datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} - Closing trade for {trading_symbol} due to threshold."
                                            f"Closing trade for the day for instrument {instrument_token}. "
                                            f"Exit threshold points: {exit_trades_threshold_points}, ",
                                            f"loss_trades_threshold_points:{loss_trades_threshold_points},"
                                            f"Exit threshold points with LOSS AT : {exit_trades_threshold_points + loss_trades_threshold_points}, ",
                                            f"Profit threshold points: {self.profit_threshold_points}"
                                        )
                logging.info(
                    f"datetime:{datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} - Closing trade for {trading_symbol} due to threshold."
                    f"Closing trade for the day for instrument {instrument_token}. "
                    f"Exit threshold points: {exit_trades_threshold_points}, "
                    f"loss_trades_threshold_points:{loss_trades_threshold_points},"
                    f"Profit threshold points: {self.profit_threshold_points}"
                )
                self.close_trade_for_the_day = True
                return True  # Trade should be closed
            return False  # Trade should not be closed
        except Exception as error:
            logging.error(f"Error should_close_trade: {str(error)}")
            close_order_logger.info(f"Error should_close_trade: {str(error)}")
            return False


    def exit_trade_for_the_instrument(self,kite,current_price,instrument_token, trading_symbol, exchange, per_instrument_exit_trades_threshold_points,
                                      strategy_response, lot_size, percentage,per_trade_profit_loss_per_share):
        try:
            """
            Determine if the trade should be closed based on the exit trades threshold points.
            
            Args:
                instrument_token (int): The token of the instrument.
                exit_trades_threshold_points (float): The threshold for exiting trades.
            
            Returns:
                bool: True if the trade should be closed, False otherwise.
            """
            # Set up a dedicated logger for this function
            close_order_logger = logging.getLogger("close_trade_logger")
            close_order_logger.setLevel(logging.DEBUG)
            reverse_order_id_sq_off = None
            # Avoid duplicate handlers
            if not close_order_logger.handlers:
                file_handler = logging.FileHandler("close_trade_logger.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                close_order_logger.addHandler(file_handler)

            if self.close_trade_for_the_day:
                print("in should_close_trade close_trade already for",trading_symbol)
                return True
            if not self.close_trade_for_the_day and per_trade_profit_loss_per_share and\
                per_trade_profit_loss_per_share>=(per_instrument_exit_trades_threshold_points*(self.per_trade_candle_based_profit)) and (self.current_order_type == 'Buy' or self.current_order_type== 'Sell'):            
                close_order_logger.info(
                    "Per Instrument Threshold hit | Threshold: %.2f | P/L per share: %.2f | Condition: %s| Symbol: %s | Price: %.2f",
                    per_instrument_exit_trades_threshold_points * self.per_trade_candle_based_profit,
                    per_trade_profit_loss_per_share,
                    per_trade_profit_loss_per_share >= (per_instrument_exit_trades_threshold_points * self.per_trade_candle_based_profit),
                    trading_symbol,
                    current_price
                )


                # Calculate daily profit or loss before reversing the order
                #self.fetch_and_calculate_daily_profit_loss(kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points, strategy_response, lot_size, percentage)
                # Stop-loss hit, place reverse order
                reverse_order_type = "Sell" if self.current_order_type and self.current_order_type == "Buy" else "Buy"
                close_order_logger.info(f"Reverse order type determined as: {reverse_order_type}")
                
                # Place the reverse order at the stop-loss price for square off
                # Fetch current positions
                for position in kite.positions()['net']:
                    if position['tradingsymbol'] ==  trading_symbol and position['quantity']!=0:
                        #squaringoffopenpositions
                        close_order_logger.info(f"Reverse order placement  {reverse_order_type} for {trading_symbol} with {position['quantity']}")
                        reverse_order_id_sq_off = self.place_single_order(
                                                            kite,
                                                            instrument_token,
                                                            trading_symbol,
                                                            exchange,
                                                            per_instrument_exit_trades_threshold_points,
                                                            per_instrument_exit_trades_threshold_points, #place holders
                                                            reverse_order_type,
                                                            lot_size,
                                                            current_price,
                                                            current_price,  # Using stop-loss price as the price for the reverse order
                                                            percentage,
                                                            order_mode="Interim Square Off"
                                                        )
                close_order_logger.info(
                                            f"datetime:{datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} - Closing trade for {trading_symbol} due to threshold."
                                            f"exiting trade for instrument small profit  {instrument_token}. "
                                            f"Exit threshold points: {per_instrument_exit_trades_threshold_points}, "
                                            f"Per Trade Profit threshold points: {per_trade_profit_loss_per_share}"
                                        )
                logging.info(
                    "Closing trade | Time: %s | Symbol: %s | Instrument: %s | Exit Threshold: %.2f | P/L per share: %.2f | Reason: Threshold hit (small profit)",
                    datetime.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S"),
                    trading_symbol,
                    instrument_token,
                    per_instrument_exit_trades_threshold_points * self.per_trade_candle_based_profit,
                    per_trade_profit_loss_per_share
)

                #self.close_trade_for_the_day = True
                if reverse_order_id_sq_off:
                    self.previous_order_type = self.current_order_type
                    self.just_closed_trade = True
                    self.keep_check_strategy = False
                    self.order_active = False
                    self.alert_candle = None
                    self.buy_alert_candle = None
                    self.sell_alert_candle = None
                    self.length_of_candles_at_small_exit = len(self.candles)
                    self.current_order_type = None
                    # Correct way to parse a datetime string
                    self.per_trade_exit_candle_start_time = datetime.datetime.strptime(self.candles[-1]['start_time'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo('Asia/Kolkata'))
                    
                return True  # Trade should be closed
            
            return False  # Trade should not be closed
        except Exception as error:
            logging.error(f"Error should_close_trade: {str(error)}")
            close_order_logger.info(f"Error should_close_trade: {str(error)}")
            return False

    def write_profit_loss_to_json(self,profit_loss_data, filename="current_profit_loss.json"):
        """
        Appends profit or loss data to a JSON file in the format
        :param profit_loss_data: Dictionary containing stock symbols and their profit/loss values
        :param filename: The name of the JSON file to write to (default: profit_loss.json)
        """
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
        

    def check_re_entry_eligibility(self):
        """
        Check if the instrument is eligible for re-entry based on VWAP.
        Logs the decision and rotates logs to prevent overflow.
        """
        # Setup logger (only once)
        if not hasattr(self, "re_entry_logger"):
            self.re_entry_logger = logging.getLogger("re_entry_logger")
            self.re_entry_logger.setLevel(logging.DEBUG)

            handler = RotatingFileHandler(
                "re_entry.log",
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3              # Keep 3 rotated files
            )
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)

            if not self.re_entry_logger.handlers:
                self.re_entry_logger.addHandler(handler)

        try:
            if not self.just_closed_trade:
                print("No recent trade closed, eligible for entry.")
                return True, False

            current_vwap = self.get_vwap_upto_n_minus_1_candles(self.candles)
            last_close = self.candles[-2]['close']

            # if self.trade_side == "BUY":
            #     if last_close < current_vwap and self.length_of_candles_at_small_exit and self.length_of_candles_at_small_exit<len(self.candles):
            #         self.re_entry_logger.info(f"BUY re-entry allowed: Close={last_close} < VWAP={current_vwap}")
            #         return True, False
            #     else:
            #         self.re_entry_logger.info(f"BUY re-entry blocked: Close={last_close} >= VWAP={current_vwap}")
            #         self.alert_candle = None
            #         self.buy_alert_candle = None
            #         self.sell_alert_candle = None
            #         return False, True

            # elif self.trade_side == "SELL":
            #     if last_close > current_vwap and self.length_of_candles_at_small_exit and self.length_of_candles_at_small_exit<len(self.candles):
            #         self.re_entry_logger.info(f"SELL re-entry allowed: Close={last_close} > VWAP={current_vwap}")
            #         return True, False
            #     else:
            #         self.re_entry_logger.info(f"SELL re-entry blocked: Close={last_close} <= VWAP={current_vwap}")
            #         self.alert_candle = None
            #         self.buy_alert_candle = None
            #         self.sell_alert_candle = None
            #         return False, True
            # elif self.trade_side == "BOTH":
                    # if self.previous_order_type and (self.previous_order_type== "BUY" or self.previous_order_type== "Buy"):
                    #     if last_close < current_vwap and self.length_of_candles_at_small_exit and self.length_of_candles_at_small_exit < len(self.candles):
                    #         self.re_entry_logger.info(f"BUY re-entry allowed: Close={last_close} < VWAP={current_vwap}")
                    #         return True, False
                    #     else:
                    #         self.re_entry_logger.info(f"BUY re-entry blocked: Close={last_close} >= VWAP={current_vwap}")
                    # elif self.previous_order_type and (self.previous_order_type== "SELL" or  self.previous_order_type == "Sell"):
                    #     if last_close > current_vwap and self.length_of_candles_at_small_exit and self.length_of_candles_at_small_exit < len(self.candles):
                    #         self.re_entry_logger.info(f"SELL re-entry allowed: Close={last_close} > VWAP={current_vwap}")
                    #         return True, False
                    #     else:
                    #         self.re_entry_logger.info(f"SELL re-entry blocked: Close={last_close} <= VWAP={current_vwap}")
                    # else:
                    #     self.re_entry_logger.warning(f"Unknown previous_order_type: {self.previous_order_type} Cannot determine re-entry eligibility.")
            now = datetime.datetime.now(ZoneInfo('Asia/Kolkata')).replace(second=0, microsecond=0)

            # Truncate `per_trade_exit_candle_start_time` to hours and minutes
            start_time = self.per_trade_exit_candle_start_time.replace(second=0, microsecond=0)

            # Compute exit time and truncate it too
            exit_time = (start_time + datetime.timedelta(minutes=self.interval_minutes)).replace(second=0, microsecond=0)

            #if self.per_trade_exit_candle_start_time and datetime.datetime.now(ZoneInfo('Asia/Kolkata')) >= (self.per_trade_exit_candle_start_time + datetime.timedelta(minutes=self.interval_minutes)):
            if self.per_trade_exit_candle_start_time and now >= exit_time:
                self.re_entry_logger.info(f"Re-entry allowed for BOTH trade side: at {datetime.datetime.now(ZoneInfo('Asia/Kolkata'))}")
                self.current_candle = None
                self.candles = []#self.candles[-1]  # This can remain as a list if needed elsewhere
                self.reset_candles()
                #print(self.candles,file=open('right_now_candles.txt', 'a'))
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
                self.alert_candle = None
                self.buy_alert_candle = None
                self.sell_alert_candle = None
                self.previous_order_type = None
                self.last_second_alert_candle = None
                self.order_id = None
                self.just_closed_trade = False
                self.keep_check_strategy = True
                self.last_used_vwap_candle = None
                self.cached_vwap = 0
                self.last_calculated_vwap = 0
                self.trailing_stop_loss_json_data = None
                self.length_of_candles_at_small_exit = None
                self.per_trade_exit_candle_start_time = None
                return True, False
            else:
                 self.re_entry_logger.info(f"re-entry blocked  {datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} , {self.per_trade_exit_candle_start_time + datetime.timedelta(minutes=self.interval_minutes)}")
            self.alert_candle = None
            self.buy_alert_candle = None
            self.sell_alert_candle = None
            return False, True

            self.re_entry_logger.warning("Unknown trade side; defaulting to not eligible.")
            return False, True

        except Exception as e:
            self.re_entry_logger.error(f"Error in check_re_entry_eligibility: {e}")
            return True, False

        
        
        
            
# WebSocket Handler Class
class WebSocketHandler:
    original_exit_threshold_points = None
    def __init__(self, kite, instruments=[]):
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
                                                    instrument_details_dict = self.restructure_for_combined_threshold(instruments,key = "exit_trades_threshold_points"))
                                                    for x in instruments
        }

        # Define on_ticks method
        self.kite_ticker.on_ticks = self.on_ticks
        self.kite_ticker.on_connect = self.on_connect
        self.kite_ticker.on_close = self.on_close
        self.kite_ticker.on_error = self.on_error
        self.kite_ticker.on_noreconnect = self.on_noreconnect
        self.kite_ticker.on_reconnect = self.on_reconnect

    def on_connect(self, ws, response):
        logging.info("WebSocket connected. Subscribing to instruments.")
        self.kite_ticker.subscribe(self.instrument_tokens)

    def on_close(self, ws, code, reason):
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
        # Process each tick and store candles
        try:
            #logging.info(f"Received ticks: {ticks}")
            print(f"datetime:{datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} Received ticks: {ticks}", file=open('ticks.txt', 'a'))
            current_datetime = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
            # Check if the current time is before 9 AM
            if  datetime.datetime.now(ZoneInfo("Asia/Kolkata")).hour < 9:
                # Continue if the time is before 9 AM
                return None
            
            for tick in ticks:
                try:
                    instrument_token = tick['instrument_token']
                    #logging.info(f"Processing tick for instrument_token: {instrument_token}")
                    #logging.debug(f"Tick data: {tick}")

                    if current_datetime.hour>=23:
                        print("Time More than 11 PM, Bot Will not trade further for the day")
                        continue
                    if current_datetime.hour < 9:
                        print("Time less than 9 AM, trading not started yet")
                        continue  # Skip the rest of the loop until it's 9:15 AM or later

                    # Get instrument-specific data
                    instrument_data = next((x for x in self.instruments if int(x['instrument_token']) == instrument_token), None)
                    if instrument_data is None:
                        logging.error(f"Instrument data not found for token: {instrument_token}")
                        continue


                    logging.info(f"Instrument data found for token: {instrument_token}, Data: {instrument_data}")
                    lot_size = int(instrument_data['lot_size'])
                    percentage = float(instrument_data['trade_calculation_percentage'])
                    trading_symbol = instrument_data['instrument_details']['tradingsymbol']
                    exchange = instrument_data['instrument_details']['exchange']
                    exit_trades_threshold_points = float(instrument_data['exit_trades_threshold_points'])
                    loss_trades_threshold_points = float(instrument_data['loss_trades_threshold_points'])
                    per_trade_exit_trades_threshold_points = float(instrument_data['per_trade_exit_trades_threshold_points'])
                    
                    if self.original_exit_threshold_points is None:
                        self.original_exit_threshold_points = exit_trades_threshold_points
                        logging.info(f"Assigning original_exit_threshold_points = {self.original_exit_threshold_points} and exit = {exit_trades_threshold_points}")
                        
                        
                    if exchange in ['NFO','NSE','BSE'] and current_datetime.hour>=16:
                        print("Time More than 3 PM for equity, Bot Will not trade further for the day")
                        continue

                    if exchange in ['NFO','NSE','BSE'] and (current_datetime.hour < 9 or (current_datetime.hour == 9 and current_datetime.minute < 15)):
                        continue  # Skip the rest of the loop until it's 9:15 AM or later

                    tick['current_datetime'] = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))

                    # Process the tick using the respective CandleAggregator for the instrument
                    candle_aggregator = self.candle_aggregators.get(str(instrument_token))
                    if candle_aggregator is None:
                        logging.error(f"Candle aggregator not found for token: {instrument_token}")
                        continue

                    # Call the async function directly
                    #asyncio.run(candle_aggregator.fetch_and_calculate_daily_profit_loss(self.kite))

                    logging.info("tsymbol:order_active:exit,loss,current_profit,closed - %s,%s, %s, %s, %s, %s, %s", 
                                        str(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                                        trading_symbol, 
                                        candle_aggregator.order_active, 
                                        exit_trades_threshold_points, 
                                        loss_trades_threshold_points,
                                        candle_aggregator.profit_threshold_points, 
                                        candle_aggregator.close_trade_for_the_day)


                    if candle_aggregator.close_trade_for_the_day:
                        logging.info(
                            f"closed trade for the day for instrument {trading_symbol}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"loss EXIT threshold points: {loss_trades_threshold_points}, "
                            f"loss EXIT AT points: {exit_trades_threshold_points + loss_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                        )
                        print("------------------closed1--------------------------------",trading_symbol,loss_trades_threshold_points,exit_trades_threshold_points,loss_trades_threshold_points,candle_aggregator.profit_threshold_points)
                        continue

                    trading_symbols_list = [x['tradingsymbol'] for x in  candle_aggregator.instrument_details_dict[str(int(self.original_exit_threshold_points))]]
                    print(trading_symbols_list)
                    if not candle_aggregator.order_active and (trading_symbol in trading_symbols_list) and not candle_aggregator.close_trade_for_the_day:
                        # Assign the daily profit/loss to the profit threshold points
                        candle_aggregator.profit_threshold_points = candle_aggregator.fetch_profit_loss_from_json_dict(trading_symbols_list)
                        if candle_aggregator.profit_threshold_points>=exit_trades_threshold_points:
                            logging.info(
                            f"****************1234*****************************************************"
                            f"closing trade for the day for instrument at second stage {trading_symbol}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"loss EXIT threshold points: {loss_trades_threshold_points}, "
                            f"loss EXIT AT points: {exit_trades_threshold_points + loss_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                            )
                            candle_aggregator.close_trade_for_the_day = True
                            
                        if candle_aggregator.profit_threshold_points<=(exit_trades_threshold_points + loss_trades_threshold_points):
                            logging.info(
                            f"****************1234*****************************************************"
                            f"closing trade for the day for instrument at second stage {trading_symbol}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"loss EXIT threshold points: {loss_trades_threshold_points}, "
                            f"WILL EXIT ON LOSS AT threshold points: {exit_trades_threshold_points + loss_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                            )
                            candle_aggregator.close_trade_for_the_day = True
                    
                    
                    logging.info(f"Candle aggregator found for token: {instrument_token}")
                    candle_aggregator.process_tick(tick)

                    # Log the current candle and updated tick info
                    #logging.debug(f"Updated tick processed: {tick}")
                    #logging.debug(f"Current candle: {candle_aggregator.current_candle}")

                    # Update trailing stop loss based on the latest tick
                    new_stop_loss = candle_aggregator.update_trailing_stop_loss(self.kite, percentage,trading_symbol)
                    #logging.info(f"Updated trailing stop loss for token {instrument_token}: {new_stop_loss}")

                    # Check if the current price hits the stored stop loss

                    current_price = candle_aggregator.current_candle['close']
                    # Call the async function directly
                    #smallprofitbaseperinstrumentexit
                    candle_aggregator.fetch_and_calculate_per_trade_per_instrument_profit_loss(self.kite,current_price,instrument_token, trading_symbol, exchange, per_trade_exit_trades_threshold_points, {}, lot_size, percentage,candle_aggregator.order_id)
                    candle_aggregator.fetch_and_calculate_daily_profit_loss(self.kite,current_price,instrument_token, trading_symbol, exchange, exit_trades_threshold_points,loss_trades_threshold_points, {}, lot_size, percentage,self.original_exit_threshold_points)
                    logging.info(f"Current price for token {instrument_token}: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss}, Order Type:{candle_aggregator.current_order_type}")
                    if  candle_aggregator.keep_check_strategy == True and (candle_aggregator.order_active and
                            ((candle_aggregator.current_order_type == 'Buy' and candle_aggregator.current_stop_loss and current_price <= candle_aggregator.current_stop_loss) or
                            (candle_aggregator.current_order_type ==  'Sell' and candle_aggregator.current_stop_loss and current_price >= candle_aggregator.current_stop_loss))):
                                              
                        if exit_trades_threshold_points and self.original_exit_threshold_points  and self.original_exit_threshold_points==exit_trades_threshold_points:
                            logging.info(f"Now Stop Loss Hit and og = {self.original_exit_threshold_points} and exit = {exit_trades_threshold_points}")
                            logging.info("halving stop loss")
                            exit_trades_threshold_points = exit_trades_threshold_points//2
                            logging.info(f"New og = {self.original_exit_threshold_points} and exit = {exit_trades_threshold_points}")
                            
                        # Stop-loss hit, handle reverse order
                        logging.warning(f"Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss}")
                        print(f"{datetime.datetime.now(ZoneInfo('Asia/Kolkata'))} Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss},Order Type:{candle_aggregator.current_order_type}", file=open("reverse_logic entered.log", "a"))
                        candle_aggregator.handle_reverse_order(
                            self.kite,
                            instrument_token, 
                            trading_symbol,
                            exchange,
                            exit_trades_threshold_points,
                            loss_trades_threshold_points,
                            {'order_type': candle_aggregator.current_order_type, 'stop_loss': candle_aggregator.current_stop_loss}, 
                            lot_size, 
                            percentage
                        )

                        # Mark order as inactive to prevent new orders until a fresh signal
                        #candle_aggregator.order_active = False  
                        #logging.info(f"Reverse order added continuing the flow")
                        if candle_aggregator.order_active == False:
                            logging.info(f"Stop Loss hit for {trading_symbol} Order Closed")
                            print(f"Stop Loss hit for {trading_symbol} Order Closed")
                    
                    
                    
                    
                    
                    
                    if candle_aggregator.just_closed_trade:
                        candle_aggregator.keep_check_strategy,candle_aggregator.just_closed_trade = candle_aggregator.check_re_entry_eligibility()
                    # Check strategy based on the candle data and the specific percentage
                    if candle_aggregator.keep_check_strategy == False:
                        candle_aggregator.alert_candle = None
                        print("--------Ineligible For ReEntry--------------------")
                    strategy_response = candle_aggregator.check_strategy(instrument_token, percentage)
                    #logging.debug(f"Strategy response for token {instrument_token}: {strategy_response}")
                    
                    if not strategy_response and (candle_aggregator.order_active):
                        print("the order is already active, continuing exection and strategy response is")
                        print("strategy response: ",strategy_response)
                        continue
                    
                    if strategy_response and candle_aggregator.order_active and strategy_response['order_type'] == candle_aggregator.current_order_type:
                        logging.info(f"Strategy response matches current order type for {trading_symbol}. No new order placed.")
                        print(f"Strategy response matches current order type for {trading_symbol}. No new order placed.")
                        print("strategy response: ",strategy_response)
                        continue
                    if candle_aggregator.close_trade_for_the_day:
                        logging.info(
                            f"------------------closed--------------------------------"
                            f"Part 2 closed trade for the day for instrument {trading_symbol}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"LOSS Exit threshold points: {loss_trades_threshold_points}, "
                            f"LOSS based Exit AT points: {exit_trades_threshold_points + loss_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                            f"------------------****--------------------------------"
                        )
                        print("------------------closed--------------------------------",trading_symbol,exit_trades_threshold_points,loss_trades_threshold_points,candle_aggregator.profit_threshold_points)
                        continue
                    
                    if strategy_response and strategy_response['order_type'] != candle_aggregator.current_order_type and candle_aggregator.order_active:
                        logging.info(f"Square off for reverser order mode for {trading_symbol} with current order type {candle_aggregator.current_order_type}")
                        # Place order with lot size and stop loss from strategy
                        candle_aggregator.handle_reverse_order(
                            self.kite,
                            instrument_token, 
                            trading_symbol,
                            exchange,
                            exit_trades_threshold_points,
                            loss_trades_threshold_points,
                            {'order_type': candle_aggregator.current_order_type, 'stop_loss': candle_aggregator.current_stop_loss}, 
                            lot_size, 
                            percentage,
                            mode="Square Off For Reversal Order"
                        )
                    #this will be first order placement when no order has been placed for the day, rest 
                    if strategy_response and not candle_aggregator.order_active:
                        logging.info(f"Placing order for token {instrument_token} based on strategy through normal mode")
                        # Place order with lot size and stop loss from strategy
                        order_id = candle_aggregator.place_single_order(
                            self.kite,
                            instrument_token,
                            trading_symbol,
                            exchange,
                            exit_trades_threshold_points,
                            loss_trades_threshold_points,
                            strategy_response['order_type'],
                            lot_size,  # Quantity based on the lot size
                            strategy_response['stop_loss'],
                            current_price,
                            order_mode="Normal Order"
                        )
                        if order_id:
                            logging.info(f"Order placed successfully: {order_id} for {strategy_response['order_type']} {instrument_token}")

                            # Mark the order as active and store the current stop loss and order type
                            candle_aggregator.order_active = True
                            #make false
                            #candle_aggregator.order_active = False
                            candle_aggregator.current_stop_loss = strategy_response['stop_loss']
                            candle_aggregator.order_type = strategy_response['order_type']

                            # Update trailing stop loss immediately after placing the order
                            candle_aggregator.update_trailing_stop_loss(self.kite, percentage,trading_symbol)
                            logging.info(f"Trailing stop loss updated after placing order for {instrument_token}.")
                        else:
                            logging.error(f"Failed to place order for token {instrument_token}. Strategy response: {strategy_response}")

                except KeyError as ke:
                    logging.error(f"KeyError processing tick for token {tick.get('instrument_token', 'Unknown')}: {ke}")
                    logging.debug(f"Tick data at KeyError: {tick}")
                    return None
                except Exception as e:
                    logging.error(f"Error processing tick for token {tick.get('instrument_token', 'Unknown')}: {e}")
                    logging.debug(f"Exception details: {str(e)}. Tick data: {tick}")
                    return None

        except Exception as error:
            logging.error(f"Error in on_ticks: {error}")
            logging.debug(f"Exception details: {str(error)}. Ticks: {ticks}")
            return None



    def stop_websocket(self):
        """Stop the WebSocket and handle cleanup, with logging."""
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
        self.kite_ticker.connect(threaded=True)

        # Backoff parameters
        backoff_time = 5  # Initial backoff time in seconds
        max_backoff_time = 60  # Maximum backoff time in seconds
        is_reconnecting = False


    def restructure_for_combined_threshold(self, instruments_data=[],key = "exit_trades_threshold_points"):
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
                exit_threshold = instrument.get(key)
                if exit_threshold is None:
                    continue  # Skip invalid entries with missing 'exit_trades_threshold_points'
                
                grouped_data[exit_threshold].append({
                    "instrument_token": instrument.get('instrument_token'),
                    "tradingsymbol": instrument.get('instrument_details', {}).get('tradingsymbol'),
                    key: exit_threshold,
                    "lot_size":instrument.get('lot_size')
                })
            
            return dict(grouped_data)  # Convert defaultdict to regular dict for output consistency
        
        except (KeyError, TypeError) as error:
            # Log the error for better debugging
            print(f"Error restructuring data: {error}")
            return {}