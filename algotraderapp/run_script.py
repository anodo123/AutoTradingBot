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

# Initialize Redis client using Django settings
redis_client = redis.StrictRedis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB
)

logging.basicConfig(level=logging.DEBUG)


class CandleAggregator:
    def __init__(self, instrument_token,tradingsymbol ,interval_minutes=15 ,file_path='minute_candles.json'):
        self.file_path = str(instrument_token)+'_'+str(interval_minutes) + '_' + file_path
        self.instrument_token = instrument_token  # Add the instrument token
        self.tradingsymbol = tradingsymbol  # Add the instrument token
        self.interval_minutes = interval_minutes
        self.current_candle = None
        self.candles = []  # This can remain as a list if needed elsewhere

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

    def process_tick(self, tick):
        """ Process a new tick and update the candle data. """
        try:
            # Ensure required fields exist in the tick data
            if 'last_price' not in tick or 'last_traded_quantity' not in tick or 'current_datetime' not in tick:
                logging.error(f"Missing required fields in tick: {tick}")
                return  # Skip processing this tick if essential fields are missing

            last_price = tick['last_price']
            with open('last_price_log.txt', 'a') as log_file: log_file.write(f"last_price: {tick['last_price']},{tick['current_datetime']}\n")

            tick_time = datetime.datetime.strptime(str(tick['current_datetime']), '%Y-%m-%d %H:%M:%S.%f')

            # Round the time to the nearest candle start time
            candle_start_time = tick_time.replace(minute=(tick_time.minute // self.interval_minutes) * self.interval_minutes, second=0, microsecond=0)

            if self.current_candle is None or candle_start_time != datetime.datetime.strptime(self.current_candle['start_time'], '%Y-%m-%d %H:%M:%S'):
                # If this is a new candle, store the previous one and start a new one
                if self.current_candle is not None:
                    self.candles= self.save_candles(self.current_candle)
                    #self.save_candles(self.current_candle)  # Save candles after every interval

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

                if self.current_candle is not None:
                    # Save the updated candle
                    self.candles = self.save_candles(self.current_candle)
                    logging.debug(f"Candle updated and saved: {self.current_candle}")  # Log updated and saved candle

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
            if len(self.candles) < 3:
                print(f"Not enough candles. Candles count: {len(self.candles)}", file=log_file)
                return None  # Not enough candles to make a decision

            # Get previous two candles
            prev_candle_1 = self.candles[-2]  # Most recent completed candle
            prev_candle_2 = self.candles[-3]  # The candle before the most recent one

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
            current_high = self.current_candle['high']
            current_low = self.current_candle['low']

            print(f"Current Candle High: {current_high}, Current Candle Low: {current_low}", file=log_file)

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
            prev_candle_2 = self.candles[-3]

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


    def place_single_order(self,kite,instrument_token, trading_symbol, exchange, exit_trades_threshold_points, order_type, quantity, stop_loss, price=None,percentage = 0.00,order_mode="Reverse_side"):
        log_file = 'order_placement.log'
        with open(log_file, 'a') as f:  # Open log file in append mode
            try:
                # Check for existing orders
                order_id = None
                
                # if should_close_trade(instrument_token, exit_trades_threshold_points):
                #     f.write(f"Trade closing condition met for {trading_symbol}. No order placed.\n")
                #     return None
                    
                f.write(f"Attempting {order_mode} to place order for {trading_symbol} - {order_type} {quantity} stop loss {stop_loss} price {price}.\n")
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
                        f.write(f"{order_type} {order_mode} order placed for {trading_symbol}. Order ID: {order_id}, Stop Loss: {self.current_stop_loss}, Quantity: {quantity}, Price: {price}\n")
                        # Fetch all orders
                    else:
                        self.current_order_type = None
                        self.current_stop_loss = None
                        # Update the current stop loss in the object for the new reverse order
                        self.order_active = False
                        f.write(f"{order_type} {order_mode} order NOT placed for {trading_symbol}. Order ID: {order_id}, Stop Loss: {self.current_stop_loss}, Quantity: {quantity}, Price: {price}\n")
                
                f.write(f"Order placed successfully for {trading_symbol}. Order ID: {order_id}\n")
                return order_id

            except Exception as e:
                f.write(f"Error placing order for {trading_symbol}: {str(e)}\n")
                return None

    def handle_reverse_order(self, kite,instrument_token, trading_symbol, exchange, exit_trades_threshold_points, strategy_response, lot_size, percentage):
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
        
        # if self.should_close_trade(instrument_token, exit_trades_threshold_points):
        #     reverse_order_logger.info(f"Trade closure conditions met for {instrument_token}. Exiting handle_reverse_order.")
        #     return None

        # Check stop-loss condition
        if (self.current_order_type == 'Buy' and stop_loss_price and current_price <= stop_loss_price) or \
        (self.current_order_type== 'Sell' and stop_loss_price and current_price >= stop_loss_price):
            
            reverse_order_logger.info(f"Stop-loss hit for {instrument_token} at price: {current_price}")
            print(f"Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {stop_loss_price} inside reverse handling function", file=open("reverse_logic entered.log", "a"))

            # Calculate daily profit or loss before reversing the order
            reverse_order_logger.info("Calculating daily profit or loss.")
            self.fetch_and_calculate_daily_profit_loss(kite)

            # Stop-loss hit, place reverse order
            reverse_order_type = "Sell" if strategy_response['order_type'] == "Buy" else "Buy"
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
                                                        reverse_order_type,
                                                        lot_size,
                                                        stop_loss_price,
                                                        stop_loss_price,  # Using stop-loss price as the price for the reverse order
                                                        percentage,
                                                        order_mode="Square OFF"
                                                    )
            # for position in kite.positions()['net']:
            #     if position['tradingsymbol'] ==  trading_symbol and position['quantity']==0:
            #         #squaredoffsuccessfully
            #         self.order_active = False
            # Place the reverse order at the stop-loss price
            reverse_order_id = self.place_single_order(
                                                kite,
                                                instrument_token,
                                                trading_symbol,
                                                exchange,
                                                exit_trades_threshold_points,
                                                reverse_order_type,
                                                lot_size,
                                                stop_loss_price,
                                                stop_loss_price,  # Using stop-loss price as the price for the reverse order
                                                percentage,
                                                order_mode="Reverse Mode"
                                            )
            
            if reverse_order_id:
                reverse_order_logger.info(f"Reverse order placed with ID: {reverse_order_id} for {reverse_order_type} on {trading_symbol}")
            else:
                reverse_order_logger.warning(f"Failed to place reverse order for {trading_symbol}.")
        else:
            reverse_order_logger.debug("Stop-loss condition not met. No reverse order placed.")

    
    async def fetch_and_calculate_daily_profit_loss(self, kite):
        """
        Fetch orders from Kite API and calculate daily profit or loss, with extensive logging.
        """
        # Configure logging to write to a file within the function
        logging.basicConfig(
            filename="daily_profit_loss_calculation.log",  # The file to write logs to
            filemode="a",  # Append to the file instead of overwriting
            format="%(asctime)s - %(levelname)s - %(message)s",  # Log message format
            level=logging.DEBUG  # Log level; use DEBUG for detailed logs
        )
        
        logging.info("Starting fetch_and_calculate_daily_profit_loss process.")
        
        try:
            # Fetch all orders
            all_orders = kite.orders()
            logging.info(f"Fetched {len(all_orders)} orders from Kite API.")

            # Filter for completed buy/sell orders
            completed_orders = [
                order for order in all_orders if order['status'] == 'COMPLETE' and
                order['transaction_type'] in ['BUY', 'SELL']
            ]
            logging.info(f"Filtered completed buy/sell orders. Count: {len(completed_orders)}")

            # Sort orders by timestamp
            sorted_orders = sorted(completed_orders, key=lambda x: x['order_timestamp'])
            logging.info("Sorted orders by timestamp.")

            # Calculate daily profit or loss based on the sorted orders
            daily_profit_loss_per_share = await self.calculate_daily_profit_loss(sorted_orders)
            logging.info(f"Calculated daily profit/loss: {daily_profit_loss_per_share}")
            
            # Assign the daily profit/loss to the profit threshold points
            self.profit_threshold_points = daily_profit_loss_per_share
            #self.profit_threshold_points = 0 #assigned to zero for testing
            logging.info(f"Updated profit threshold points: {self.profit_threshold_points}")

            # Optional console output
            print(f"Total Profit/Loss for the day: {daily_profit_loss_per_share}")

            logging.info("Completed fetch_and_calculate_daily_profit_loss process successfully.")
            return daily_profit_loss_per_share
        except Exception as error:
            logging.error(f"Error in fetch_and_calculate_daily_profit_loss: {error}", exc_info=True)
            return 0
    
    async def calculate_daily_profit_loss(self, sorted_orders):
        """
        Calculate daily profit or loss based on completed buy and sell orders in sequence,
        and calculate average profit/loss points per share.
        """
        try:
            # Set up logging with a FileHandler
            logger = logging.getLogger("calculate_daily_profit_loss")
            logger.setLevel(logging.INFO)

            # Avoid duplicate handlers
            if not logger.handlers:
                file_handler = logging.FileHandler("calculate_daily_profit_loss.log")
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

            daily_profit_loss = 0
            total_quantity_traded = 0  # To track total quantity traded for calculating P/L per share
            self.open_position = False
            self.open_price = None
            self.open_quantity = 0
            self.current_order_type = None

            for order in sorted_orders:
                quantity = order['quantity']
                avg_price = order['average_price']
                transaction_type = order['transaction_type']

                if transaction_type == 'BUY':
                    if not self.open_position:
                        # Open a new Buy position
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Buy"
                        self.open_position = True
                        #logging.info(f"New Buy position: Price {avg_price}, Quantity {quantity}")

                    elif self.current_order_type == "Sell":
                        if self.open_quantity == quantity:
                            # Fully close Sell position
                            profit_or_loss = (self.open_price - avg_price)
                            daily_profit_loss += profit_or_loss
                            total_quantity_traded += quantity  # Track traded quantity for P/L per share
                            #logging.info(f"Closed Sell position with Buy: Open Price {self.open_price}, Close Price {avg_price}, "
                            #            f"Quantity: {quantity}, Profit/Loss: {profit_or_loss}")
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            # Partially close Sell position
                            profit_or_loss = (self.open_price - avg_price)
                            daily_profit_loss += profit_or_loss
                            total_quantity_traded += quantity
                            self.open_quantity -= quantity
                            #logging.info(f"Partially closed Sell position with Buy: Open Price {self.open_price}, Close Price {avg_price}, "
                            #            f"Quantity: {quantity}, Remaining Quantity: {self.open_quantity}, Profit/Loss: {profit_or_loss}")
                        else:
                            pass
                            #logging.warning("Buy quantity exceeds remaining Sell quantity. Possible mismatched order.")

                elif transaction_type == 'SELL':
                    if not self.open_position:
                        # Open a new Sell position
                        self.open_price = avg_price
                        self.open_quantity = quantity
                        self.current_order_type = "Sell"
                        self.open_position = True
                        #logging.info(f"New Sell position: Price {avg_price}, Quantity {quantity}")

                    elif self.current_order_type == "Buy":
                        if self.open_quantity == quantity:
                            # Fully close Buy position
                            profit_or_loss = (avg_price - self.open_price)
                            daily_profit_loss += profit_or_loss
                            total_quantity_traded += quantity
                            #logging.info(f"Closed Buy position with Sell: Open Price {self.open_price}, Close Price {avg_price}, "
                            #            f"Quantity: {quantity}, Profit/Loss: {profit_or_loss}")
                            self._reset_position()
                        elif quantity < self.open_quantity:
                            # Partially close Buy position
                            profit_or_loss = (avg_price - self.open_price)
                            daily_profit_loss += profit_or_loss
                            total_quantity_traded += quantity
                            self.open_quantity -= quantity
                            #logging.info(f"Partially closed Buy position with Sell: Open Price {self.open_price}, Close Price {avg_price}, "
                            #            f"Quantity: {quantity}, Remaining Quantity: {self.open_quantity}, Profit/Loss: {profit_or_loss}")
                        else:
                            pass
                            #logging.warning("Sell quantity exceeds remaining Buy quantity. Possible mismatched order.")

            # Calculate profit or loss points per share
            profit_or_loss_per_share = daily_profit_loss if total_quantity_traded > 0 else 0
            logging.info(f"Total Profit/Loss Points per Share: {profit_or_loss_per_share}")
            return profit_or_loss_per_share
        except Exception as error:
            logging.error(f"Error in fetch_and_calculate_daily_profit_loss: {error}", exc_info=True)
            return 0



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
            if len(self.candles) < 3:
                #logger.info("Insufficient candle data (less than 3 candles). Exiting function.")
                return

            prev_candle_1 = self.candles[-2]
            prev_candle_2 = self.candles[-3]

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


    def should_close_trade(self, instrument_token, exit_trades_threshold_points):
        try:
            """
            Determine if the trade should be closed based on the exit trades threshold points.
            
            Args:
                instrument_token (int): The token of the instrument.
                exit_trades_threshold_points (float): The threshold for exiting trades.
            
            Returns:
                bool: True if the trade should be closed, False otherwise.
            """
            if self.close_trade_for_the_day:
                return True
            if not self.close_trade_for_the_day and self.profit_threshold_points and self.profit_threshold_points>=exit_trades_threshold_points:
                # Log details before setting the close trade flag
                logging.info(
                    f"Closing trade for the day for instrument {instrument_token}. "
                    f"Exit threshold points: {exit_trades_threshold_points}, "
                    f"Profit threshold points: {self.profit_threshold_points}"
                )
                print(
                    f"datetime:{datetime.datetime.now()} - Closing trade for {instrument_token} due to threshold. "
                    f"Exit threshold points: {exit_trades_threshold_points}, "
                    f"Profit threshold points: {self.profit_threshold_points}", 
                    file=open('trade_close_log.txt', 'a')
                )
                self.close_trade_for_the_day = True
                return True  # Trade should be closed
            return False  # Trade should not be closed
        except Exception as error:
            logging.error(f"Error should_close_trade: {str(error)}")
            return False

# WebSocket Handler Class
class WebSocketHandler:
    def __init__(self, kite, instruments=[]):
        self.kite = kite
        self.kite_ticker = KiteTicker(kite.api_key, kite.access_token)
        
        # Store instrument details
        self.instruments = instruments
        self.instrument_tokens = [int(x['instrument_token']) for x in instruments]
        # Create a CandleAggregator instance for each instrument, passing the instrument_token
        self.candle_aggregators = {
            x['instrument_token']: CandleAggregator(instrument_token=int(x['instrument_token']),tradingsymbol=x['instrument_details']['tradingsymbol'],interval_minutes=5) for x in instruments
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
        logging.info("WebSocket closed.")
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
            time.sleep(10)
            self.kite_ticker.connect()  # Reconnect
        except Exception as e:
            logging.error(f"Error while reconnecting WebSocket: {e}")

    def on_ticks(self, ws, ticks):
        # Process each tick and store candles
        try:
            logging.info(f"Received ticks: {ticks}")
            print(f"datetime:{datetime.datetime.now()} Received ticks: {ticks}", file=open('ticks.txt', 'a'))
            current_datetime = datetime.datetime.now()
            # Check if the current time is before 9 AM
            if  datetime.datetime.now().hour < 9:
                # Continue if the time is before 9 AM
                return None

            for tick in ticks:
                try:
                    instrument_token = tick['instrument_token']
                    logging.info(f"Processing tick for instrument_token: {instrument_token}")
                    logging.debug(f"Tick data: {tick}")
                    #Check the closing point hit  
                    # if candle_aggregator.close_trade_for_the_day:
                    #     continue

                    if current_datetime.hour>=23:
                        print("Time More than 11 PM, Bot Will not trade further for the day")
                        continue
                    if current_datetime.hour < 9:
                        print("Time less than 9 AM, tradeing not started yet")
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

                    if exchange in ['NSE','BSE'] and current_datetime.hour>=15:
                        print("Time More than 3 PM for equity, Bot Will not trade further for the day")
                        continue

                    if exchange in ['NSE','BSE'] and (current_datetime.hour < 9 or (current_datetime.hour == 9 and current_datetime.minute < 15)):
                        continue  # Skip the rest of the loop until it's 9:15 AM or later

                    tick['current_datetime'] = datetime.datetime.now()

                    # Process the tick using the respective CandleAggregator for the instrument
                    candle_aggregator = self.candle_aggregators.get(str(instrument_token))
                    if candle_aggregator is None:
                        logging.error(f"Candle aggregator not found for token: {instrument_token}")
                        continue

                    # Call the async function directly
                    asyncio.run(candle_aggregator.fetch_and_calculate_daily_profit_loss(self.kite))

                    print(" candle_aggregator.close_trade_for_the_day",candle_aggregator.close_trade_for_the_day)

                    if candle_aggregator.close_trade_for_the_day:
                        logging.info(
                            f"closed trade for the day for instrument {instrument_token}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                        )
                        print("------------------closed--------------------------------",instrument_token,exit_trades_threshold_points,candle_aggregator.profit_threshold_points)
                        continue
                    if not candle_aggregator.close_trade_for_the_day and int(candle_aggregator.profit_threshold_points) and candle_aggregator.profit_threshold_points>=exit_trades_threshold_points:
                        # Log details before setting the close trade flag
                        logging.info(
                            f"Closing trade for the day for instrument {instrument_token}. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}"
                        )
                        print(
                            f"datetime:{datetime.datetime.now()} - Closing trade for {instrument_token} due to threshold. "
                            f"Exit threshold points: {exit_trades_threshold_points}, "
                            f"Profit threshold points: {candle_aggregator.profit_threshold_points}", 
                            file=open('trade_close_log.txt', 'a')
                        )
                        candle_aggregator.close_trade_for_the_day = True
                        print(" candle_aggregator.close_trade_for_the_day",candle_aggregator.close_trade_for_the_day)
                        continue

                    
                    logging.info(f"Candle aggregator found for token: {instrument_token}")
                    candle_aggregator.process_tick(tick)

                    # Log the current candle and updated tick info
                    logging.debug(f"Updated tick processed: {tick}")
                    logging.debug(f"Current candle: {candle_aggregator.current_candle}")

                    # Update trailing stop loss based on the latest tick
                    new_stop_loss = candle_aggregator.update_trailing_stop_loss(self.kite, percentage,trading_symbol)
                    logging.info(f"Updated trailing stop loss for token {instrument_token}: {new_stop_loss}")

                    # Check if the current price hits the stored stop loss
                    current_price = candle_aggregator.current_candle['close']
                    logging.info(f"Current price for token {instrument_token}: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss}, Order Type:{candle_aggregator.current_order_type}")
                    if (candle_aggregator.order_active and
                            ((candle_aggregator.current_order_type == 'Buy' and candle_aggregator.current_stop_loss and current_price <= candle_aggregator.current_stop_loss) or
                            (candle_aggregator.current_order_type == 'Sell' and candle_aggregator.current_stop_loss and current_price >= candle_aggregator.current_stop_loss))):
                        
                        # Stop-loss hit, handle reverse order
                        logging.warning(f"Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss}")
                        print(f"{datetime.datetime.now()} Stop-loss hit for {instrument_token}. Current price: {current_price}, Stop-loss: {candle_aggregator.current_stop_loss},Order Type:{candle_aggregator.current_order_type}", file=open("reverse_logic entered.log", "a"))
                        candle_aggregator.handle_reverse_order(
                            self.kite,
                            instrument_token, 
                            trading_symbol,
                            exchange,
                            exit_trades_threshold_points,
                            {'order_type': candle_aggregator.current_order_type, 'stop_loss': candle_aggregator.current_stop_loss}, 
                            lot_size, 
                            percentage
                        )

                        # Mark order as inactive to prevent new orders until a fresh signal
                        #candle_aggregator.order_active = False  
                        logging.info(f"Reverse order added continuing the flow")
                        continue
                    if (candle_aggregator.order_active):
                        print("the order is already active, continuing exection")
                        continue
                    # Check strategy based on the candle data and the specific percentage
                    strategy_response = candle_aggregator.check_strategy(instrument_token, percentage)
                    logging.debug(f"Strategy response for token {instrument_token}: {strategy_response}")



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



    def run_websocket(self):
        """ Start the WebSocket and listen for ticks, with connection checks and retries. """
        # Connect to the WebSocket initially
        self.kite_ticker.connect(threaded=True)

        # Backoff parameters
        backoff_time = 5  # Initial backoff time in seconds
        max_backoff_time = 60  # Maximum backoff time in seconds
        is_reconnecting = False

        # while True:
        #     try:
        #         if self.kite_ticker.is_connected():
        #             logging.info("WebSocket is connected and running.")
        #             is_reconnecting = False  # Reset reconnection flag
        #             backoff_time = 5  # Reset backoff time
        #         else:
        #             if not is_reconnecting:
        #                 logging.info("WebSocket is not connected, attempting to reconnect...")
        #                 is_reconnecting = True

        #                 # Close existing connection and reconnect
        #                 #self.kite_ticker.close()
        #                 time.sleep(10)
        #                 self.kite_ticker.connect(threaded=True)

        #             time.sleep(backoff_time)  # Wait before next reconnection attempt
        #             backoff_time = min(max_backoff_time, backoff_time * 2)  # Exponential backoff
        #     except Exception as e:
        #         logging.error(f"Error handling WebSocket: {e}")
        #         time.sleep(backoff_time)  # Wait before retrying on error
        #         backoff_time = min(max_backoff_time, backoff_time * 2)  # Exponential backoff
