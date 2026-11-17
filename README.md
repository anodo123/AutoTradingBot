# Supertrend trading bot

Django API for Zerodha Kite intraday trading. MongoDB stores instrument settings.

## Trading rules

- Use Supertrend with Wilder ATR; default length **7**, factor **2**.
- Evaluate direction only on completed candles. Green means BUY; red means SELL.
- The first live completed candle trades its current colour, without waiting for a colour change.
- BOTH: close an opposite position, confirm the exit is filled and the broker position is flat, then enter the new direction.
- BUY only: green enters/holds BUY; red exits without selling short.
- SELL only: red enters/holds SELL; green exits without buying long.
- Matching positions are held; repeated ticks do not place duplicate entries.
- There is one daily profit target: `exit_trades_threshold_points`. Instruments with the same target retain the existing combined group behavior. Realized losses remain included. Reaching the target latches the group closed for the day and squares off every open member on reconciliation.
- Old percentage breakout, trailing SL, per-trade profit exits and first-loss target reduction are removed. Old MongoDB fields are ignored.

## Configuration API

Existing login, instrument and start/stop URLs remain under `/algotraderapp/`.
Add instruments with POST form data to `tradinginstruments/addtradinginstruments`:

```text
instrument_token=123
lot_size=10
timeframe=2
trade_side=BOTH
exit_trades_threshold_points=15
supertrend_length=7
supertrend_factor=2
```

`lot_size` is the actual order quantity, as in the original bot. The two Supertrend fields are optional and default to 7 and 2. Existing instruments automatically use those defaults. `timeframe` is a positive integer in minutes. `trade_side` accepts BOTH, BUY or SELL.

Update selected fields with POST to `tradinginstruments/updatetradinginstruments`, including `instrument_token`. Removed strategy fields are no longer accepted by the update API. Settings load when the handler starts; restart the bot to apply changes. The existing stop endpoint also stops the hosting process/container.

## Candle history and timing

Startup fetches up to 30 days of broker minute candles, aggregates them into the configured timeframe, and combines them with saved Supertrend candles. The calculation continues across session boundaries; it does not reset every morning. Historical candles initialize the indicator and never place historical trades. The first startup/restart entry waits for the next live candle completion.

Equity candles align to 09:15 Asia/Kolkata, including custom timeframes such as 2 minutes. Other exchanges retain a 09:00 anchor. Equity entries stop at 15:30; other entries stop at 23:00, retaining the former general commodity cutoff. Candle closure is detected on the first subsequent tick, so execution can be delayed on inactive instruments. A boundary tick belongs to the new candle, never the candle just closed. Reconnection refreshes missing history before resuming.

History is saved as `<token>_<timeframe>_minute_candles.json`; startup no longer deletes it. Legacy candle files are replaced with broker-derived history because their old boundary handling could contaminate OHLC. Newly listed instruments without enough historical bars fail startup explicitly. Broker historical-data access is required. Initial seed length and data-feed differences can cause differences from a chart with a longer history.

## Orders and profit target

Orders use MARKET, MIS, regular variety and 10 percent market protection. Existing open MIS positions and daily filled orders for each configured exchange/symbol are included, including manual orders. Closed P/L and mark-to-market P/L are divided by configured quantity to express points, then summed across the group. Brokerage and other charges are excluded. No first-loss adjustment or partial target exists.

Broker reconciliation is limited to once per second while ticks arrive. Pending orders block duplicate submissions. Reversals wait for confirmed fills. Failed/cancelled orders and ambiguous submission errors block that instrument for broker review rather than automatically repeating orders. `supertrend_trading_state.json` persists pending orders, blocked instruments and reached daily targets across restart. To recover a blocked instrument, stop the bot, inspect/reconcile broker orders and positions, then remove its token from the state's `blocked` list before restarting. Do not delete state while orders are pending. Keep history and state on persistent storage when using Docker.

## Setup and checks

```shell
pip install -r requirements.txt
python manage.py runserver
python manage.py test algotraderapp
```

Set `api_key`, `api_secret`, and optionally `access_token` in `.env`. MongoDB connection settings are in `algotraderapp/product_setting.py`. Login through `generate_login_link` and `generate_session` (or the callback), configure instruments, then POST `access_web_socket`.

Automated tests use mocked broker calls; no live broker orders are submitted during tests.

Supertrend band rules follow TradingView's documented calculation:
https://www.tradingview.com/support/solutions/43000634738-supertrend/
