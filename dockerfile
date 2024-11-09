# Dockerfile for Django trading bot
FROM python:3.9-slim

WORKDIR /app
COPY . /app

RUN pip install --upgrade pip
RUN pip install -r requirements.txt  # Ensure you have a requirements.txt

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
