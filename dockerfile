# Use a lightweight Python image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt /app/

# Upgrade pip and install dependencies
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the rest of the application files
COPY . /app

# Expose the Gunicorn server port
EXPOSE 8000

# Ensure old logs are handled cleanly
RUN [ -e server.log ] && rm server.log || echo "No server.log to remove"

# Set the default command to run the application with Gunicorn
CMD ["python", "manage.py", "runserver","0.0.0.0:8000"]
