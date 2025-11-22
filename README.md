# GLM Awesomify Proxy

A smart proxy server for Cerebras API with intelligent key rotation, request routing, and API key management.

## Features

- 🔄 **Smart API Key Rotation** - Automatic rotation on rate limits (429) with cooldown tracking
- 🚀 **Strategic Routing** - Routes large requests (>120k tokens) to alternative APIs (Synthetic/Z.ai)
- ⚡ **Fallback on Cooldown** - Routes to alternative APIs when all Cerebras keys are rate-limited
- 🔧 **Smart Error Handling** - Auto-retries with alternative APIs on 400/503 errors from Cerebras
- 🔐 **Incoming API Key Management** - SQLite-based authentication for client requests
- 🛠️ **Auto Tool Call Validation** - Fixes missing tool responses automatically
- 📝 **Request/Response Logging** - Optional filesystem logging for debugging
- 📊 **Status Monitoring** - Built-in `/_status` endpoint

## Quick Start

### Using Docker Compose (Recommended)

1. Clone and configure:
```bash
git clone git@github.com:janfeddersen-wq/glm_awsomify_proxy.git
cd glm_awsomify_proxy
cp .env.example .env
```

2. Edit `.env` with your Cerebras API keys:
```bash
CEREBRAS_API_KEYS={"key1":"sk-xxx","key2":"sk-yyy"}
```

3. Start the proxy:
```bash
docker-compose up -d
```

The proxy runs at `http://localhost:18080`

### Local Installation

```bash
pip install -r requirements.txt
export CEREBRAS_API_KEYS='{"key1":"sk-xxx","key2":"sk-yyy"}'
python proxy_server.py
```

## Incoming API Key Management

Protect your proxy with client authentication using SQLite-based API keys.

### Enable Authentication

Set in `.env`:
```bash
ENABLE_INCOMING_AUTH=true
```

### Manage API Keys

```bash
# Add a new client API key
python manage_keys.py add "Client Name"
# Output: sk-abc123... (give this to your client)

# List all API keys with usage stats
python manage_keys.py list

# Revoke an API key
python manage_keys.py revoke sk-abc123...

# View statistics
python manage_keys.py stats
```

### Using with Docker

```bash
# Add key
docker-compose exec cerebras-proxy python manage_keys.py add "Client Name"

# List keys
docker-compose exec cerebras-proxy python manage_keys.py list
```

### Client Usage

Clients must include the API key in requests:
```bash
curl -X POST http://localhost:18080/chat/completions \
  -H "Authorization: Bearer sk-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.3-70b","messages":[...]}'
```

## Strategic Routing for Large Requests

Requests with user/system message content >120k tokens (~204k characters) are automatically routed to alternative APIs:

**Token Estimation:** Uses empirically-determined approximation of 1 token ≈ 1.7 characters based on actual Cerebras API usage data.

1. **Primary**: Synthetic API (`api.synthetic.new`) - Model: `hf:zai-org/GLM-4.6`
2. **Fallback**: Z.ai API (`api.z.ai`) - Model: `glm-4.6`

### Configure Alternative APIs

Set in `.env`:
```bash
SYNTHETIC_API_KEY=sk-your-synthetic-key
ZAI_API_KEY=sk-your-zai-key
```

Normal-sized requests continue using Cerebras API.

## Fallback on Cooldown

When all Cerebras API keys are rate-limited, enable automatic fallback to alternative APIs instead of waiting for cooldown.

### Enable Fallback

Set in `.env`:
```bash
FALLBACK_ON_COOLDOWN=true
SYNTHETIC_API_KEY=sk-your-synthetic-key
ZAI_API_KEY=sk-your-zai-key
```

### How It Works

**Without Fallback (default):**
- All Cerebras keys hit rate limit → Wait 60s for cooldown → Retry

**With Fallback enabled:**
- All Cerebras keys hit rate limit → Route to Synthetic API → Falls back to Z.ai if needed → Instant response ⚡

**Use Case:** During high-traffic periods when all Cerebras keys are exhausted, this provides faster response times by utilizing alternative APIs instead of waiting.

## Smart Error Handling

