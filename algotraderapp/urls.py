from django.urls import path
from . import views  # Import your views


urlpatterns = [
    # API endpoints for trading instruments
    path('tradinginstruments/addtradinginstruments', views.add_trading_instrument, name='add_trading_instrument'),  # Add Trading Instrument
    path('tradinginstruments/viewaddedtradinginstruments', views.view_added_trading_instrument, name='view_added_trading_instrument'),  # View Added Trading Instrument
    path('tradinginstruments/updatetradinginstruments', views.update_trading_instrument, name='update_trading_instrument'),  # Update Trading Instrument
]
