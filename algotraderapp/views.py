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
# Global variable to hold the WebSocket handler
ws_handler = None
ws_lock = threading.Lock()

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

        if access_token not in [None, ""]:
            with ws_lock:
                # Check if WebSocket handler is already running
                if ws_handler is None:
                    instrument_details = view_all_added_trading_instrument()
                    ws_handler = run_script.WebSocketHandler(kite, instrument_details)
                    threading.Thread(target=ws_handler.run_websocket).start()

            return JsonResponse({"access_token": access_token})

        return JsonResponse({"Some Error Occurred": True}, status=500)

    except Exception as error:
        return JsonResponse({"Some Error Occurred": str(error)}, status=500)
@api_view(['POST'])
def download_all_instruments(request):
    try:
            # Fetch all instruments
        instruments = kite.instruments()

        # Convert the instruments list to a DataFrame
        instruments_df = pd.DataFrame(instruments)
        # Get the current date and time for the filename
        current_datetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
        trade_calculation_percentage= request.POST['trade_calculation_percentage']
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
            "trade_calculation_percentage":trade_calculation_percentage,
            "instrument_details":instrument_details
        })
        return JsonResponse({
            "lot_size":lot_size,
            "instrument_token":instrument_token,
            "exit_trades_threshold_points":exit_trades_threshold_points,
            "trade_calculation_percentage":trade_calculation_percentage,
            "instrument_details":instrument_details,
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
        old_data['timeofaction'] = str(datetime.datetime.now())
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
            if key not in ["lot_size","instrument_token","exit_trades_threshold_points","trade_calculation_percentage"]:
                return JsonResponse({"Invalid Parameter":key})
            else:
                if key =="instrument_token":
                    continue
                data[key]=value
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        tradeconfigurationlog_collection = database['tradeconfigurationlog']
        old_data = collection.find_one({"instrument_token":instrument_token})
        old_data['old_id'] = str(old_data['_id'])
        old_data['action'] = 'updation'
        old_data['timeofaction'] = str(datetime.datetime.now())
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
    

def view_all_added_trading_instrument():
    try:
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        return list(collection.find({},{"_id":0}))
    except Exception as error:
        return []