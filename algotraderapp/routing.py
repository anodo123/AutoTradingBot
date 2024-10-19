from django.urls import path
from algotraderapp.consumers import ZerodhaWebSocketConsumer  # Import your consumers

websocket_urlpatterns = [
    path('ws/zerodhaendpoint/',  ZerodhaWebSocketConsumer.as_asgi()),  # Update the path and consumer
]
