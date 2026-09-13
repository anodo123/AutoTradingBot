# Completed-brick trading bot

Django APIs configure instruments in MongoDB and authenticate with Zerodha Kite.
The `price_action_bot` branch trades completed fixed-size price bricks.

## Strategy

- The first tick anchors the price. Entry waits for the first newly completed brick.
- Continuation takes one brick size; reversal takes two brick sizes.
- BUY: green enters a long, red reversal closes it and leaves the bot flat.
- SELL: red enters a short, green reversal closes it and leaves the bot flat.
- BOTH: reversal closes the position, then opens the opposite position after confirmed exit.
- Same-colour continuation bricks never add to a position.
- `lot_size` is the exact order quantity, not a multiplier of exchange lot size.
- Orders use regular MARKET / MIS / DAY with 10% market protection (as in the previous trading branch).
- VWAP, percentage triggers, separate stop-loss, loss thresholds and per-trade profit exits are not used.

Required instrument configuration: `instrument_token`, `instrument_details` (broker symbol and exchange),
positive integer `lot_size`, positive finite `brick_size` (default 5), and `trade_side` (BUY/SELL/BOTH).
Legacy configuration fields are accepted but do not drive trading. Configuration changes take effect on a fresh run.

## Operation

Install `requirements.txt`, configure Kite credentials through the environment / `.env`, and configure
MongoDB in `algotraderapp/product_setting.py`. Run `python manage.py runserver`.
The existing login, instrument configuration, start and stop APIs live under `/algotraderapp/`.
The dashboard is `/algotraderapp/price-action/` and refreshes every second.

POST `/algotraderapp/access_web_socket` accepts multipart form-data or x-www-form-urlencoded:

```text
start_time=09:20:30
```

Raw JSON requests return HTTP 415; use Postman Body > form-data.

`start_time` must be a valid `HH:MM:SS` IST time earlier than `15:15:00`.
Omitting it defaults to `09:15:10`. Invalid values return HTTP 400 before any
startup or cleanup. The response includes the configured start/end time and timezone.
The WebSocket connects immediately, but bricks and trading wait for the selected time.
The first tick at or after that time anchors the price; the first completed brick triggers entry.
If the start time has already passed, collection begins immediately (before the cutoff).
An already-running session retains its schedule; changing it requires a fresh run.
Bricks are collected from the selected start inclusive to 15:15 exclusive IST. Raw received ticks are
logged even outside that window. The end of the window and the stop API do not square off positions.
The stop API also stops its Docker container, falling back to process termination if Docker stop fails,
because closing the WebSocket alone has been unreliable in deployment. Broker positions remain open.
A fresh run refuses configured instruments with
existing broker positions or nonterminal orders; reconcile those in the broker account first.

Orders are tracked through Kite order updates with order-history polling on received ticks as fallback.
Pending orders block duplicate submissions. Rejected/cancelled orders or uncertain placement responses
halt execution for that instrument; inspect the broker account and logs before restarting. Actual partial
fills are tracked. State is process-local: run a single application process and do not run a second bot
on the same configured instruments. Fresh runs start flat; network reconnects retain the current state.

## Logs and persistence

`bot_logs/session.log` records completed bricks, decision reasons, position state, exact order payloads,
order IDs, responses, fill updates, broker errors and WebSocket lifecycle events. Authentication credentials
are not included in order payload logging. Raw ticks are in `raw_ticks/*.jsonl` (100 MiB parts, 2 GiB total cap).
Brick history is stored as `<token>_price_action_bricks.json`.

An explicit fresh start clears prior bot logs, known legacy bot log files, raw tick files and brick history.
Automatic network reconnects do not clear logs. Unrelated user files are preserved.

## Verification

`python manage.py test algotraderapp`

Tests use mocked broker calls and temporary files; they do not submit live orders.


## Combined profit exit

The Add Instrument API requires form field `exit_trades_threshold_points`.
Missing, blank, nonnumeric, nonfinite or nonpositive values return HTTP 400 before any database or broker calls.
Existing records without a threshold retain brick-only behavior until updated. Numerically equal values
(e.g. `20` and `20.0`) group instruments together. Configuration applies on the next fresh run.

Confirmed order fills maintain entry cost and realized points locally. Each latest tick
updates unrealized points. The group sums realized plus unrealized points and, at or above
the threshold, freezes all entries for the run and submits exits for its positions.
Pending orders must settle before their remaining position can be flattened. Execution
errors/uncertain orders retain the existing halt and require broker reconciliation.

P/L is gross points, not currency and not multiplied by order quantity. Partial fills
contribute proportionally to configured quantity, using cumulative broker fill averages
without counting duplicate updates twice. Full-quantity trades contribute ordinary price
 differences. Charges are excluded. The ledger covers this run's fills only, not earlier
trades in the account; it resets on a fresh run and survives network reconnects in memory.

P/L checks call no broker APIs. Existing pending-order history polling and startup broker
reconciliation remain. Group checks use each instrument's latest available tick, including
outside the brick collection window while connected. Detailed P/L checks and exit decisions
are recorded in `bot_logs/session.log`.


### Separate exit audit

`bot_logs/exits.log` contains exit decisions and reasons (brick reversal or combined profit
target), instrument/symbol, triggering brick or group P/L snapshot, request payload, order ID,
broker updates, final fill result, and failures or waits. Each pending exit retains its original
reason even if later bricks arrive. These records also remain in `session.log`. Fresh runs
clear both files; automatic reconnects preserve them.


### First loss fallback target

Optional Add/Update Instrument form field: `first_loss_exit_threshold_points`.
Example: `exit_trades_threshold_points=15`, `first_loss_exit_threshold_points=5`.
The first fully closed losing position in the group switches its target from 15 to 5,
once per run. Unrealized losses and partially closed positions do not trigger it.
Earlier realized P/L stays in the total: after -4 points, +9 points are needed to reach +5.
A profitable/breakeven exit does not trigger the switch; the first later losing exit does.
All instruments in the original group keep their membership even after the target changes.
Groups originally configured with 5 remain separate. A group already closed for profit stays closed.

The fallback must be finite, positive and below the initial target. Blank/missing preserves
the existing target. All instruments sharing an initial target must have the same fallback
(or all omit it); inconsistent groups are rejected at startup. Updates apply next fresh run.
The switch and reason are recorded in both `session.log` and `exits.log` as
`first_loss_threshold_switch`, with original/new target, loss points and group members.
A fresh run restores the initial target; reconnects preserve the current target.
