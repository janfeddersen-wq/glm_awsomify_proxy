import os
import json
import asyncio
import aiohttp
from aiohttp import web
import logging
from typing import Dict, Any, List
from datetime import datetime
from pathlib import Path
import uuid
import copy

from api_key_manager import ApiKeyManager
from incoming_key_manager import IncomingKeyManager


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Target API host
TARGET_API_HOST = "https://api.cerebras.ai/v1/"

# Alternative API hosts for large requests (>120k tokens)
SYNTHETIC_API_HOST = "https://api.synthetic.new/openai/v1/"
SYNTHETIC_MODEL = "hf:zai-org/GLM-4.6"
SYNTHETIC_VISION_MODEL = "hf:Qwen/Qwen3-VL-235B-A22B-Instruct"
ZAI_API_HOST = "https://api.z.ai/api/coding/paas/v4/"
ZAI_MODEL = "glm-4.6"

# Token estimation threshold based on Content-Length header
# From empirical analysis: 4.7 bytes/token average
# 120k tokens * 4.7 = 564,000 bytes (~550 KB)
TOKEN_THRESHOLD = 120000  # 120k tokens
BYTES_PER_TOKEN = 4.7
CONTENT_LENGTH_THRESHOLD = int(TOKEN_THRESHOLD * BYTES_PER_TOKEN)  # 564,000 bytes

# Error codes that trigger key rotation
ROTATE_KEY_ERROR_CODES = {429, 500}

# Request/Response logging configuration
LOG_REQUESTS_ENABLED = os.environ.get("LOG_REQUESTS", "true").lower() == "true"
LOG_DIR = os.environ.get("LOG_DIR", "./logs")


