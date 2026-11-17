from .supertrend import validate_config
from django.shortcuts import render
from .product_setting import mongo_port, mongo_url,mongo_username,mongo_password,mongo_database
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.http import JsonResponse
from django.http import HttpResponse
from pymongo import MongoClient
import json
from kiteconnect import KiteConnect
import requests
import os
import pandas as pd
import datetime
from algotraderapp.consumers import ZerodhaWebSocketConsumer 
import threading
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from . import run_script
from zoneinfo import ZoneInfo
import logging
from django.http import JsonResponse
from rest_framework.decorators import api_view
import subprocess

# Global variable to hold the WebSocket handler
ws_handler = None
ws_lock = threading.Lock()

login_flag = False

env_path = Path('./.env')
load_dotenv(dotenv_path=env_path)
kite = KiteConnect(api_key=os.getenv("api_key"))
# Initialize Redis client using Django setting
# View to add an item
# Function to start the WebSocket connection
def start_websocket():
    consumer = ZerodhaWebSocketConsumer()
    asyncio.run(consumer.connect())  # Start WebSocket connection using asyncio

@api_view(['POST'])
def generate_login_link(request):
    try:
        # kite = KiteConnect(api_key=os.getenv("api_key"))
        # print(os.getenv("api_key"))
        return JsonResponse({"login_url": kite.login_url()})
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)
    
@api_view(['POST'])
def generate_session(request):
    try:
        request_token = request.POST['request_token']
        # Assuming you have initialized KiteConnect instance with api_key
        # Generate session using the request token and secret
        data = kite.generate_session(request_token, api_secret=os.getenv("api_secret"))
        #access_token = os.getenv('access_token')  # Change to access the real token from generated session response
        access_token = data['access_token']

        if access_token not in [None, ""]:
            # Temporarily setting an access token (replace with real access_token logic)
            os.environ['access_token'] =access_token
            kite.set_access_token(access_token)  # Set access token in KiteConnect
            global login_flag
            login_flag = True
            # # Step 4: Start Zerodha WebSocket in a separate thread after setting the access token
            # websocket_thread = threading.Thread(target=run_script.run_websocket(access_token))
            # websocket_thread.start()

            return JsonResponse({"access_token": access_token})
        
        return JsonResponse({"Some Error Occurred": True}, status=500)

    except Exception as error:
        return JsonResponse({"Some Error Occurred": str(error)}, status=500)
    

@api_view(['POST'])
def access_web_socket(request):
    global ws_handler
    try:
        access_token = os.getenv('access_token')
        kite.set_access_token(access_token)
        # existing_orders = kite.orders()
        # for order in existing_orders:
        #     print(order)
        # return JsonResponse({"existing_orders": existing_orders})
        if access_token not in [None, ""]:
            with ws_lock:
                # Check if WebSocket handler is already running
                if ws_handler is None:
                    instrument_details = view_all_added_trading_instrument()
                    handler = run_script.WebSocketHandler(kite, instrument_details)
                    handler.run_websocket()
                    ws_handler = handler
                else:
                    return JsonResponse({"Websocket Already Running": True})            
            return JsonResponse({"Websocket Started": True,"access_token": access_token})
        else:
            return JsonResponse({"Session Not Started, Please Generate Session":True},status = status.HTTP_412_PRECONDITION_FAILED)
    except Exception as error:
        return JsonResponse({"Some Error Occurred": str(error)}, status=500)
    

@api_view(['POST'])
def stop_web_socket(request):
    try:
        global ws_handler

        logger = logging.getLogger("stop_web_socket")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            log_file_path = os.path.join(os.getcwd(), "stop_web_socket.log")
            file_handler = logging.FileHandler(log_file_path)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        logger.info("Received request to stop WebSocket.")

        with ws_lock:
            if ws_handler is None:
                logger.warning("WebSocket handler is None. No active WebSocket to stop.")
                return JsonResponse(
                    {"status": "error", "message": "WebSocket is not initialized or already stopped."},
                    status=400
                )

            try:
                if ws_handler.is_running():
                    ws_handler.stop_websocket()
                    logger.info("WebSocket stopped successfully.")
                else:
                    logger.warning("WebSocket is not running.")
                
                ws_handler = None
                logger.info("WebSocket handler cleared.")
                
                # Stop the container using Docker CLI
                logger.info("Stopping the Docker container.")
                container_id = os.getenv("HOSTNAME")  # Gets the container ID
                try:
                    subprocess.run(["docker", "stop", container_id], check=True)
                except Exception as error:
                    os._exit(1)

                return JsonResponse({"status": "success", "message": "WebSocket and container stopped successfully."})
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to stop the Docker container: {e}")
                return JsonResponse(
                    {"status": "error", "message": "Failed to stop the Docker container.", "details": str(e)},
                    status=500
                )
            except Exception as error:
                logger.error(f"Failed to stop WebSocket: {error}")
                return JsonResponse(
                    {"status": "error", "message": "Failed to stop WebSocket.", "details": str(error)},
                    status=500
                )
    except Exception as error:
        logger = logging.getLogger("stop_web_socket")
        logger.critical(f"Unhandled exception occurred: {error}")
        return JsonResponse({"status": "error", "message": "An unexpected error occurred.", "details": str(error)}, status=500)




