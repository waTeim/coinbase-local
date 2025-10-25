# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a Python FastAPI service that maintains real-time order books for Coinbase Advanced Trade products. It uses the official `coinbase-advanced-py` SDK to subscribe to WebSocket feeds (level2 and market_trades channels) and exposes HTTP endpoints for querying order book depth and market trade statistics.

## Running and Testing

### Running the service

```bash
# Install dependencies
pip install -r requirements.txt

# Run with CLI arguments
python -m src.main BTC-USD ETH-USD \
  --api-key ./coinbase-key.json \
  --api-secret ./coinbase-secret.pem \
  --port 8080

# Or with environment variables
export PRODUCTS="BTC-USD,ETH-USD"
export COINBASE_API_KEY_FILE=/path/to/coinbase-key.json
export COINBASE_API_SECRET_FILE=/path/to/coinbase-secret.pem
python -m src.main

# Or using uvicorn directly (requires config via env vars)
uvicorn src.server:create_app --factory --host 0.0.0.0 --port 8080
```

### Syntax checking

```bash
python -m compileall src
```

### Docker build

```bash
# Build image (uses make.env if present)
make build

# Or manually
docker build --platform linux/amd64 -t coinbase-local:latest .
```

## Architecture

### Core Components

The application follows a clean separation of concerns:

1. **`src/config.py`** - Configuration resolution layer
   - Handles CLI arguments, environment variables, TOML config files, and JSON credential files
   - Supports multiple credential input formats (direct key/secret, file paths, JSON bundles)
   - All credential paths are resolved relative to config file location when applicable
   - Use `AppConfig.from_sources()` when adding new configuration options

2. **`src/orderbook.py`** - Order book manager (`CoinbaseOrderBookManager`)
   - Core business logic for maintaining order books per product
   - Each product has a `ProductOrderBook` with separate `asks`/`bids` dicts keyed by price (Decimal)
   - All book mutations are protected by per-product `asyncio.Lock`
   - Handles REST snapshots (authenticated or public fallback) and WebSocket delta updates
   - Maintains a rolling buffer of recent market trades (configurable via `market_order_buffer`)
   - WebSocket reconnection is automatic with exponential backoff (1s → 60s max)

3. **`src/server.py`** - FastAPI application factory
   - Creates the FastAPI app and registers endpoints
   - Lifecycle hooks manage the order book manager startup/shutdown
   - Two main endpoints mirror original TypeScript contract:
     - `/api/orderBook/interval` - current depth snapshot with optional price aggregation
     - `/api/orderBook/marketOrders` - VWAP statistics since a sequence number
   - Health check at `/healthz` returns 503 until all books are ready

4. **`src/schemas.py`** - Pydantic response models
   - Defines the JSON contract for API responses
   - Price levels are tuples: `[price, size, numOrders]`

5. **`src/main.py`** - CLI entrypoint
   - Parses arguments, builds config, configures logging
   - Filters `/healthz` from access logs via `HealthzAccessFilter`
   - Runs uvicorn server with the FastAPI app

### State Management and Threading

- Order books use `asyncio.Lock` for all mutations; always acquire the lock before reading/writing book state
- WebSocket messages arrive on a background thread and are dispatched to the asyncio event loop via `call_soon_threadsafe`
- Each product has a `ready` event that is set once the book has both asks and bids populated
- Endpoints wait up to 30 seconds for a book to become ready before returning an error

### Price Aggregation

The `/api/orderBook/interval` endpoint supports price aggregation (bucketing):
- `aggregation=0` returns raw levels
- `aggregation>0` groups levels into fixed-size buckets, computing VWAP per bucket
- Asks round up to the next bucket; bids round down
- Empty buckets are synthesized with zero size
- Aggregation logic is in `CoinbaseOrderBookManager._aggregate_side()`

### Credential Handling

The config system supports flexible credential inputs in priority order:
1. Direct CLI args (`--api-key`, `--api-secret`)
2. File paths (`--api-key-file`, `--api-secret-file`)
3. Combined credentials file (`--api-credentials-file` pointing to JSON)
4. TOML config file under `[api]` section
5. Environment variables (`COINBASE_API_KEY`, `COINBASE_API_SECRET`, etc.)

JSON credential files are parsed for common key names: `id`, `key`, `keyName`, `name`, `keyId`, `privateKey`, `secret`.

## Development Notes

- All Decimal arithmetic uses precision 28 to match cryptocurrency precision requirements
- Sequence numbers from WebSocket are monotonically increasing and used to track market trade recency
- The service falls back to public REST endpoints if authentication fails, but WebSocket streams still require credentials
- When adding new WebSocket channels, extend `_process_ws_payload()` in `orderbook.py`
- The `chart/` and `auth/` directories contain ancillary tooling (chart generation, auth utilities)
