# Coinbase Advanced Trade Order Book (Python)

This repository provides a Python-first implementation of a lightweight order book
and market-trade service backed by the official
[`coinbase-advanced-py`](https://pypi.org/project/coinbase-advanced-py/)
SDK. The FastAPI application subscribes to Coinbase Advanced Trade level2 and
market trade channels, keeps an in-memory depth book per product, and exposes
simple HTTP endpoints for UI widgets and analytics.

The previous TypeScript prototype and other historical artifacts now live under
`./archived/`; the `./src/` package is the canonical code base.

## Key Features

- **Official SDK integration** – Both REST snapshots and WebSocket streams use
  Coinbase’s maintained Python client, including automatic JWT handling and
  reconnection support.
- **Real-time order book** – Maintains per-product depth with optional price
  aggregation and a rolling buffer of recent fills.
- **FastAPI service** – Exposes two JSON endpoints compatible with the original
  TypeScript contract:
  - `GET /api/orderBook/interval` – returns the current depth snapshot at the
    requested aggregation and depth.
  - `GET /api/orderBook/marketOrders` – returns VWAP-style statistics for recent
    market orders since a sequence number.
- **Credential flexibility** – Supports direct CLI arguments, JSON key files, or
  environment variables for API credentials.
- **Graceful fallbacks** – Falls back to public REST endpoints if authenticated
  snapshots are unavailable, while still streaming via WebSockets.

## Repository Layout

```
├─ archived/            # Legacy implementations (TypeScript, prototypes, etc.)
├─ requirements.txt     # Python dependencies
└─ src/                 # Canonical FastAPI + Coinbase SDK service
   ├─ config.py         # CLI/env configuration resolver
   ├─ main.py           # CLI entrypoint (`python -m src.main`)
   ├─ orderbook.py      # Order book manager (REST + WebSocket integration)
   ├─ schemas.py        # Pydantic response models
   └─ server.py         # FastAPI application factory
```

## Prerequisites

- Python 3.11 or newer (the SDK ships wheels for 3.10+; the devcontainer uses 3.11).
- Coinbase Advanced Trade API key with **view** permissions (and any required
  portfolio scopes). Download the JSON key bundle or copy the API key name and
  private key PEM.

### Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate            # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running the Service

You can configure credentials via CLI flags, environment variables, or files:

- `--api-key` / `COINBASE_API_KEY` – Either the full API key name
  (`organizations/{org}/apiKeys/{key}`) **or** a path to a JSON key bundle.
- `--api-secret` / `COINBASE_API_SECRET` – The EC private key PEM or a path to a
  PEM file. `COINBASE_API_SECRET_FILE` is also supported.
- `--port` / `PORT` – Listening port (defaults to `63200`).
- `--products` CLI arguments or `PRODUCTS` env var – One or more product IDs
  (defaults to the CLI list; env var may use comma, colon, or space separators).

Example using explicit files:

```bash
python -m src.main BTC-USD ETH-USD \
  --api-key ./coinbase-key.json \
  --api-secret ./coinbase-secret.pem \
  --port 8080
```

Or with environment variables:

```bash
export PRODUCTS="BTC-USD,ETH-USD"
export COINBASE_API_KEY_FILE=/path/to/coinbase-key.json
export COINBASE_API_SECRET_FILE=/path/to/coinbase-secret.pem
python -m src.main
```

When the service starts you should see log lines similar to:

```
INFO  Snapshot primed for BTC-USD …
INFO  Connecting to Coinbase Advanced Trade websocket …
```

Once the WebSocket subscriptions settle, the FastAPI app is available at
`http://localhost:<port>`.

## HTTP API

| Endpoint | Description | Query Parameters |
|----------|-------------|------------------|
| `GET /api/orderBook/interval` | Returns depth snapshot for a product. | `product` (required), `aggregation` (default `0`), `depth` (default `50`, max `500`). |
| `GET /api/orderBook/marketOrders` | Returns VWAP, total size, and count of recent market orders since a sequence number. | `product` (required), `since` (optional sequence; omit for a zeroed response). |

Example:

```bash
curl "http://localhost:63200/api/orderBook/interval?product=BTC-USD&aggregation=0&depth=10"
curl "http://localhost:63200/api/orderBook/marketOrders?product=BTC-USD&since=0"
```

## Development Tips

- The order book manager lives in `src/orderbook.py`. It keeps product state
  behind `asyncio.Lock`s, so any new endpoints should respect those locks before
  mutating internal structures.
- `src/config.py` normalizes credential inputs; reuse it if you add CLI commands
  or background workers.
- To add additional WebSocket channels, extend `_process_ws_payload` with the
  relevant `WebsocketResponse` fields from the SDK.
- For quick sanity checks, run `python -m compileall src` to verify syntax or
  use `uvicorn src.server:create_app --factory` for direct ASGI hosting.

## Troubleshooting

- **401 Unauthorized on startup** – Confirm the API key has the `view` scope and
  that the private key matches. The service falls back to public endpoints, but
  authenticated snapshots offer higher fidelity.
- **Disconnected WebSockets** – The SDK handles automatic retries; logs at
  `WARNING` level call out reconnection attempts. Persistent failures usually
  indicate credential or network issues.
- **Large depth requests** – Depths above 500 are clamped by the Coinbase API;
  the service enforces the same bound.

---

For details on historical code or migration references, browse the `archived/`
directory. Contributions and improvements to the Python implementation are
welcome—feel free to open issues or PRs describing desired enhancements.