@api_view(['POST'])
def download_all_instruments(request):
    try:
        # Fetch all instruments
        instruments = kite.instruments()

        # Convert the instruments list to a DataFrame
        instruments_df = pd.DataFrame(instruments)
        # Get the current date and time for the filename
        current_datetime = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")
        csv_filename = f'zerodha_instruments_{current_datetime}.csv'

        # Create a HttpResponse object with the appropriate CSV headers
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={csv_filename}'

        # Write the CSV data to the response
        instruments_df.to_csv(path_or_buf=response, index=False)
        return response
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)


@api_view(['POST'])
def add_trading_instrument(request):
    try:
        lot_size = request.POST['lot_size']
        instrument_token = request.POST['instrument_token']
        exit_trades_threshold_points= request.POST['exit_trades_threshold_points']
        timeframe = request.POST['timeframe']
        trade_side = request.POST.get('trade_side', 'BOTH')
        try:
            config = validate_config(dict(lot_size=lot_size, timeframe=timeframe,
                trade_side=trade_side, exit_trades_threshold_points=exit_trades_threshold_points,
                supertrend_length=request.POST.get('supertrend_length', 7),
                supertrend_factor=request.POST.get('supertrend_factor', 2)))
        except (ValueError, TypeError) as error:
            return JsonResponse({'error': str(error)}, status=400)
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        existing_document = collection.find_one({"instrument_token":instrument_token},{"_id":0})
        # Fetch the full instruments list
        if existing_document:
            return JsonResponse({"Existing Instrument Found with Following Details, Please Update using Update API":existing_document})
        instruments = kite.instruments()
        instrument_details = {}
        for single_dict in instruments:
            if single_dict['instrument_token'] == int(instrument_token):
                instrument_details = single_dict
        if not instrument_details:
            return HttpResponse("No Instrument token {} exists".format(instrument_token))
        instrument_details['expiry'] = str(instrument_details['expiry'])
        result = collection.insert_one({
            "lot_size":lot_size,
            "instrument_token":instrument_token,
            "exit_trades_threshold_points":exit_trades_threshold_points,
            "timeframe":timeframe,
            "instrument_details":instrument_details,
            "supertrend_length":config["supertrend_length"],
            "supertrend_factor":config["supertrend_factor"],
            "trade_side":config["trade_side"]
        })
        return JsonResponse({
            "lot_size":lot_size,
            "instrument_token":instrument_token,
            "exit_trades_threshold_points":exit_trades_threshold_points,
            "timeframe":timeframe,
            "instrument_details":instrument_details,
            "supertrend_length":config["supertrend_length"],
            "supertrend_factor":config["supertrend_factor"],
            "trade_side":config["trade_side"],
            "insertion_id":str(result.inserted_id)})
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)
    


@api_view(['POST'])
def view_added_trading_instrument(request):
    try:
        instrument_token = request.POST.get('instrument_token',"")
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        if instrument_token!="":
            existing_document = collection.find_one({"instrument_token":instrument_token},{"_id":0})
            if not existing_document:
                return HttpResponse(f"No Instrument Found with {instrument_token} instrument_token",status.HTTP_204_NO_CONTENT)
            return JsonResponse(existing_document)
        return JsonResponse(list(collection.find({},{"_id":0})),safe = False)
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)
    

@api_view(['POST'])
def delete_added_trading_instrument(request):
    try:
        instrument_token = request.POST.get('instrument_token',"")
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        if instrument_token!="":
            existing_document = collection.find_one({"instrument_token":instrument_token},{"_id":0})
            if not existing_document:
                return HttpResponse(f"No Instrument Found with {instrument_token} instrument_token",status.HTTP_204_NO_CONTENT)
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        tradeconfigurationlog_collection = database['tradeconfigurationlog']
        old_data = collection.find_one({"instrument_token":instrument_token})
        old_data['old_id'] = str(old_data['_id'])
        old_data['action'] = 'deletion'
        old_data['timeofaction'] = str(datetime.datetime.now(ZoneInfo("Asia/Kolkata")))
        del old_data['_id']
        tradeconfigurationlog_collection.insert_one(old_data)
        if old_data["instrument_token"]:
            result = collection.delete_one({"instrument_token":instrument_token})
        del old_data['instrument_details']
        del old_data['_id']
        del old_data['old_id']
        return JsonResponse({"instrument_deleted":result.acknowledged,
                            "instrument_token":instrument_token,
                            "deleted_data":old_data})
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)
    

