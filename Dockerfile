# Use Python 3.9 slim image as the base
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy requirements file first for better Docker caching
COPY requirements.txt ./

# Install dependencies
RUN pip install -r requirements.txt

# Copy the Python files
COPY api_key_manager.py proxy_server.py ./

# Expose port 8080
EXPOSE 8080

# Run the proxy server
CMD ["python", "proxy_server.py"]