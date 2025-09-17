# Cerebras API Proxy Server

This is a Python-based proxy server designed to forward requests to the Cerebras API while managing multiple API keys through a round-robin rotation mechanism. It handles rate limiting (429) and server errors (500) by automatically rotating keys and retrying requests.

## Features

- **API Key Management**: Accepts a JSON configuration of API keys and manages them for round-robin rotation.
- **Request Forwarding**: Forwards all incoming HTTP requests to the Cerebras API endpoint (`https://api.cerebras.ai/v1/`).
- **Dynamic Authorization**: Injects the `Authorization: Bearer <api_key>` header dynamically for each request using the current key in rotation.
- **Error Handling & Retry**: Automatically rotates keys and retries requests on receiving `429 Too Many Requests` or `500 Internal Server Error` responses.
- **Concurrency Support**: Built with `aiohttp` to efficiently handle multiple concurrent requests with thread-safe key rotation.

## Requirements

- Python 3.7 or higher
- `aiohttp` library
- `Brotli` library (required for handling Brotli-compressed responses from Cerebras API)

Install the required libraries:
```bash
pip install aiohttp Brotli
```

Or install from requirements.txt:
```bash
pip install -r requirements.txt
```

## Configuration

The proxy server expects a JSON string containing your Cerebras API keys to be provided via the `CEREBRAS_API_KEYS` environment variable.

Example JSON format:
```json
{
  "key1": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "key2": "sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
  "key3": "sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
}
```

## Usage

1. Set the `CEREBRAS_API_KEYS` environment variable with your JSON configuration:
   ```bash
   export CEREBRAS_API_KEYS='{"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy","key3":"sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"}'
   ```

2. Run the proxy server:
   ```bash
   python proxy_server.py
   ```
   
   By default, the server will start on `127.0.0.1:8080`. You can modify the host and port by adjusting the parameters in the `run()` method call within `proxy_server.py`.

## Docker Setup

This project includes Docker configuration for easy deployment.

### Using Docker

1. Build the Docker image:
   ```bash
   docker build -t cerebras-proxy .
   ```

2. Run the container with your API keys:
   ```bash
   docker run -p 18080:8080 -e CEREBRAS_API_KEYS='{"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"}' cerebras-proxy
   ```

### Using Docker Compose

1. Create a `.env` file in the project root with your API keys:
   ```bash
   CEREBRAS_API_KEYS={"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"}
   ```

2. Build and run with Docker Compose:
   ```bash
   docker-compose up --build
   ```

The proxy will be available at `http://localhost:18080`.

3. Make requests to the proxy server as you would to the Cerebras API, but use the proxy's address instead:
   
   For example, if the Cerebras API endpoint is:
   ```
   POST https://api.cerebras.ai/v1/chat/completions
   ```
   
   You would send your request to:
   ```
   POST http://127.0.0.1:8080/chat/completions
   ```
   
   The proxy will handle adding the appropriate `Authorization` header.

## Architecture

This proxy server is built using the `aiohttp` framework for high performance and concurrency. It consists of two main components:

1.  **`ApiKeyManager` (`api_key_manager.py`)**: Manages the pool of API keys, rotating through them in a round-robin fashion. It uses an `asyncio.Lock` to ensure that key rotation is thread-safe when handling multiple concurrent requests.
2.  **`ProxyServer` (`proxy_server.py`)**: The main application that listens for HTTP requests. It has a catch-all route that forwards requests to the Cerebras API. It integrates with the `ApiKeyManager` to retrieve keys for the `Authorization` header and implements a retry loop for handling specific error responses.

## Error Handling

- If a request to the Cerebras API fails with a `429` or `500` status code, the proxy will rotate to the next available API key and retry the original request. This process is repeated until a successful response is received or all keys have been tried.
- If all keys are exhausted (i.e., all have returned a `429` or `500` error) for a specific request, the proxy will return a `503 Service Unavailable` response to the client.
- If the request to the Cerebras API fails with any other error code, or if there's a network or client error during the proxy process itself, the appropriate error response will be returned to the client (often a `502 Bad Gateway` or `500 Internal Server Error`).

## License

This project is licensed under the MIT License - see the LICENSE file for details.