# View to update an item
@api_view(['POST'])
def update_trading_instrument(request):
    try:
        instrument_token = request.POST['instrument_token']
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        data = {}
        for key,value in request.POST.items():
            if key not in ["supertrend_length","supertrend_factor","lot_size","instrument_token","exit_trades_threshold_points","timeframe","trade_side"]:
                return JsonResponse({"Invalid Parameter":key})
            else:
                if key =="instrument_token":
                    continue
                data[key]=value
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        tradeconfigurationlog_collection = database['tradeconfigurationlog']
        old_data = collection.find_one({"instrument_token":instrument_token})
        if old_data is None:
            return JsonResponse({'error': 'Instrument not found'}, status=404)
        try:
            validated = validate_config({**old_data, **data})
            data = {key: validated[key] for key in data}
        except (ValueError, TypeError) as error:
            return JsonResponse({'error': str(error)}, status=400)
        if not data:
            return JsonResponse({"error": "No configuration fields supplied"}, status=400)
        old_data['old_id'] = str(old_data['_id'])
        old_data['action'] = 'updation'
        old_data['timeofaction'] = str(datetime.datetime.now(ZoneInfo("Asia/Kolkata")))
        del old_data['_id']
        tradeconfigurationlog_collection.insert_one(old_data)
        if data:
            result = collection.update_one({"instrument_token":instrument_token},{"$set":data})
            updated_data = collection.find_one({"instrument_token":instrument_token},{"_id":0})
        del updated_data['instrument_details']
        del old_data['instrument_details']
        del old_data['_id']
        del old_data['old_id']
        del old_data['action']
        del old_data['timeofaction']
        return JsonResponse({"document_modified":result.modified_count,
                            "instrument_token":instrument_token,
                            "updated_data":updated_data,
                            "old_data":old_data})
    except Exception as error:
        return JsonResponse({"Some Error Occured":str(error)},status = 500)




# View to update an item
@api_view(['GET'])
def callback(request):
    try:
        request_token = request.GET['request_token']
        print(request_token)
        # Assuming you have initialized KiteConnect instance with api_key
        # Generate session using the request token and secret
        data = kite.generate_session(request_token, api_secret=os.getenv("api_secret"))
        #access_token = os.getenv('access_token')  # Change to access the real token from generated session response
        access_token = data['access_token']

        if access_token not in [None, ""]:
            # Temporarily setting an access token (replace with real access_token logic)
            os.environ['access_token'] =access_token
            kite.set_access_token(access_token)  # Set access token in KiteConnect
            global login_flag
            login_flag = True
            return JsonResponse({"access_token": access_token,"login Successfull":True})
        
        return JsonResponse({"Some Error Occurred": True}, status=500)
    except Exception as error:
        return JsonResponse({"Some Error Occurred": str(error)}, status=500)


# check current login status
@api_view(['GET'])    
def check_login_status(request):
    try:
        return JsonResponse({"current_login_status":login_flag})
    except Exception as error:
        return JsonResponse({"current_login_status":False})


@api_view(['POST'])
def fetch_candle_data(request):
    try:
        # Extract parameters
        instrument_token = request.POST.get('instrumentToken')
        interval_minutes = request.POST.get('timeframe')

        # Validate input
        if not instrument_token or not interval_minutes:
            return JsonResponse({"error": "instrument_token and timeframe are required"}, status=400)

        # Construct the file path
        candle_file_path = f"{instrument_token}_{interval_minutes}_minute_candles.json"

        # Check if the file exists
        if not os.path.exists(candle_file_path):
            return JsonResponse({"file_exists": False})

        # Load the JSON data from the file
        with open(candle_file_path, 'r') as file:
            candle_data = json.load(file)

        return JsonResponse({"file_exists": True, "candle_data": candle_data})

    except FileNotFoundError:
        return JsonResponse({"error": "Candle data file not found"}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Error decoding JSON file"}, status=500)
    except Exception as error:
        return JsonResponse({"error": str(error)}, status=500)


def view_all_added_trading_instrument():
    try:
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        return list(collection.find({},{"_id":0}))
    except Exception as error:
        return []
