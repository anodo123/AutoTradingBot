"""
ASGI config for Algotrader project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from algotraderapp import routing  # Replace 'your_app' with the actual app name where routing.py is located

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Algotrader.settings')

# Define the ASGI application
application = ProtocolTypeRouter({
    "http": get_asgi_application(),  # Handle HTTP requests
    "websocket":URLRouter(
            routing.websocket_urlpatterns  # Add your WebSocket URL patterns
        )
})
