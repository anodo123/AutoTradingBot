import json
import os
from channels.generic.websocket import WebsocketConsumer
from kiteconnect import KiteTicker
import asyncio

class ZerodhaWebSocketConsumer(WebsocketConsumer):
    async def connect(self):
        # Accept WebSocket connection
        self.accept()

        # Initialize Zerodha KiteTicker
        self.api_key = os.getenv("api_key")
        self.access_token = os.getenv('access_token')

        # Set up KiteTicker instance
        self.ticker = KiteTicker(self.api_key, self.access_token)

        # Assign the callbacks
        self.ticker.on_ticks = self.on_ticks  # Callback to receive live market data
        self.ticker.on_connect = self.on_connect  # Callback when connection is established
        self.ticker.on_close = self.on_close  # Callback when connection is closed

        # Subscribe to instruments (replace with actual instrument tokens)
        instrument_tokens = [1292]  # Replace with actual instrument tokens
        self.ticker.subscribe(instrument_tokens)

        # Connect to Zerodha WebSocket
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.ticker.connect)

    def on_ticks(self, ws, ticks):
        """
        Called when Zerodha sends tick data.
        """
        # Log ticks to console for debugging
        print("Received ticks:", ticks)

        # Use asyncio to send data back to WebSocket client
        asyncio.run(self.send(text_data=json.dumps({
            "ticks": ticks  # Forward the tick data
        })))

    def on_connect(self, ws, response):
        """
        Called when Zerodha WebSocket connection is established.
        """
        print("WebSocket connection established:", response)

    def on_close(self, ws, code, reason):
        """
        Called when Zerodha WebSocket connection is closed.
        """
        print("WebSocket closed:", code, reason)

    async def disconnect(self, close_code):
        # Close the WebSocket connection
        await self.close()

        # Close Zerodha WebSocket connection
        if self.ticker:
            self.ticker.close()
