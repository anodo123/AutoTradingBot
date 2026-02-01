
from datetime import timedelta
import datetime
import logging
from kiteconnect import KiteConnect, KiteTicker
import json
class HistoricalDataFetcher:
    def __init__(self, kite):
        self.kite = kite

    def fetch_historical_data(self, instrument_token, to_date, timeframe_minutes, ema_interval):
        try:
            extra_minutes = timeframe_minutes * ema_interval
            from_date = to_date - timedelta(minutes=extra_minutes)
            exact_minutes = "minute" if timeframe_minutes == 1 else f"{timeframe_minutes}minute"
            data = self.kite.historical_data(
                instrument_token,
                from_date,
                to_date,
                exact_minutes
            )

            return data

        except Exception as e:
            logging.error(f"Error fetching historical data: {e}")
            return []
        

    def fetch_all_historical_data(self, instrument_details_list):
        """
        Takes full instrument_details list and returns
        historical candles mapped to instrument_token.
        """
        try:

            #fetcher = self.HistoricalDataFetcher(self.kite)
            results = {}

            for config in instrument_details_list:
                try:
                    instrument_token = int(config["instrument_token"])
                    timeframe_minutes = int(config["timeframe"])
                    ema_interval = int(config["ema_interval"])

                    # Build to_date
                    if "historical_data_date" in config and config["historical_data_date"]:
                        to_date = datetime.datetime.strptime(
                            config["historical_data_date"], "%d-%m-%Y"
                        ).replace(hour=15, minute=30, second=0)
                    else:
                        to_date = datetime.datetime.now()

                    candles = self.fetch_historical_data(
                        instrument_token=instrument_token,
                        to_date=to_date,
                        timeframe_minutes=timeframe_minutes,
                        ema_interval=ema_interval
                    )
                    new_candles = []
                    for candle in candles:
                        candle['start_time'] = candle["date"].replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
                        new_candles.append(
                            {
                                "start_time": candle['start_time'],
                                "close": candle['close'],
                                "final_save": True
                            }
                        )
                        
                    #create_json_file_from_candles
                    # Load previous candles from the file, if available
                    ema_candles_json_path = str(instrument_token)+'_'+str(timeframe_minutes) + '_' + "ema_close_candles.json"
                    
                    with open(ema_candles_json_path, 'w') as file:
                        json.dump(new_candles, file, indent=4)
                    
                    results[instrument_token] = {
                        "symbol": config["instrument_details"]["tradingsymbol"],
                        "timeframe": timeframe_minutes,
                        "ema_interval": ema_interval,
                        "candles": new_candles
                    }

                except Exception as e:
                    logging.error(f"Failed for {config.get('instrument_token')}: {e}")

            return results
        except Exception as error:
            logging.critical(f"Unhandled exception in fetch_all_historical_data: {error}")
            return {}



class EMACalculator:
    def __init__(self, period: int):
        self.period = period

    def _validate_and_prepare(self, candles):
        if not candles or len(candles) < self.period:
            raise ValueError("Not enough candles to calculate EMA")

        # sort candles by time (safety)
        candles = sorted(candles, key=lambda x: x["date"])

        closes = [c["close"] for c in candles]
        return closes

    def calculate_full_ema(self, candles):
        """
        Returns full EMA series (same length as closes)
        """
        closes = self._validate_and_prepare(candles)

        k = 2 / (self.period + 1)

        # Start EMA from SMA of first `period`
        sma = sum(closes[:self.period]) / self.period
        ema_values = [None] * (self.period - 1)
        ema_values.append(sma)

        ema_prev = sma

        for price in closes[self.period:]:
            ema_now = (price - ema_prev) * k + ema_prev
            ema_values.append(ema_now)
            ema_prev = ema_now

        return ema_values

    def calculate_latest_ema(self, candles):
        """
        Returns only the most recent EMA value
        """
        ema_series = self.calculate_full_ema(candles)
        return ema_series[-1]