The proxy automatically routes to alternative APIs when Cerebras encounters certain errors, providing seamless failover without manual intervention.

### Supported Error Types

**400 Context Length Exceeded:**
- Cerebras returns: `{"code": "context_length_exceeded", "message": "...Current length is 132032 while limit is 131072"}`
- Action: Automatically route to Synthetic API → Falls back to Z.ai if needed
- Benefit: Seamlessly uses higher-capacity alternative APIs when requests exceed Cerebras's context window

**503 Service Unavailable:**
- Cerebras returns: 503 (service temporarily unavailable)
- Action: Automatically route to Synthetic API → Falls back to Z.ai if needed
- Benefit: Maintains availability during Cerebras downtime or maintenance

**Requirements:** `SYNTHETIC_API_KEY` and/or `ZAI_API_KEY` must be configured for error handling to work.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CEREBRAS_API_KEYS` | *required* | JSON object with Cerebras API keys |
| `CEREBRAS_COOLDOWN` | `60` | Cooldown seconds after rate limiting |
| `ENABLE_INCOMING_AUTH` | `false` | Enable client API key authentication |
| `INCOMING_KEY_DB` | `./data/incoming_keys.db` | SQLite database path |
| `SYNTHETIC_API_KEY` | - | API key for Synthetic API |
| `ZAI_API_KEY` | - | API key for Z.ai API |
| `FALLBACK_ON_COOLDOWN` | `false` | Route to alternative APIs when all Cerebras keys are rate-limited |
| `LOG_REQUESTS` | `true` | Enable request/response logging |
| `LOG_DIR` | `./logs` | Directory for log files |

### File Persistence

Docker volumes automatically persist data:
- `./logs/` - Request/response logs
- `./data/` - SQLite database for API keys

## How It Works

### Smart Key Rotation

1. Sticks with one Cerebras API key until rate limited (429) or error (500)
2. Automatically switches to next available key
3. Tracks cooldown periods (default 60s)
4. Waits for available key instead of failing

### Request Flow

```
Client Request
    ↓
[Verify Incoming API Key] (if ENABLE_INCOMING_AUTH=true)
    ↓
[Estimate Token Count from Message Content]
    ↓
> 120k tokens? → Route to Synthetic API → Fails? → Route to Z.ai API
    ↓
< 120k tokens? → [Check if all Cerebras keys rate-limited]
    ↓                                    ↓
    ↓                    Yes + FALLBACK_ON_COOLDOWN=true?
    ↓                                    ↓
    ↓                         Route to Synthetic/Z.ai API
    ↓
    ↓  No or disabled → Route to Cerebras API (with smart rotation/wait)
    ↓                                    ↓
    ↓                         Returns 400 context_length_exceeded or 503?
    ↓                                    ↓
    ↓                         Route to Synthetic/Z.ai API
    ↓
[Fix Tool Calls if needed]
    ↓
[Log Request/Response] (if LOG_REQUESTS=true)
    ↓
Return to Client
```

## Monitoring

Check proxy status:
```bash
curl http://localhost:18080/_status
```

Response:
```json
{
  "keys": [
    {
      "name": "key1",
      "available": true,
      "rate_limited_for": 0,
      "error_count": 0
    }
  ],
  "current_key": "key1"
}
```

## API Key Database Schema

The SQLite database tracks:
- `api_key` - The client API key
- `name` - Descriptive name
- `created_at` - Creation timestamp
- `revoked` - Revoked status
- `last_used_at` - Last request timestamp
- `request_count` - Total requests made

## Example Usage

### Without Authentication
```bash
curl -X POST http://localhost:18080/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### With Authentication
```bash
# 1. Create client API key
python manage_keys.py add "Production Client"
# Output: sk-abc123...

# 2. Client uses the key
curl -X POST http://localhost:18080/chat/completions \
  -H "Authorization: Bearer sk-abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Troubleshooting

### Docker container won't start
```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Database file not created
The database is auto-created on first use of `manage_keys.py`. Ensure the `./data/` directory has write permissions.

### Logs not persisting
Check that `./logs/` directory exists and is writable. Verify `LOG_REQUESTS=true` in `.env`.

## License

MIT License
