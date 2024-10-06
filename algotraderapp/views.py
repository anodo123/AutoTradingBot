from django.shortcuts import render
from .product_setting import mongo_port, mongo_url,mongo_username,mongo_password,mongo_database
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.http import JsonResponse
from django.http import HttpResponse
from pymongo import MongoClient
import json
# View to add an item
@api_view(['POST'])
def add_trading_instrument(request):
    try:
        lot_size = request.POST['lot_size']
        instrument_token = request.POST['instrument_token']
        exit_trades_threshold_points= request.POST['exit_trades_threshold_points']
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}")
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        existing_document = collection.find_one({"instrument_token":instrument_token},{"_id":0})
        if existing_document:
            return JsonResponse({"Existing Instrument Found with Following Details, Please Update using Update API":existing_document})
        result = collection.insert_one({
            "lot_size":lot_size,
            "instrument_token":instrument_token,
            "exit_trades_threshold_points":exit_trades_threshold_points,
        })
        return JsonResponse({"insertion_id":str(result.inserted_id)})
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
    

# View to update an item
@api_view(['POST'])
def update_trading_instrument(request):
    try:
        data = json.loads(request.POST['data'])
        instrument_token = request.POST['instrument_token']
        client = MongoClient(f"mongodb://{mongo_username}:{mongo_password}@{mongo_url}:{mongo_port}/")
        for key,value in data.items():
            if key not in ["lot_size","instrument_token","exit_trades_threshold_points"]:
                return JsonResponse({"Invalid Parameter":key})
        database = client[mongo_database]  # Access the database
        collection = database['tradeconfiguration']  # Replace 'mycollection' with your collection name
        result = collection.update_one({"instrument_token":instrument_token},{"$set":data})
        return JsonResponse({"document_modified":result.modified_count,"instrument_token":instrument_token})
    except Exception as error:
        return JsonResponse({"Some Error Occured":True},status = 500)
    
