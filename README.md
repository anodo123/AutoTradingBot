# **Trading Automation System with KiteConnect**

This repository contains a Django-based Python application for algorithmic trading. It uses the **KiteConnect API** to process real-time market data and manage trades effectively. The application is designed to operate autonomously, integrating advanced strategies, order management, and profit/loss tracking. It includes robust logging and error handling for reliable performance in live trading environments.

---

## **Key Features**

### 1. **Real-time Market Data Processing**
- Utilizes **KiteConnect's WebSocket API (`KiteTicker`)** for real-time tick data streaming.
- Processes tick data for a list of specified instruments, allowing efficient trade execution.
- Includes the `WebSocketHandler` class for:
  - Establishing and managing WebSocket connections.
  - Reconnecting in case of network issues or connection drops.
  - Handling and processing received tick data.

### 2. **Candle Aggregation**
- Implements the `CandleAggregator` class to:
  - Aggregate tick data into **OHLC (Open-High-Low-Close)** candles at configurable time intervals.
  - Save aggregated candles to JSON files, ensuring session persistence and traceability.
  - Support multiple instruments, each with its dedicated candle aggregator.

### 3. **Trading Strategy Execution**
- Analyzes aggregated candle data to generate **buy/sell signals**.
- Allows customizable trading strategies, including:
  - Percentage-based calculations for entry/exit signals.
  - Custom trailing stop-loss updates to lock in profits dynamically.
  - Reverse order mechanisms for adapting to market trends after a stop-loss is hit.
- Supports **user-defined trade sides**:
  - `"BOTH"`: Enables bi-directional trading with reverse orders.
  - `"BUY"`: Restricts trading to BUY positions only.
  - `"SELL"`: Restricts trading to SELL positions only.

### 4. **Order Management**
- Integrates order placement via the **KiteConnect API** with support for:
  - Market orders.
  - Stop-loss and trailing stop-loss updates.
  - Reverse orders based on stop-loss triggers (when `trade_side` is set to `"BOTH"`).
- Tracks active orders to prevent duplicate executions and ensure controlled trading.

### 5. **Profit and Loss Calculation**
- Computes both **realized** and **unrealized** profits/losses for the trading day.
- Monitors cumulative profit/loss and stops trading when a predefined threshold is reached.
- Includes extensive logging for profit/loss calculations to ensure transparency and debugging ease.
- Includes a feature to calculate cumulative profit across multiple instruments based on user-defined exit thresholds.
  
### 6. **Error Handling and Logging**
- Implements structured error handling for:
  - WebSocket errors and reconnection logic.
  - Data validation and processing errors (e.g., missing fields in tick data).
  - Order placement and strategy evaluation issues.
- Comprehensive logging for:
  - WebSocket connection events.
  - Tick data processing and candle updates.
  - Strategy execution and order placements.
  - Profit/loss tracking and daily trade closures.

---

## **Architecture**

### **Main Components**
1. **`CandleAggregator`:**
   - Handles tick data aggregation and candle management.
   - Implements trading strategies and stop-loss logic.

2. **`WebSocketHandler`:**
   - Manages WebSocket connections for streaming real-time tick data.
   - Handles reconnection logic and processes tick data using `CandleAggregator`.

### **External Dependencies**
- **KiteConnect**: For accessing market data, placing orders, and managing trades.
- **Redis**: (Optional) For caching or state management, though not actively used in the provided script.
- **Python Libraries**: Standard and third-party libraries for computations, file handling, logging, and asynchronous operations.

---

## **Getting Started**

### **Prerequisites**
- Python 3.x installed.
- Django framework set up in your environment.
- API key and access token for KiteConnect.
- Optional: Redis server for advanced caching.

### **Installation**
1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/trading-automation.git
   cd no_indicator_bot
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure your API credentials in the appropriate settings file.
4. Run the Django application:
   ```bash
   python manage.py runserver
   ```

---

## **Usage**
1. Define your trading instruments, time intervals, and strategies in the configuration using APIs.
2. Start the WebSocket connection to receive real-time tick data.
3. Monitor logs for detailed trade activity, including profit/loss updates, order placements, and stop-loss adjustments.

---

## **Advantages**
- **Automated Trading:** Executes trades autonomously, reducing manual effort.
- **Customizable Strategies:** Easily adapt strategies to changing market conditions.
- **Real-time Data Processing:** Leverages real-time data for timely trade decisions.
- **Robust Logging and Debugging:** Tracks all key events for transparency and troubleshooting.

---

## **Contributing**
Feel free to fork this repository, submit issues, or create pull requests for new features or bug fixes.

### **Probable Enhancement: Using Redis for Candle Management**
The current implementation uses JSON files to store and read candle data. While functional, this approach may introduce latency in scenarios with high-frequency data or when processing large datasets. 

**Proposed Enhancement**:
- **Redis Integration**: Replace JSON-based storage with Redis to manage candle data.
- **Benefits**:
  1. **Faster Reads and Writes**: Redis operates in-memory, providing significantly faster operations compared to file-based storage.
  2. **Scalability**: Handles higher volumes of tick data efficiently, making it suitable for high-frequency trading scenarios.
  3. **Reduced I/O Overhead**: Minimizes disk I/O operations, enhancing overall performance.
  4. **Data Persistence Options**: Redis offers configurable persistence for long-term storage needs.
  
**Implementation Steps**:
1. Use Redis Hashes to store OHLC candles for each instrument.
   - Key: `instrument_token:timeframe`
   - Fields: `open`, `high`, `low`, `close`, `volume`, etc.
2. Modify the `CandleAggregator` class to interact with Redis instead of JSON.
3. Utilize Redis pipelines or transactions to handle bulk updates efficiently.
4. Incorporate expiry mechanisms (e.g., time-to-live for keys) to clean up outdated data automatically.

This enhancement would align the application with real-time trading demands and make it more robust for live trading environments.

---
## **License**
This project is licensed under the [MIT License](LICENSE).
