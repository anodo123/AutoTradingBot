from django.urls import path
from . import views  # Import your views


urlpatterns = [
    # API endpoints for trading instruments
    path('tradinginstruments/addtradinginstruments', views.add_trading_instrument, name='add_trading_instrument'),  # Add Trading Instrument
    path('tradinginstruments/viewaddedtradinginstruments', views.view_added_trading_instrument, name='view_added_trading_instrument'),  # View Added Trading Instrument
    path('tradinginstruments/updatetradinginstruments', views.update_trading_instrument, name='update_trading_instrument'),  # Update Trading Instrument
    path('generate_login_link',views.generate_login_link,name = 'generate_login_link'),
    path('generate_session',views.generate_session,name = 'generate_session'),
    path('access_web_socket',views.access_web_socket,name = 'access_web_socket'),
    path('stop_web_socket',views.stop_web_socket,name = 'stop_web_socket'),
    path('download_all_instruments',views.download_all_instruments,name = 'download_all_instruments'),
    path('delete_added_trading_instrument',views.delete_added_trading_instrument,name = 'delete_added_trading_instrument'),
    path('callback',views.callback,name = 'callback'),
    path('check_login_status',views.check_login_status,name = 'check_login_status'),
    path('fetch_candle_data',views.fetch_candle_data,name = 'fetch_candle_data'),
    path('price-action/', views.price_action_dashboard, name='price_action_dashboard'),
    path('api/price-action/instruments/', views.price_action_instruments, name='price_action_instruments'),
    path('api/price-action/bricks/', views.fetch_price_action_data, name='fetch_price_action_data'),
]