class ProxyServer:
    """
    A proxy server that forwards requests to the Cerebras API
    with round-robin API key rotation.
    """
    def __init__(self, api_key_manager: ApiKeyManager, incoming_key_manager: IncomingKeyManager = None,
                 synthetic_api_key: str = None, zai_api_key: str = None, fallback_on_cooldown: bool = False):
        self.api_key_manager = api_key_manager
        self.incoming_key_manager = incoming_key_manager
        self.synthetic_api_key = synthetic_api_key
        self.zai_api_key = zai_api_key
        self.fallback_on_cooldown = fallback_on_cooldown
        self.app = web.Application()
        # Add status endpoint
        self.app.router.add_get("/_status", self.status_handler)
        # Add the catch-all route (must be last)
        self.app.router.add_route("*", "/{path:.*}", self.proxy_handler)

        # Create logs directory if logging is enabled
        if LOG_REQUESTS_ENABLED:
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            logger.info(f"Request/Response logging enabled. Logs will be saved to: {LOG_DIR}")

    def _sanitize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Sanitize headers by removing sensitive information like API keys.
        """
        sanitized = dict(headers)
        # Remove or mask authorization headers
        if 'Authorization' in sanitized:
            sanitized['Authorization'] = '[REDACTED]'
        if 'authorization' in sanitized:
            sanitized['authorization'] = '[REDACTED]'
        return sanitized

    def _fix_missing_tool_responses(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates and fixes messages array to ensure all tool_calls have corresponding tool responses.
        If a tool_call is missing its response, injects a fake "failed" response.
        """
        # Only process chat completion requests with messages
        if 'messages' not in request_data or not isinstance(request_data['messages'], list):
            return request_data

        messages: List[Dict[str, Any]] = request_data['messages']
        fixed_messages: List[Dict[str, Any]] = []
        pending_tool_calls: List[str] = []  # Track tool_calls waiting for responses

        for i, msg in enumerate(messages):
            # Make a copy of the message to avoid modifying original
            msg_copy = copy.deepcopy(msg)

            # Check if this message has tool_calls
            if msg_copy.get('role') == 'assistant' and 'tool_calls' in msg_copy:
                # Add this message
                fixed_messages.append(msg_copy)
                # Track all tool_call IDs that need responses
                for tool_call in msg_copy.get('tool_calls', []):
                    if 'id' in tool_call:
                        pending_tool_calls.append(tool_call['id'])
                continue

            # Check if this is a tool response
            if msg_copy.get('role') == 'tool' and 'tool_call_id' in msg_copy:
                # Remove this tool_call_id from pending
                tool_call_id = msg_copy['tool_call_id']
                if tool_call_id in pending_tool_calls:
                    pending_tool_calls.remove(tool_call_id)
                fixed_messages.append(msg_copy)
                continue

            # If we have pending tool_calls and this is NOT a tool response,
            # inject fake responses for all pending tool_calls
            if pending_tool_calls:
                logger.warning(f"Found {len(pending_tool_calls)} tool_calls without responses. Injecting fake 'failed' responses.")
                for tool_call_id in pending_tool_calls:
                    fake_response = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "failed"
                    }
                    fixed_messages.append(fake_response)
                    logger.info(f"Injected fake tool response for tool_call_id: {tool_call_id}")
                pending_tool_calls.clear()

            # Add the current message
            fixed_messages.append(msg_copy)

        # Handle any remaining pending tool_calls at the end
        if pending_tool_calls:
            logger.warning(f"Found {len(pending_tool_calls)} tool_calls without responses at end of messages. Injecting fake 'failed' responses.")
            for tool_call_id in pending_tool_calls:
                fake_response = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "failed"
                }
                fixed_messages.append(fake_response)
                logger.info(f"Injected fake tool response for tool_call_id: {tool_call_id}")

        # Create a deep copy of request_data and update messages
        fixed_request_data = copy.deepcopy(request_data)
        fixed_request_data['messages'] = fixed_messages
        return fixed_request_data

    def _has_image_content(self, request_data: Dict[str, Any]) -> bool:
        """
        Check if the request contains image content in any message.

        Images can be present in the OpenAI-style format where message content
        is an array containing objects with type "image_url".

        Returns:
            True if any message contains image content, False otherwise
        """
        messages = request_data.get('messages', [])
        if not isinstance(messages, list):
            return False

        for msg in messages:
            content = msg.get('content')
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'image_url':
                        return True
        return False

    async def _route_to_alternative_api(
        self,
        request_data: Dict[str, Any],
        path: str,
        method: str,
        original_headers: Dict[str, str],
        start_time: datetime,
        original_request_body: bytes,
        override_model: str = None
    ) -> web.Response:
        """
        Route large requests to alternative APIs with fallback logic.
        First tries Synthetic API, then falls back to Z.ai API if that fails.

        Args:
            override_model: Optional model to use instead of SYNTHETIC_MODEL (e.g., for vision requests)
        """
        # Prepare modified request data with model change
        synthetic_model = override_model if override_model else SYNTHETIC_MODEL
        synthetic_request_data = copy.deepcopy(request_data)
        if 'model' in synthetic_request_data:
            synthetic_request_data['model'] = synthetic_model

        zai_request_data = copy.deepcopy(request_data)
        if 'model' in zai_request_data:
            zai_request_data['model'] = ZAI_MODEL

        # Try Synthetic API first
        if self.synthetic_api_key:
            logger.info(f"Routing request to Synthetic API with model: {synthetic_model}")
            try:
                synthetic_url = f"{SYNTHETIC_API_HOST}{path}"
                synthetic_body = json.dumps(synthetic_request_data, separators=(',', ':')).encode('utf-8')

                headers = {key: value for key, value in original_headers.items()
                          if key.lower() not in ('authorization', 'host', 'content-length')}
                headers["Authorization"] = f"Bearer {self.synthetic_api_key}"
                headers["User-Agent"] = "Cerebras-Proxy/1.0"
                headers["Content-Length"] = str(len(synthetic_body))

                async with aiohttp.ClientSession() as session:
                    async with session.request(method, synthetic_url, headers=headers, data=synthetic_body) as resp:
                        body = await resp.read()

                        # If successful, return immediately
                        if resp.status < 400:
                            logger.info(f"Synthetic API request succeeded with status {resp.status}")
                            response = web.Response(
                                status=resp.status,
                                body=body,
                                headers={key: value for key, value in resp.headers.items()
                                        if key.lower() not in ('content-length', 'transfer-encoding', 'content-encoding')}
                            )

                            # Log request/response
                            end_time = datetime.utcnow()
                            duration_ms = (end_time - start_time).total_seconds() * 1000
                            await self._save_request_response_log(
                                request_method=method,
                                request_path=f"[SYNTHETIC] {path}",
                                request_headers=original_headers,
                                request_body=original_request_body,
                                response_status=resp.status,
                                response_headers=dict(resp.headers),
                                response_body=body,
                                duration_ms=duration_ms
                            )
                            return response
                        else:
                            logger.warning(f"Synthetic API returned error {resp.status}, falling back to Z.ai API")
            except Exception as e:
                logger.warning(f"Synthetic API failed with error: {e}, falling back to Z.ai API")
        else:
            logger.warning("Synthetic API key not configured, skipping to Z.ai API")

        # Fallback to Z.ai API
        if self.zai_api_key:
            logger.info(f"Routing request to Z.ai API (fallback)")
            try:
                zai_url = f"{ZAI_API_HOST}{path}"
                zai_body = json.dumps(zai_request_data, separators=(',', ':')).encode('utf-8')

                headers = {key: value for key, value in original_headers.items()
                          if key.lower() not in ('authorization', 'host', 'content-length')}
                headers["Authorization"] = f"Bearer {self.zai_api_key}"
                headers["User-Agent"] = "Cerebras-Proxy/1.0"
                headers["Content-Length"] = str(len(zai_body))

                async with aiohttp.ClientSession() as session:
                    async with session.request(method, zai_url, headers=headers, data=zai_body) as resp:
                        body = await resp.read()

                        logger.info(f"Z.ai API request completed with status {resp.status}")
                        response = web.Response(
                            status=resp.status,
                            body=body,
                            headers={key: value for key, value in resp.headers.items()
                                    if key.lower() not in ('content-length', 'transfer-encoding', 'content-encoding')}
                        )

                        # Log request/response
                        end_time = datetime.utcnow()
                        duration_ms = (end_time - start_time).total_seconds() * 1000
                        await self._save_request_response_log(
                            request_method=method,
                            request_path=f"[ZAI] {path}",
                            request_headers=original_headers,
                            request_body=original_request_body,
                            response_status=resp.status,
                            response_headers=dict(resp.headers),
                            response_body=body,
                            duration_ms=duration_ms
                        )
                        return response
            except Exception as e:
                logger.error(f"Z.ai API failed with error: {e}")
                return web.Response(status=503, text=f"All alternative APIs failed: {e}")
        else:
            logger.error("Z.ai API key not configured")
            return web.Response(status=503, text="No alternative APIs configured")

    async def _save_request_response_log(
        self,
        request_method: str,
        request_path: str,
        request_headers: Dict[str, str],
        request_body: bytes,
        response_status: int,
        response_headers: Dict[str, str],
        response_body: bytes,
        duration_ms: float
    ):
        """
        Save request and response data to a JSON file in the logs directory.
        """
        if not LOG_REQUESTS_ENABLED:
            return

        try:
            # Create timestamp-based filename
            timestamp = datetime.utcnow()
            date_dir = timestamp.strftime("%Y-%m-%d")
            timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")
            request_id = str(uuid.uuid4())[:8]

            # Sanitize path for filename (replace / with _)
            safe_path = request_path.replace('/', '_').replace('\\', '_')[:50]
            filename = f"{timestamp_str}_{request_method}_{safe_path}_{request_id}.json"

            # Create date subdirectory
            log_dir = Path(LOG_DIR) / date_dir
            log_dir.mkdir(parents=True, exist_ok=True)

            # Decode body if possible
            def decode_body(body: bytes) -> Any:
                if not body:
                    return None
                try:
                    # First, try to parse as JSON
                    return json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    # Not JSON, try to decode as plain text (e.g., SSE streaming)
                    try:
                        return body.decode('utf-8')
                    except UnicodeDecodeError:
                        # Not valid UTF-8, store as base64 binary
                        import base64
                        return {"_binary": base64.b64encode(body).decode('ascii')}
                except UnicodeDecodeError:
                    # Not valid UTF-8, store as base64 binary
                    import base64
                    return {"_binary": base64.b64encode(body).decode('ascii')}

            # Create log entry
            log_entry = {
                "timestamp": timestamp.isoformat(),
                "request_id": request_id,
                "request": {
                    "method": request_method,
                    "path": request_path,
                    "headers": self._sanitize_headers(request_headers),
                    "body": decode_body(request_body)
                },
                "response": {
                    "status": response_status,
                    "headers": dict(response_headers),
                    "body": decode_body(response_body)
                },
                "duration_ms": duration_ms
            }

            # Write to file
            log_file = log_dir / filename
            with open(log_file, 'w') as f:
                json.dump(log_entry, f, indent=2)

            logger.debug(f"Saved request/response log to {log_file}")

        except Exception as e:
            logger.error(f"Failed to save request/response log: {e}")

    async def status_handler(self, request: web.Request) -> web.Response:
        """
        Returns the current status of all API keys.
        """
        status = await self.api_key_manager.get_status()
        return web.json_response(status)

    async def proxy_handler(self, request: web.Request) -> web.Response:
        """
        Handles all incoming requests, forwards them to the target API,
        and returns the response. Implements smart retry logic with key rotation.
        """
        start_time = datetime.utcnow()

        # Verify incoming API key if key management is enabled
        if self.incoming_key_manager:
            auth_header = request.headers.get('Authorization', '')
            if not auth_header:
                logger.warning("Request rejected: Missing Authorization header")
                return web.Response(
                    status=401,
                    text='{"error": {"message": "Missing Authorization header", "type": "invalid_request_error", "code": "missing_authorization"}}'
                )

            # Extract the API key from "Bearer <key>" format
            parts = auth_header.split()
            if len(parts) != 2 or parts[0].lower() != 'bearer':
                logger.warning("Request rejected: Invalid Authorization header format")
                return web.Response(
                    status=401,
                    text='{"error": {"message": "Invalid Authorization header format", "type": "invalid_request_error", "code": "invalid_authorization"}}'
                )

            incoming_api_key = parts[1]

            # Verify the API key
            if not self.incoming_key_manager.verify_api_key(incoming_api_key):
                logger.warning(f"Request rejected: Invalid or revoked API key: {incoming_api_key[:10]}...")
                return web.Response(
                    status=401,
                    text='{"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}'
                )

        path = request.match_info["path"]

        # Avoid /v1/v1 duplication if the request path already includes v1/
        if path.startswith("v1/"):
            path = path[3:]  # Remove the "v1/" prefix

        target_url = f"{TARGET_API_HOST}{path}"

        # Check Content-Length header for early routing decision (before reading body)
        content_length = request.headers.get('Content-Length')
        if content_length and 'chat/completions' in path:
            try:
                content_length_int = int(content_length)
                estimated_tokens = int(content_length_int / BYTES_PER_TOKEN)

                if content_length_int > CONTENT_LENGTH_THRESHOLD:
                    logger.info(f"Request size ({content_length_int:,} bytes, ~{estimated_tokens:,} tokens) exceeds threshold ({CONTENT_LENGTH_THRESHOLD:,} bytes, {TOKEN_THRESHOLD:,} tokens)")

                    # Read body and parse for routing
                    request_body = await request.read()
                    try:
                        request_data = json.loads(request_body.decode('utf-8'))
                        logger.info(f"Routing large request to alternative APIs")
                        return await self._route_to_alternative_api(
                            request_data=request_data,
                            path=path,
                            method=request.method,
                            original_headers=dict(request.headers),
                            start_time=start_time,
                            original_request_body=request_body
                        )
                    except json.JSONDecodeError:
                        logger.warning("Large request is not valid JSON, continuing with Cerebras")
            except (ValueError, TypeError):
                pass  # Invalid Content-Length, continue normally

        # Read request body once for both forwarding and logging
        request_body = await request.read()
        original_request_body = request_body

        # Apply tool_call validation fix for chat completion requests (ALWAYS ENABLED)
        request_data_for_routing = None
        if 'chat/completions' in path and request_body:
            try:
                request_data = json.loads(request_body.decode('utf-8'))
                original_msg_count = len(request_data.get('messages', []))

                fixed_request_data = self._fix_missing_tool_responses(request_data)
                fixed_msg_count = len(fixed_request_data.get('messages', []))

                # Only use the fixed body if we actually made changes
                if fixed_msg_count > original_msg_count:
                    # Serialize with compact separators to match original formatting
                    fixed_body = json.dumps(fixed_request_data, separators=(',', ':')).encode('utf-8')

                    # Validate the serialized JSON
                    test_parse = json.loads(fixed_body.decode('utf-8'))
                    if not isinstance(test_parse, dict):
                        raise ValueError("Serialized data is not a valid JSON object")

                    request_body = fixed_body
                    logger.info(f"Applied tool_call fix: {original_msg_count} -> {fixed_msg_count} messages (size: {len(original_request_body)} -> {len(fixed_body)} bytes)")
                    request_data_for_routing = fixed_request_data
                else:
                    request_data_for_routing = request_data
            except json.JSONDecodeError:
                # Not JSON, continue with normal routing
                pass
            except Exception as e:
                logger.error(f"Tool call fix failed: {e}", exc_info=True)
                request_body = original_request_body

        # Get headers AFTER body modification, excluding Authorization, Host, and Content-Length
        # Content-Length must be recalculated to match the (possibly modified) body
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in ('authorization', 'host', 'content-length')}
        headers["User-Agent"] = "Cerebras-Proxy/1.0"

        # Set correct Content-Length for the (possibly modified) body
        if request_body:
            headers["Content-Length"] = str(len(request_body))

        logger.info(f"Processing request to {target_url}")

        # Check if request contains images and route to vision model
        if request_data_for_routing and self._has_image_content(request_data_for_routing):
            if self.synthetic_api_key:
                logger.info("Image content detected, routing to Synthetic API with vision model")
                return await self._route_to_alternative_api(
                    request_data=request_data_for_routing,
                    path=path,
                    method=request.method,
                    original_headers=dict(request.headers),
                    start_time=start_time,
                    original_request_body=original_request_body,
                    override_model=SYNTHETIC_VISION_MODEL
                )
            else:
                logger.warning("Image content detected but Synthetic API key not configured")

        # Check if all Cerebras keys are rate-limited and fallback is enabled
        if self.fallback_on_cooldown and await self.api_key_manager.all_keys_rate_limited():
            if self.synthetic_api_key or self.zai_api_key:
                logger.warning("All Cerebras keys are rate-limited. Falling back to alternative APIs.")
                # Parse request data for routing if not already done
                if request_data_for_routing is None and 'chat/completions' in path and request_body:
                    try:
                        request_data_for_routing = json.loads(request_body.decode('utf-8'))
                    except:
                        pass

                if request_data_for_routing:
                    return await self._route_to_alternative_api(
                        request_data=request_data_for_routing,
                        path=path,
                        method=request.method,
                        original_headers=dict(request.headers),
                        start_time=start_time,
                        original_request_body=original_request_body
                    )
            else:
                logger.warning("All Cerebras keys rate-limited but no alternative APIs configured")

        # Retry with automatic key rotation
        max_retries = self.api_key_manager.get_key_count() * 2  # Allow multiple passes through all keys

        for attempt in range(max_retries):
            # Get the current API key (will wait if all are rate-limited)
            api_key = await self.api_key_manager.get_current_key()
            headers["Authorization"] = f"Bearer {api_key}"

            try:
                # Use aiohttp client to make request
                async with aiohttp.ClientSession() as session:
                    # Prepare the request based on the method
                    method = request.method
                    if method in ("GET", "HEAD", "OPTIONS"):
                        async with session.request(method, target_url, headers=headers) as resp:
                            # Stream the response body back to the client
                            body = await resp.read()
                            # Create a new response with the target API's status and headers
                            response = web.Response(
                                status=resp.status,
                                body=body,
                                headers={key: value for key, value in resp.headers.items()
                                         if key.lower() not in ('content-length', 'transfer-encoding', 'content-encoding')}
                            )

                            # Handle rate limiting
                            if resp.status == 429:
                                logger.warning(f"Rate limited (429), marking key and switching...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)

                                # Check if all keys are now rate-limited and fallback is enabled
                                if self.fallback_on_cooldown and await self.api_key_manager.all_keys_rate_limited():
                                    if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                        logger.warning("All Cerebras keys now rate-limited after 429. Falling back to alternative APIs.")
                                        return await self._route_to_alternative_api(
                                            request_data=request_data_for_routing,
                                            path=path,
                                            method=method,
                                            original_headers=dict(request.headers),
                                            start_time=start_time,
                                            original_request_body=original_request_body
                                        )
                                continue
                            elif resp.status == 500:
                                logger.warning(f"Server error (500), trying next key...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)

                                # Check if all keys are now rate-limited and fallback is enabled
                                if self.fallback_on_cooldown and await self.api_key_manager.all_keys_rate_limited():
                                    if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                        logger.warning("All Cerebras keys now rate-limited after 500. Falling back to alternative APIs.")
                                        return await self._route_to_alternative_api(
                                            request_data=request_data_for_routing,
                                            path=path,
                                            method=method,
                                            original_headers=dict(request.headers),
                                            start_time=start_time,
                                            original_request_body=original_request_body
                                        )
                                continue
                            elif resp.status == 400:
                                # Check if this is a context_length_exceeded error
                                try:
                                    error_data = json.loads(body.decode('utf-8'))
                                    error_code = error_data.get('error', {}).get('code') or error_data.get('code')
                                    if error_code == 'context_length_exceeded':
                                        logger.warning(f"Context length exceeded (400), routing to alternative APIs")
                                        if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                            return await self._route_to_alternative_api(
                                                request_data=request_data_for_routing,
                                                path=path,
                                                method=method,
                                                original_headers=dict(request.headers),
                                                start_time=start_time,
                                                original_request_body=original_request_body
                                            )
                                except:
                                    pass
                                # If not context_length_exceeded or can't route, fall through to return the 400 error
                                logger.info(f"Request completed with status {resp.status}")
                            elif resp.status == 503:
                                # Service unavailable, route to alternative APIs if available
                                logger.warning(f"Service unavailable (503), routing to alternative APIs")
                                if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                    return await self._route_to_alternative_api(
                                        request_data=request_data_for_routing,
                                        path=path,
                                        method=method,
                                        original_headers=dict(request.headers),
                                        start_time=start_time,
                                        original_request_body=original_request_body
                                    )
                                # If can't route to alternative APIs, fall through to return the 503 error
                                logger.info(f"Request completed with status {resp.status}")
                            else:
                                # Success or non-retryable error
                                if resp.status < 400:
                                    # Check for embedded token quota error in response body
                                    try:
                                        response_data = json.loads(body.decode('utf-8'))
                                        choices = response_data.get('choices', [])
                                        if choices and len(choices) > 0:
                                            message_content = choices[0].get('message', {}).get('content', '')
                                            if 'token quota is not enough' in message_content:
                                                logger.warning("Detected embedded token quota error in response, routing to alternative APIs")
                                                if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                                    return await self._route_to_alternative_api(
                                                        request_data=request_data_for_routing,
                                                        path=path,
                                                        method=method,
                                                        original_headers=dict(request.headers),
                                                        start_time=start_time,
                                                        original_request_body=original_request_body
                                                    )
                                    except:
                                        pass  # Not JSON or parsing failed, continue normally

                                    await self.api_key_manager.mark_key_success(api_key)
                                logger.info(f"Request completed with status {resp.status}")

                                # Log request/response if enabled
                                end_time = datetime.utcnow()
                                duration_ms = (end_time - start_time).total_seconds() * 1000
                                await self._save_request_response_log(
                                    request_method=method,
                                    request_path=path,
                                    request_headers=dict(request.headers),
                                    request_body=original_request_body,
                                    response_status=resp.status,
                                    response_headers=dict(resp.headers),
                                    response_body=body,
                                    duration_ms=duration_ms
                                )

                                return response
                    else:
                        # For methods with bodies (POST, PUT, PATCH, DELETE)
                        # Use the fixed request body for forwarding
                        async with session.request(method, target_url, headers=headers,
                                                   data=request_body) as resp:
                            # Stream the response body back to the client
                            body = await resp.read()
                            response = web.Response(
                                status=resp.status,
                                body=body,
                                headers={key: value for key, value in resp.headers.items()
                                         if key.lower() not in ('content-length', 'transfer-encoding', 'content-encoding')}
                            )

                            # Handle rate limiting
                            if resp.status == 429:
                                logger.warning(f"Rate limited (429), marking key and switching...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)

                                # Check if all keys are now rate-limited and fallback is enabled
                                if self.fallback_on_cooldown and await self.api_key_manager.all_keys_rate_limited():
                                    if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                        logger.warning("All Cerebras keys now rate-limited after 429. Falling back to alternative APIs.")
                                        return await self._route_to_alternative_api(
                                            request_data=request_data_for_routing,
                                            path=path,
                                            method=method,
                                            original_headers=dict(request.headers),
                                            start_time=start_time,
                                            original_request_body=original_request_body
                                        )
                                continue
                            elif resp.status == 500:
                                logger.warning(f"Server error (500), trying next key...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)

                                # Check if all keys are now rate-limited and fallback is enabled
                                if self.fallback_on_cooldown and await self.api_key_manager.all_keys_rate_limited():
                                    if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                        logger.warning("All Cerebras keys now rate-limited after 500. Falling back to alternative APIs.")
                                        return await self._route_to_alternative_api(
                                            request_data=request_data_for_routing,
                                            path=path,
                                            method=method,
                                            original_headers=dict(request.headers),
                                            start_time=start_time,
                                            original_request_body=original_request_body
                                        )
                                continue
                            elif resp.status == 400:
                                # Check if this is a context_length_exceeded error
                                try:
                                    error_data = json.loads(body.decode('utf-8'))
                                    error_code = error_data.get('error', {}).get('code') or error_data.get('code')
                                    if error_code == 'context_length_exceeded':
                                        logger.warning(f"Context length exceeded (400), routing to alternative APIs")
                                        if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                            return await self._route_to_alternative_api(
                                                request_data=request_data_for_routing,
                                                path=path,
                                                method=method,
                                                original_headers=dict(request.headers),
                                                start_time=start_time,
                                                original_request_body=original_request_body
                                            )
                                except:
                                    pass
                                # If not context_length_exceeded or can't route, fall through to return the 400 error
                                logger.info(f"Request completed with status {resp.status}")
                            elif resp.status == 503:
                                # Service unavailable, route to alternative APIs if available
                                logger.warning(f"Service unavailable (503), routing to alternative APIs")
                                if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                    return await self._route_to_alternative_api(
                                        request_data=request_data_for_routing,
                                        path=path,
                                        method=method,
                                        original_headers=dict(request.headers),
                                        start_time=start_time,
                                        original_request_body=original_request_body
                                    )
                                # If can't route to alternative APIs, fall through to return the 503 error
                                logger.info(f"Request completed with status {resp.status}")
                            else:
                                # Success or non-retryable error
                                if resp.status < 400:
                                    # Check for embedded token quota error in response body
                                    try:
                                        response_data = json.loads(body.decode('utf-8'))
                                        choices = response_data.get('choices', [])
                                        if choices and len(choices) > 0:
                                            message_content = choices[0].get('message', {}).get('content', '')
                                            if 'token quota is not enough' in message_content:
                                                logger.warning("Detected embedded token quota error in response, routing to alternative APIs")
                                                if (self.synthetic_api_key or self.zai_api_key) and request_data_for_routing:
                                                    return await self._route_to_alternative_api(
                                                        request_data=request_data_for_routing,
                                                        path=path,
                                                        method=method,
                                                        original_headers=dict(request.headers),
                                                        start_time=start_time,
                                                        original_request_body=original_request_body
                                                    )
                                    except:
                                        pass  # Not JSON or parsing failed, continue normally

                                    await self.api_key_manager.mark_key_success(api_key)
                                logger.info(f"Request completed with status {resp.status}")

                                # Log request/response if enabled
                                end_time = datetime.utcnow()
                                duration_ms = (end_time - start_time).total_seconds() * 1000
                                await self._save_request_response_log(
                                    request_method=method,
                                    request_path=path,
                                    request_headers=dict(request.headers),
                                    request_body=original_request_body,
                                    response_status=resp.status,
                                    response_headers=dict(resp.headers),
                                    response_body=body,
                                    duration_ms=duration_ms
                                )

                                return response

            except aiohttp.ClientError as e:
                logger.error(f"Client error on attempt {attempt + 1}: {e}")
                # For client errors, try the next key
                await self.api_key_manager.mark_key_rate_limited(api_key)
                continue
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt + 1}: {e}")
                # Return a 500 error to the client for unexpected issues
                return web.Response(status=500, text=f"Proxy error: {e}")

        # If we get here, all attempts failed
        logger.error("Maximum retry attempts exceeded.")
        return web.Response(status=503, text="Service unavailable: Maximum retries exceeded.")
        
    async def run(self, host: str = "0.0.0.0", port: int = 8080):
        """
        Starts the proxy server using the existing event loop.
        """
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        
        # Keep the server running
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour, effectively running forever


# Example startup logic to load API keys from environment variable
# This would normally be handled by a configuration management system
# but is included here for demonstration purposes
async def main():
    """
    Main entry point for the proxy server.
    """
    logger.info("Starting main() function...")

    # Get the API keys from the environment variable
    api_keys_json = os.environ.get("CEREBRAS_API_KEYS", "{}")
    logger.info(f"Retrieved API keys JSON: {repr(api_keys_json)}")

    try:
        api_keys: Dict[str, str] = json.loads(api_keys_json)
        logger.info(f"Successfully parsed {len(api_keys)} API keys")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for API keys: {e}")
        logger.error(f"Raw API keys value: {repr(api_keys_json)}")
        return

    # Get optional cooldown configuration
    cooldown_seconds = int(os.environ.get("CEREBRAS_COOLDOWN", "60"))
    logger.info(f"Cooldown period set to {cooldown_seconds} seconds")

    # Create the API key manager
    api_key_manager = ApiKeyManager(api_keys, cooldown_seconds=cooldown_seconds)
    logger.info("Created API key manager successfully")

    # Get alternative API keys for large requests
    synthetic_api_key = os.environ.get("SYNTHETIC_API_KEY")
    zai_api_key = os.environ.get("ZAI_API_KEY")

    if synthetic_api_key:
        logger.info("Synthetic API key configured for large requests (>120k tokens)")
    if zai_api_key:
        logger.info("Z.ai API key configured as fallback for large requests")

    # Create incoming API key manager (optional)
    incoming_key_manager = None
    enable_incoming_auth = os.environ.get("ENABLE_INCOMING_AUTH", "false").lower() == "true"
    if enable_incoming_auth:
        incoming_key_db = os.environ.get("INCOMING_KEY_DB", "./data/incoming_keys.db")
        incoming_key_manager = IncomingKeyManager(incoming_key_db)
        logger.info(f"Incoming API key authentication enabled. Database: {incoming_key_db}")
    else:
        logger.info("Incoming API key authentication disabled (set ENABLE_INCOMING_AUTH=true to enable)")

    # Get fallback on cooldown configuration
    fallback_on_cooldown = os.environ.get("FALLBACK_ON_COOLDOWN", "false").lower() == "true"
    if fallback_on_cooldown:
        if synthetic_api_key or zai_api_key:
            logger.info("Fallback on cooldown enabled: will route to alternative APIs when all Cerebras keys are rate-limited")
        else:
            logger.warning("Fallback on cooldown enabled but no alternative APIs configured")

    # Create and run the proxy server
    proxy = ProxyServer(
        api_key_manager,
        incoming_key_manager=incoming_key_manager,
        synthetic_api_key=synthetic_api_key,
        zai_api_key=zai_api_key,
        fallback_on_cooldown=fallback_on_cooldown
    )
    logger.info("About to call proxy.run() with proper event loop integration")
    await proxy.run()


if __name__ == "__main__":
    try:
        # Try to get the current event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # If no loop is running, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Run the main function in the appropriate event loop
    loop.run_until_complete(main())